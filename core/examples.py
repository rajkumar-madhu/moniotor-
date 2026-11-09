"""Example (demo) data lifecycle: what the `seed_*` commands created, how much of it is left, and removing it.

Example records only ever come from the `manage.py seed_*` commands (and the "Load example fabric" button of the
architecture page, which runs the same code). Each of them marks what it writes (an `example` tag, an `[example]` or
`Example:` description prefix, an `example` label, a `seed_automation` creator ...) and removes it again by that mark with
its own `--remove` (`--clear` for seed_cmdb). This module is the one place that knows all of them:

* :func:`summary` counts what is left per area (read-only, tenant-scoped; the counts use the same predicates as the removal);
* :func:`exists` is the cheap yes/no behind the admin banner and the go-live warning;
* :func:`remove` deletes it, one area at a time, each in its own transaction, by running the seed command's own removal.
  A real record that merely looks similar (same name without the mark) is never touched.

An area is a :class:`Area`: ``counts()`` returns ``[(noun, n)]`` and ``removal`` says how to delete. Apps that gain a seed
command add an Area to :data:`AREAS` (and a test in core/test_examples.py that seeds, counts and removes it).
"""
import io
import logging
from dataclasses import dataclass

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import transaction
from django.db.models import Q

log = logging.getLogger("linkedeye.examples")

SESSION_KEY = "le_examples"            # how many example records the admin's session last saw (0 = none)
DISMISS_KEY = "le_examples_dismissed"  # the admin hid the banner for this session


def _tenant():
    return settings.LINKEDEYE_TENANT


def _model(label, name):
    try:
        return apps.get_model(label, name)
    except LookupError:
        return None


def _qs(label, model_name, /, **filters):
    """Tenant-scoped queryset, or None when the app is not installed."""
    m = _model(label, model_name)
    if m is None:
        return None
    if any(f.name == "tenant_key" for f in m._meta.fields):
        filters.setdefault("tenant_key", _tenant())
    return m.objects.filter(**filters)


def _n(qs):
    return 0 if qs is None else qs.count()


def _example_devices(names=None, tag="example"):
    """Network devices carrying the `example` tag (exact tag, case-insensitive), optionally limited to seed names."""
    Device = _model("network", "Device")
    if Device is None:
        return []
    qs = Device.objects.filter(tenant_key=_tenant(), tags__icontains=tag)
    if names is not None:
        qs = qs.filter(name__in=list(names))
    return [d for d in qs if tag in [t.lower() for t in d.tag_list]]


# ------------------------------------------------------------------ per-area counts (same predicates as the removals)
def _count_monitoring():
    from core.management.commands import seed_examples as se
    from domainmonitor.management.commands import seed_domainmonitor as sd
    urls = _qs("urlchecker", "UrlCheck", name__in=[u[0] for u in se.URLS], tags__startswith="example,")
    hosts = {d[0] for d in se.DOMAINS} | {e[0] for e in sd.EXAMPLES}
    doms = _qs("domainmonitor", "Domain", hostname__in=list(hosts), description__startswith="Example:")
    return [("URL checks", _n(urls)), ("domains", _n(doms))]


def _count_administration():
    from administration.management.commands import seed_administration as sa
    return [("sites", len(example_site_pks())), ("users", len(example_user_pks()))]


def example_user_pks():
    """Users seed_administration made (example.* with no usable password) and the servicedesk agents (example.<name>)."""
    from django.contrib.auth import get_user_model
    from administration.management.commands import seed_administration as sa
    from servicedesk.management.commands import seed_servicedesk as ss
    names = [u["username"] for u in sa.EXAMPLE_USERS] + [f"example.{a[0]}" for a in ss.AGENTS]
    return [u.pk for u in get_user_model().objects.filter(username__in=names) if not u.has_usable_password()]


def example_site_pks():
    """Example sites that no real user is a member of (a site someone started using is kept)."""
    from administration.management.commands import seed_administration as sa
    Site = _model("core", "Site")
    Membership = _model("core", "Membership")
    user_pks = set(example_user_pks())
    out = []
    for s in Site.objects.filter(tenant_key=_tenant(), name__in=[x["name"] for x in sa.EXAMPLE_SITES], description__icontains="demo record"):
        if Membership.objects.filter(site=s).exclude(user_id__in=user_pks).exists():
            continue
        out.append(s.pk)
    return out


def _count_cmdb():
    from cmdb.management.commands import seed_cmdb as sc
    return [("configuration items", _n(_qs("cmdb", "ConfigurationItem", attributes__example=True))),
            ("software", _n(_qs("cmdb", "Software", description__startswith=sc.MARK))),
            ("licenses", _n(_qs("cmdb", "License", notes__startswith=sc.MARK)))]


def _count_network():
    from network.management.commands import seed_network as sn
    devs = _example_devices([e["name"] for e in sn.EXAMPLES])
    ports = 0
    Interface = _model("network", "Interface")
    if Interface is not None and devs:
        ports = Interface.objects.filter(device__in=[d.pk for d in devs]).count()
    return [("devices", len(devs)), ("interfaces", ports)]


def _count_compute():
    from compute.management.commands import seed_compute as sc
    names = [s[0] for s in sc.SERVERS] + [v[0] for v in sc.VMS]
    return [("servers and VMs", len(_example_devices(names))),
            ("clusters", _n(_qs("compute", "Cluster", name__in=[c[0] for c in sc.CLUSTERS], tags__icontains="example")))]


def _count_hypervisor():
    from hypervisor.management.commands import seed_hypervisor as sh
    from hypervisor.models import EXAMPLE_MARK
    return [("vCenter sources", _n(_qs("hypervisor", "VCenterSource", name=sh.NAME, description__startswith=EXAMPLE_MARK))),
            ("VM devices", _n(_qs("network", "Device", description__startswith=sh.MARK)))]


def _count_hardware():
    from hardware.management.commands import seed_hardware as sh
    return [("servers", len(_example_devices([sh.NAME])))]


def _count_architecture():
    from architecture import example as ex
    from architecture.models import EXAMPLE_TAG
    names = [d["name"] for d in ex._devices()]
    devs = _example_devices(names, tag=EXAMPLE_TAG)
    racks = _qs("dcim", "Rack", room__name=ex.ROOM, room__description__icontains=ex.MARK, notes__icontains=ex.MARK)
    return [("devices", len(devs)), ("racks", _n(racks))]


def _count_dcim():
    from dcim.management.commands import seed_dcim as sd
    rooms = _qs("dcim", "Room", name__in=[r["name"] for r in sd.ROOMS], description__icontains=sd.MARK)
    racks = _qs("dcim", "Rack", room__in=rooms) if rooms is not None else None
    return [("rooms", _n(rooms)), ("racks", _n(racks))]


def _count_ipam():
    from ipam.management.commands import seed_ipam as si
    t = _tenant()
    zones = _n(_qs("ipam", "DNSZone", name__in=[z[0] for z in si.ZONES], description__startswith="Example"))
    addrs = vlans = subnets = 0
    IPAddress, Subnet, VLAN = _model("ipam", "IPAddress"), _model("ipam", "Subnet"), _model("ipam", "VLAN")
    if IPAddress is not None:
        cond = Q()
        for addr, vrf, *_ in si.ADDRESSES:
            cond |= Q(vrf=vrf, address=addr)
        addrs = IPAddress.objects.filter(cond, tenant_key=t, description=si.SEED_ADDRESS_DESC).count()
        cond = Q()
        for cidr, vrf, _role, _vid, _gw, desc in si.SUBNETS:
            cond |= Q(vrf=vrf, cidr=cidr, description=desc)
        subnets = Subnet.objects.filter(cond, tenant_key=t, site=si.GLOBAL).count()
        cond = Q()
        for vid, _name, desc in si.VLANS:
            cond |= Q(vid=vid, description=desc)
        vlans = VLAN.objects.filter(cond, tenant_key=t, site=si.GLOBAL).count()
    return [("VLANs", vlans), ("subnets", subnets), ("IP addresses", addrs), ("DNS zones", zones)]


def _count_apm():
    from apm.management.commands import seed_apm as sa
    from apm.models import EXAMPLE_MARK
    return [("sources", _n(_qs("apm", "Source", name=sa.NAME, description__startswith=EXAMPLE_MARK)))]


def _count_servicedesk():
    from servicedesk.management.commands import seed_servicedesk as ss
    return [("tickets", _n(_qs("servicedesk", "Ticket", fingerprint__startswith=ss.FP))),
            ("catalog items", _n(_qs("servicedesk", "CatalogItem", name__in=[c[0] for c in ss.CATALOG],
                                    description__startswith="Example:")))]


def _count_automation():
    from automation.management.commands import seed_automation as sa
    return [("runbooks", _n(_qs("automation", "Runbook", created_by="seed_automation", title__in=[r["title"] for r in sa.RUNBOOKS]))),
            ("rules", _n(_qs("automation", "Rule", created_by="seed_automation", name__in=[r["name"] for r in sa.RULES]))),
            ("templates", _n(_qs("automation", "Template", created_by="seed_automation", name__in=[t["name"] for t in sa.TEMPLATES])))]


def _count_bodeod():
    from bodeod.management.commands.seed_bodeod import example_items
    return [("holidays", _n(_qs("bodeod", "Holiday", source="example"))),
            ("checklist items", _n(example_items(_tenant()) if _model("bodeod", "ChecklistItem") else None))]


def _count_telemetry():
    return [("alerts", _n(_qs("telemetry", "Alert", fingerprint__startswith="example", labels__example="true"))),
            ("saved queries", _n(_qs("telemetry", "SavedQuery", owner__isnull=True, name__startswith="Example: ")))]


def _count_observe():
    from observe.management.commands import seed_observe as so
    return [("exporters", _n(_qs("observe", "Exporter", name__in=[e[0] for e in so.EXPORTERS], description=so.NOTE))),
            ("log lines", _n(_qs("observe", "LogEntry", attributes__example=True)))]


def _count_tcpcheck():
    from tcpcheck.management.commands import seed_tcpcheck as st
    return [("TCP checks", _n(_qs("tcpcheck", "TcpCheck", tags__icontains="example", name__in=[e["name"] for e in st.EXAMPLES])))]


# ------------------------------------------------------------------ removals that have no seed `--remove` of their own
def _remove_monitoring():
    """seed_examples + seed_domainmonitor: both write `Example:` domains and seed_examples writes `example,` URL checks."""
    from core.management.commands import seed_examples as se
    from domainmonitor.management.commands import seed_domainmonitor as sd
    n = 0
    urls = _qs("urlchecker", "UrlCheck", name__in=[u[0] for u in se.URLS], tags__startswith="example,")
    if urls is not None:
        n += urls.delete()[0]
    hosts = {d[0] for d in se.DOMAINS} | {e[0] for e in sd.EXAMPLES}
    doms = _qs("domainmonitor", "Domain", hostname__in=list(hosts), description__startswith="Example:")
    if doms is not None:
        n += doms.delete()[0]
    return n


def remove_administration_examples():
    """Delete the example sites (those no real user belongs to) and example users. Returns the number of rows removed."""
    from django.contrib.auth import get_user_model
    n = 0
    site_pks = example_site_pks()
    user_pks = example_user_pks()
    Site = _model("core", "Site")
    n += Site.objects.filter(pk__in=site_pks).delete()[0]
    n += get_user_model().objects.filter(pk__in=user_pks).delete()[0]
    return n


def remove_observe_examples():
    from observe.management.commands import seed_observe as so
    n = 0
    exps = _qs("observe", "Exporter", name__in=[e[0] for e in so.EXPORTERS], description=so.NOTE)
    if exps is not None:
        n += exps.delete()[0]
    logs = _qs("observe", "LogEntry", attributes__example=True)
    if logs is not None:
        n += logs.delete()[0]
    return n


def remove_core_examples():
    return _remove_monitoring()


# ------------------------------------------------------------------ registry
@dataclass
class Area:
    key: str
    label: str
    app: str                       # installed app label; skipped when it is not installed
    counts: callable
    seed: str                      # how it was created, shown in the card
    commands: tuple = ()           # ((command, args...), ...) run by remove(): the seed command's own removal
    func: callable = None          # or a function returning the number of rows removed
    link: str = ""                 # URL name where the examples can be looked at


AREAS = [
    Area("monitoring", "URL checks and domains", "urlchecker", _count_monitoring, "seed_examples, seed_domainmonitor",
         func=remove_core_examples, link="urlchecker:index"),
    Area("administration", "Example sites and users", "administration", _count_administration, "seed_administration, seed_servicedesk",
         func=remove_administration_examples, link="administration:sites"),
    Area("servicedesk", "Service desk", "servicedesk", _count_servicedesk, "seed_servicedesk",
         commands=(("seed_servicedesk", "--remove"),), link="servicedesk:tickets"),
    Area("cmdb", "CMDB", "cmdb", _count_cmdb, "seed_cmdb", commands=(("seed_cmdb", "--clear"),), link="cmdb:items"),
    Area("network", "Network devices", "network", _count_network, "seed_network, seed_ports",
         commands=(("seed_ports", "--remove"), ("seed_network", "--remove")), link="network:all"),
    Area("compute", "Compute", "compute", _count_compute, "seed_compute", commands=(("seed_compute", "--remove"),),
         link="compute:overview"),
    Area("hypervisor", "Hypervisor", "hypervisor", _count_hypervisor, "seed_hypervisor",
         commands=(("seed_hypervisor", "--remove"),), link="hypervisor:sources"),
    Area("hardware", "Server hardware", "hardware", _count_hardware, "seed_hardware", commands=(("seed_hardware", "--remove"),),
         link="hardware:endpoints"),
    Area("architecture", "Architecture", "architecture", _count_architecture, "seed_architecture",
         commands=(("seed_architecture", "--remove"),), link="architecture:overview"),
    Area("dcim", "Data centre", "dcim", _count_dcim, "seed_dcim", commands=(("seed_dcim", "--remove"),), link="dcim:racks"),
    Area("ipam", "IPAM", "ipam", _count_ipam, "seed_ipam", commands=(("seed_ipam", "--remove"),), link="ipam:overview"),
    Area("apm", "APM", "apm", _count_apm, "seed_apm", commands=(("seed_apm", "--remove"),), link="apm:sources"),
    Area("automation", "Automation", "automation", _count_automation, "seed_automation",
         commands=(("seed_automation", "--remove"),), link="automation:runbooks"),
    Area("bodeod", "BOD / EOD", "bodeod", _count_bodeod, "seed_bodeod", commands=(("seed_bodeod", "--remove"),),
         link="bodeod:checklist"),
    Area("telemetry", "Telemetry", "telemetry", _count_telemetry, "seed_telemetry", commands=(("seed_telemetry", "--remove"),),
         link="telemetry:alertmanager"),
    Area("observe", "Exporters and logs", "observe", _count_observe, "seed_observe", func=remove_observe_examples,
         link="observe:exporters"),
    Area("tcpcheck", "TCP checks", "tcpcheck", _count_tcpcheck, "seed_tcpcheck", commands=(("seed_tcpcheck", "--remove"),),
         link="tcpcheck:index"),
]


def areas():
    out = []
    for a in AREAS:
        try:
            if apps.get_app_config(a.app):
                out.append(a)
        except LookupError:
            continue
    return out


def summary(only=None):
    """[{'key','label','seed','link','rows':[(noun,n)],'total':int}] for every installed area (zero-count areas included)."""
    out = []
    for a in areas():
        if only and a.key not in only:
            continue
        try:
            rows = [(noun, n) for noun, n in a.counts() if n]
        except Exception:   # a broken counter must not take the Administration page or the banner down
            log.warning("could not count example data for %s", a.key, exc_info=True)
            rows = []
        out.append({"key": a.key, "label": a.label, "seed": a.seed, "link": a.link, "rows": rows, "total": sum(n for _, n in rows)})
    return out


def total(report=None):
    return sum(r["total"] for r in (report if report is not None else summary()))


def exists():
    """True as soon as one area has an example record (stops at the first hit)."""
    for a in areas():
        try:
            if any(n for _, n in a.counts()):
                return True
        except Exception:
            log.warning("could not count example data for %s", a.key, exc_info=True)
    return False


def remove(only=None, dry_run=False):
    """Remove example data area by area. Returns [{'key','label','before','removed','error'}].

    `dry_run` only counts. Each area runs in its own transaction: if one fails, its rows stay and the others are still
    removed. The removal is the seed command's own `--remove`, so exactly the rows that command created go."""
    results = []
    for a in areas():
        if only and a.key not in only:
            continue
        try:
            before = sum(n for _, n in a.counts())
        except Exception as exc:
            results.append({"key": a.key, "label": a.label, "before": 0, "removed": 0, "error": exc.__class__.__name__})
            continue
        row = {"key": a.key, "label": a.label, "before": before, "removed": 0, "error": ""}
        results.append(row)
        if dry_run or not before:
            continue
        try:
            with transaction.atomic():
                snapshot = _pk_snapshot()
                for cmd, *args in a.commands:
                    call_command(cmd, *args, stdout=io.StringIO(), stderr=io.StringIO())
                if a.func:
                    a.func()
                _forget_events(snapshot, _pk_snapshot())
            row["removed"] = before - sum(n for _, n in a.counts())
        except Exception as exc:
            log.warning("removing example data of %s failed", a.key, exc_info=True)
            row["error"] = exc.__class__.__name__
    return results


# Models whose events (core.Event rows carrying object_type/object_id) describe a record that an area removal can delete.
EVENT_MODELS = ("network.device", "urlchecker.urlcheck", "domainmonitor.domain", "tcpcheck.tcpcheck", "telemetry.alert",
                "servicedesk.ticket", "apm.source", "hypervisor.vcentersource", "hardware.bmcendpoint",
                "cmdb.configurationitem", "automation.rule")


def _pk_snapshot():
    out = {}
    for label in EVENT_MODELS:
        try:
            m = apps.get_model(label)
        except LookupError:
            continue
        out[label] = set(m.objects.values_list("pk", flat=True))
    return out


def _forget_events(before, after):
    """Delete the stream events about records that are gone (an example host's 'is down' must not outlive the host)."""
    Event = _model("core", "Event")
    n = 0
    for label, pks in before.items():
        gone = [str(p) for p in pks - after.get(label, set())]
        for i in range(0, len(gone), 500):
            n += Event.objects.filter(tenant_key=_tenant(), object_type=label, object_id__in=gone[i:i + 500]).delete()[0]
    return n


def known_keys():
    return [a.key for a in areas()]


# ------------------------------------------------------------------ session flag for the admin banner
def remember(request, count=None):
    """Store how many example records exist in the session (no query on later page views). Returns the count."""
    if count is None:
        count = total()
    try:
        request.session[SESSION_KEY] = int(count)
    except Exception:  # pragma: no cover  (a request without a session)
        pass
    return count


def refresh_session_flag(request, user=None):
    """At sign-in: admins learn whether example data exists. Cheap for everybody else (no query at all)."""
    try:
        from administration.services import is_global_admin
        user = user or request.user
        if is_global_admin(user):
            request.session[SESSION_KEY] = 1 if exists() else 0
            request.session.pop(DISMISS_KEY, None)
        else:
            request.session.pop(SESSION_KEY, None)
    except Exception:
        log.warning("could not check for example data at sign-in", exc_info=True)
