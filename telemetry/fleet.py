"""Fleet home data layer – the Netdata room "Home" / Overview equivalent (Monitoring → Fleet).

Nothing here is invented: every panel is derived from data LinkedEye already has.

* ``telemetry.hosts.fetch_hosts``     – Prometheus ``up``, CPU, memory, disk, load, uptime per node_exporter
                                        instance (cached for ``hosts.CACHE_SECONDS``).
* ``telemetry.targets.fetch_targets`` – every scrape target with its health, last scrape and scrape interval.
* ``cmdb.ConfigurationItem``          – CMDB name, site, environment and owner for an instance; the client
                                        comes from ``core.Site.client`` of that site.
* ``telemetry.models.Alert``          – Alertmanager webhook history, for the alert leaderboards.
* two extra cached Prometheus queries – the series count and the per-host OS label.

Prometheus is often unconfigured or broken. Every function here returns the data it could get **plus an
error string** and never raises ``PrometheusError``, so the page always renders with a banner explaining
what is missing.
"""
import logging
import re
from datetime import timedelta

from django.core.cache import cache
from django.utils import timezone

from core import prometheus
from core.access import visible_sites

from . import hosts as hosts_svc
from . import palette as pal
from . import promapi
from . import targets as targets_svc
from .fmt import finite
from .models import Alert, severity_tone

log = logging.getLogger("linkedeye.telemetry")

CACHE_SECONDS = hosts_svc.CACHE_SECONDS          # host / target data, same freshness as the Infrastructure page
SLOW_CACHE_SECONDS = 300                         # series count and OS labels: expensive and barely move
RANGES = hosts_svc.RANGES                        # 1h / 6h / 24h / 7d, shared with the host pages

# ----------------------------------------------------------------------------- the four host states
LIVE, DOWN, STALE, NEVER_SEEN = "live", "down", "stale", "never_seen"
STATES = (LIVE, DOWN, STALE, NEVER_SEEN)
STATE_LABEL = {LIVE: "Live", DOWN: "Down", STALE: "Stale", NEVER_SEEN: "Never seen"}
STATE_TONE = {LIVE: "ok", DOWN: "crit", STALE: "warn", NEVER_SEEN: "mute"}
STATE_COLOR = {LIVE: pal.GREEN, DOWN: pal.DOWN, STALE: pal.AMBER, NEVER_SEEN: pal.STONE_LIGHT}
STATE_HELP = {
    LIVE: "Scraped successfully (up == 1)",
    DOWN: "Scrape failing (up == 0) or Prometheus reports the target as down",
    STALE: "A scrape target exists but its data stopped arriving",
    NEVER_SEEN: "Known from the CMDB only – Prometheus has no sample for it",
}

STALE_FACTOR = 2                                 # "older than 2 × the scrape interval" (the spec's rule)
_DURATION = re.compile(r"(\d+(?:\.\d+)?)(ms|[smhdw])")
_UNIT_SECONDS = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
NODE_JOB_RE = re.compile(r"node", re.I)          # the jobs telemetry.hosts scrapes hosts from (job=~".*node.*")


def parse_interval(text):
    """Prometheus duration string ('15s', '1m30s', '500ms') → seconds, or None when missing/unparseable."""
    if not text or not isinstance(text, str):
        return None
    parts = _DURATION.findall(text.strip())
    if not parts:
        return None
    total = sum(float(n) * _UNIT_SECONDS[u] for n, u in parts)
    return total or None


def host_state(up, target=None, now=None, stale_factor=STALE_FACTOR, discovered=False, grace=0):
    """The one rule for a host's state. Returns exactly one of ``live`` / ``down`` / ``stale`` / ``never_seen``.

    ``up``     – latest value of the instance's ``up`` series: 1, 0, or None when Prometheus has no sample.
    ``target`` – the matching row from ``telemetry.targets.fetch_targets`` (``None`` when the instance is not
                 a scrape target: hosts can also arrive by remote write or federation).
    ``grace``  – seconds added to the stale threshold. Target rows are cached for ``targets_svc.CACHE_SECONDS``,
                 so a row's ``last_scrape`` can be that much older than the real last scrape; without the
                 allowance a healthy 15 s target read from a 30 s cache would flap to stale.
    ``discovered`` – True when Prometheus discovery has scraped this instance before (a
                 ``DiscoveredTarget`` row exists), so it has produced samples at some point.

    * ``down``       – ``up == 0``, or Prometheus reports the scrape target itself as down (a failed scrape
                       is the authoritative "this host is not answering" signal, and it is the reason an
                       ``up`` sample can be missing altogether).
    * ``stale``      – a scrape target exists but Prometheus has no ``up`` sample for it (scraping stopped,
                       or the metric was relabelled away), **or** its last scrape is older than
                       ``stale_factor`` × the target's scrape interval. An interval the target does not
                       report cannot be judged, so such a target is never called stale on age alone.
    * ``never_seen`` – no sample and no scrape target: the instance is known only from an inventory source
                       (a CMDB item's ``prometheus_instance``), so it has never produced a sample. An instance
                       Prometheus discovery has recorded as scraped before (``discovered``) that has since lost
                       both its target and its samples is **stale** instead: it did exist. Without a discovery
                       row LinkedEye keeps no sample history, so such an instance reads as never seen.
    * ``live``       – everything else: ``up == 1`` with a scrape that is not overdue.

    The Fleet page and the Infrastructure page both label hosts through this function, so the two pages can
    never disagree about a host.
    """
    health = (target or {}).get("health") if target else None
    if health == "down":
        return DOWN
    if not finite(up):
        return STALE if (target or discovered) else NEVER_SEEN
    if target:
        interval = parse_interval(target.get("interval"))
        last = target.get("last_scrape")
        if interval and last is not None:
            age = ((now or timezone.now()) - last).total_seconds()
            if age > stale_factor * interval + grace:
                return STALE
    return DOWN if int(up) == 0 else LIVE


def annotate_states(rows, targets_by_instance=None, now=None, discovered=(), grace=targets_svc.CACHE_SECONDS):
    """Set ``state`` (and ``state_label``) on host dicts in place, then return them.

    Used by both the Fleet page and the Infrastructure page: the labelling lives in exactly one place.
    """
    targets_by_instance = targets_by_instance or {}
    now = now or timezone.now()
    for h in rows:
        h["state"] = host_state(h.get("up"), targets_by_instance.get(h.get("instance")), now=now,
                                discovered=h.get("instance") in discovered, grace=grace)
        h["state_label"] = STATE_LABEL[h["state"]]
    return rows


# ----------------------------------------------------------------------------- Prometheus / CMDB sources
def node_target_map(use_cache=True):
    """``({instance: target}, error)`` for scrape targets in node jobs. Never raises."""
    try:
        rows = targets_svc.fetch_targets(use_cache=use_cache)
    except prometheus.PrometheusError as e:
        return {}, str(e)
    out = {}
    for t in rows:
        if not t.get("instance") or not NODE_JOB_RE.search(t.get("job") or ""):
            continue
        cur = out.get(t["instance"])
        # several node jobs may scrape one instance: the healthiest row wins, like fetch_hosts' max(up)
        if cur is None or targets_svc.HEALTH_ORDER[t["health"]] > targets_svc.HEALTH_ORDER[cur["health"]]:
            out[t["instance"]] = t
    return out, ""


def all_targets(use_cache=True):
    """``(rows, error)`` for every scrape target (not only node jobs). Never raises."""
    try:
        return targets_svc.fetch_targets(use_cache=use_cache), ""
    except prometheus.PrometheusError as e:
        return [], str(e)


SERIES_QUERY = 'count({__name__!=""})'


def series_count(use_cache=True):
    """``(count, error)`` – how many series the Prometheus TSDB holds, cached for SLOW_CACHE_SECONDS.

    ``count({__name__!=""})`` is one of the heavier queries Prometheus can be asked, hence the long cache.
    """
    key = "telemetry:fleet:series:v1"
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    try:
        rows = promapi.query(SERIES_QUERY)
        value = int(rows[0]["value"]) if rows and finite(rows[0].get("value")) else None
        out = (value, "")
    except prometheus.PrometheusError as e:
        out = (None, str(e))
    cache.set(key, out, SLOW_CACHE_SECONDS if out[0] is not None else CACHE_SECONDS)  # retry a failure soon
    return out


OS_QUERIES = {"os": "node_os_info", "uname": "node_uname_info"}


def os_labels(use_cache=True):
    """``({instance: 'Debian GNU/Linux 12'}, error)`` from node_os_info, falling back to node_uname_info."""
    key = "telemetry:fleet:os:v1"
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    try:
        res = hosts_svc._run_parallel(OS_QUERIES, promapi.query)
    except prometheus.PrometheusError as e:
        return {}, str(e)
    out = {}
    for r in res.get("uname") or ():
        m = r.get("metric") or {}
        inst, name = m.get("instance"), " ".join(x for x in (m.get("sysname", ""), m.get("release", "")) if x)
        if inst and name:
            out[inst] = name
    for r in res.get("os") or ():
        m = r.get("metric") or {}
        inst = m.get("instance")
        name = m.get("pretty_name") or m.get("name") or ""
        if inst and name:
            out[inst] = name
    pair = (out, "")
    cache.set(key, pair, SLOW_CACHE_SECONDS)
    return pair


def cmdb_instances():
    """``{instance: ConfigurationItem}`` for every CI that names a prometheus_instance. ``{}`` without cmdb."""
    try:
        from django.apps import apps
        from django.conf import settings
        CI = apps.get_model("cmdb", "ConfigurationItem")
    except (LookupError, ImportError):
        return {}
    try:
        qs = CI.objects.exclude(prometheus_instance="")
        if hasattr(CI, "tenant_key"):
            qs = qs.filter(tenant_key=settings.LINKEDEYE_TENANT)
        return {ci.prometheus_instance: ci
                for ci in qs.only("pk", "name", "site", "ci_type", "environment", "owner", "prometheus_instance")}
    except Exception as e:  # cmdb tables missing (not migrated) must not break the page
        log.warning("CMDB instance lookup failed: %s", e)
        return {}


def discovered_hosts():
    """``{instance: {os, site, job}}`` for host-kind rows of Prometheus discovery (telemetry.DiscoveredTarget)
    that were scraped at some point and are not ignored. ``{}`` when the table is unavailable."""
    try:
        from django.apps import apps
        DT = apps.get_model("telemetry", "DiscoveredTarget")
        out = {}
        rows = sorted(DT.objects.for_tenant().hosts().exclude(state=DT.IGNORED),
                      key=lambda d: (d.missing_since is not None, d.job))   # a row still being scraped wins
        for d in rows:
            det = d.details or {}
            out.setdefault(d.instance, {"os": str(det.get("os") or det.get("kernel") or "").strip(),
                                        "site": d.site if d.site and d.site != "All Sites" else "", "job": d.job})
        return out
    except Exception as e:  # discovery tables missing (not migrated) must not break the page
        log.warning("Discovered-host lookup failed: %s", e)
        return {}


def site_clients():
    """``{site name: client name}`` from core.Site. ``{}`` when sites carry no client."""
    try:
        from django.apps import apps
        from django.conf import settings
        Site = apps.get_model("core", "Site")
        qs = Site.objects.filter(tenant_key=settings.LINKEDEYE_TENANT).select_related("client")
        return {s.name: (s.client.name if s.client_id else "") for s in qs}
    except Exception as e:
        log.warning("Site/client lookup failed: %s", e)
        return {}


def host_url(instance):
    try:
        from django.urls import reverse
        return reverse("telemetry:host_detail", args=[instance])
    except Exception:  # pragma: no cover - URLconf always has it
        return ""


# ----------------------------------------------------------------------------- the fleet
ENV_LABEL = {"prod": "Production", "uat": "UAT", "dr": "DR", "dev": "Development"}
ENV_COLOR = {"prod": pal.BLUE, "uat": pal.PURPLE, "dr": pal.TEAL, "dev": pal.STONE}
UNKNOWN = "Unassigned"


def host_states(use_cache=True, with_prometheus=True):
    """Every host LinkedEye knows about, each with exactly one state.

    Returns ``{"hosts": [...], "errors": {...}}``. ``errors`` maps a source name ("hosts", "targets", "os")
    to the message that source failed with; the hosts list then simply carries less information (a fleet
    with no Prometheus at all is every CMDB instance as ``never_seen``).

    Each host dict has the ``telemetry.hosts.fetch_hosts`` fields (instance, job, up, cpu, memory, disk,
    load1, cores, rx, tx, uptime, health) plus ``state``, ``os``, ``site``, ``environment``, ``owner``,
    ``client``, ``ci_name``, ``ci_url``, ``url``, ``last_scrape`` and ``interval``.
    """
    errors = {}
    rows, tmap, oses = [], {}, {}
    if with_prometheus:  # unconfigured Prometheus is not an error: the fleet is then the CMDB inventory
        try:
            rows = [dict(h) for h in hosts_svc.fetch_hosts(use_cache=use_cache)]
        except prometheus.PrometheusError as e:
            errors["hosts"] = str(e)
        tmap, terr = node_target_map(use_cache=use_cache)
        if terr:
            errors["targets"] = terr
        oses, oserr = os_labels(use_cache=use_cache)
        if oserr:
            errors["os"] = oserr

    known = {h["instance"] for h in rows}
    # scrape targets Prometheus knows but which produced no `up` sample, and CMDB instances with neither
    for inst, t in sorted(tmap.items()):
        if inst not in known:
            rows.append({"instance": inst, "job": t.get("job", ""), "up": None, "health": "unknown"})
            known.add(inst)
    cis = cmdb_instances()
    disc = discovered_hosts() if with_prometheus else {}
    # hosts discovery scraped before that have since lost target and samples: stale, not "never seen"
    for inst in sorted(disc):
        if inst not in known:
            rows.append({"instance": inst, "job": disc[inst]["job"], "up": None, "health": "unknown"})
            known.add(inst)
    for inst in sorted(cis):
        if inst not in known:
            rows.append({"instance": inst, "job": "", "up": None, "health": "unknown"})
            known.add(inst)

    annotate_states(rows, tmap, discovered=disc)
    clients = site_clients()
    for h in rows:
        inst = h["instance"]
        ci = cis.get(inst)
        t = tmap.get(inst)
        h["ci"] = ci
        h["ci_name"] = getattr(ci, "name", "") or ""
        h["ci_url"] = hosts_svc.ci_url(ci)
        h["site"] = (getattr(ci, "site", "") or "") if ci is not None else (disc.get(inst) or {}).get("site", "")
        h["environment"] = (getattr(ci, "environment", "") or "") if ci is not None else ""
        h["environment_label"] = ENV_LABEL.get(h["environment"], h["environment"] or UNKNOWN)
        h["owner"] = (getattr(ci, "owner", "") or "") if ci is not None else ""
        h["client"] = clients.get(h["site"], "") or ""
        h["os"] = oses.get(inst) or (disc.get(inst) or {}).get("os", "")
        h["name"] = h["ci_name"] or inst
        h["url"] = host_url(inst)
        h["last_scrape"] = t.get("last_scrape") if t else None
        h["interval"] = t.get("interval") if t else ""
        h.setdefault("cpu", None)
        h.setdefault("memory", None)
        h.setdefault("disk", None)
        h.setdefault("load1", None)
        h.setdefault("load_per_core", None)
        h.setdefault("cores", None)
        h.setdefault("uptime", None)
        h.setdefault("rx", None)
        h.setdefault("tx", None)
        h.setdefault("health", "unknown")
    rows.sort(key=lambda h: (STATES.index(h["state"]), h["instance"]))
    return {"hosts": rows, "errors": errors}


def site_visible(user, site):
    """True when a record on ``site`` is visible to the user (global records always are)."""
    sites = visible_sites(user)
    return sites is None or site in ("", "All Sites") or site in sites


def scope_hosts(user, rows):
    """Hosts the user may see: the site of the host's CMDB item must be one of their sites. Hosts with no
    CMDB item, or on "All Sites", stay visible – the same rule the Infrastructure page applies."""
    if visible_sites(user) is None:
        return list(rows)
    return [h for h in rows if site_visible(user, h.get("site") or "")]


def scope_targets(user, rows):
    """Scrape targets the user may see, by the site of the CMDB item for their instance."""
    if visible_sites(user) is None:
        return list(rows)
    cis = cmdb_instances()
    return [t for t in rows if site_visible(user, (getattr(cis.get(t.get("instance")), "site", "") or ""))]


def state_counts(rows):
    return {s: sum(1 for h in rows if h.get("state") == s) for s in STATES}


# ----------------------------------------------------------------------------- group by / colour by
GROUPS = [("", "No grouping"), ("site", "Site"), ("job", "Job / scrape pool"), ("environment", "Environment"),
          ("os", "Operating system"), ("client", "Client"), ("state", "State")]
GROUP_KEYS = tuple(k for k, _l in GROUPS if k)
COLOURS = [("state", "State"), ("cpu", "CPU"), ("memory", "Memory"), ("disk", "Disk"), ("alerts", "Alerts")]
COLOUR_KEYS = tuple(k for k, _l in COLOURS)

ALERT_BUCKETS = [("critical", "Critical", pal.CRIT), ("warning", "Warning", pal.AMBER),
                 ("other", "Other", pal.BLUE), ("none", "No alerts", pal.GREEN)]
GRADE_COLOR = {"ok": pal.GREEN, "warn": pal.AMBER, "crit": pal.CRIT, "mute": pal.STONE_LIGHT}


def group_key(value):
    """The group-by dimension from a GET parameter; "" (no grouping) for anything unknown."""
    value = (value or "").strip().lower()
    return value if value in GROUP_KEYS else ""


def colour_key(value):
    """The colour-by dimension from a GET parameter; "state" for anything unknown."""
    value = (value or "").strip().lower()
    return value if value in COLOUR_KEYS else "state"


def group_value(host, key):
    """The label of the group a host falls in, for the chosen dimension."""
    if not key:
        return ""
    if key == "state":
        return STATE_LABEL[host["state"]]
    if key == "environment":
        return host.get("environment_label") or UNKNOWN
    return (host.get(key) or "").strip() or UNKNOWN


def grouped(rows, key):
    """``[(label, [hosts])]`` – named groups first (alphabetically, biggest first within equal names is not
    needed: hosts keep the fleet's state order), ``Unassigned`` last. ``[("", rows)]`` without grouping."""
    if not key:
        return [("", list(rows))]
    buckets = {}
    for h in rows:
        buckets.setdefault(group_value(h, key), []).append(h)
    if key == "state":  # states have a meaningful order of their own
        order = [STATE_LABEL[s] for s in STATES]
        return [(lbl, buckets[lbl]) for lbl in order if lbl in buckets]
    named = sorted((k for k in buckets if k != UNKNOWN), key=lambda s: s.lower())
    return [(k, buckets[k]) for k in named] + ([(UNKNOWN, buckets[UNKNOWN])] if UNKNOWN in buckets else [])


def alert_bucket(host):
    if host.get("alerts_critical"):
        return "critical"
    if host.get("alerts_warning"):
        return "warning"
    return "other" if host.get("alerts_firing") else "none"


def colour_of(host, key):
    """``(colour, text)`` for one host's status-grid cell under the chosen colour-by dimension."""
    if key == "state":
        return STATE_COLOR[host["state"]], STATE_LABEL[host["state"]]
    if key == "alerts":
        bucket = alert_bucket(host)
        colour = dict((b, c) for b, _l, c in ALERT_BUCKETS)[bucket]
        n = host.get("alerts_firing") or 0
        return colour, f"{n} firing alert{'' if n == 1 else 's'}"
    value = host.get(key)
    g = hosts_svc.grade(key, value)
    text = f"{value:.1f}%" if finite(value) else "no data"
    return GRADE_COLOR[g], text


def colour_legend(key):
    """``[(label, colour)]`` for the status grid's legend, so the colours are always explained."""
    if key == "state":
        return [(STATE_LABEL[s], STATE_COLOR[s]) for s in STATES]
    if key == "alerts":
        return [(l, c) for _b, l, c in ALERT_BUCKETS]
    warn, crit = hosts_svc.thresholds().get(key, (75.0, 90.0))
    return [(f"< {warn:.0f}%", pal.GREEN), (f"{warn:.0f}–{crit:.0f}%", pal.AMBER),
            (f"≥ {crit:.0f}%", pal.CRIT), ("No data", pal.STONE_LIGHT)]


# ----------------------------------------------------------------------------- alerts
def alert_window(range_key):
    """``(range_key, label, seconds, start)`` for the alert panels. Unknown keys fall back to 24h."""
    key = range_key if range_key in RANGES else "24h"
    label, seconds = RANGES[key]
    return key, label, seconds, timezone.now() - timedelta(seconds=seconds)


def alerts_in_window(user, start, site=""):
    """Site-scoped alerts last seen at or after ``start``.

    An alert exactly on the boundary counts as inside the window (``last_received_at >= start``), so
    moving the selector from 6h to 24h can only ever add alerts, never drop one.
    """
    qs = Alert.objects.for_tenant().filter(last_received_at__gte=start)
    sites = visible_sites(user)
    if sites is not None:
        qs = qs.filter(site__in=list(sites) + ["All Sites", ""])
    if site:
        qs = qs.filter(site=site)
    return qs


def _alert_site(alert, host_sites):
    return (alert.site or host_sites.get(alert.instance, "") or "").strip()


def firing_alerts(user, site=""):
    """Every alert firing right now, site-scoped. The "active alerts" panel and the alert counter card both
    read from this one queryset, so the page can never show two different "firing" totals."""
    qs = Alert.objects.for_tenant().firing()
    sites = visible_sites(user)
    if sites is not None:
        qs = qs.filter(site__in=list(sites) + ["All Sites", ""])
    if site:
        qs = qs.filter(site=site)
    return qs


def firing_by_instance(alerts):
    """``{instance: {"firing": n, "critical": n, "warning": n}}`` for the given firing alerts."""
    out = {}
    for a in alerts:
        row = out.setdefault(a.instance, {"firing": 0, "critical": 0, "warning": 0})
        row["firing"] += 1
        tone = severity_tone(a.severity)
        if tone == "crit":
            row["critical"] += 1
        elif tone == "warn":
            row["warning"] += 1
    return out


def attach_alert_counts(rows, counts):
    for h in rows:
        c = counts.get(h["instance"]) or {}
        h["alerts_firing"] = c.get("firing", 0)
        h["alerts_critical"] = c.get("critical", 0)
        h["alerts_warning"] = c.get("warning", 0)
    return rows


def severity_split(alerts):
    """``{"critical": n, "warning": n, "other": n, "total": n}`` for a stacked severity bar."""
    out = {"critical": 0, "warning": 0, "other": 0, "total": 0}
    for a in alerts:
        out["total"] += 1
        tone = severity_tone(a.severity)
        out["critical" if tone == "crit" else "warning" if tone == "warn" else "other"] += 1
    return out


def _occurrences(a):
    """How many times the alert fired. The webhook counts firing transitions; a row that only ever arrived
    as "resolved" still happened once."""
    return max(int(a.fire_count or 0), 1)


def _window_seconds(a, start, now):
    """Seconds the alert was firing *inside* the window (so a 7-day-old alert does not dominate a 1h view)."""
    begin = a.starts_at or a.last_received_at
    end = a.ends_at if (a.status == Alert.RESOLVED and a.ends_at) else now
    if begin is None or end is None:
        return 0
    begin, end = max(begin, start), min(end, now)
    return max(0, int((end - begin).total_seconds()))


def host_alert_leaders(alerts, hosts, start, limit=10, now=None):
    """Hosts with the most alerts in the window: ``[{instance, name, url, critical, warning, other, total}]``.

    Sorted by total alerts, then criticals, then instance name, so ties are stable and reproducible.
    Alerts whose instance is not a known host still count – they are shown under their raw instance label.
    """
    now = now or timezone.now()
    by_host = {h["instance"]: h for h in hosts}
    rows = {}
    for a in alerts:
        inst = (a.instance or "").strip()
        if not inst:
            continue
        host = by_host.get(inst)
        r = rows.setdefault(inst, {"instance": inst, "name": (host or {}).get("name") or inst,
                                   "url": (host or {}).get("url") or "", "critical": 0, "warning": 0,
                                   "other": 0, "total": 0, "seconds": 0})
        tone = severity_tone(a.severity)
        r["critical" if tone == "crit" else "warning" if tone == "warn" else "other"] += _occurrences(a)
        r["total"] += _occurrences(a)
        r["seconds"] += _window_seconds(a, start, now)
    out = sorted(rows.values(), key=lambda r: (-r["total"], -r["critical"], r["instance"]))[:limit]
    worst = max((r["total"] for r in out), default=0)
    for r in out:
        r["pct"] = (100.0 * r["total"] / worst) if worst else 0
        r["crit_pct"] = (100.0 * r["critical"] / r["total"]) if r["total"] else 0
        r["warn_pct"] = (100.0 * r["warning"] / r["total"]) if r["total"] else 0
        r["other_pct"] = max(0.0, 100.0 - r["crit_pct"] - r["warn_pct"])
    return out


def top_alerts(alerts, start, limit=10, now=None):
    """Top alerts in the window: ``[{alertname, instance, occurrences, seconds, severity, tone}]``.

    One row per (alert name, instance) pair – the same grouping Netdata's "top alerts" table uses – sorted
    by occurrences, then total firing time, then name, so ties are stable.
    """
    now = now or timezone.now()
    rows = {}
    for a in alerts:
        key = (a.alertname, a.instance or "")
        r = rows.setdefault(key, {"alertname": a.alertname, "instance": a.instance or "", "occurrences": 0,
                                  "seconds": 0, "severity": "", "tone": "mute", "firing": 0})
        r["occurrences"] += _occurrences(a)
        r["seconds"] += _window_seconds(a, start, now)
        r["firing"] += 1 if a.status == Alert.FIRING else 0
        if _tone_rank(severity_tone(a.severity)) > _tone_rank(r["tone"]):
            r["severity"], r["tone"] = a.severity, severity_tone(a.severity)
    return sorted(rows.values(),
                  key=lambda r: (-r["occurrences"], -r["seconds"], r["alertname"], r["instance"]))[:limit]


_TONE_RANK = {"crit": 3, "warn": 2, "info": 1, "mute": 0}


def _tone_rank(tone):
    return _TONE_RANK.get(tone, 0)


# ----------------------------------------------------------------------------- roll-ups and counters
def site_rollup(hosts, alerts):
    """Per-site breakdown (the LinkedEye stand-in for Netdata's world map).

    ``[{site, hosts, live, down, stale, never_seen, alerts, avg_cpu, avg_memory}]``, busiest first.
    An alert with no site label is counted against the site of its host, when we know it.
    """
    host_sites = {h["instance"]: h.get("site") or "" for h in hosts}
    rows = {}
    for h in hosts:
        site = (h.get("site") or "").strip() or UNKNOWN
        r = rows.setdefault(site, {"site": site, "hosts": 0, "alerts": 0, "cpu": [], "memory": [],
                                   **{s: 0 for s in STATES}})
        r["hosts"] += 1
        r[h["state"]] += 1
        for field in ("cpu", "memory"):
            if finite(h.get(field)):
                r[field].append(h[field])
    for a in alerts:
        site = _alert_site(a, host_sites) or UNKNOWN
        r = rows.get(site)
        if r is not None:
            r["alerts"] += 1
    out = []
    for r in rows.values():
        cpu, mem = r.pop("cpu"), r.pop("memory")
        r["avg_cpu"] = (sum(cpu) / len(cpu)) if cpu else None
        r["avg_memory"] = (sum(mem) / len(mem)) if mem else None
        out.append(r)
    return sorted(out, key=lambda r: (-r["hosts"], r["site"].lower()))


def counters(hosts, targets, series, firing_total):
    """The counter cards: hosts, series available, scrape targets up/down, alerts firing, sites, orphans."""
    counts = state_counts(hosts)
    sites = {(h.get("site") or "").strip() for h in hosts if (h.get("site") or "").strip()}
    return {
        "hosts": len(hosts),
        "states": counts,
        "series": series,
        "targets": len(targets),
        "targets_up": sum(1 for t in targets if t.get("health") == "up"),
        "targets_down": sum(1 for t in targets if t.get("health") == "down"),
        "target_pools": len({t.get("pool") or t.get("job") or "" for t in targets}),
        "alerts_firing": firing_total,
        "sites": len(sites),
        "no_cmdb": sum(1 for h in hosts if not h.get("ci_name")),
    }


DARK_INK, LIGHT_INK = "#1c1a17", "#ffffff"
LABEL_MIN_PCT = 10          # narrower segments drop their in-bar label rather than truncate it


def _luminance(colour):
    """WCAG relative luminance of a #rrggbb colour, or None when it is not one."""
    try:
        channels = [int(colour[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
    except (ValueError, IndexError, TypeError):
        return None
    lin = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ink_on(colour):
    """Readable label colour for text drawn on ``colour`` (a #rrggbb literal from telemetry.palette).

    Whichever of the two inks has the better WCAG contrast ratio wins, so pale segments (stone, amber,
    teal) get dark text and dark ones (crit, down, blue) get white.
    """
    bg = _luminance(colour)
    if bg is None:  # pragma: no cover - palette colours are always #rrggbb
        return LIGHT_INK

    def ratio(ink):
        a, b = sorted((bg, _luminance(ink)), reverse=True)
        return (a + 0.05) / (b + 0.05)
    return DARK_INK if ratio(DARK_INK) > ratio(LIGHT_INK) else LIGHT_INK


def bar_segments(pairs, total=None):
    """``[{key, label, value, pct, colour, ink}]`` for a labelled stacked bar; zero segments are dropped.

    ``pairs`` is ``[(key, label, value, colour)]``. Percentages are rounded to 2 dp and the widths always
    add up to 100 for a non-empty bar, so the bar never shows a sliver of background.
    """
    total = sum(v for _k, _l, v, _c in pairs) if total is None else total
    out = []
    for key, label, value, colour in pairs:
        if not value:
            continue
        pct = round(100.0 * value / total, 2) if total else 0
        # a segment narrower than LABEL_MIN_PCT cannot hold its own label; the key row under the bar and the
        # segment's title attribute still name it, so nothing is lost
        out.append({"key": key, "label": label, "value": value, "colour": colour, "ink": ink_on(colour),
                    "pct": pct, "wide": pct >= LABEL_MIN_PCT})
    if out:
        drift = round(100.0 - sum(s["pct"] for s in out), 2)
        out[-1]["pct"] = round(out[-1]["pct"] + drift, 2)
    return out


def state_bar(hosts):
    counts = state_counts(hosts)
    return bar_segments([(s, STATE_LABEL[s], counts[s], STATE_COLOR[s]) for s in STATES])


def environment_bar(hosts):
    """The fleet split by environment – LinkedEye's equivalent of Netdata's "node kind" bar."""
    buckets = {}
    for h in hosts:
        env = h.get("environment") or ""
        buckets[env] = buckets.get(env, 0) + 1
    pairs = [(env, ENV_LABEL[env], buckets.get(env, 0), ENV_COLOR[env]) for env in ENV_LABEL]
    other = sum(v for k, v in buckets.items() if k not in ENV_LABEL)
    pairs.append(("", UNKNOWN, other, pal.STONE_LIGHT))
    return bar_segments(pairs)


def alert_bar(split):
    return bar_segments([("critical", "Critical", split["critical"], pal.CRIT),
                         ("warning", "Warning", split["warning"], pal.AMBER),
                         ("other", "Other", split["other"], pal.BLUE)], total=split["total"] or None)
