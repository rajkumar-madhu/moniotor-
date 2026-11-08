"""DCIM services: access helpers, capacity maths, Prometheus readings and the alert evaluation.

Public helpers other modules may use (import lazily):
    dcim.services.racks_for(user) / rooms_for(user)     site-scoped querysets
    dcim.services.rack_summaries(racks)                  racks annotated with u_used, load_kw, power_pct …
"""
import logging
import math
import time

from django.conf import settings
from django.db.models import Prefetch
from django.utils import timezone

from core import prometheus
from core.access import PERMS, RANK, can, scope_by_site
from core.events import emit

from .models import GLOBAL_SITE, CoolingStatus, CoolingUnit, PowerFeed, Rack, RackItem, RackStatus, Room

log = logging.getLogger("linkedeye.dcim")

HOT_PCT = 80.0             # racks above this share of space or power are flagged on the overview
POWER_ALERT_PCT = 90.0     # the readings job warns above this share of a rack's power budget
MAX_READS = 400            # Prometheus queries per job pass (bounded; the rest wait for the next pass)
MAX_SECONDS = 240          # wall-clock budget of one pass (the job runs every 300 s)
MAX_KW = 100000.0
TEMP_RANGE = (-50.0, 150.0)


def tenant():
    return settings.LINKEDEYE_TENANT


# ------------------------------------------------------------------ access
def rooms_for(user):
    return scope_by_site(Room.objects.filter(tenant_key=tenant()), user)


def racks_for(user):
    return scope_by_site(Rack.objects.filter(tenant_key=tenant()), user).select_related("room")


def items_for(user):
    return scope_by_site(RackItem.objects.filter(tenant_key=tenant()), user, field="rack__site")


def feeds_for(user):
    return scope_by_site(PowerFeed.objects.filter(tenant_key=tenant()), user, field="rack__site")


def cooling_for(user):
    return scope_by_site(CoolingUnit.objects.filter(tenant_key=tenant()), user).select_related("room")


def is_global(site):
    return not site or site == GLOBAL_SITE


def can_manage(user, perm, site):
    """`perm` on a record's site. Global ("All Sites") records need the permission tenant-wide."""
    if not is_global(site):
        return can(user, perm, site_name=site)
    if not can(user, perm):
        return False
    if user.is_superuser:
        return True
    from core.access import _memberships
    ms = _memberships(user)
    if not ms:  # users without memberships hold their default role everywhere
        return True
    return any(m.site is None and RANK[m.role] >= PERMS[perm] for m in ms)


def site_names():
    from django.apps import apps
    Site = apps.get_model("core", "Site")
    return list(Site.objects.filter(tenant_key=tenant()).order_by("name").values_list("name", flat=True))


def manageable_sites(user, perm="operate"):
    """Site names (plus "All Sites") the user may create or move records to."""
    names = [GLOBAL_SITE] + site_names()
    return [n for n in names if can_manage(user, perm, n)]


def visible_site_names(user):
    from core.access import visible_sites
    vis = visible_sites(user)
    names = site_names()
    return names if vis is None else [n for n in names if n in vis]


def sync_room_site(room):
    """Copy the room's site onto its racks and cooling units (after a room moved to another site)."""
    Rack.objects.filter(room=room).update(site=room.site)
    CoolingUnit.objects.filter(room=room).update(site=room.site)


# ------------------------------------------------------------------ capacity maths
def _occupied(rack, items):
    units = set()
    for it in items:
        top = min(it.u_top, rack.u_height)
        units.update(range(max(1, it.u_position), top + 1))
    return units


def summarize_rack(rack, items=None, feeds=None):
    """Annotate one rack in place (items/feeds may be passed to avoid queries) and return it."""
    items = list(rack.items.all()) if items is None else items
    feeds = list(rack.feeds.all()) if feeds is None else feeds
    rack.item_list, rack.feed_list = items, feeds
    rack.u_used = len(_occupied(rack, items))
    rack.u_free = rack.u_height - rack.u_used
    rack.space_pct = 100.0 * rack.u_used / rack.u_height if rack.u_height else 0.0
    measured = [f.load_kw for f in feeds if f.load_kw is not None]
    drawn = [it.power_draw_w for it in items if it.power_draw_w]
    if measured:
        rack.load_kw = sum(measured)
        rack.load_source = "live" if any(f.source == "live" for f in feeds) else "manual"
    elif drawn:
        rack.load_kw = sum(drawn) / 1000.0
        rack.load_source = "estimated"
    else:
        rack.load_kw, rack.load_source = None, ""
    rack.estimated_kw = sum(drawn) / 1000.0 if drawn else None
    rack.feed_capacity_kw = sum(f.capacity_kw for f in feeds) if feeds else None
    rack.power_pct = (100.0 * rack.load_kw / rack.max_power_kw
                      if rack.load_kw is not None and rack.max_power_kw else None)
    rack.cooling_pct = (100.0 * rack.load_kw / rack.max_cooling_kw
                        if rack.load_kw is not None and rack.max_cooling_kw else None)
    rack.worst_pct = max(rack.space_pct, rack.power_pct or 0.0)
    rack.is_hot = rack.space_pct >= HOT_PCT or (rack.power_pct or 0.0) >= HOT_PCT
    return rack


def rack_summaries(racks):
    """Racks (a queryset) with items and feeds prefetched and annotated by summarize_rack."""
    qs = racks.prefetch_related(Prefetch("items", queryset=RackItem.objects.order_by()),
                                Prefetch("feeds", queryset=PowerFeed.objects.order_by("name")))
    out = []
    for r in qs:
        out.append(summarize_rack(r, list(r.items.all()), list(r.feeds.all())))
    return out


def counts_toward_capacity(rack):
    return rack.status != RackStatus.DECOMMISSIONED


def summarize_room(room, racks, units):
    """Room totals from its (already summarized) racks and its cooling units."""
    live = [r for r in racks if counts_toward_capacity(r)]
    room.rack_list = racks
    room.unit_list = units
    room.u_total = sum(r.u_height for r in live)
    room.u_used = sum(r.u_used for r in live)
    room.space_pct = 100.0 * room.u_used / room.u_total if room.u_total else None
    loads = [r.load_kw for r in live if r.load_kw is not None]
    room.load_kw = sum(loads) if loads else None
    room.estimated = any(r.load_source == "estimated" for r in live)
    budgets = [r.max_power_kw for r in live if r.max_power_kw]
    room.power_budget_kw = sum(budgets) if budgets else None
    room.power_pct = (100.0 * room.load_kw / room.power_budget_kw
                      if room.load_kw is not None and room.power_budget_kw else None)
    feed_caps = [r.feed_capacity_kw for r in live if r.feed_capacity_kw]
    room.feed_capacity_kw = sum(feed_caps) if feed_caps else None
    active = [u for u in units if u.status == CoolingStatus.ACTIVE]
    room.cooling_capacity_kw = sum(u.capacity_kw for u in active) if active else None
    room.standby_kw = sum(u.capacity_kw for u in units if u.status == CoolingStatus.STANDBY)
    room.headroom_kw = (room.cooling_capacity_kw - (room.load_kw or 0.0)
                        if room.cooling_capacity_kw is not None else None)
    room.cooling_pct = (100.0 * (room.load_kw or 0.0) / room.cooling_capacity_kw
                        if room.cooling_capacity_kw else None)
    return room


def room_summaries(rooms, racks=None, units=None):
    """Rooms (queryset) annotated with totals; racks/units default to every rack/unit of those rooms."""
    rooms = list(rooms)
    ids = [r.pk for r in rooms]
    racks = rack_summaries(Rack.objects.filter(room_id__in=ids)) if racks is None else racks
    units = list(CoolingUnit.objects.filter(room_id__in=ids)) if units is None else units
    by_room, u_by_room = {}, {}
    for r in racks:
        by_room.setdefault(r.room_id, []).append(r)
    for u in units:
        u_by_room.setdefault(u.room_id, []).append(u)
    for room in rooms:
        summarize_room(room, by_room.get(room.pk, []), u_by_room.get(room.pk, []))
    return rooms


def totals(rooms):
    """Overview KPIs over summarized rooms."""
    racks = [r for room in rooms for r in room.rack_list]
    live = [r for r in racks if counts_toward_capacity(r)]
    loads = [r.load_kw for r in live if r.load_kw is not None]
    budgets = [r.max_power_kw for r in live if r.max_power_kw]
    caps = [room.cooling_capacity_kw for room in rooms if room.cooling_capacity_kw is not None]
    cooled_load = [room.load_kw or 0.0 for room in rooms if room.cooling_capacity_kw is not None]
    u_total = sum(r.u_height for r in live)
    u_used = sum(r.u_used for r in live)
    load = sum(loads) if loads else None
    budget = sum(budgets) if budgets else None
    return {
        "rooms": len(rooms), "racks": len(racks), "active_racks": len(live),
        "u_total": u_total, "u_used": u_used, "u_free": u_total - u_used, "u_pct": 100.0 * u_used / u_total if u_total else None,
        "load_kw": load, "budget_kw": budget,
        "power_pct": 100.0 * load / budget if load is not None and budget else None,
        "estimated": any(r.load_source == "estimated" for r in live),
        "cooling_kw": sum(caps) if caps else None,
        "headroom_kw": sum(caps) - sum(cooled_load) if caps else None,
        "negative_rooms": sum(1 for room in rooms if room.headroom_kw is not None and room.headroom_kw < 0),
        "hot": [r for r in live if r.is_hot],
        "items": sum(len(r.item_list) for r in racks),
    }


# ------------------------------------------------------------------ Prometheus readings
_SHAPE_ERRORS = (ValueError, TypeError, AttributeError, KeyError, IndexError)
BAD_SHAPE = ("Prometheus returned a result this query type cannot be read (for example a scalar). "
             "Wrap scalar() in vector(), e.g. vector(scalar(sum(pdu_w))/1000).")
_OUTAGE_PREFIXES = ("Prometheus unreachable", "Prometheus returned HTTP 5", "Prometheus is not connected")


def is_outage(error):
    """True for a failure that will hit every other query too (unreachable / timeout / 5xx / not an API
    answer), as opposed to one bad query. After one the pass stops asking Prometheus."""
    text = str(error)
    return text.startswith(_OUTAGE_PREFIXES) or "not a valid API response" in text


def _query(promql):
    """core.prometheus.query with every malformed shape (scalar / string results, short samples) turned
    into PrometheusError, so one odd query can never raise out of a pass or a form save."""
    try:
        rows = prometheus.query(promql)
    except prometheus.PrometheusError:
        raise
    except _SHAPE_ERRORS:
        raise prometheus.PrometheusError(BAD_SHAPE) from None
    if not isinstance(rows, list):
        raise prometheus.PrometheusError(BAD_SHAPE)
    return [r for r in rows if isinstance(r, dict)]


def _read(promql, lo=0.0, hi=MAX_KW, mean=False):
    """Sum (or, for temperatures, the mean) of an instant query's values. Raises PrometheusError with a
    user-safe message."""
    res = _query(promql)
    vals = [r["value"] for r in res if isinstance(r.get("value"), (int, float)) and math.isfinite(r["value"])]
    if not vals:
        raise prometheus.PrometheusError("Query returned no data")
    total = sum(vals) / len(vals) if mean else sum(vals)
    if not lo <= total <= hi:
        raise prometheus.PrometheusError(f"Query returned {total:g}, outside the expected range {lo:g}…{hi:g}")
    return total


def _store_feed(feed, value, error, now):
    feed.last_value, feed.last_error, feed.last_read_at = value, str(error)[:300], now   # no value = never show a stale one
    PowerFeed.objects.filter(pk=feed.pk).update(last_value=value, last_error=feed.last_error, last_read_at=now)
    return value is not None


def read_feed(feed, now=None, failure=None):
    """Read one feed's prometheus_query and store the result. Returns True on success. `failure` is an outage
    already seen in this pass: the feed is marked failed with that error without asking Prometheus again."""
    now = now or timezone.now()
    if failure:
        return _store_feed(feed, None, failure, now)
    feed.outage = ""
    try:
        return _store_feed(feed, _read(feed.prometheus_query), "", now)
    except prometheus.PrometheusError as e:
        feed.outage = str(e) if is_outage(e) else ""
        return _store_feed(feed, None, e, now)


def _unit_error(label_error):
    return "; ".join(f"{label}: {err}" for label, err in label_error)[:300]


def read_unit(unit, now=None, failure=None):
    """Read a cooling unit's supply / return temperatures (the mean when a query matches several sensors)."""
    now = now or timezone.now()
    errors = []
    unit.outage = ""
    for attr, label, query in (("last_supply_c", "Supply", unit.supply_query), ("last_return_c", "Return", unit.return_query)):
        if not query:
            setattr(unit, attr, None)
            continue
        setattr(unit, attr, None)
        if failure:
            errors.append((label, failure))
            continue
        try:
            setattr(unit, attr, _read(query, *TEMP_RANGE, mean=True))
        except prometheus.PrometheusError as e:
            errors.append((label, e))
            if is_outage(e):
                failure = unit.outage = str(e)      # do not wait for the Return query to time out as well
    unit.last_error = _unit_error(errors)
    unit.last_read_at = now
    CoolingUnit.objects.filter(pk=unit.pk).update(last_supply_c=unit.last_supply_c, last_return_c=unit.last_return_c,
                                                  last_error=unit.last_error, last_read_at=now)
    return not errors


def read_all(feeds=None, units=None, limit=MAX_READS):
    """Read Prometheus values for feeds/cooling units that have queries. Returns (ok, failed) counts.
    Raises nothing; when Prometheus is not configured nothing is read. One item can fail without stopping the
    pass; after an outage (unreachable / timeout) the remaining items are marked failed with that same error
    instead of waiting for one timeout each, and the pass stops after MAX_SECONDS (the rest waits for the next)."""
    if not prometheus.configured():
        return 0, 0
    feeds = PowerFeed.objects.filter(tenant_key=tenant()) if feeds is None else feeds
    units = CoolingUnit.objects.filter(tenant_key=tenant()) if units is None else units
    now = timezone.now()
    started = time.monotonic()
    ok = failed = 0
    outage = ""
    budget = limit
    work = [(f, read_feed) for f in feeds.exclude(prometheus_query="").order_by("last_read_at", "pk")[:budget]]
    budget -= len(work)
    if budget > 0:
        work += [(u, read_unit) for u in units.exclude(supply_query="", return_query="").order_by("last_read_at", "pk")[:budget]]
    for n, (obj, reader) in enumerate(work):
        if n and time.monotonic() - started > MAX_SECONDS:
            break
        try:
            good = reader(obj, now, failure=outage) if outage else reader(obj, now)
        except Exception as e:  # noqa: BLE001 - one broken item must not stall the others
            log.exception("dcim reading failed for %r", obj)
            good = False
            _mark_failed(obj, f"Unexpected error: {e}", now)
        if good:
            ok += 1
        else:
            failed += 1
            outage = outage or getattr(obj, "outage", "")
    return ok, failed


def _mark_failed(obj, error, now):
    if isinstance(obj, PowerFeed):
        _store_feed(obj, None, error, now)
    else:
        obj.last_supply_c = obj.last_return_c = None
        obj.last_error, obj.last_read_at = str(error)[:300], now
        CoolingUnit.objects.filter(pk=obj.pk).update(last_supply_c=None, last_return_c=None,
                                                     last_error=obj.last_error, last_read_at=now)


# ------------------------------------------------------------------ alerts (state change only)
def evaluate_alerts(rooms=None):
    """Emit a warning when a rack crosses 90 % of its power budget or a room's cooling headroom goes negative,
    and an "ok" event when it recovers. Only state changes emit. Returns the number of events emitted."""
    rooms = Room.objects.filter(tenant_key=tenant()) if rooms is None else rooms
    emitted = 0
    for room in room_summaries(rooms):
        for rack in room.rack_list:
            high = (rack.max_power_kw and rack.load_kw is not None
                    and rack.load_kw >= rack.max_power_kw * POWER_ALERT_PCT / 100.0
                    and counts_toward_capacity(rack))
            new = "high" if high else ""
            if new == rack.power_state:
                continue
            Rack.objects.filter(pk=rack.pk).update(power_state=new)
            fp = f"dcim:rack:{rack.pk}:power"
            if new:
                emit(source="dcim", severity="warning", site=rack.site, obj=rack, fingerprint=fp,
                     title=f"Rack {rack.name} at {rack.power_pct:.0f}% of its power budget",
                     message=(f"{rack.name} in {room.name} draws {rack.load_kw:.2f} kW of its {rack.max_power_kw:g} kW "
                              f"budget ({rack.load_source} load)."),
                     load_kw=round(rack.load_kw, 3), budget_kw=rack.max_power_kw, source_kind=rack.load_source)
                emitted += 1
            elif rack.power_state == "high":
                emit(source="dcim", severity="ok", site=rack.site, obj=rack, fingerprint=fp,
                     title=f"Rack {rack.name} back below {POWER_ALERT_PCT:.0f}% of its power budget",
                     message=(f"{rack.name} in {room.name} now draws "
                              + (f"{rack.load_kw:.2f} kW." if rack.load_kw is not None else "no measured load.")))
                emitted += 1
        negative = room.headroom_kw is not None and room.headroom_kw < 0
        new = "negative" if negative else ""
        if new == room.cooling_state:
            continue
        Room.objects.filter(pk=room.pk).update(cooling_state=new)
        fp = f"dcim:room:{room.pk}:cooling"
        if new:
            emit(source="dcim", severity="warning", site=room.site, obj=room, fingerprint=fp,
                 title=f"Room {room.name}: IT load exceeds cooling capacity",
                 message=(f"IT load {room.load_kw or 0:.2f} kW vs {room.cooling_capacity_kw:g} kW of active cooling "
                          f"(headroom {room.headroom_kw:.2f} kW)."),
                 load_kw=round(room.load_kw or 0.0, 3), cooling_kw=room.cooling_capacity_kw)
            emitted += 1
        elif room.cooling_state == "negative":
            emit(source="dcim", severity="ok", site=room.site, obj=room, fingerprint=fp,
                 title=f"Room {room.name}: cooling headroom restored",
                 message=("Cooling headroom is "
                          + (f"{room.headroom_kw:.2f} kW." if room.headroom_kw is not None else "no longer measured.")))
            emitted += 1
    return emitted


def run_readings():
    """Job dcim.readings: read Prometheus values (when configured) and evaluate alerts. Never raises."""
    try:
        ok, failed = read_all()
    except Exception:  # noqa: BLE001 - a job must not raise
        log.exception("dcim readings failed")
        ok, failed = 0, 0
    try:
        events = evaluate_alerts()
    except Exception:  # noqa: BLE001
        log.exception("dcim alert evaluation failed")
        events = 0
    if not prometheus.configured():
        return f"prometheus not configured; {events} event(s)"
    return f"{ok} reading(s), {failed} failed, {events} event(s)"
