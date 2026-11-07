"""BOD / EOD validation engine: trading calendar, reference lookups, runs, sign-offs and the scheduler job.

Market hours (IST): BOD 07:00–09:00, trading 09:00–15:30, EOD 15:30–16:00.
Other modules are reached lazily through `apps.get_model` so this app keeps working when one is absent.
"""
import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.apps import apps
from django.conf import settings
from django.db import DatabaseError, IntegrityError, transaction
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from core.access import can_on, is_global_site
from core.events import emit

from .models import GLOBAL_SITES, ChecklistItem, Holiday, Mode, Run, RunItem

log = logging.getLogger("linkedeye.bodeod")

IST = ZoneInfo("Asia/Kolkata")
TRADING_EXCHANGES = ("ALL", "NSE")      # holidays that close the market for this console
PHASES = [  # (key, label, start, end) in IST
    ("bod", "BOD window", time(7, 0), time(9, 0)),
    ("trading", "Trading hours", time(9, 0), time(15, 30)),
    ("eod", "EOD window", time(15, 30), time(16, 0)),
]
STALE_RUN_MINUTES = 30
LATE_START_GRACE_MINUTES = 60
STATUS_LABELS = dict(RunItem.STATUSES)
RUN_SEVERITY = {"fail": "critical", "warn": "warning", "pass": "ok"}
MAX_CSV_BYTES = 512 * 1024
MAX_CSV_ROWS = 2000


def tenant():
    return settings.LINKEDEYE_TENANT


def now_ist(now=None):
    return timezone.localtime(now or timezone.now(), IST)


def today_ist(now=None):
    return now_ist(now).date()


def modes_qs():
    return Mode.objects.filter(tenant_key=tenant())


def user_can(user, perm, site=""):
    """Permission on a mode/site: site records need that site's role; global modes ("All Sites" / "") need the
    role on every site, so a one-site operator cannot run or sign off the whole tenant's BOD/EOD."""
    return can_on(user, perm, site)


# ------------------------------------------------------------------ trading calendar
def holidays_on(day, exchanges=TRADING_EXCHANGES):
    return list(Holiday.objects.filter(tenant_key=tenant(), date=day, exchange__in=exchanges))


def trading_day(day):
    """(is_trading_day, reason). Weekends and ALL/NSE holidays close the market."""
    if day.weekday() >= 5:
        return False, f"Weekend ({day:%A})"
    hs = holidays_on(day)
    if hs:
        return False, "Holiday: " + "; ".join(f"{h.description} ({h.exchange})" for h in hs)
    return True, "Trading day"


def next_trading_day(day, limit=30):
    d = day + timedelta(days=1)
    closed = set(Holiday.objects.filter(tenant_key=tenant(), exchange__in=TRADING_EXCHANGES,
                                        date__gte=d, date__lte=d + timedelta(days=limit)).values_list("date", flat=True))
    for _ in range(limit):
        if d.weekday() < 5 and d not in closed:
            return d
        d += timedelta(days=1)
    return None


def market_phase(now=None):
    """Current market phase dict {key, label, start, end} (key 'closed' outside the windows)."""
    t = now_ist(now).time()
    for key, label, start, end in PHASES:
        if start <= t < end:
            return {"key": key, "label": label, "start": start, "end": end}
    return {"key": "closed", "label": "Market closed", "start": None, "end": None}


def window_state(mode, now=None, trading=None):
    """'open' | 'upcoming' | 'closed' for the mode's window today (IST); 'no_trading' on weekends and holidays."""
    n = now_ist(now)
    if trading is None:
        trading = trading_day(n.date())[0]
    if not trading:
        return "no_trading"
    t = n.time()
    if mode.window_start <= t < mode.window_end:
        return "open"
    if t < mode.window_start:
        return "upcoming"
    return "closed"


def trading_banner(now=None):
    n = now_ist(now)
    is_open, reason = trading_day(n.date())
    hs = holidays_on(n.date(), exchanges=("ALL", "NSE", "BSE", "MCX"))
    return {"now": n, "date": n.date(), "trading": is_open, "reason": reason, "phase": market_phase(now),
            "holidays": hs, "weekend": n.weekday() >= 5, "next_trading": None if is_open else next_trading_day(n.date())}


# ------------------------------------------------------------------ reference lookups
def _model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except (LookupError, ValueError):
        return None


# kind -> (app_label, model, name field, site field)
REF_MODELS = {"url_check": ("urlchecker", "UrlCheck", "name", "site"),
              "domain": ("domainmonitor", "Domain", "hostname", "site_key"),
              "tcp_check": ("tcpcheck", "TcpCheck", "name", "site")}


def ref_sites(site):
    """Sites whose checks a mode may reference: its own site only, or None (every site) for a global mode."""
    return None if is_global_site(site) else [site]


def _ref_url(kind, obj):
    try:
        if kind == "url_check":
            return reverse("urlchecker:detail", args=[obj.pk])
        if kind == "tcp_check":
            return reverse("tcpcheck:detail", args=[obj.pk])
        if kind == "domain":
            return reverse("domains:list") + f"?selected={obj.pk}"
    except NoReverseMatch:
        return ""
    return ""


def query_refs(M, field_name, names=None, sites=None, site_field=None):
    """{name: obj} for the tenant's records of model M (all of them when names is None, capped at 500).
    `sites` scopes the lookup to those sites plus the global ones ("All Sites" / ""), so a site's checklist
    never reads another site's check; None means every site. Returns None when the table is not available."""
    try:
        with transaction.atomic():  # savepoint: a missing table must not poison an outer transaction
            qs = M.objects.all()
            if names is not None:
                qs = qs.filter(**{f"{field_name}__in": list(names)})
            if sites is not None and site_field:
                qs = qs.filter(**{f"{site_field}__in": list(sites) + list(GLOBAL_SITES)})
            if any(f.name == "tenant_key" for f in M._meta.get_fields()):
                qs = qs.filter(tenant_key=tenant())
            return {getattr(o, field_name): o for o in qs.order_by(field_name)[:500]}
    except DatabaseError:
        log.warning("bodeod: %s is not available (missing table?)", M._meta.label)
        return None


class Resolver:
    """Looks up referenced checks by name in one query per kind (lazy, cached).

    `sites` is the list of sites whose checks may be resolved (None = every site); build it with ref_sites()
    from the mode's own site so a site mode cannot read another site's check.
    """

    def __init__(self, items=(), sites=None):
        self._cache = {}
        self._wanted = {}
        self._broken = set()
        self._sites = None if sites is None else list(sites)
        for it in items:
            if it.kind in REF_MODELS and it.ref_name:
                self._wanted.setdefault(it.kind, set()).add(it.ref_name.strip())

    def available(self, kind):
        if kind == "manual":
            return True
        if kind not in REF_MODELS or _model(*REF_MODELS[kind][:2]) is None:
            return False
        self._load(kind)
        return kind not in self._broken

    def _load(self, kind):
        if kind in self._cache:
            return self._cache[kind]
        app_label, model_name, field_name, site_field = REF_MODELS[kind]
        M = _model(app_label, model_name)
        found = {}
        if M is not None:
            names = self._wanted.get(kind, set())
            if names:
                found = query_refs(M, field_name, names, sites=self._sites, site_field=site_field)
                if found is None:  # table missing (module installed but not migrated)
                    self._broken.add(kind)
                    found = {}
        self._cache[kind] = found
        return found

    def get(self, kind, ref_name):
        if kind not in REF_MODELS or not ref_name:
            return None
        ref_name = ref_name.strip()
        if ref_name not in self._wanted.get(kind, set()):
            self._wanted.setdefault(kind, set()).add(ref_name)
            self._cache.pop(kind, None)
        return self._load(kind).get(ref_name)

    def link(self, kind, ref_name):
        obj = self.get(kind, ref_name)
        return _ref_url(kind, obj) if obj is not None else ""


def _when(dt):
    return timezone.localtime(dt, IST).strftime("%d %b %H:%M IST") if dt else "never"


def status_of(kind, obj):
    """Map a referenced check to (status, detail). obj=None means the reference is missing."""
    if kind == "domain":
        if obj is None:
            return "unknown", "Domain not found in Domain Monitor"
        if not obj.is_active:
            return "unknown", "Domain is inactive"
        if not obj.monitor_enabled:
            # monitoring is off, so the stored status/SSL state is frozen and proves nothing about today
            return "unknown", "Domain monitoring is paused"
        if obj.last_checked_at is None or obj.status == "unknown":
            return "unknown", "Domain has not been checked yet"
        when = _when(obj.last_checked_at)
        if obj.status == "down" or obj.dns_status == "Failed":
            return "critical", f"Down: {obj.last_error or 'not reachable'} (checked {when})"
        if obj.ssl_status in ("Expired", "Invalid"):
            return "critical", f"SSL certificate {obj.ssl_status.lower()} (checked {when})"
        if obj.ssl_status == "Expiring":
            days = obj.ssl_days_left
            return "warning", f"Up; SSL expiring{f' in {days} days' if days is not None else ''} (checked {when})"
        if obj.ssl_status == "Unreachable":
            return "warning", f"Up, but the SSL certificate could not be checked (checked {when})"
        return "ok", f"Up, DNS resolved, SSL {obj.ssl_status.lower()} (checked {when})"

    label = "URL check" if kind == "url_check" else "TCP check"
    if obj is None:
        return "unknown", f"{label} not found"
    if not obj.enabled:
        return "unknown", f"{label} is disabled"
    st = obj.last_status
    if obj.last_checked_at is None or st == "unknown":
        return "unknown", f"{label} has not run yet"
    when = _when(obj.last_checked_at)
    ms = f"{obj.last_response_ms:.0f} ms, " if obj.last_response_ms is not None else ""
    if st == "online":
        return "ok", f"Online ({ms}checked {when})"
    if st == "degraded":
        return "warning", f"Degraded: {obj.last_error or 'slow response'} ({ms}checked {when})"
    if st == "down":
        return "critical", f"Down: {obj.last_error or 'not reachable'} (checked {when})"
    return "unknown", f"Status {st} (checked {when})"


def live_status(item, resolver=None):
    """Current status of a checklist item from its referenced check's latest stored result (no network)."""
    if item.kind == "manual":
        return "pending", "Manual check: needs an operator sign-off"
    resolver = resolver or Resolver([item], sites=ref_sites(item.mode.site))
    if not resolver.available(item.kind):
        return "unknown", f"{item.get_kind_display()} module is not available"
    if not item.ref_name:
        return "unknown", "No reference configured"
    return status_of(item.kind, resolver.get(item.kind, item.ref_name))


@dataclass
class RefreshOutcome:
    """What a re-run of the referenced checks actually achieved."""

    ran: int = 0                                 # checks that were re-run
    failed: set = field(default_factory=set)     # kinds whose re-run could not be done

    @property
    def ok(self):
        """True only when something really was re-run and no kind failed."""
        return self.ran > 0 and not self.failed

    def _batch(self, kind, func, objs):
        try:
            func(objs)
            self.ran += len(objs)
        except Exception:
            log.exception("bodeod: re-running the %s references failed", kind)
            self.failed.add(kind)


def refresh_references(items, resolver):
    """Re-run every referenced check once, in parallel per kind. Errors are recorded, never raised.

    Returns a RefreshOutcome: the caller must not claim the checks were re-run unless `outcome.ok`.
    """
    batches = {"url_check": [], "domain": [], "tcp_check": []}
    for it in items:
        obj = resolver.get(it.kind, it.ref_name) if it.kind in batches else None
        if obj is None:
            continue
        active = obj.is_active and obj.monitor_enabled if it.kind == "domain" else obj.enabled
        if active and obj not in batches[it.kind]:
            batches[it.kind].append(obj)
    out = RefreshOutcome()
    if batches["url_check"] or batches["domain"]:
        try:
            from probes import runner
        except Exception:
            log.exception("bodeod: the probes module is not available; using stored URL/domain results")
            out.failed.update(k for k in ("url_check", "domain") if batches[k])
        else:
            if batches["url_check"]:
                out._batch("url_check", runner.run_url_checks, batches["url_check"])
            if batches["domain"]:
                out._batch("domain", runner.run_domain_checks, batches["domain"])
    if batches["tcp_check"]:
        try:
            from tcpcheck import services as tcp_services
        except ImportError:
            log.info("bodeod: tcpcheck module not available; using stored TCP results")
            out.failed.add("tcp_check")
        else:
            out._batch("tcp_check", tcp_services.run_checks, batches["tcp_check"])
    return out


# ------------------------------------------------------------------ run status
def compute_status(results):
    """fail if a critical item is critical; warn if anything is warning/unknown/pending (or a non-critical item is
    critical); otherwise pass. A run that validated nothing proves nothing, so it warns instead of passing."""
    results = list(results)
    if not results:
        return "warn"
    if any(r.status == "critical" and r.critical for r in results):
        return "fail"
    if any(r.status in ("warning", "unknown", "pending", "critical") for r in results):
        return "warn"
    return "pass"


def counts(results):
    c = {k: 0 for k in ("ok", "warning", "critical", "unknown", "pending", "signed_off")}
    for r in results:
        c[r.status] = c.get(r.status, 0) + 1
    c["total"] = sum(v for k, v in c.items() if k != "total")
    c["done"] = c["ok"] + c["signed_off"]
    return c


def summarize(results):
    c = counts(results)
    if not c["total"]:
        return "No enabled checklist items: nothing was validated"
    parts = [f"{c['done']}/{c['total']} OK"]
    for key, word in (("critical", "critical"), ("warning", "warning"), ("unknown", "unknown"), ("pending", "pending sign-off")):
        if c[key]:
            parts.append(f"{c[key]} {word}")
    return ", ".join(parts)


def _emit_run(run, previous=None):
    sev = RUN_SEVERITY.get(run.status)
    if not sev:
        return None
    label = dict(Run.STATUSES)[run.status]
    verb = {"pass": "passed", "warn": "completed with warnings", "fail": "failed"}[run.status]
    title = f"{run.mode.code} validation {verb} ({run.trading_date:%d %b %Y})"
    return emit("bodeod", title, severity=sev, message=run.summary, site=run.mode.site,
                obj=run, fingerprint=f"bodeod:mode:{run.mode_id}", mode=run.mode.code, run=run.pk,
                trading_date=run.trading_date.isoformat(), status=run.status, status_label=label,
                previous=previous or "", trigger=run.trigger)


def finalize(run, previous=None, emit_event=True):
    """Write the run's status and summary from its items.

    The read of the items, the computation and the write happen under a row lock on the run, and the items are
    re-read from the database rather than from the caller's (possibly stale or prefetched) copy: two sign-offs
    landing at the same moment, or a sign-off landing during a re-validation, must not leave a run 'warn' with
    every item signed off. `finished_at` is taken from the locked row, so a re-validation never rewrites it.
    """
    with transaction.atomic():
        locked = Run.objects.select_for_update().get(pk=run.pk)
        results = list(locked.results.all())
        for r in results:  # an automated item never left pending once the run finished
            if r.status == "pending" and r.automated:
                r.status, r.detail = "unknown", r.detail or "Not evaluated"
                r.save(update_fields=["status", "detail"])
        status = compute_status(results)
        summary = summarize(results)[:300]
        finished_at = locked.finished_at or timezone.now()
        Run.objects.filter(pk=run.pk).update(status=status, summary=summary, finished_at=finished_at)
    run.status, run.summary, run.finished_at = status, summary, finished_at
    if emit_event and run.status != previous:
        _emit_run(run, previous)
    return run


# ------------------------------------------------------------------ runs
class RunError(Exception):
    pass


def latest_run(mode):
    return mode.runs.order_by("-started_at", "-pk").first()


def is_open_for_signoff(run, now=None):
    """Sign-offs are accepted on the latest finished run of a mode for today's trading date."""
    if run.status == "running" or run.trading_date != today_ist(now):
        return False
    latest = latest_run(run.mode)
    return latest is not None and latest.pk == run.pk


def _carry_forward(mode, trading_date):
    """Manual sign-offs given earlier for the same mode and trading date, by checklist item id."""
    out = {}
    qs = RunItem.objects.filter(run__mode=mode, run__trading_date=trading_date, status="signed_off",
                                item__isnull=False).order_by("signed_off_at")
    for ri in qs:
        out[ri.item_id] = ri
    return out


def start_run(mode, user=None, trigger="manual", refresh=None, trading_date=None, now=None):
    """Create and evaluate a run synchronously. Returns the finished Run. Raises RunError on refusal."""
    now = now or timezone.now()
    trading_date = trading_date or today_ist(now)
    refresh = mode.refresh_checks if refresh is None else refresh
    busy = mode.runs.filter(status="running", started_at__gte=now - timedelta(minutes=STALE_RUN_MINUTES)).first()
    if busy:
        raise RunError(f"{mode.code} run #{busy.pk} is still running.")
    items = list(mode.items.filter(enabled=True).order_by("order", "pk"))
    name = ""
    if user is not None and getattr(user, "is_authenticated", False):
        name = user.get_username()
    with transaction.atomic():
        run = Run.objects.create(mode=mode, trading_date=trading_date, started_at=now, trigger=trigger,
                                 triggered_by=user if name else None, triggered_by_name=name or ("scheduler" if trigger == "scheduler" else ""),
                                 refreshed=False)  # only a re-run that really happened sets this (see evaluate)
        RunItem.objects.bulk_create([
            RunItem(run=run, item=it, order=it.order, name=it.name, kind=it.kind, ref_name=it.ref_name,
                    critical=it.critical, status="pending") for it in items])
    try:
        evaluate(run, refresh=refresh)
    except Exception:
        log.exception("bodeod: evaluating run %s failed", run.pk)
    return finalize(run)


RERUN_FAILED_NOTE = " [stored result; re-run failed]"


def _write_pending(r, fields):
    """Write `fields` to a still-pending manual row. Re-running the referenced checks takes seconds, so an
    operator may have signed the row off in between: the update is conditional on the row still being pending
    and, when it is not, the row read back from the database wins (the sign-off is never overwritten)."""
    if RunItem.objects.filter(pk=r.pk, status="pending").update(**fields):
        for name, value in fields.items():
            setattr(r, name, value)
    else:
        r.refresh_from_db()


def evaluate(run, refresh=False):
    """(Re-)evaluate the automated items of a run; manual items keep their sign-off."""
    results = list(run.results.select_related("item"))
    sites = ref_sites(run.mode.site)
    resolver = Resolver(results, sites=sites)
    automated = [r for r in results if r.automated]
    outcome = RefreshOutcome()
    if refresh and automated:
        outcome = refresh_references(automated, resolver)
        resolver = Resolver(results, sites=sites)  # reload fresh statuses
        if outcome.ok and not run.refreshed:
            Run.objects.filter(pk=run.pk).update(refreshed=True)
            run.refreshed = True
    carried = _carry_forward(run.mode, run.trading_date)
    now = timezone.now()
    for r in results:
        if r.automated:
            if not resolver.available(r.kind):
                r.status, r.detail = "unknown", f"{r.get_kind_display()} module is not available"
            elif not r.ref_name:
                r.status, r.detail = "unknown", "No reference configured"
            else:
                r.status, r.detail = status_of(r.kind, resolver.get(r.kind, r.ref_name))
            if r.kind in outcome.failed:  # say so instead of passing a frozen result off as fresh
                r.detail += RERUN_FAILED_NOTE
            r.detail = r.detail[:500]
            r.checked_at = now
            r.save(update_fields=["status", "detail", "checked_at"])
        elif r.status == "pending":
            prev = carried.get(r.item_id)
            if prev is not None and prev.run_id != run.pk:
                _write_pending(r, {"status": "signed_off", "signed_off_by_id": prev.signed_off_by_id,
                                   "signed_off_by_name": prev.signed_off_by_name, "signed_off_at": prev.signed_off_at,
                                   "note": prev.note, "checked_at": now,
                                   "detail": f"Signed off earlier today (run #{prev.run_id})"})
            else:
                _write_pending(r, {"detail": "Awaiting operator sign-off"})
    return results


def revalidate(run, refresh=True):
    """Re-check the automated items of a finished run (sign-offs are kept).

    The re-validation is recorded in `revalidated_at`; `finished_at` keeps the moment the run itself ended, so
    re-validating hours later does not inflate the run's duration or the history average.
    """
    if run.status == "running":
        raise RunError("The run is still running.")
    previous = run.status
    evaluate(run, refresh=refresh)
    at = timezone.now()
    Run.objects.filter(pk=run.pk).update(revalidated_at=at)
    run.revalidated_at = at
    return finalize(run, previous=previous)


def sign_off(result, user, note):
    """Operator sign-off of a pending manual item. Returns the run (status recomputed)."""
    note = (note or "").strip()
    if result.automated:
        raise RunError("Only manual items need a sign-off.")
    if not note:
        raise RunError("Add a note describing what you verified.")
    run = result.run
    if not is_open_for_signoff(run):
        raise RunError("Sign-offs are only accepted on the latest run for today's trading date.")
    with transaction.atomic():
        # the row lock on the run serialises concurrent sign-offs (and re-validations) of the same run, so the
        # "already signed off" check, the item write and the run's new status cannot interleave; the run's status
        # before this sign-off is read under the lock too, so the state-change event names the right transition
        previous = Run.objects.select_for_update().get(pk=run.pk).status
        fresh = RunItem.objects.get(pk=result.pk)
        if fresh.status == "signed_off":
            raise RunError("This item is already signed off.")
        at = timezone.now()
        fields = {"status": "signed_off", "signed_off_by": user, "signed_off_by_name": user.get_username(),
                  "signed_off_at": at, "note": note[:500], "detail": "Signed off", "checked_at": at}
        RunItem.objects.filter(pk=result.pk).update(**fields)
        for name, value in fields.items():
            setattr(result, name, value)
        finalize(run, previous=previous, emit_event=False)
    if run.status != previous:  # after the commit: listeners (automation, service desk) run outside the row lock
        _emit_run(run, previous)
    return run


def finalize_stale(now=None):
    """Runs left 'running' (e.g. the process died) are closed with what they have."""
    now = now or timezone.now()
    n = 0
    for run in Run.objects.filter(mode__tenant_key=tenant(), status="running",
                                  started_at__lt=now - timedelta(minutes=STALE_RUN_MINUTES)).select_related("mode"):
        finalize(run, previous="running")
        n += 1
    return n


# ------------------------------------------------------------------ scheduler
def due_modes(now=None):
    """Enabled modes whose auto-run time has come today and that have no scheduled run yet."""
    n = now_ist(now)
    today = n.date()
    out = []
    for mode in modes_qs().filter(enabled=True, auto_run_time__isnull=False):
        start = datetime.combine(today, mode.auto_run_time, tzinfo=IST)
        deadline = start + timedelta(minutes=LATE_START_GRACE_MINUTES)
        end = datetime.combine(today, mode.window_end, tzinfo=IST)
        if end > start:
            deadline = max(deadline, end)
        if not (start <= n < deadline):
            continue  # not yet, or the window passed while the scheduler was down: no late runs
        if mode.runs.filter(trading_date=today, trigger="scheduler").exists():
            continue
        if not mode.items.filter(enabled=True).exists():
            log.info("bodeod: %s has no enabled checklist items; not starting a scheduled run", mode.code)
            continue
        out.append(mode)
    return out


def auto_run(now=None):
    """Job (every 60 s): start each mode's run once at its auto-run time on trading days. Never raises."""
    try:
        now = now or timezone.now()
        stale = finalize_stale(now)
        is_open, reason = trading_day(today_ist(now))
        if not is_open:
            return f"closed stale run(s): {stale}" if stale else None
        started = []
        for mode in due_modes(now):
            try:
                run = start_run(mode, trigger="scheduler", now=now)
                started.append(f"{mode.code} {run.status}")
            except IntegrityError:
                continue  # another worker started it
            except RunError as e:
                log.info("bodeod: %s", e)
            except Exception:
                log.exception("bodeod: scheduled run of %s failed", mode.code)
        if started:
            return "started " + ", ".join(started)
        return None
    except Exception:
        log.exception("bodeod.auto_run failed")
        return "error"


def schedule_for(mode, now=None):
    """Next automatic start (IST datetime) for a mode, or None."""
    if not mode.enabled or not mode.auto_run_time:
        return None
    n = now_ist(now)
    day = n.date()
    for _ in range(40):
        is_open, _reason = trading_day(day)
        at = datetime.combine(day, mode.auto_run_time, tzinfo=IST)
        if is_open and (day > n.date() or (at >= n and not mode.runs.filter(trading_date=day, trigger="scheduler").exists())):
            return at
        day += timedelta(days=1)
    return None


# ------------------------------------------------------------------ holidays CSV
DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y", "%d.%m.%Y")
EXCHANGE_CODES = {code for code, _ in Holiday.EXCHANGES}


def parse_date(value):
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class CsvImport:
    rows: list = field(default_factory=list)      # [(exchange, date, description)]
    errors: list = field(default_factory=list)    # ["line 3: ..."]


def _clean(value):
    """A parsed cell: NUL bytes removed (a PostgreSQL text column refuses them) and blanks stripped."""
    return (value or "").replace("\x00", "").strip()


def _csv_error(out, reader, exc):
    """Record a malformed-CSV error (an over-long field, broken quoting…) and keep whatever parsed so far.
    csv.Error messages describe the problem, never the field's content, so nothing is leaked here."""
    out.errors.append(f"line {reader.line_num}: the file is not valid CSV ({exc})")


def parse_holidays_csv(data, default_exchange="NSE"):
    """Parse an uploaded holiday CSV (bytes or str). Columns: date, description (or holiday/name), optional exchange."""
    out = CsvImport()
    if isinstance(data, bytes):
        if len(data) > MAX_CSV_BYTES:
            out.errors.append(f"File is larger than {MAX_CSV_BYTES // 1024} KB.")
            return out
        try:
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            out.errors.append("File is not UTF-8 text.")
            return out
    reader = csv.reader(io.StringIO(data))
    header = None
    try:
        for row in reader:
            if any(c.strip() for c in row):
                header = [_clean(c).lower() for c in row]
                break
    except csv.Error as e:  # e.g. a field longer than csv's field-size limit: an error, not a 500
        _csv_error(out, reader, e)
        return out
    if not header:
        out.errors.append("The file is empty.")
        return out

    def col(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return None

    i_date = col("date", "holiday date")
    i_desc = col("description", "holiday", "name", "holidays", "occasion")
    i_exch = col("exchange", "segment_exchange", "market")
    if i_date is None or i_desc is None:
        out.errors.append("Header must contain 'date' and 'description' columns (optional 'exchange').")
        return out
    seen = set()
    processed = 0
    try:
        for line_no, row in enumerate(reader, start=2):
            if not any(c.strip() for c in row):
                continue
            processed += 1
            if len(out.rows) >= MAX_CSV_ROWS or processed > MAX_CSV_ROWS * 2:
                out.errors.append(f"Stopped at line {line_no}: at most {MAX_CSV_ROWS} holidays (and {MAX_CSV_ROWS * 2} rows) per file.")
                break
            get = lambda i: _clean(row[i]) if i is not None and i < len(row) else ""  # noqa: E731
            d = parse_date(get(i_date))
            desc = get(i_desc)
            exch = (get(i_exch) or default_exchange).upper()
            if d is None:
                out.errors.append(f"line {line_no}: invalid date “{get(i_date)}” (use YYYY-MM-DD or DD-Mon-YYYY)")
                continue
            if not desc:
                out.errors.append(f"line {line_no}: description is empty")
                continue
            if exch not in EXCHANGE_CODES:
                out.errors.append(f"line {line_no}: unknown exchange “{exch}” (use ALL, NSE, BSE or MCX)")
                continue
            if (exch, d) in seen:
                continue
            seen.add((exch, d))
            out.rows.append((exch, d, desc[:200]))
    except csv.Error as e:  # the rows parsed before the break stay importable
        _csv_error(out, reader, e)
    return out


def import_holidays(rows, source="csv"):
    """Upsert parsed rows. Returns (created, updated)."""
    created = updated = 0
    with transaction.atomic():
        for exch, d, desc in rows:
            obj, was_created = Holiday.objects.update_or_create(
                tenant_key=tenant(), exchange=exch, date=d, defaults={"description": desc, "source": source})
            created += was_created
            updated += not was_created
    return created, updated


def year_bounds(year):
    return date(year, 1, 1), date(year, 12, 31)


def checklist_items(mode=None):
    qs = ChecklistItem.objects.filter(mode__tenant_key=tenant()).select_related("mode")
    if mode is not None:
        qs = qs.filter(mode=mode)
    return qs.order_by("mode__window_start", "mode__code", "order", "pk")
