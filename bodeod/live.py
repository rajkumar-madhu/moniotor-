"""Live view of the automated BOD / EOD checklist items.

Everything the page shows live comes from the referenced Domain / URL check / TCP check *as last stored*: reading it
(`evaluate`, `mode_payload`) never runs a probe, so a GET is cheap and cannot be used to make the server connect
anywhere. Fresh data comes from the referenced checks' own schedule (the worker) or from an explicit, operator-only
re-check (`recheck`), which goes through `services.refresh_references` and so through probes.checks (SSRF policy).

Vocabulary: items are `ok | warning | failed | unknown` here (the stored RunItem "critical" is "failed" in the live
view). A live failure only ever changes what the page shows; it never signs anything off and never edits a run.
"""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from django.core.cache import cache
from django.db import connections
from django.utils import timezone

from . import services
from .models import ChecklistItem, RunItem

log = logging.getLogger("linkedeye.bodeod")

INTERVAL = 20                 # seconds between polls the page is told to use (the script clamps it to 15-30)
RECHECK_WAIT = 6.0            # seconds a re-check request waits for the probes before answering "still running"
RECHECK_LOCK_TTL = 90         # a re-check that never reports back frees its mode after this many seconds
LIVE_LIMIT, LIVE_WINDOW = 120, 60        # polls per user per minute (a tab polls 3-4 times a minute)
RECHECK_LIMIT, RECHECK_WINDOW = 12, 60   # re-check requests per user per minute

STATE = {"ok": "ok", "warning": "warning", "critical": "failed", "unknown": "unknown"}
# live state -> (status code, tone, short label); the same pills the rest of the app uses
STATE_META = {"ok": (0, "ok", "OK"), "warning": (1, "warn", "WARN"), "failed": (2, "crit", "CRIT"), "unknown": (3, "mute", "UNKNOWN")}
STATE_WORD = {"ok": "OK", "warning": "warning", "failed": "failed", "unknown": "unknown"}
KIND_LABEL = dict(ChecklistItem.KINDS)
AUTOMATED = ("url_check", "tcp_check", "domain")


# ------------------------------------------------------------------ wording
def human_age(seconds):
    """'12 s', '5 min', '3 h', '2 d' (the page shows it as 'checked 12 s ago')."""
    if seconds is None:
        return ""
    s = max(0, int(seconds))
    if s < 90:
        return f"{s} s"
    if s < 5400:
        return f"{round(s / 60)} min"
    if s < 172800:
        return f"{round(s / 3600)} h"
    return f"{round(s / 86400)} d"


def _days(days):
    return f"{days} day" + ("" if days == 1 else "s")


def short_detail(kind, obj, status, long_detail):
    """One short sentence for a row ("DNS resolves; certificate valid, 41 days left"). The long wording of
    `services.status_of` (with the absolute check time) stays the tooltip; the age is shown separately."""
    if obj is None or status == "unknown":
        return long_detail
    if kind == "domain":
        days = obj.ssl_days_left
        if obj.status == "down" or obj.dns_status == "Failed":
            if obj.dns_status == "Failed":
                return f"DNS failed: {obj.last_error}" if obj.last_error else "DNS does not resolve"
            return f"Down: {obj.last_error or 'not reachable'}"
        if obj.ssl_status in ("Expired", "Invalid"):
            return f"DNS resolves; certificate {obj.ssl_status.lower()}"
        if obj.ssl_status == "Expiring":
            return "DNS resolves; certificate expiring" + (f" in {_days(days)}" if days is not None else "")
        if obj.ssl_status == "Unreachable":
            return "DNS resolves; the certificate could not be checked"
        return f"DNS resolves; certificate {obj.ssl_status.lower()}" + (f", {_days(days)} left" if days is not None and days >= 0 else "")
    ms = f" ({obj.last_response_ms:.0f} ms)" if getattr(obj, "last_response_ms", None) is not None else ""
    st = obj.last_status
    if st == "online":
        return f"Online{ms}"
    if st == "degraded":
        return f"Degraded: {obj.last_error or 'slow response'}{ms}"
    if st == "down":
        return f"Down: {obj.last_error or 'not reachable'}"
    return long_detail


# ------------------------------------------------------------------ evaluation (no network)
def evaluate(kind, ref_name, resolver, now=None):
    """(status, long_detail, short_detail, checked_at, age_s) of one automated reference from stored data."""
    now = now or timezone.now()
    label = KIND_LABEL.get(kind, kind)
    if not resolver.available(kind):
        text = f"{label} module is not available"
        return "unknown", text, text, None, None
    if not ref_name:
        return "unknown", "No reference configured", "No reference configured", None, None
    obj = resolver.get(kind, ref_name)
    status, long = services.status_of(kind, obj)
    checked = getattr(obj, "last_checked_at", None) if obj is not None else None
    age = max(0, int((now - checked).total_seconds())) if checked else None
    return status, long, short_detail(kind, obj, status, long), checked, age


def entry(key, item_id, name, kind, ref_name, critical, resolver, now, link_of=None, recorded=None):
    """The JSON object for one automated item."""
    status, long, short, checked, age = evaluate(kind, ref_name, resolver, now)
    state = STATE[status]
    code, tone, label = STATE_META[state]
    out = {
        "key": key, "id": item_id, "name": name, "kind": kind, "kind_label": KIND_LABEL.get(kind, kind),
        "ref": ref_name, "critical": bool(critical), "status": state, "tone": tone, "code": code, "label": label,
        "detail": short, "detail_long": long,
        "link": resolver.link(kind, ref_name) if (ref_name and resolver.available(kind)) else "",
        "checked_at": checked.isoformat() if checked else None,
        "checked_at_ist": timezone.localtime(checked, services.IST).strftime("%d %b %Y %H:%M:%S IST") if checked else "",
        "age_s": age, "age": human_age(age),
    }
    if recorded is not None:
        out["recorded"] = STATE.get(recorded, recorded)
    return out


def summarize_entries(entries):
    """{status, counts} for a mode's live automated items: failed when a critical item failed, warning when anything is
    warning (or a non-critical item failed), unknown when something is unknown (or nothing is automated), else ok."""
    counts = {"ok": 0, "warning": 0, "failed": 0, "unknown": 0}
    for e in entries:
        counts[e["status"]] += 1
    counts["total"] = len(entries)
    if not entries:
        status = "unknown"
    elif any(e["status"] == "failed" and e["critical"] for e in entries):
        status = "failed"
    elif counts["warning"] or counts["failed"]:
        status = "warning"
    elif counts["unknown"]:
        status = "unknown"
    else:
        status = "ok"
    return status, counts


def summary(entries):
    """summarize_entries() as the dict the templates and the JSON share."""
    status, counts = summarize_entries(entries)
    return {"status": status, "tone": STATE_META[status][1], "label": STATE_META[status][2], "counts": counts}


def run_info(run):
    if run is None:
        return None
    from django.urls import reverse
    return {"id": run.pk, "status": run.status, "status_label": run.get_status_display(), "summary": run.summary,
            "trading_date": run.trading_date.isoformat(), "url": reverse("bodeod:detail", args=[run.pk]),
            "started_at": run.started_at.isoformat()}


def mode_payload(mode, run=None, now=None, entries=None):
    """The live JSON for one mode.

    The mode's `run` is always its latest run's identity (the page uses it to tell the viewer that a newer run exists).
    Without `run`, the items are the mode's enabled automated checklist items (keys `i<id>`). With `run`, the items are that
    run's automated rows (keys `r<id>`) and each carries the status the run recorded."""
    now = now or timezone.now()
    if entries is None:
        entries = []
        if run is not None:
            rows = [r for r in run.results.all() if r.automated]
            resolver = services.Resolver(rows, sites=services.ref_sites(mode.site))
            for r in rows:
                entries.append(entry(f"r{r.pk}", r.item_id, r.name, r.kind, r.ref_name, r.critical, resolver, now, recorded=r.status))
        else:
            items = list(mode.items.filter(enabled=True, kind__in=AUTOMATED).order_by("order", "pk"))
            resolver = services.Resolver(items, sites=services.ref_sites(mode.site))
            for it in items:
                entries.append(entry(f"i{it.pk}", it.pk, it.name, it.kind, it.ref_name, it.critical, resolver, now))
    latest = services.latest_run(mode)
    return {"code": mode.code, "id": mode.pk, "name": mode.name, "enabled": mode.enabled, "site": mode.site,
            **summary(entries), "run": run_info(latest), "rechecking": is_rechecking(mode), "items": entries}


# ------------------------------------------------------------------ rate limiting and the re-check lock
def hit(key, window):
    """Fixed-window counter; a broken cache never blocks the page (returns 0)."""
    try:
        if cache.add(key, 1, window):
            return 1
        try:
            return cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)
            return 1
    except Exception:  # noqa: BLE001
        log.warning("bodeod: rate limiting unavailable", exc_info=True)
        return 0


def limited(user, scope, limit, window):
    return hit(f"bodeod:rl:{scope}:{user.pk}", window) > limit


def _lock_key(mode):
    return f"bodeod:recheck:{mode.pk}"


def is_rechecking(mode):
    try:
        return cache.get(_lock_key(mode)) is not None
    except Exception:  # noqa: BLE001
        return False


def _acquire(mode):
    try:
        return cache.add(_lock_key(mode), int(timezone.now().timestamp()), RECHECK_LOCK_TTL)
    except Exception:  # noqa: BLE001
        return True


def _release(mode):
    try:
        cache.delete(_lock_key(mode))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ re-check
class _DbThreadExecutor(ThreadPoolExecutor):
    """Worker threads own their database connections: close them when a job ends."""

    def submit(self, fn, /, *args, **kwargs):
        def run():
            try:
                return fn(*args, **kwargs)
            finally:
                connections.close_all()
        return super().submit(run)


_executor = None
_executor_lock = threading.Lock()


def get_executor():
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = _DbThreadExecutor(max_workers=2, thread_name_prefix="bodeod-recheck")
        return _executor


def _job(mode, refs):
    """Runs on a worker thread: re-run the referenced checks once. Never raises; always frees the mode's lock."""
    try:
        resolver = services.Resolver(refs, sites=services.ref_sites(mode.site))
        outcome = services.refresh_references(refs, resolver)
        return {"ran": outcome.ran, "failed": sorted(outcome.failed)}
    except Exception:  # noqa: BLE001
        log.exception("bodeod: re-check of %s failed", mode.code)
        return {"ran": 0, "failed": ["error"]}
    finally:
        _release(mode)


def pick_refs(mode, keys=None, run=None):
    """The automated rows to re-check: the named ones (`i<id>` checklist items / `r<id>` rows of `run`), else every enabled
    automated item of the mode (or of `run`). Manual rows are never refreshed."""
    if run is not None:
        qs = RunItem.objects.filter(run=run, kind__in=AUTOMATED)
        if keys:
            ids = [int(k[1:]) for k in keys if k[:1] == "r" and k[1:].isdigit()]
            qs = qs.filter(pk__in=ids)
        return list(qs.order_by("order", "pk"))
    qs = ChecklistItem.objects.filter(mode=mode, enabled=True, kind__in=AUTOMATED)
    if keys:
        ids = [int(k[1:]) for k in keys if k[:1] == "i" and k[1:].isdigit()]
        qs = qs.filter(pk__in=ids)
    return list(qs.order_by("order", "pk"))


def recheck(mode, refs, wait=None):
    """Start the re-check and wait up to `wait` seconds for it. Returns (state, info):

    "done" (everything re-run), "partial" (some kinds could not be re-run), "failed" (nothing could be re-run because
    the probes failed), "skipped" (nothing eligible: disabled or missing references), "running" (still going: the probes
    carry on in the background and the page keeps polling), "busy" (another re-check of this mode is already running)."""
    wait = RECHECK_WAIT if wait is None else wait
    if not refs:
        return "skipped", {"ran": 0, "failed": []}
    if not _acquire(mode):
        return "busy", {"ran": 0, "failed": []}
    try:
        fut = get_executor().submit(_job, mode, refs)
    except Exception:  # noqa: BLE001
        _release(mode)
        log.exception("bodeod: could not start the re-check of %s", mode.code)
        return "failed", {"ran": 0, "failed": ["error"]}
    try:
        info = fut.result(timeout=wait)
    except FutureTimeout:
        return "running", {"ran": 0, "failed": []}
    except Exception:  # noqa: BLE001
        return "failed", {"ran": 0, "failed": ["error"]}
    if info["failed"] and not info["ran"]:
        return "failed", info
    if info["failed"]:
        return "partial", info
    if not info["ran"]:
        return "skipped", info
    return "done", info

