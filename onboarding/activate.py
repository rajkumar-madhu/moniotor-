"""Activation: wire an onboarded target into the rest of the product, then show where it landed.

``activate(target)`` is the ``steps.activate`` hook. It is **idempotent**: running it twice for the
same target, or onboarding a host the product already knows, links to the existing records instead of
creating new ones. For one target it ends with

* a ``cmdb.ConfigurationItem`` whose ``prometheus_instance`` is the instance Prometheus will report,
* a ``network.Device`` for SNMP targets, or a compute server (a ``physical_server`` device, optionally
  a ``compute.ClusterMember``) for Kubernetes nodes and hypervisors,
* an ``observe.Exporter`` row for the metrics endpoint (the observe job then scrapes it every minute),
* membership of the service-discovery endpoint (``target.enabled`` and a scrape-ready state),
* a line in the run's step log and an audit row per record created or linked.

How a host is matched to an existing record, in order: the record this target already points at, the
record carrying the same ``prometheus_instance``, the same name, the same IP address. A match is only
used when it belongs to the run's site (or to "All Sites"); a name that is taken by another site's
record gets a disambiguated name instead of stealing it. An existing record is only ever *filled in*
(blank fields), never overwritten, and never moved to another site.

``coverage(run, user)`` is what the Done page shows: for each target, a link to every place the host
appears and whether that place already has data. ``poll()`` is the background job (every 60 s): it asks
Prometheus whether each waiting target reports ``up == 1`` with samples, stamps ``first_sample_at``,
moves the target to live and completes the run once every target is.

Nothing here reads a secret. The facts that reach a CMDB attribute are an allow-list of scalars.
"""
import ipaddress
import logging
from urllib.parse import urlencode

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core.access import scope_by_site
from core.audit import audit
from telemetry.fmt import promql_string

from . import services
from .models import DEFAULT_EXPORTERS, OnboardingRun, RunStatus, TargetState, TargetType

log = logging.getLogger("linkedeye.onboarding")

GLOBAL_SITES = ("", "All Sites")
STALE_AFTER = 15 * 60            # seconds a scraping target may wait before the page says what to check

#: facts that are copied onto a new CMDB item (an allow-list of plain scalars: never a free-form dict)
CI_FACTS = ("os", "kernel", "arch", "cpus", "memory_mb", "exporter", "exporter_version")


def tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- small helpers
def _site_ok(record_site, run_site):
    """May a record on `record_site` be linked to a run on `run_site`?"""
    if (run_site or "") in GLOBAL_SITES:
        return True
    return (record_site or "") in GLOBAL_SITES or record_site == run_site


def _host(target):
    return (target.address or target.hostname or "").strip()


def _ip(value):
    try:
        return str(ipaddress.ip_address((value or "").strip().strip("[]")))
    except ValueError:
        return None


def _scalar_facts(facts):
    out = {}
    for key in CI_FACTS:
        value = (facts or {}).get(key)
        if isinstance(value, (str, int, float)) and not isinstance(value, bool) and value not in ("", None):
            out[key] = value if not isinstance(value, str) else value[:200]
    return out


def _unique_name(model, base, site):
    """A name that is free in this tenant: `base`, else `base (site)`, else `base (site) 2`, ..."""
    candidates = [base]
    if site and site not in GLOBAL_SITES:
        candidates.append(f"{base} ({site})")
    root = candidates[-1]
    candidates += [f"{root} {n}" for n in range(2, 50)]
    for name in candidates:
        name = name[:model._meta.get_field("name").max_length]
        if not model.objects.filter(tenant_key=tenant(), name__iexact=name).exists():
            return name
    return f"{base[:100]} {int(timezone.now().timestamp())}"


def metrics_instance(target):
    """The ``instance`` label Prometheus reports for this target.

    A scraped host or exporter is ``host:port``. A network device is scraped through the snmp_exporter,
    whose series carry the device address as their instance, so the device and its CMDB item use the
    plain host."""
    if target.target_type == TargetType.DEVICE:
        return _host(target)
    return target.instance


def _is_compute(target):
    facts = target.facts or {}
    return (target.target_type == TargetType.K8S_NODE or bool(facts.get("hypervisor"))
            or str(facts.get("role", "")).lower() == "hypervisor")


# ----------------------------------------------------------------------------- the CMDB item
def _ci_type(target):
    from cmdb.models import CIType
    return {TargetType.DEVICE: CIType.NETWORK_DEVICE, TargetType.URL: CIType.SERVICE,
            TargetType.PROMETHEUS: CIType.SERVICE}.get(target.target_type, CIType.SERVER)


def _environment(run, model):
    keys = {value for value, _ in model._meta.get_field("environment").choices}
    return run.environment if run.environment in keys else "prod"


def link_ci(target):
    """(ConfigurationItem, created) for the target. Fills blanks on an existing item; never overwrites."""
    from cmdb.models import ConfigurationItem as CI

    run, instance, host = target.run, metrics_instance(target), _host(target)
    base = CI.objects.filter(tenant_key=tenant())
    ci = None
    if target.ci_id:
        ci = base.filter(pk=target.ci_id).first()
    if ci is None and instance:
        ci = next((c for c in base.filter(prometheus_instance=instance).order_by("pk") if _site_ok(c.site, run.site)), None)
    if ci is None:
        ci = next((c for c in base.filter(name__iexact=target.hostname).order_by("pk") if _site_ok(c.site, run.site)), None)
    if ci is None and _ip(host):
        ci = next((c for c in base.filter(ip_address=_ip(host)).order_by("pk") if _site_ok(c.site, run.site)), None)

    facts = _scalar_facts(target.facts)
    mark = {"onboarding_run": run.pk}
    if ci is not None:
        changed = []
        if not ci.prometheus_instance and instance:
            ci.prometheus_instance = instance
            changed.append("prometheus_instance")
        if ci.ip_address is None and _ip(host):
            ci.ip_address = _ip(host)
            changed.append("ip_address")
        attrs = dict(ci.attributes or {})
        merged = {**{k: v for k, v in facts.items() if k not in attrs}, **mark}
        if any(attrs.get(k) != v for k, v in merged.items()):
            ci.attributes = {**attrs, **merged}
            changed.append("attributes")
        if changed:
            ci.save(update_fields=changed + ["updated_at"])
        return ci, False

    name = _unique_name(CI, target.hostname, run.site)
    ci, created = CI.objects.get_or_create(
        tenant_key=tenant(), name=name,
        defaults={"ci_type": _ci_type(target), "site": run.site or "All Sites", "environment": _environment(run, CI),
                  "ip_address": _ip(host), "prometheus_instance": instance,
                  "description": f"Onboarded by LinkedEye (run #{run.pk}).",
                  "attributes": {**facts, **mark}})
    return ci, created


# ----------------------------------------------------------------------------- device / compute server
def _device_defaults(target, instance):
    from network.models import Device, DeviceType
    facts = target.facts or {}
    snmp = facts.get("snmp") if isinstance(facts.get("snmp"), dict) else {}
    if target.target_type == TargetType.DEVICE:
        wanted = facts.get("device_type")
        dtype = wanted if wanted in DeviceType.values else DeviceType.SWITCH
        firmware = str(snmp.get("sysDescr") or facts.get("os") or "")[:150]
        port = 22
    else:
        dtype = DeviceType.PHYSICAL_SERVER
        firmware = str(facts.get("os") or "")[:150]
        port = target.port if target.target_type in (TargetType.LINUX, TargetType.WINDOWS) else 22
    try:
        port = int(facts.get("check_port") or port)
    except (TypeError, ValueError):
        pass
    if not 1 <= port <= 65535:
        port = 22
    return {"device_type": dtype, "mgmt_ip": _host(target)[:255], "check_port": port,
            "firmware": firmware, "environment": _environment(target.run, Device),
            "description": f"Onboarded by LinkedEye (run #{target.run_id}).",
            "prometheus_instance": instance, "enabled": True}


def link_device(target):
    """(Device, created) for an SNMP target or a compute server (Kubernetes node, hypervisor)."""
    from network.models import Device

    run, instance, host = target.run, metrics_instance(target), _host(target)
    base = Device.objects.filter(tenant_key=tenant())
    dev = None
    if target.device_id:
        dev = base.filter(pk=target.device_id).first()
    if dev is None and instance:
        dev = next((d for d in base.filter(prometheus_instance=instance).order_by("pk") if _site_ok(d.site, run.site)), None)
    if dev is None:
        dev = next((d for d in base.filter(name__iexact=target.hostname).order_by("pk") if _site_ok(d.site, run.site)), None)
    if dev is None and host:
        dev = next((d for d in base.filter(mgmt_ip=host).order_by("pk") if _site_ok(d.site, run.site)), None)
    if dev is not None:
        changed = []
        if not dev.prometheus_instance and instance:
            dev.prometheus_instance = instance
            changed.append("prometheus_instance")
        if not dev.mgmt_ip and host:
            dev.mgmt_ip = host
            changed.append("mgmt_ip")
        if changed:
            dev.save(update_fields=changed + ["updated_at"])
        return dev, False
    name = _unique_name(Device, target.hostname, run.site)
    dev, created = Device.objects.get_or_create(
        tenant_key=tenant(), name=name,
        defaults={**_device_defaults(target, instance), "site": run.site or ""})
    return dev, created


def link_cluster_member(target, device):
    """Add the compute server to the Kubernetes/hypervisor cluster named in its facts, when that cluster
    exists on the same site. Returns the ClusterMember or None. Never creates a cluster."""
    if not apps.is_installed("compute"):
        return None
    name = str((target.facts or {}).get("cluster") or "").strip()
    if not name:
        return None
    from compute.models import Cluster, ClusterMember, MemberRole
    cluster = Cluster.objects.filter(tenant_key=tenant(), name__iexact=name).first()
    if cluster is None or not _site_ok(cluster.site, target.run.site):
        return None
    role = MemberRole.WORKER if target.target_type == TargetType.K8S_NODE else MemberRole.HOST
    member, _ = ClusterMember.objects.get_or_create(tenant_key=tenant(), cluster=cluster, device=device,
                                                    defaults={"role": role})
    return member


# ----------------------------------------------------------------------------- exporter
EXPORTER_STYLE = {          # exporter kind -> (display type, observe category)
    "node_exporter": ("Node Exporter", "Servers"),
    "windows_exporter": ("Windows", "Servers"),
    "snmp_exporter": ("SNMP", "Network"),
    "kube-state-metrics": ("Kubernetes", "Kubernetes"),
    "blackbox_exporter": ("Blackbox (HTTP/TCP)", "Applications"),
    "prometheus": ("Custom", "Custom"),
}


def exporter_url(target):
    """The URL the observe job scrapes for this target. A device is read through the snmp_exporter."""
    if target.target_type == TargetType.DEVICE:
        from . import devices
        base = devices.exporter_url()
        if not base:
            return ""
        cfg = devices.snmp_settings(target)
        return f"{base.rstrip('/')}/snmp?" + urlencode(
            {"target": _host(target), "module": cfg["module"], "auth": cfg["auth_profile"]})
    return target.metrics_url


def link_exporter(target):
    """(Exporter, created). Matched by the metrics URL, then the name; fills blanks only."""
    from observe.models import Exporter

    run = target.run
    kind = target.exporter_kind or DEFAULT_EXPORTERS.get(target.target_type, "")
    label, category = EXPORTER_STYLE.get(kind, (kind or "Custom", "Custom"))
    url = exporter_url(target)
    base = Exporter.objects.filter(tenant_key=tenant())
    exp = None
    if target.exporter_id:
        exp = base.filter(pk=target.exporter_id).first()
    if exp is None and url:
        exp = next((e for e in base.filter(metrics_url=url).order_by("pk") if _site_ok(e.site, run.site)), None)
    if exp is None:
        exp = next((e for e in base.filter(name__iexact=f"{kind or 'exporter'} {target.hostname}").order_by("pk")
                    if _site_ok(e.site, run.site)), None)
    if exp is not None:
        changed = []
        if not exp.metrics_url and url:
            exp.metrics_url = url
            changed.append("metrics_url")
            exp.apply_config_status(url_changed=True)
            changed += ["status", "status_changed_at", "last_error", "samples", "last_scrape_ms", "last_seen"]
        if not exp.target:
            exp.target = target.hostname
            changed.append("target")
        if changed:
            exp.save(update_fields=sorted(set(changed)) + ["updated_at"])
        return exp, False
    name = _unique_name(Exporter, f"{kind or 'exporter'} {target.hostname}", run.site)
    exp, created = Exporter.objects.get_or_create(
        tenant_key=tenant(), name=name,
        defaults={"exporter_type": label, "category": category, "target": target.hostname, "metrics_url": url,
                  "site": run.site, "enabled": True,
                  "description": f"Onboarded by LinkedEye (run #{run.pk}). Scraped every minute for reachability."})
    if created:
        exp.apply_config_status(url_changed=True)
        exp.save(update_fields=["status", "status_changed_at", "last_error", "samples", "last_scrape_ms",
                                "last_seen", "updated_at"])
    return exp, created


# ----------------------------------------------------------------------------- the hook
def activate(target, request=None):
    """The ``steps.activate`` hook. See the module docstring. Returns the plain dict the wizard persists."""
    run = target.run
    created, linked, warnings = [], [], []
    if target.state in (TargetState.UNREACHABLE, TargetState.FAILED):
        warnings.append(f"{target.hostname} was {target.get_state_display().lower()} at the last check; it is "
                        "registered anyway because you asked for it, and goes live when Prometheus can scrape it.")
    elif target.state == TargetState.NEW:
        warnings.append(f"{target.hostname} has not been verified; it is registered on your say-so.")

    with transaction.atomic():
        ci, ci_new = link_ci(target)
        (created if ci_new else linked).append("cmdb item")
        device = member = None
        if target.target_type == TargetType.DEVICE or _is_compute(target):
            device, dev_new = link_device(target)
            (created if dev_new else linked).append("network device" if target.target_type == TargetType.DEVICE
                                                    else "compute server")
            member = link_cluster_member(target, device)
            if member is not None:
                linked.append("cluster membership")
        exporter, exp_new = link_exporter(target)
        (created if exp_new else linked).append("exporter")
        if ci.prometheus_instance != metrics_instance(target) and metrics_instance(target):
            warnings.append(f"CMDB item “{ci.name}” already carries the instance “{ci.prometheus_instance}”; "
                            "it was kept, so telemetry pages find this host under that name.")

        target.ci, target.device, target.exporter = ci, device, exporter
        target.enabled = True                # service-discovery membership
        target.save(update_fields=["ci", "device", "exporter", "enabled", "updated_at"])

    audit(request, "onboarding.activate.target", target, site=run.site, hostname=target.hostname,
          created=created, linked=linked, ci=ci.pk, device=getattr(device, "pk", None), exporter=exporter.pk)
    parts = [f"created {', '.join(created)}" if created else "", f"linked {', '.join(linked)}" if linked else "",
             "joined service discovery"]
    services.log_step(run, "confirm", "ok", f"{target.hostname}: " + "; ".join(p for p in parts if p),
                      target=target)
    for text in warnings:
        services.log_step(run, "confirm", "warn", text[:500], target=target)

    return {"state": TargetState.LIVE if target.state == TargetState.LIVE else TargetState.SCRAPING,
            "ci": ci.pk, "device": getattr(device, "pk", None), "exporter": exporter.pk,
            "cluster_member": getattr(member, "pk", None), "created": created, "linked": linked,
            "service_discovery": True, "instance": target.instance, "warnings": warnings,
            "activated_at": timezone.now().isoformat(timespec="seconds")}


# ----------------------------------------------------------------------------- the background job
def _label_sets(rows):
    """(le_target ids, instances) present in a list of query rows."""
    ids, instances = set(), set()
    for row in rows:
        metric = row.get("metric") or {}
        if metric.get("le_target"):
            ids.add(str(metric["le_target"]))
        if metric.get("instance"):
            instances.add(metric["instance"])
    return ids, instances


def _seen(target, label_sets):
    ids, instances = label_sets
    return str(target.pk) in ids or target.instance in instances or metrics_instance(target) in instances


def poll(tenant_key=None):
    """Move scraping targets to live once Prometheus has samples for them *and* reports ``up == 1``,
    stamp ``first_sample_at``, and complete (and announce) every run whose targets are all live.

    The samples check is here (``scrape_samples_scraped > 0`` for the target); the ``up == 1`` check, the
    state change, the step-log line and the run completion with its event are ``services.refresh_liveness``.
    Never raises. Returns {"checked", "live", "completed", "note"}. Prometheus not being connected, or not
    answering, changes nothing: the targets keep waiting."""
    from core import prometheus
    out = {"checked": 0, "live": 0, "completed": 0, "note": ""}
    try:
        waiting = list(services.waiting_targets(tenant_key)[:services.POLL_LIMIT])
        out["checked"] = len(waiting)
        if not waiting:
            return out
        if not prometheus.configured():
            out["note"] = "Prometheus is not connected"
            return out
        try:
            samples = _label_sets(prometheus.query("scrape_samples_scraped > 0"))
        except prometheus.PrometheusError as exc:
            log.info("onboarding liveness poll skipped: %s", exc)
            out["note"] = "Prometheus did not answer"
            return out
        now = timezone.now()
        ready = []
        for target in waiting:
            if _seen(target, samples):
                ready.append(target)
            else:
                _note_stale(target, now)
        open_runs = {t.run_id for t in ready if t.run.status != RunStatus.COMPLETE}
        out["live"] = services.refresh_liveness(ready)
        if out["live"] and open_runs:
            out["completed"] = OnboardingRun.objects.filter(pk__in=open_runs, status=RunStatus.COMPLETE).count()
    except Exception:                               # a job never raises
        log.exception("onboarding liveness poll failed")
        out["note"] = "error"
    return out


def _note_stale(target, now):
    """After STALE_AFTER seconds of waiting, say once what to check."""
    facts = target.facts or {}
    at = (facts.get("activation") or {}).get("activated_at")
    if not at or facts.get("waiting_warned"):
        return
    from django.utils.dateparse import parse_datetime
    started = parse_datetime(at)
    if started is None or (now - started).total_seconds() < STALE_AFTER:
        return
    hint = (f"Prometheus has not reported {target.instance} after {STALE_AFTER // 60} minutes. Check that the "
            "scrape_config from the service discovery page is pasted into Prometheus and that Prometheus can "
            "reach this host and port.")
    services.set_target_state(target, target.state, error=hint, facts={"waiting_warned": True})
    services.log_step(target.run, "confirm", "warn", f"{target.hostname}: {hint}", target=target)


# ----------------------------------------------------------------------------- the Done page
STATE_LABEL = {"yes": "Has data", "no": "No data yet", "na": "Not applicable", "unknown": "Not checked"}
STATE_TONE = {"yes": "ok", "no": "warn", "na": "mute", "unknown": "mute"}


def _url(name, *args, **params):
    try:
        href = reverse(name, args=args)
    except NoReverseMatch:
        return ""
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return f"{href}?{urlencode(clean)}" if clean else href


def prom_snapshot():
    """What Prometheus says about every instance right now: {"ok", "configured", "rows"}. One query."""
    from core import prometheus
    out = {"ok": False, "configured": False, "rows": []}
    try:
        if not prometheus.configured():
            return out
        out["configured"] = True
        out["rows"] = prometheus.query("up")
        out["ok"] = True
    except prometheus.PrometheusError as exc:
        log.info("onboarding coverage: Prometheus did not answer: %s", exc)
    except Exception:                               # noqa: BLE001 - the Done page must still render
        log.exception("onboarding coverage: Prometheus snapshot failed")
    return out


def _series_for(target, snapshot):
    """The ``up`` row for this target (by le_target, then instance), or None."""
    for row in snapshot["rows"]:
        if (row.get("metric") or {}).get("le_target") == str(target.pk):
            return row["metric"]
    wanted = {target.instance, metrics_instance(target)}
    for row in snapshot["rows"]:
        if (row.get("metric") or {}).get("instance") in wanted:
            return row["metric"]
    return None


def _item(key, label, icon, href, state, note=""):
    return {"key": key, "label": label, "icon": icon, "href": href, "state": state,
            "state_label": STATE_LABEL[state], "tone": STATE_TONE[state], "note": note}


def _log_count(target, user):
    from observe.models import LogEntry
    hosts = {h for h in (target.hostname, target.address, (target.facts or {}).get("hostname")) if h}
    qs = scope_by_site(LogEntry.objects.filter(tenant_key=tenant(), host__in=hosts), user)
    return qs.count()


def _alert_count(target, user, instances):
    Alert = apps.get_model("telemetry", "Alert")
    qs = scope_by_site(Alert.objects.for_tenant().filter(instance__in=instances), user, field="site")
    return qs.count(), qs.filter(status=Alert.FIRING).count()


def target_items(target, user, snapshot):
    """Every place `target` shows, with a link and whether it already has data."""
    run = target.run
    series = _series_for(target, snapshot)
    instance = (series or {}).get("instance") or metrics_instance(target)
    reported = series is not None
    known = snapshot["ok"]
    in_prom = "yes" if reported else ("no" if known else "unknown")
    is_node = target.target_type == TargetType.LINUX and (
        not reported or "node" in str((series or {}).get("job", "node")).lower())
    host_q = target.hostname
    items = []

    items.append(_item("fleet", "Fleet overview", "gauge",
                       _url("dashboard", site=run.site), "yes" if target.ci_id else "no",
                       "Counted in the site matrix once its CMDB item exists."))
    if is_node:
        items.append(_item("infrastructure", "Infrastructure", "server", _url("telemetry:infrastructure", q=host_q),
                           in_prom, "Listed with live CPU, memory, disk and network once Prometheus reports it."))
        items.append(_item("metrics", "Full metrics page", "analytics", _url("telemetry:host_metrics", instance),
                           in_prom, "Every chart: CPU, memory, storage, network, system."))
    else:
        items.append(_item("infrastructure", "Infrastructure", "server", _url("telemetry:infrastructure", q=host_q),
                           "na", "That list shows node_exporter hosts; this target is scraped by "
                                 f"{target.exporter_kind or 'another exporter'}."))
        if target.device_id:
            has = bool(getattr(target.device, "metrics_updated_at", None))
            items.append(_item("metrics", "Full metrics page", "analytics", _url("network:detail", target.device_id),
                               "yes" if has else "no", "The device page charts interfaces, CPU, memory and uptime."))
    from core.access import visible_sites
    if visible_sites(user) is None:
        items.append(_item("explorer", "Metrics explorer", "search",
                           _url("telemetry:explorer", expr=f"up{{instance={promql_string(instance)}}}"),
                           in_prom, "Run any PromQL against this host."))
    logs = _log_count(target, user)
    items.append(_item("logs", "Logs", "logs", _url("observe:logs", host=target.hostname, range="7d"),
                       "yes" if logs else "no",
                       f"{logs} line(s) in the last stored period." if logs else
                       "Nothing has arrived yet: ship logs with the config from the Logs step."))
    if target.ci_id:
        items.append(_item("cmdb", "CMDB record", "database", _url("cmdb:item_detail", target.ci_id), "yes",
                           f"“{target.ci.name}” carries instance {target.ci.prometheus_instance or '—'}."))
    total, firing = _alert_count(target, user, {i for i in (instance, target.instance, metrics_instance(target)) if i})
    items.append(_item("alerts", "Alerts", "bell", _url("telemetry:alertmanager", tab="all", q=instance),
                       "yes" if total else "no",
                       f"{firing} firing, {total} on record." if total else
                       "No alerts for this host. Rules in Prometheus raise them here when something breaks."))
    if target.device_id:
        label = "Network device" if target.target_type == TargetType.DEVICE else "Compute server"
        items.append(_item("device", label, "network", _url("network:detail", target.device_id), "yes",
                           f"“{target.device.name}” in the device inventory."))
    if target.exporter_id:
        exp = target.exporter
        has = exp.status == "active"
        items.append(_item("exporter", "Exporter", "exporters", _url("observe:exporter_detail", target.exporter_id),
                           "yes" if has else "no",
                           f"{exp.get_status_display()}" + (f", {exp.samples} samples" if exp.samples else "")))
    return [i for i in items if i["href"]]


def coverage(run, user):
    """[{"target", "items", "with_data", "applicable"}] for the Done page, one block per target."""
    snapshot = prom_snapshot()
    blocks = []
    for target in run.targets.select_related("ci", "device", "exporter"):
        items = target_items(target, user, snapshot)
        usable = [i for i in items if i["state"] != "na"]
        blocks.append({"target": target, "items": items, "applicable": len(usable),
                       "with_data": sum(1 for i in usable if i["state"] == "yes"),
                       "prometheus": snapshot["ok"]})
    return blocks


def activation_rows(run):
    """What activation created or linked for each target of the run (the Confirm page table)."""
    rows = []
    for target in run.targets.select_related("ci", "device", "exporter"):
        info = (target.facts or {}).get("activation") or {}
        rows.append({
            "target": target,
            "activated": bool(target.ci_id or target.exporter_id),
            "ci": target.ci, "device": target.device, "exporter": target.exporter,
            "ci_url": _url("cmdb:item_detail", target.ci_id) if target.ci_id else "",
            "device_url": _url("network:detail", target.device_id) if target.device_id else "",
            "exporter_url": _url("observe:exporter_detail", target.exporter_id) if target.exporter_id else "",
            "created": info.get("created") or [], "linked": info.get("linked") or [],
            "warnings": info.get("warnings") or [],
        })
    return rows
