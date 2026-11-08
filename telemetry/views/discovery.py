"""Monitoring › Discovery: what Prometheus knows, and turning it into inventory.

Permissions (per site, like the other inventory pages):
  * view    – see the targets of the sites the user may see (core.access.scope_by_site);
  * operate – the row and bulk actions, and only on a target's own site: an operator for Mumbai cannot add a
    Chennai target, and a target on "All Sites" needs the role tenant-wide (core.access.can_on);
  * admin   – "Discover now", tenant-wide (core.access.tenant_wide), because one pass reads every target in
    the tenant and may create records on every site.
Every action is a POST with a CSRF token and writes an audit event.
"""
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core import prometheus
from core.access import can_on, is_global_site, require, scope_by_site, tenant_wide, visible_sites
from core.audit import audit

from .. import discovery as svc, palette as pal
from ..models import HOST_KINDS, DiscoveredTarget, TargetKind
from .common import csv_response, matches, paginate, perms, prom_context, query_without_page

STATE_CHOICES = DiscoveredTarget.STATES
HEALTH_CHOICES = DiscoveredTarget.HEALTHS
ACTIONS = ("add_to_cmdb", "create_device", "link", "ignore", "unignore", "set_site")
MAX_BULK = 200


def _scope(user):
    return scope_by_site(DiscoveredTarget.objects.for_tenant(), user, field="site").select_related(
        "ci", "device", "exporter")


def can_act_on(user, site):
    """May the user change inventory for this target's site? "All Sites" targets need the role everywhere."""
    return can_on(user, "operate", site)


def can_discover(user):
    """"Discover now" reads every target in the tenant and may create records on every site, so it needs the
    admin role tenant-wide: an admin for one site must not drive a pass that writes to the others."""
    return tenant_wide(user, "admin")


def hide_foreign_links(user, rows):
    """Blank (in memory only, never saved) the linked CMDB item / device / exporter the user may not see.

    Linking by name or address is not site-scoped (telemetry.discovery.link_row), so a target a Mumbai user may
    see can be linked to a Chennai record: its name, site and link must not reach that user."""
    allowed = visible_sites(user)
    if allowed is None:
        return rows
    for r in rows:
        for field in ("ci", "device", "exporter"):
            obj = getattr(r, field, None)
            site = (getattr(obj, "site", "") or "") if obj is not None else ""
            if obj is not None and not is_global_site(site) and site not in allowed:
                setattr(r, field, None)
    return rows


def _mark(user, rows):
    for r in rows:
        r.can_act = can_act_on(user, r.site)
    return rows


def _search_q(user, text):
    """The list filter as SQL, so the page does not load every target to search it. Linked record names are only
    searchable when the user may see that record (the same rule as hide_foreign_links)."""
    allowed = visible_sites(user)
    q = Q()
    for field in ("instance", "job", "site", "details__hostname", "details__os", "details__kernel",
                  "details__sysDescr", "details__sysName"):
        q |= Q(**{f"{field}__icontains": text})
    for rel in ("ci", "device", "exporter"):
        match = Q(**{f"{rel}__name__icontains": text})
        if allowed is not None:
            match &= Q(**{f"{rel}__site__in": list(allowed) + ["All Sites", ""]})
        q |= match
    return q


# ----------------------------------------------------------------------------- list page
@require("view")
def discovery(request):
    ctx = {**prom_context(), **perms(request), "error": ""}
    qs = _scope(request.user)
    kind, health, state = request.GET.get("kind", ""), request.GET.get("health", ""), request.GET.get("state", "")
    site, q = request.GET.get("site", ""), request.GET.get("q", "")
    show_missing = request.GET.get("missing") == "1"

    rows = qs if show_missing else qs.filter(missing_since__isnull=True)
    if kind:
        rows = rows.filter(kind=kind)
    if health:
        rows = rows.filter(health=health)
    if state:
        rows = rows.filter(state=state)
    if site:
        rows = rows.filter(site=site)
    if q.strip():
        rows = rows.filter(_search_q(request.user, q.strip()))
    rows = rows.defer("labels", "discovered_labels").order_by("kind", "instance")

    if request.GET.get("format") == "csv":
        return _csv(hide_foreign_links(request.user, list(rows)))

    present = qs.filter(missing_since__isnull=True)
    day_ago = timezone.now() - timedelta(days=1)
    kpis = {
        "targets": present.count(),
        "up": present.filter(health=DiscoveredTarget.UP).count(),
        "down": present.filter(health=DiscoveredTarget.DOWN).count(),
        "hosts": present.hosts().count(),
        "unlinked": present.unlinked_hosts().count(),
        "new_24h": present.filter(first_seen__gte=day_ago).count(),
        "missing": qs.filter(missing_since__isnull=False).count(),
    }
    kpis["unknown"] = max(0, kpis["targets"] - kpis["up"] - kpis["down"])
    page = paginate(request, rows, default=20)
    page.object_list = hide_foreign_links(request.user, list(page.object_list))   # only the 20 rows shown
    _mark(request.user, page.object_list)
    counts = {k: 0 for k, _ in TargetKind.choices}
    for row in present.order_by().values("kind").annotate(n=Count("id")):
        counts[row["kind"]] = counts.get(row["kind"], 0) + row["n"]
    ctx.update({
        "page": page, "kpis": kpis, "last_run": svc.last_run(), "site_label": svc.site_label(),
        "kinds": [(v, l, counts.get(v, 0)) for v, l in TargetKind.choices if counts.get(v)],
        "kind_choices": TargetKind.choices, "healths": HEALTH_CHOICES, "states": STATE_CHOICES,
        "sites": sorted(s for s in qs.order_by().values_list("site", flat=True).distinct() if s),
        "site_choices": _site_choices(request.user),
        "query": query_without_page(request), "show_missing": show_missing,
        # the "show/hide gone" toggle keeps every other filter the user set
        "gone_query": query_without_page(request, "missing"),
        "filtered": bool(kind or health or state or site or q),
        "can_discover": can_discover(request.user),
        "donut": {"segments": [
            {"label": "Up", "value": kpis["up"], "color": pal.OK},
            {"label": "Down", "value": kpis["down"], "color": pal.CRIT},
            {"label": "Unknown", "value": kpis["unknown"], "color": pal.STONE_LIGHT}],
            "center": {"value": str(kpis["targets"]), "label": "Targets"}},
    })
    return render(request, "telemetry/discovery.html", ctx)


def _csv(rows):
    return csv_response(
        "prometheus-discovery.csv",
        ["instance", "hostname", "kind", "job", "scrape_pool", "health", "state", "site", "os", "kernel", "arch",
         "cores", "memory_bytes", "exporter_version", "sys_descr", "linked_ci", "linked_device", "linked_exporter",
         "scrape_url", "last_error", "first_seen", "last_seen", "missing_since"],
        [[r.instance, r.hostname, r.get_kind_display(), r.job, r.scrape_pool, r.health, r.state, r.site,
          (r.details or {}).get("os", ""), (r.details or {}).get("kernel", ""), (r.details or {}).get("arch", ""),
          (r.details or {}).get("cores", ""), (r.details or {}).get("memory_bytes", ""),
          (r.details or {}).get("exporter_version", ""), (r.details or {}).get("sysDescr", ""),
          getattr(r.ci, "name", ""), getattr(r.device, "name", ""), getattr(r.exporter, "name", ""),
          r.scrape_url, r.last_error,
          r.first_seen.isoformat() if r.first_seen else "", r.last_seen.isoformat() if r.last_seen else "",
          r.missing_since.isoformat() if r.missing_since else ""] for r in rows])


def _site_choices(user):
    """Site names the user may move a target to ("All Sites" first).

    The registered sites, plus any site name already carried by a target the user can see — site names are free
    text across the product (a Prometheus `site` label need not be a registered site), and without those a
    target could not be moved back to its own site."""
    allowed = visible_sites(user)
    names = []
    try:
        Site = apps.get_model("core", "Site")
        qs = Site.objects.filter(tenant_key=settings.LINKEDEYE_TENANT) if hasattr(Site, "tenant_key") \
            else Site.objects.all()
        names += list(qs.order_by("name").values_list("name", flat=True))
    except LookupError:
        pass
    names += list(_scope(user).order_by("site").values_list("site", flat=True).distinct())
    out = ["All Sites"]
    for name in names:
        if name and name not in out and (allowed is None or name in allowed):
            out.append(name)
    return out[:1] + sorted(out[1:])


# ----------------------------------------------------------------------------- detail page
@require("view")
def discovery_detail(request, pk):
    row = get_object_or_404(_scope(request.user), pk=pk)
    hide_foreign_links(request.user, [row])
    can_act = can_act_on(request.user, row.site)
    d = row.details or {}
    return render(request, "telemetry/discovery_detail.html", {
        **prom_context(), **perms(request), "t": row, "d": d, "can_act": can_act,
        "site_choices": _site_choices(request.user),
        "cis": _link_options(request.user, row, "cmdb", "ConfigurationItem") if can_act else [],
        "devices": _link_options(request.user, row, "network", "Device") if can_act else [],
        "interfaces": (d.get("interfaces") or [])[:50], "filesystems": (d.get("filesystems") or [])[:50],
        "host_url": _host_url(row), "discovery_url": reverse("telemetry:discovery"),
    })


def _host_url(row):
    """Link to the host metrics page for Linux / Windows targets (that page reads node_exporter live)."""
    if row.kind in [k.value for k in HOST_KINDS] and row.instance:
        try:
            return reverse("telemetry:host_metrics", args=[row.instance])
        except Exception:
            return ""
    return ""


def _link_options(user, row, app_label, model_name):
    """Existing records on the target's site the operator may link it to (same site, or "All Sites").

    Linking writes the instance into the record itself, so only records the user may change are offered: a
    record that belongs to every site needs the operator role tenant-wide (core.access.can_on)."""
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError:
        return []
    try:
        qs = model.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
        qs = scope_by_site(qs, user, field="site")
        if row.site and row.site != "All Sites":
            qs = qs.filter(site__in=[row.site, "All Sites", ""])
        rows = list(qs.order_by("name").only("pk", "name", "site")[:500])
    except Exception:
        return []
    return [o for o in rows if can_on(user, "operate", getattr(o, "site", None))]


# ----------------------------------------------------------------------------- actions
def _back(request):
    """Where a POST returns to: the `next` field, but only a path on this host (never an open redirect)."""
    nxt = (request.POST.get("next") or "").strip()
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()},
                                               require_https=request.is_secure()):
        return nxt
    return reverse("telemetry:discovery")


def _deny(request, message):
    if request.headers.get("Accept", "").startswith("application/json"):
        return JsonResponse({"error": message}, status=403)
    messages.error(request, message)
    return redirect(_back(request))


def _selected(request):
    """The posted targets the user may see, by primary key.

    `isdigit()` alone is not enough: it is true for "²" and other non-ASCII digits that int() then refuses, and
    a 40-digit id is out of range for the column on PostgreSQL. Either would turn a crafted POST into a 500."""
    ids = [n for n in (_pk(i) for i in request.POST.getlist("target")) if n][:MAX_BULK]
    return list(_scope(request.user).filter(pk__in=ids))


def _pk(raw):
    """A posted id as a usable primary key, or 0."""
    raw = str(raw or "")
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 10:
        return 0
    n = int(raw)
    return n if 0 < n < 2 ** 31 else 0


@require("admin")
@require_POST
def discovery_run(request):
    """"Discover now": one pass, synchronously, so the operator sees the result straight away."""
    back = _back(request)
    if not can_discover(request.user):
        return _deny(request, "Discovery runs across every site, so it needs the admin role on All Sites.")
    if not prometheus.configured():
        messages.error(request, "Prometheus is not connected.")
        return redirect(back)
    run = svc.run(trigger="manual")
    audit(request, "discover", object_type="telemetry.discoveredtarget", result="ok" if run.ok else "failed",
          targets=run.targets, new=run.new, created=run.created, error=run.error)
    (messages.success if run.ok else messages.error)(
        request, f"Discovery: {run.summary}" if run.ok else f"Discovery failed: {run.error}")
    return redirect(back)


@require("operate")
@require_POST
def discovery_action(request):
    """Row and bulk actions. Every selected target is checked against the user's role on *its* site."""
    action = request.POST.get("action", "")
    back = _back(request)
    if action not in ACTIONS:
        messages.error(request, "Unknown action.")
        return redirect(back)
    rows = _selected(request)
    if not rows:
        messages.warning(request, "Select at least one target.")
        return redirect(back)
    refused = [r for r in rows if not can_act_on(request.user, r.site)]
    rows = [r for r in rows if can_act_on(request.user, r.site)]
    if not rows:
        return _deny(request, "You cannot change inventory for "
                              f"{', '.join(sorted({r.site or 'All Sites' for r in refused}))}.")
    handler = {"add_to_cmdb": _add_to_cmdb, "create_device": _create_device, "link": _link,
               "ignore": _ignore, "unignore": _unignore, "set_site": _set_site}[action]
    done, skipped = handler(request, rows)
    if refused:
        messages.warning(request, f"Skipped {len(refused)} target(s) on sites you cannot change.")
    if done:
        messages.success(request, done)
    if skipped:
        messages.warning(request, skipped)
    return redirect(back)


def _add_to_cmdb(request, rows):
    created, already, wrong = [], 0, 0
    host_kinds = [k.value for k in HOST_KINDS]
    for r in rows:
        if r.kind not in host_kinds:
            wrong += 1
            continue
        if r.ci_id is not None:
            already += 1
            continue
        if svc.link_row(r):                 # an item with this instance appeared since the last pass: link it
            r.state = DiscoveredTarget.LINKED if r.linked_to else r.state
            r.save(update_fields=["ci", "device", "exporter", "state", "updated_at"])
            if r.ci_id is not None:
                already += 1
                continue
        ci = svc.create_ci(r)
        if ci is None:
            continue
        r.ci = ci
        r.state = DiscoveredTarget.LINKED
        r.save(update_fields=["ci", "state", "updated_at"])
        audit(request, "create", ci, via="prometheus_discovery", instance=r.instance, job=r.job, site=r.site)
        created.append(ci.name)
    msg = f"Added {len(created)} host(s) to the CMDB: {', '.join(created[:6])}" if created else ""
    skip = " ".join(x for x in (f"{already} already linked." if already else "",
                                f"{wrong} target(s) are not hosts." if wrong else "") if x)
    return msg, skip


def _create_device(request, rows):
    created, already, wrong = [], 0, 0
    for r in rows:
        if r.kind != TargetKind.SNMP:
            wrong += 1
            continue
        if r.device_id is not None:
            already += 1
            continue
        dev = svc.create_device(r)
        if dev is None:
            continue
        r.device = dev
        r.state = DiscoveredTarget.LINKED
        r.save(update_fields=["device", "state", "updated_at"])
        audit(request, "create", dev, via="prometheus_discovery", instance=r.instance, job=r.job, site=r.site)
        created.append(dev.name)
    msg = f"Created {len(created)} device(s): {', '.join(created[:6])}" if created else ""
    skip = " ".join(x for x in (f"{already} already linked." if already else "",
                                f"{wrong} target(s) are not SNMP targets." if wrong else "") if x)
    return msg, skip


def _link(request, rows):
    """Link one target to an existing CMDB item or device (chosen on the detail page)."""
    if len(rows) != 1:
        return "", "Link one target at a time."
    row = rows[0]
    for field, app_label, model_name in (("ci", "cmdb", "ConfigurationItem"), ("device", "network", "Device")):
        raw = request.POST.get(field, "")
        if not raw.isdigit():
            continue
        options = {str(o.pk): o for o in _link_options(request.user, row, app_label, model_name)}
        obj = options.get(raw)
        if obj is None:
            return "", "That record is not one you can link on this target's site."
        if not can_on(request.user, "operate", getattr(obj, "site", None)):      # belt and braces
            return "", "That record is not one you can link on this target's site."
        setattr(row, field, obj)
        # keep the record pointing back at Prometheus, so the live pages find it too
        if not getattr(obj, "prometheus_instance", None):
            obj.prometheus_instance = row.instance[:200]
            obj.save(update_fields=["prometheus_instance"])
        row.state = DiscoveredTarget.LINKED
        row.save(update_fields=[field, "state", "updated_at"])
        audit(request, "update", row, linked_to=obj._meta.label_lower, name=obj.name, instance=row.instance)
        return f"Linked {row.instance} to {obj.name}.", ""
    return "", "Choose a record to link to."


def _ignore(request, rows):
    n = 0
    for r in rows:
        if r.state != DiscoveredTarget.IGNORED:
            r.state = DiscoveredTarget.IGNORED
            r.save(update_fields=["state", "updated_at"])
            audit(request, "ignore", r, instance=r.instance, job=r.job)
            n += 1
    return (f"Ignored {n} target(s). Discovery keeps them up to date but never creates records for them."
            if n else ""), ""


def _unignore(request, rows):
    n = 0
    for r in rows:
        if r.state == DiscoveredTarget.IGNORED:
            r.state = DiscoveredTarget.LINKED if r.linked_to else DiscoveredTarget.NEW
            r.save(update_fields=["state", "updated_at"])
            audit(request, "update", r, instance=r.instance, state=r.state)
            n += 1
    return f"{n} target(s) are back in the review list." if n else "", ""


def _set_site(request, rows):
    site = (request.POST.get("site") or "").strip()[:100]
    if site not in _site_choices(request.user):
        return "", "Choose one of your sites."
    if not can_on(request.user, "operate", site if site != "All Sites" else None):
        return "", f"You cannot manage targets on {site}."
    n = 0
    for r in rows:
        if r.site != site or not r.site_manual:
            r.site, r.site_manual = site, True
            r.save(update_fields=["site", "site_manual", "updated_at"])
            audit(request, "update", r, instance=r.instance, site=site)
            n += 1
    return f"Moved {n} target(s) to {site}. Discovery keeps the site you chose." if n else "", ""
