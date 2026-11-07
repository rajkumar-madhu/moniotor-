"""Prometheus discovery: once Prometheus is connected, the product gathers its inventory by itself.

One pass of `run()`:

1. reads /api/v1/targets (core.prometheus.targets through telemetry.promapi);
2. runs a small, fixed set of BULK instant queries — one query per metric across *every* instance, never a
   query per host — to learn what each target is and what the host reports about itself;
3. upserts one telemetry.DiscoveredTarget per (instance, job), sets `missing_since` on rows Prometheus no
   longer lists, resolves the site, links the row to a CMDB item / network device / exporter, and (opt-in)
   creates those records;
4. writes a telemetry.DiscoveryRun summary and emits core events for newly discovered targets.

Query budget (independent of how many hosts there are):
  * phase 1 — three identity probes that are always run: `node_uname_info`, `windows_cs_hostname`, `sysName`.
    They say which instances are Linux hosts, Windows hosts and SNMP devices;
  * phase 2 — the detail queries of the families actually found (Linux ≤ 10, Windows ≤ 8, SNMP ≤ 2, plus
    kube-state-metrics ≤ 7 when a target is named like one), run concurrently through telemetry.hosts._run_parallel.
So a pass is 3 queries when nothing matches and at most 30 when every family is present.

Kind detection (telemetry.models.TargetKind), in this order:
  * metric evidence wins: the instance answers `node_uname_info` -> node, `windows_cs_hostname` -> windows,
    `sysName` -> snmp;
  * otherwise the job name, then the scrape pool name, is matched case-insensitively against KIND_HINTS
    (e.g. a job called "windows-prod" is windows, "blackbox_http" is blackbox, "pg-exporter" is postgres);
  * otherwise `other`.

Nothing here writes to the audit log: discovery is a system job, not a user action. The operator actions on
the Discovery page audit themselves.
"""
import ipaddress
import logging
import re
import time

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connections, transaction
from django.utils import timezone

from core import prometheus
from core.events import emit

from . import hosts as host_svc, promapi
from .fmt import finite, parse_ts
from .models import HOST_KINDS, DiscoveredTarget, DiscoveryRun, TargetKind

log = logging.getLogger("linkedeye.telemetry")

JOB_INTERVAL = 300          # core.jobs registration interval, seconds
NEW_TARGET_EVENT_CAP = 20   # per-target events emitted in one run (the run summary event is always emitted)
MAX_DETAIL_ROWS = 50        # interfaces / filesystems kept per target
MAX_TEXT = 200              # longest single string kept in `details` (a Prometheus label value is unbounded)
MAX_LABELS = 60             # target labels / discovered labels kept per target
MAX_EPOCH = 4_102_444_800   # 2100-01-01: a reported boot time beyond this is a broken exporter, not a host
RUN_HISTORY = 50            # DiscoveryRun rows kept per tenant
RULES_CACHE_SECONDS = 60

# job / scrape-pool name fragment -> kind. Checked in order, so "kube-state" wins over "kube".
KIND_HINTS = [
    ("windows", TargetKind.WINDOWS), ("wmi", TargetKind.WINDOWS),
    ("kube-state", TargetKind.KUBE_STATE), ("kube_state", TargetKind.KUBE_STATE), ("ksm", TargetKind.KUBE_STATE),
    ("cadvisor", TargetKind.CADVISOR), ("kubelet", TargetKind.CADVISOR), ("container", TargetKind.CADVISOR),
    ("blackbox", TargetKind.BLACKBOX), ("probe", TargetKind.BLACKBOX), ("icmp", TargetKind.BLACKBOX),
    ("http_2xx", TargetKind.BLACKBOX),
    ("snmp", TargetKind.SNMP),
    ("mysql", TargetKind.MYSQL), ("mariadb", TargetKind.MYSQL),
    ("postgres", TargetKind.POSTGRES), ("pgsql", TargetKind.POSTGRES), ("pg-", TargetKind.POSTGRES),
    ("redis", TargetKind.REDIS),
    ("nginx", TargetKind.NGINX),
    ("node", TargetKind.NODE), ("linux", TargetKind.NODE), ("unix", TargetKind.NODE),
]

# phase 1: who is what. Bare selectors, so Prometheus returns the metric name in __name__.
IDENTITY_QUERIES = {
    "uname": "node_uname_info",
    "win_host": "windows_cs_hostname",
    "snmp_name": "sysName",
}

# phase 2, Linux (node_exporter)
NODE_QUERIES = {
    "os": "node_os_info",
    "build": "node_exporter_build_info",
    "net": "node_network_info",
    "net_speed": "node_network_speed_bytes",
    "mem": "node_memory_MemTotal_bytes",
    "boot": "node_boot_time_seconds",
    "cores": 'count by (instance) (node_cpu_seconds_total{mode="idle"})',
    "fs_size": f"node_filesystem_size_bytes{{{host_svc.FS_FILTER}}}",
    "fs_avail": f"node_filesystem_avail_bytes{{{host_svc.FS_FILTER}}}",
    "dmi": "node_dmi_info",
}

# phase 2, Windows (windows_exporter)
WINDOWS_QUERIES = {
    "win_os": "windows_os_info",
    "win_cores": "windows_cs_logical_processors",
    "win_mem": "windows_cs_physical_memory_bytes",
    # the boot timestamp: windows_exporter 0.31 renamed windows_system_system_up_time
    "win_boot": "windows_system_boot_time_timestamp or windows_system_system_up_time",
    "win_build": "windows_exporter_build_info",
    "win_net": "windows_net_nic_address_info",
    "win_disk_size": 'windows_logical_disk_size_bytes{volume!~"HarddiskVolume.*"}',
    "win_disk_free": 'windows_logical_disk_free_bytes{volume!~"HarddiskVolume.*"}',
}

# phase 2, SNMP (snmp_exporter info metrics carry the value in a label of the same name)
SNMP_QUERIES = {
    "snmp_descr": "sysDescr",
    "snmp_location": "sysLocation",
}

# phase 2, kube-state-metrics: cluster size, per kube-state-metrics instance. Run only when a target is named like
# kube-state-metrics (KIND_HINTS) – the Kubernetes objects are not a host family phase 1 could probe for.
KSM_QUERIES = {
    "ksm_build": "kube_state_metrics_build_info",
    "ksm_nodes": "count by (instance) (kube_node_info)",
    "ksm_nodes_ready": 'sum by (instance) (kube_node_status_condition{condition="Ready",status="true"})',
    "ksm_pods": "count by (instance) (kube_pod_info)",
    "ksm_pods_running": 'sum by (instance) (kube_pod_status_phase{phase="Running"})',
    "ksm_namespaces": "count by (instance) (kube_namespace_created)",
    "ksm_deployments": "count by (instance) (kube_deployment_created)",
}
# details["cluster"] key <- query
KSM_FIELDS = {"nodes": "ksm_nodes", "nodes_ready": "ksm_nodes_ready", "pods": "ksm_pods",
              "pods_running": "ksm_pods_running", "namespaces": "ksm_namespaces", "deployments": "ksm_deployments"}


# ----------------------------------------------------------------------------- settings
def site_label():
    """Target label discovery reads the site from (Integrations › Prometheus, default "site")."""
    from administration import integration_config as ic

    return (ic.prometheus_site_label() or "site").strip() or "site"


def _flags():
    from administration import integration_config as ic

    return {"ci": ic.prometheus_autocreate_ci(), "device": ic.prometheus_autocreate_devices(),
            "exporter": ic.prometheus_autoregister_exporters()}


def tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- helpers
def address_of(instance):
    """Host part of an instance label: "db-01:9100" -> "db-01", "[::1]:9100" -> "::1", a URL -> its netloc."""
    inst = (instance or "").strip()
    if "://" in inst:
        from urllib.parse import urlparse
        return urlparse(inst).hostname or inst
    if inst.startswith("["):
        return inst[1:inst.find("]")] if "]" in inst else inst
    return inst.rsplit(":", 1)[0] if inst.count(":") == 1 else inst


def redact_url(url):
    """A scrape URL without the user:password@ part. Discovery stores the URL and every viewer of the target sees
    it, so a credential an operator put in the URL must not be copied into the database."""
    url = str(url or "").strip()
    if "@" not in url.split("?", 1)[0].split("#", 1)[0]:
        return url
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        if parts.username is None and parts.password is None:
            return url
        host = parts.netloc.rsplit("@", 1)[1]
        return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    except ValueError:
        return ""


def as_ip(value):
    try:
        return str(ipaddress.ip_address((value or "").strip()))
    except ValueError:
        return ""


def kind_from_name(*names):
    for name in names:
        low = (name or "").lower()
        if not low:
            continue
        for fragment, kind in KIND_HINTS:
            if fragment in low:
                return kind
    return None


def detect_kind(job="", pool="", has_node=False, has_windows=False, has_snmp=False):
    """What a scrape target is. Metric evidence wins over the job / pool name (see the module docstring);
    when more than one family claims the instance, the job name decides between them."""
    claims = [k for k, ok in ((TargetKind.NODE, has_node), (TargetKind.WINDOWS, has_windows),
                              (TargetKind.SNMP, has_snmp)) if ok]
    if claims:
        by_name = kind_from_name(job, pool)
        return by_name if by_name in claims else claims[0]
    return kind_from_name(job, pool) or TargetKind.OTHER


def resolve_site(labels, discovered_labels, label_name=None):
    """Site name from the configured target label, else "All Sites" (never invented)."""
    name = (label_name or site_label())
    for src in (labels or {}, discovered_labels or {}):
        val = _clean(src.get(name) or "").strip()
        if val:
            return val[:100]
    return "All Sites"


def _by_instance(rows, value=False, require=()):
    """{instance: metric labels} (or {instance: float}) for a bulk query result.

    `require`: keep a series only when it carries one of these labels. The identity probes use it, because an
    info metric is only evidence (and only useful) when it carries the label its text lives in — that also
    filters out a Prometheus-compatible endpoint that answers every metric name with a bare series."""
    out = {}
    for r in rows or ():
        m = r.get("metric") or {}
        inst = m.get("instance")
        if not inst or (require and not any(str(m.get(k) or "").strip() for k in require)):
            continue
        if value:
            v = r.get("value")
            if finite(v):
                out[inst] = float(v)
        else:
            out.setdefault(inst, m)
    return out


def _grouped(rows):
    """{instance: [metric labels, ...]} for multi-series metrics (interfaces, filesystems)."""
    out = {}
    for r in rows or ():
        m = r.get("metric") or {}
        inst = m.get("instance")
        if inst:
            out.setdefault(inst, []).append(r)
    return out


def _epoch(value):
    """A boot time an exporter reports, or 0 when it is not a usable epoch second.

    The detail page renders this through datetime.fromtimestamp, which raises on a wild value, so a broken or
    hostile exporter must not get one into the database."""
    return int(value) if finite(value) and 0 < value < MAX_EPOCH else 0


_DURATION_UNITS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|y|w|d|h|m|s)")


def _seconds(value):
    """Seconds from what /api/v1/targets reports: a Prometheus duration string ("15s", "1m30s", "500ms") for
    scrapeInterval / scrapeTimeout, a plain number for lastScrapeDuration. None when it is neither."""
    if isinstance(value, bool):
        return None
    if finite(value):
        return float(value) if 0 <= value < 10 ** 7 else None
    text = str(value or "").strip()
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        return None
    total = sum(float(n) * _DURATION_UNITS[u] for n, u in parts)
    return total if 0 <= total < 10 ** 7 else None


def _clean(value):
    """str(value) without NUL characters: PostgreSQL refuses \\u0000 in text and jsonb columns, so a label value
    carrying one would otherwise abort the whole pass with a DataError (SQLite would store it)."""
    return str(value).replace("\x00", "")


def _label_value(metric, *names, limit=MAX_TEXT):
    """snmp_exporter / node_exporter info metrics carry their text in a label; take the first one that is set.

    Truncated to `limit`: a Prometheus label value has no length limit of its own, and everything this returns
    ends up in the `details` JSON (and from there in a CMDB item's `attributes`)."""
    m = metric or {}
    for n in names:
        val = _clean(m.get(n) or "").strip()
        if val:
            return val[:limit]
    return ""


# ----------------------------------------------------------------------------- Prometheus reads
def _query(promql):
    """promapi.query for a worker thread: close the thread's own database connection when done.

    The first call after the 60 s integration-settings cache expires reads the Prometheus URL from the
    database; in a pool thread that opens a connection nobody would ever close (and which blocks dropping the
    test database on PostgreSQL)."""
    try:
        return promapi.query(promql)
    finally:
        connections.close_all()


def _fetch_metrics(instances_by_family):
    """Run phase 2 for the families that phase 1 found. Returns {name: rows}."""
    wanted = {}
    if instances_by_family.get("node"):
        wanted.update(NODE_QUERIES)
    if instances_by_family.get("windows"):
        wanted.update(WINDOWS_QUERIES)
    if instances_by_family.get("snmp"):
        wanted.update(SNMP_QUERIES)
    if instances_by_family.get("kube_state"):
        wanted.update(KSM_QUERIES)
    if not wanted:
        return {}, 0
    return host_svc._run_parallel(wanted, _query), len(wanted)


def _details_for(inst, kind, data):
    """Everything the exporter says about this target, as the `details` JSON (see telemetry.models)."""
    d = {}
    address = address_of(inst)
    if address:
        d["address"] = address
    ip = as_ip(address)
    if ip:
        d["ip"] = ip

    if kind == TargetKind.NODE:
        uname = data["uname"].get(inst) or {}
        if uname:
            d["hostname"] = _label_value(uname, "nodename")
            release, sysname = _label_value(uname, "release"), _label_value(uname, "sysname")
            d["kernel"] = " ".join(x for x in (sysname, release) if x)
            d["arch"] = _label_value(uname, "machine")
        osinfo = data["os"].get(inst) or {}
        if osinfo:
            d["os"] = _label_value(osinfo, "pretty_name", "name", "id")
        build = data["build"].get(inst) or {}
        if build:
            d["exporter_version"] = _label_value(build, "version")
        dmi = data["dmi"].get(inst) or {}
        if dmi:
            d["vendor"] = _label_value(dmi, "system_vendor", "board_vendor", "chassis_vendor")
            d["model"] = _label_value(dmi, "product_name", "board_name")
        if inst in data["cores"]:
            d["cores"] = int(data["cores"][inst])
        if inst in data["mem"]:
            d["memory_bytes"] = int(data["mem"][inst])
        boot = _epoch(data["boot"].get(inst))
        if boot:
            d["boot_time"] = boot
        speeds = {(r.get("metric") or {}).get("device"): r.get("value")
                  for r in data["net_speed_rows"].get(inst, [])}
        ifaces = []
        for r in data["net_rows"].get(inst, [])[:MAX_DETAIL_ROWS]:
            m = r.get("metric") or {}
            dev = _label_value(m, "device")
            speed = speeds.get(dev)
            ifaces.append({"name": dev, "mac": _label_value(m, "address"),
                           "operstate": _label_value(m, "operstate"),
                           "speed": int(speed) if finite(speed) and speed >= 0 else None})
        if ifaces:
            d["interfaces"] = ifaces
        fs = _filesystems(data["fs_size_rows"].get(inst, []), data["fs_avail_rows"].get(inst, []),
                          "mountpoint", "device", "fstype")
        if fs:
            d["filesystems"] = fs

    elif kind == TargetKind.WINDOWS:
        host = data["win_host"].get(inst) or {}
        if host:
            d["hostname"] = _label_value(host, "hostname", "fqdn", "netbios")
        osinfo = data["win_os"].get(inst) or {}
        if osinfo:
            d["os"] = _label_value(osinfo, "product", "caption", "version")
            d["kernel"] = _label_value(osinfo, "version", "build_number")
        if inst in data["win_cores"]:
            d["cores"] = int(data["win_cores"][inst])
        if inst in data["win_mem"]:
            d["memory_bytes"] = int(data["win_mem"][inst])
        boot = _epoch(data["win_boot"].get(inst))
        if boot:
            d["boot_time"] = boot
        build = data["win_build"].get(inst) or {}
        if build:
            d["exporter_version"] = _label_value(build, "version")
        ifaces = []
        for r in data["win_net_rows"].get(inst, [])[:MAX_DETAIL_ROWS]:
            m = r.get("metric") or {}
            ifaces.append({"name": _label_value(m, "friendly_name", "nic"), "mac": _label_value(m, "address"),
                           "operstate": "", "speed": None})
        if ifaces:
            d["interfaces"] = ifaces
        fs = _filesystems(data["win_disk_size_rows"].get(inst, []), data["win_disk_free_rows"].get(inst, []),
                          "volume", "volume", None)
        if fs:
            d["filesystems"] = fs

    elif kind == TargetKind.KUBE_STATE:
        build = data["ksm_build"].get(inst) or {}
        if build:
            d["exporter_version"] = _label_value(build, "version")
        cluster = {field: int(data[q][inst]) for field, q in KSM_FIELDS.items() if inst in data[q]}
        if cluster:
            d["cluster"] = cluster

    elif kind == TargetKind.SNMP:
        name = data["snmp_name"].get(inst) or {}
        if name:
            d["sysName"] = _label_value(name, "sysName")
            d["hostname"] = d["sysName"]
        descr = data["snmp_descr"].get(inst) or {}
        if descr:
            d["sysDescr"] = _label_value(descr, "sysDescr", limit=500)
        loc = data["snmp_location"].get(inst) or {}
        if loc:
            d["sysLocation"] = _label_value(loc, "sysLocation")[:200]
    return {k: v for k, v in d.items() if v not in ("", None, [])}


def _filesystems(size_rows, avail_rows, mount_label, device_label, fstype_label):
    """[{mountpoint, device, fstype, size_bytes, avail_bytes}] from a size/avail metric pair."""
    def key(r):
        m = r.get("metric") or {}
        return (_label_value(m, mount_label), _label_value(m, device_label))
    avail = {}
    for r in avail_rows:
        if finite(r.get("value")):
            avail[key(r)] = float(r["value"])
    out = []
    for r in size_rows[:MAX_DETAIL_ROWS]:
        size, m = r.get("value"), r.get("metric") or {}
        if not finite(size) or size <= 0:
            continue
        free = avail.get(key(r))
        out.append({"mountpoint": _label_value(m, mount_label), "device": _label_value(m, device_label),
                    "fstype": _label_value(m, fstype_label) if fstype_label else "",
                    "size_bytes": int(size), "avail_bytes": int(free) if finite(free) else None})
    return sorted(out, key=lambda f: f["mountpoint"])


# ----------------------------------------------------------------------------- linking
def _model(app_label, name):
    from django.apps import apps
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


def _candidates(row):
    """Strings an inventory record may be matched on: the instance, its address and the reported hostname."""
    out = [row.instance, row.address]
    hostname = (row.details or {}).get("hostname")
    if hostname:
        out.append(hostname)
    return [s for s in dict.fromkeys(out) if s]


def link_row(row):
    """Attach the CMDB item / device / exporter that already represents this target. Returns the fields changed.

    Matching, tenant-scoped throughout (another tenant's records are never linked):
    prometheus_instance first (exact, on the instance or the bare address), then the record's own name,
    then its IP / management address, and for exporters the scrape URL.

    Deliberately NOT scoped to the target's site: a host's Prometheus `site` label and its CMDB site can
    legitimately disagree, and refusing the link there would create a duplicate item instead. The cost is that
    a viewer who may see the target also sees the name of the record behind it, which may sit on another site;
    the operator's own "Link to…" list is site-scoped (telemetry.views.discovery._link_options), since that
    writes back to the record."""
    changed = []
    names = _candidates(row)
    ip = (row.details or {}).get("ip") or as_ip(row.address)

    CI = _model("cmdb", "ConfigurationItem")
    if row.ci_id is None and CI is not None:
        ci = _first(CI.objects.filter(tenant_key=tenant(), prometheus_instance__in=names))
        if ci is None:
            ci = _first(CI.objects.filter(tenant_key=tenant(), name__in=names))
        if ci is None and ip:
            ci = _first(CI.objects.filter(tenant_key=tenant(), ip_address=ip))
        if ci is not None:
            row.ci = ci
            changed.append("ci")

    Device = _model("network", "Device")
    if row.device_id is None and Device is not None:
        dev = _first(Device.objects.filter(tenant_key=tenant(), prometheus_instance__in=names))
        if dev is None:
            dev = _first(Device.objects.filter(tenant_key=tenant(), name__in=names))
        if dev is None:
            dev = _first(Device.objects.filter(tenant_key=tenant(), mgmt_ip__in=names))
        if dev is not None:
            row.device = dev
            changed.append("device")

    Exporter = _model("observe", "Exporter")
    if row.exporter_id is None and Exporter is not None:
        exp = None
        if row.scrape_url:
            exp = _first(Exporter.objects.filter(tenant_key=tenant(), metrics_url=row.scrape_url))
        if exp is None:
            exp = _first(Exporter.objects.filter(tenant_key=tenant(), target__in=names))
        if exp is None:
            exp = _first(Exporter.objects.filter(tenant_key=tenant(), name__in=names))
        if exp is not None:
            row.exporter = exp
            changed.append("exporter")
    return changed


def _first(qs):
    try:
        return qs.first()
    except Exception:   # the other module's tables are not migrated: discovery must not fail because of it
        log.warning("discovery link lookup failed", exc_info=True)
        return None


def unique_name(model, base, field="name"):
    """`base`, or base-2, base-3… – the inventory models are unique per (tenant_key, name).

    Bounded by the model's own max_length, with room for the suffix: observe.Exporter.name holds 120 characters,
    not the 150 of a CI or a device, and a name longer than the column is a mid-pass DataError on PostgreSQL
    (SQLite would accept it), which would abort the whole discovery pass."""
    cap = max(20, model._meta.get_field(field).max_length or 120)
    base = (base or "discovered")[:cap - 12] or "discovered"
    name, n = base, 2
    while model.objects.filter(tenant_key=tenant(), **{f"{field}__iexact": name}).exists():
        name = f"{base}-{n}"
        n += 1
        if n > 50:
            return f"{base}-{int(time.time())}"
    return name


# ----------------------------------------------------------------------------- auto-create (opt-in)
def create_ci(row):
    """A ConfigurationItem for a newly discovered host. Returns the CI, or None when the module is absent."""
    CI = _model("cmdb", "ConfigurationItem")
    if CI is None:
        return None
    d = row.details or {}
    name = unique_name(CI, d.get("hostname") or row.address or row.instance)
    attrs = {k: v for k, v in d.items() if k not in ("interfaces", "filesystems", "scrape", "cluster")}
    attrs["discovered_from"] = "prometheus"
    attrs["prometheus_job"] = row.job
    ci = CI(tenant_key=tenant(), name=name, ci_type="server", site=row.site or "All Sites",
            ip_address=d.get("ip") or None, prometheus_instance=row.instance[:200],
            attributes=attrs, description=f"Discovered from Prometheus job {row.job or '(none)'}")
    try:
        with transaction.atomic():
            _validate(ci)
            ci.save()
            return ci
    except (ValidationError, IntegrityError) as e:
        log.warning("discovery: could not create a CMDB item for %s (%r): %s", row.instance, name, e)
        return None


def create_device(row):
    """A network.Device for a newly discovered SNMP target (vendor / model are left blank on purpose)."""
    Device = _model("network", "Device")
    if Device is None:
        return None
    d = row.details or {}
    host = d.get("hostname") or row.address or row.instance
    name = unique_name(Device, host)
    dev = Device(tenant_key=tenant(), name=name, device_type="switch",
                 mgmt_ip=(row.address or row.instance)[:255],
                 site=row.site or "", environment="", tags="discovered",
                 prometheus_instance=row.instance[:200],
                 description=f"Discovered from Prometheus job {row.job or '(none)'}. "
                             f"{d.get('sysDescr', '')}".strip())
    try:
        with transaction.atomic():
            _validate(dev)         # mgmt_ip carries network.models.validate_host; .create() would skip it
            dev.save()
            return dev
    except Exception as e:      # IntegrityError, or a mgmt_ip the Device validators reject
        log.warning("discovery: could not create device for %s: %s", row.instance, e)
        return None


def _validate(obj):
    """Run the model's own field validators and clean(), which Model.save() does not.

    Discovery feeds these records scraped label values, so an exporter must not be able to put an address the
    reachability check would refuse (network.models.validate_host) into the inventory. Uniqueness is handled
    by unique_name() and the database, so it is not re-checked here."""
    obj.full_clean(validate_unique=False, validate_constraints=False)


# exporter kind -> (observe.Exporter type, category)
EXPORTER_TYPES = {
    "node": ("Node Exporter", "Servers"), "windows": ("Windows", "Servers"), "snmp": ("SNMP", "Network"),
    "blackbox": ("Blackbox (HTTP/TCP)", "Applications"), "kube_state": ("Kubernetes", "Kubernetes"),
    "cadvisor": ("cAdvisor", "Kubernetes"), "mysql": ("MySQL", "Databases"), "postgres": ("PostgreSQL", "Databases"),
    "redis": ("Redis", "Databases"), "nginx": ("Nginx", "Applications"), "other": ("Custom", "Custom"),
}


def create_exporter(row):
    """An observe.Exporter for a scrape target. Skipped when Prometheus gives no scrape URL, because the
    Exporter inventory is built around a /metrics URL it can scrape itself."""
    Exporter = _model("observe", "Exporter")
    if Exporter is None or not row.scrape_url or len(row.scrape_url) > 500:
        return None
    etype, category = EXPORTER_TYPES.get(row.kind, EXPORTER_TYPES["other"])
    base = (row.details or {}).get("hostname") or row.address or row.instance
    name = unique_name(Exporter, f"{base} {etype}")
    try:
        with transaction.atomic():
            exp = Exporter.objects.create(
                tenant_key=tenant(), name=name, exporter_type=etype, category=category,
                target=row.instance[:255], metrics_url=row.scrape_url, site=row.site or "",
                description=f"Discovered from Prometheus job {row.job or '(none)'}")
        exp.apply_config_status(url_changed=True)
        exp.save(update_fields=["status", "status_changed_at", "last_error", "samples", "last_scrape_ms", "last_seen"])
        return exp
    except IntegrityError:
        log.warning("discovery: exporter %r already exists", name)
        return None


def autocreate(row, flags):
    """Create the inventory records the operator opted in to. Idempotent: a row that is already linked is
    never duplicated, so a hand-made item that appears later is linked on the next pass instead."""
    created = []
    if row.state == DiscoveredTarget.IGNORED:
        return created
    if flags.get("ci") and row.ci_id is None and row.kind in [k.value for k in HOST_KINDS]:
        ci = create_ci(row)
        if ci is not None:
            row.ci = ci
            created.append("ci")
    if flags.get("device") and row.device_id is None and row.kind == TargetKind.SNMP:
        dev = create_device(row)
        if dev is not None:
            row.device = dev
            created.append("device")
    if flags.get("exporter") and row.exporter_id is None:
        exp = create_exporter(row)
        if exp is not None:
            row.exporter = exp
            created.append("exporter")
    return created


# ----------------------------------------------------------------------------- the pass
UPSERT_FIELDS = ["job", "scrape_pool", "labels", "discovered_labels", "kind", "health", "last_error", "scrape_url",
                 "last_scrape", "last_seen", "missing_since", "site", "details", "ci", "device", "exporter", "state"]


def _apply_target(row, t, kind, data, now, label):
    """Copy one normalized target onto its DiscoveredTarget row (everything but the links and the state)."""
    row.scrape_pool = t["pool"]
    row.labels = t["labels"]
    row.discovered_labels = t["discovered_labels"]
    row.kind = kind
    row.health = t["health"]
    row.last_error = t["error"][:500]
    row.scrape_url = t["url"][:1000]
    row.last_scrape = t["last_scrape"]
    row.last_seen = now
    row.missing_since = None
    if not row.site_manual:
        row.site = resolve_site(t["labels"], t["discovered_labels"], label)
    details = _details_for(t["instance"], kind, data)
    if t.get("scrape"):
        details["scrape"] = t["scrape"]      # from /api/v1/targets itself: fresh on every pass, no query needed
    if data.get("identity_failed") or data.get("detail_failed"):
        details = {**(row.details or {}), **details}    # a query failed: keep what an earlier pass learned
    if details or not row.details:
        row.details = details
    return row


def _save_upsert(row, t, kind, data, now, label, is_new):
    """Save the row, surviving a pass that runs at the same time. Returns (row, is_new).

    Two passes can overlap: the background job runs every JOB_INTERVAL seconds while an admin may press
    "Discover now" or run `manage.py discover_targets`. Both would then see the same target as new and both
    INSERT it, and the (tenant_key, instance, job) unique constraint turns the loser's save into an
    IntegrityError that aborts the whole pass. Take the row the other pass inserted and update that instead,
    so a pass is idempotent whoever gets there first. The save is wrapped in its own atomic block because on
    PostgreSQL a failed statement poisons the surrounding transaction."""
    try:
        with transaction.atomic():
            row.save()
        return row, is_new
    except IntegrityError:
        if not is_new:
            raise
        fresh = DiscoveredTarget.objects.filter(
            tenant_key=tenant(), instance=t["instance"], job=t["job"]).first()
        if fresh is None:
            raise
        log.info("discovery: target %r / %r was inserted by a concurrent pass; updating it instead",
                 t["instance"], t["job"])
        _apply_target(fresh, t, kind, data, now, label)
        fresh.save()
        return fresh, False


def run(now=None, trigger="job"):
    """One discovery pass. Returns the DiscoveryRun row (never raises: Prometheus errors are recorded on it)."""
    now = now or timezone.now()
    t0 = time.monotonic()
    run_row = DiscoveryRun(tenant_key=tenant(), started_at=now, trigger=trigger)
    if not prometheus.configured():
        run_row.error = "Prometheus is not connected."
        _finish(run_row, t0)
        return run_row
    try:
        raw = promapi.targets()
    except prometheus.PrometheusError as e:
        run_row.error = str(e)[:500]
        _finish(run_row, t0)
        log.warning("telemetry.discovery: %s", e)
        return run_row
    except Exception as e:                      # a broken response must not kill the scheduler pass
        run_row.error = f"Unexpected Prometheus response: {e}"[:500]
        _finish(run_row, t0)
        log.exception("telemetry.discovery failed to read targets")
        return run_row

    seen = _normalize_targets(raw)
    data, n_detail = _collect(seen)
    label = site_label()
    flags = _flags()
    existing = {(r.instance, r.job): r for r in DiscoveredTarget.objects.for_tenant()}
    new_rows, created_total, linked_total, touched = [], 0, 0, []

    for key, t in seen.items():
        row = existing.get(key)
        is_new = row is None
        if is_new:
            row = DiscoveredTarget(tenant_key=tenant(), instance=t["instance"], job=t["job"], first_seen=now)
        kind = detect_kind(t["job"], t["pool"], has_node=t["instance"] in data["uname"],
                           has_windows=t["instance"] in data["win_host"], has_snmp=t["instance"] in data["snmp_name"])
        if data.get("identity_failed") and not is_new:
            kind = row.kind          # the probes did not answer: keep what an earlier pass learned, do not flap
        _apply_target(row, t, kind, data, now, label)
        row, is_new = _save_upsert(row, t, kind, data, now, label, is_new)
        touched.append(row.pk)
        linked = link_row(row)
        created = autocreate(row, flags)
        if row.linked_to and row.state == DiscoveredTarget.NEW:
            row.state = DiscoveredTarget.LINKED
        elif not row.linked_to and row.state == DiscoveredTarget.LINKED:
            row.state = DiscoveredTarget.NEW      # the item it was linked to has been deleted
        row.save(update_fields=["ci", "device", "exporter", "state", "updated_at"])
        linked_total += len(linked)
        created_total += len(created)
        if is_new:
            new_rows.append(row)

    n_missing = DiscoveredTarget.objects.for_tenant().filter(missing_since__isnull=True).exclude(
        pk__in=touched).update(missing_since=now, health=DiscoveredTarget.UNKNOWN)

    run_row.ok = True
    run_row.targets = len(seen)
    run_row.up = sum(1 for t in seen.values() if t["health"] == "up")
    run_row.down = sum(1 for t in seen.values() if t["health"] == "down")
    run_row.new = len(new_rows)
    run_row.missing = n_missing
    run_row.linked = linked_total
    run_row.created = created_total
    run_row.queries = len(IDENTITY_QUERIES) + n_detail
    _finish(run_row, t0)
    _emit_events(new_rows, run_row)
    return run_row


def _trim_labels(src):
    """A target's label set, bounded. Prometheus puts no limit on how many labels a target carries or how long
    a value is, and these go straight into a JSON column, so cap both (MAX_LABELS keys of MAX_TEXT chars)."""
    out = {}
    for k, v in list((src or {}).items())[:MAX_LABELS]:
        out[_clean(k)[:100]] = _clean(v)[:MAX_TEXT]
    return out


def _scrape_settings(t):
    """{interval, timeout, duration} in seconds, whichever /api/v1/targets reported (older Prometheus omits the
    first two)."""
    out = {}
    for key, field in (("interval", "scrapeInterval"), ("timeout", "scrapeTimeout"), ("duration", "lastScrapeDuration")):
        secs = _seconds(t.get(field))
        if secs is not None:
            out[key] = round(secs, 6)
    return out


def _normalize_targets(raw):
    """{(instance, job): normalized target}. Targets without an instance label are skipped (nothing to key on)."""
    out = {}
    for t in raw or ():
        if not isinstance(t, dict):
            continue
        labels = t.get("labels") if isinstance(t.get("labels"), dict) else {}
        discovered = t.get("discoveredLabels") if isinstance(t.get("discoveredLabels"), dict) else {}
        instance = _clean(labels.get("instance") or discovered.get("__address__") or "").strip()
        if not instance:
            continue
        job = _clean(labels.get("job") or t.get("scrapePool") or "")[:200]
        health = str(t.get("health") or "unknown").lower()
        out[(instance[:255], job)] = {
            "instance": instance[:255], "job": job, "pool": _clean(t.get("scrapePool") or job)[:200],
            "labels": _trim_labels(labels),
            "discovered_labels": _trim_labels(discovered),
            "health": health if health in ("up", "down") else "unknown",
            "error": _clean(t.get("lastError") or "").strip(),
            "url": _clean(redact_url(t.get("scrapeUrl") or t.get("globalUrl") or "")),
            "last_scrape": parse_ts(t.get("lastScrape")),
            "scrape": _scrape_settings(t),
        }
    return out


def _collect(seen):
    """Phase 1 + phase 2. Query failures degrade to "no details", never to a failed pass."""
    data = {k: {} for k in ("uname", "win_host", "snmp_name", "os", "build", "mem", "boot", "cores", "win_os",
                            "win_cores", "win_mem", "win_boot", "snmp_descr", "snmp_location", "dmi", "win_build",
                            *KSM_QUERIES)}
    for k in ("net_rows", "net_speed_rows", "fs_size_rows", "fs_avail_rows", "win_net_rows", "win_disk_size_rows",
              "win_disk_free_rows"):
        data[k] = {}
    if not seen:
        return data, 0
    try:
        ident = host_svc._run_parallel(IDENTITY_QUERIES, _query)
    except prometheus.PrometheusError as e:
        log.warning("telemetry.discovery: identity queries failed: %s", e)
        data["identity_failed"] = True
        return data, 0
    data["uname"] = _by_instance(ident.get("uname"), require=("nodename", "sysname", "release"))
    data["win_host"] = _by_instance(ident.get("win_host"), require=("hostname", "fqdn", "netbios"))
    data["snmp_name"] = _by_instance(ident.get("snmp_name"), require=("sysName",))
    try:
        ksm = any(kind_from_name(t["job"], t["pool"]) == TargetKind.KUBE_STATE for t in seen.values())
        rows, n = _fetch_metrics({"node": data["uname"], "windows": data["win_host"], "snmp": data["snmp_name"],
                                  "kube_state": ksm})
    except prometheus.PrometheusError as e:
        log.warning("telemetry.discovery: detail queries failed: %s", e)
        data["detail_failed"] = True
        return data, 0
    data["os"] = _by_instance(rows.get("os"), require=("pretty_name", "name", "id"))
    data["build"] = _by_instance(rows.get("build"), require=("version",))
    for key in ("mem", "boot", "cores"):
        data[key] = _by_instance(rows.get(key), value=True)
    data["net_rows"] = _grouped(rows.get("net"))
    data["net_speed_rows"] = _grouped(rows.get("net_speed"))
    data["fs_size_rows"] = _grouped(rows.get("fs_size"))
    data["fs_avail_rows"] = _grouped(rows.get("fs_avail"))
    data["win_os"] = _by_instance(rows.get("win_os"), require=("product", "caption", "version"))
    for key in ("win_cores", "win_mem", "win_boot"):
        data[key] = _by_instance(rows.get(key), value=True)
    data["win_net_rows"] = _grouped(rows.get("win_net"))
    data["win_disk_size_rows"] = _grouped(rows.get("win_disk_size"))
    data["win_disk_free_rows"] = _grouped(rows.get("win_disk_free"))
    data["snmp_descr"] = _by_instance(rows.get("snmp_descr"), require=("sysDescr",))
    data["snmp_location"] = _by_instance(rows.get("snmp_location"), require=("sysLocation",))
    data["dmi"] = _by_instance(rows.get("dmi"), require=("system_vendor", "board_vendor", "product_name", "board_name"))
    data["win_build"] = _by_instance(rows.get("win_build"), require=("version",))
    data["ksm_build"] = _by_instance(rows.get("ksm_build"), require=("version",))
    for key in KSM_FIELDS.values():
        data[key] = _by_instance(rows.get(key), value=True)
    return data, n


def _finish(run_row, t0):
    run_row.finished_at = timezone.now()
    run_row.duration_ms = round((time.monotonic() - t0) * 1000, 1)
    try:
        run_row.save()
        keep = list(DiscoveryRun.objects.filter(tenant_key=tenant())
                    .order_by("-started_at").values_list("pk", flat=True)[:RUN_HISTORY])
        DiscoveryRun.objects.filter(tenant_key=tenant()).exclude(pk__in=keep).delete()
    except Exception:
        log.warning("discovery: could not store the run summary", exc_info=True)


def _emit_events(new_rows, run_row):
    """One summary event per run that found new targets, plus up to NEW_TARGET_EVENT_CAP per-target events."""
    if not new_rows:
        return
    try:
        for row in new_rows[:NEW_TARGET_EVENT_CAP]:
            emit("telemetry", f"New Prometheus target {row.instance}", severity="info", kind="discovery",
                 site=row.site, obj=row,
                 message=f"{row.get_kind_display()} discovered in job {row.job or '(none)'}"
                         f"{' – ' + row.summary_line if row.summary_line else ''}",
                 fingerprint=f"discovery:{row.instance}:{row.job}",
                 instance=row.instance, job=row.job, kind_detected=row.kind, health=row.health)
        emit("telemetry", f"Prometheus discovery found {len(new_rows)} new target(s)", severity="info",
             kind="discovery", message=run_row.summary,
             new_targets=[r.instance for r in new_rows[:50]], total=run_row.targets)
    except Exception:
        log.exception("discovery: could not emit events")


# ----------------------------------------------------------------------------- alert rules (read-only)
def rule_groups(use_cache=True):
    """Prometheus alerting / recording rules from /api/v1/rules, shaped for the Prometheus page.

    Nothing is persisted – the rules live in Prometheus' configuration. Cached for RULES_CACHE_SECONDS."""
    key = f"telemetry:rules:v1:{tenant()}"
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    groups = []
    for g in promapi.rules():
        rules = []
        for r in g.get("rules") or ():
            if not isinstance(r, dict):
                continue
            labels = r.get("labels") if isinstance(r.get("labels"), dict) else {}
            annotations = r.get("annotations") if isinstance(r.get("annotations"), dict) else {}
            alerts = r.get("alerts") if isinstance(r.get("alerts"), list) else []
            rtype = str(r.get("type") or "").lower() or "recording"
            state = str(r.get("state") or "").lower()
            rules.append({
                "name": str(r.get("name") or ""), "type": rtype,
                "state": state or ("—" if rtype == "recording" else "unknown"),
                "health": str(r.get("health") or ""),
                "severity": str(labels.get("severity") or ""),
                "query": str(r.get("query") or ""),
                "duration": r.get("duration") if finite(r.get("duration")) else None,
                "summary": str(annotations.get("summary") or annotations.get("description") or ""),
                "active": sum(1 for a in alerts if isinstance(a, dict) and a.get("state") in ("firing", "pending")),
                "firing": sum(1 for a in alerts if isinstance(a, dict) and a.get("state") == "firing"),
                "last_eval": parse_ts(r.get("lastEvaluation")),
                "eval_seconds": r.get("evaluationTime") if finite(r.get("evaluationTime")) else None,
                "error": str(r.get("lastError") or ""),
            })
        groups.append({
            "name": str(g.get("name") or ""), "file": str(g.get("file") or ""),
            "interval": g.get("interval") if finite(g.get("interval")) else None,
            "last_eval": parse_ts(g.get("lastEvaluation")),
            "rules": rules,
            "alerting": sum(1 for r in rules if r["type"] == "alerting"),
            "recording": sum(1 for r in rules if r["type"] == "recording"),
            "firing": sum(r["firing"] for r in rules),
        })
    groups.sort(key=lambda g: (-g["firing"], g["name"]))
    cache.set(key, groups, RULES_CACHE_SECONDS)
    return groups


def rule_kpis(groups):
    rules = [r for g in groups for r in g["rules"]]
    return {"groups": len(groups), "rules": len(rules),
            "alerting": sum(1 for r in rules if r["type"] == "alerting"),
            "recording": sum(1 for r in rules if r["type"] == "recording"),
            "firing": sum(r["firing"] for r in rules),
            "pending": sum(r["active"] - r["firing"] for r in rules),
            "errors": sum(1 for r in rules if r["health"] not in ("", "ok", "unknown"))}


# ----------------------------------------------------------------------------- summaries for other pages
def last_run():
    return DiscoveryRun.objects.filter(tenant_key=tenant()).order_by("-started_at").first()


def unlinked_host_count(user=None):
    """Discovered hosts that are not in the CMDB yet (scoped to the user's sites when one is given)."""
    from core.access import scope_by_site
    qs = DiscoveredTarget.objects.for_tenant().unlinked_hosts()
    if user is not None:
        qs = scope_by_site(qs, user, field="site")
    try:
        return qs.count()
    except Exception:       # not migrated yet: the banner simply does not show
        log.debug("discovery: unlinked host count unavailable", exc_info=True)
        return 0
