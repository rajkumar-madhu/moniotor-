"""Outbound notifications for critical/warning events: generic JSON webhooks
(Slack/Teams/StackStorm/Apprise-compatible endpoints) and e-mail.

Delivery never runs on the emitting thread (the probe loop, a job, a web request) unless NOTIFY_ASYNC is off:

* the listener schedules the delivery with transaction.on_commit, so an event whose transaction rolls back never
  notifies anyone;
* a small bounded pool (NOTIFY_WORKERS threads, at most NOTIFY_QUEUE_SIZE pending deliveries) sends it. When the
  queue is full the notification is dropped with a warning and counted in stats()["dropped"];
* digest: once more than NOTIFY_DIGEST_THRESHOLD events of one severity arrive within NOTIFY_DIGEST_WINDOW seconds,
  the rest of that window is collapsed into one summary notification sent when the window ends.

NOTIFY_ASYNC=false (tests) delivers synchronously on the caller's thread; the digest then flushes lazily on the next
event or when flush() is called (run_probes --once calls it before exiting). The digest is per process.
"""
import json
import logging
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.core.mail import send_mail
from django.db import connections, transaction
from django.dispatch import receiver

from .events import event_emitted

log = logging.getLogger("linkedeye.notify")

DIGEST_SAMPLE = 20          # titles listed in a digest notification

_clock = time.monotonic     # patched by tests
_lock = threading.Lock()
_idle = threading.Condition(_lock)
_state = {"executor": None, "inflight": 0, "windows": {}, "timers": set(), "last_drop_log": 0.0}
STATS = {"sent": 0, "failed": 0, "dropped": 0, "digested": 0, "digests": 0}


def _reset_after_fork():
    """A forked child (gunicorn worker) must not inherit the parent's pool, lock owner or counters."""
    global _lock, _idle
    _lock = threading.Lock()
    _idle = threading.Condition(_lock)
    _state.update(executor=None, inflight=0, windows={}, timers=set(), last_drop_log=0.0)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_after_fork)


def _post(url, payload, timeout=5):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - URLs come from operator settings
        return r.status


def _send(payload, subject, body):
    """Send one notification to every configured channel. Returns how many channels accepted it. Never raises."""
    sent = 0
    for url in settings.NOTIFY_WEBHOOK_URLS:
        try:
            _post(url, payload)
            sent += 1
        except Exception as e:  # never let a notification failure break the caller
            log.warning("webhook %s failed: %s", url.split("?")[0], e)
    if settings.NOTIFY_EMAILS:
        try:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, settings.NOTIFY_EMAILS, fail_silently=False)
            sent += 1
        except Exception as e:
            log.warning("email notification failed: %s", e)
    return sent


def deliver(event):
    """Send one event now, on this thread (also used by Administration's "Send test notification")."""
    if event.severity not in settings.NOTIFY_SEVERITIES:
        return 0
    payload = {"title": event.title, "body": event.message, "severity": event.severity, "source": event.source,
               "site": event.site, "occurred_at": event.occurred_at.isoformat(), "kind": event.kind,
               "object": f"{event.object_type}:{event.object_id}"}
    return _send(payload, f"[LinkedEye {event.severity.upper()}] {event.title}", event.message or event.title)


def deliver_digest(digest):
    """Send the summary of a collapsed window: {"severity", "count", "titles", "window"}."""
    n, sev = digest["count"], digest["severity"]
    title = f"{n} more {sev} event{'s' if n != 1 else ''} in the last {digest['window']} s"
    lines = list(digest["titles"])
    if n > len(lines):
        lines.append(f"… and {n - len(lines)} more")
    body = "Notifications were collapsed into this summary to avoid a flood:\n" + "\n".join(f"- {t}" for t in lines)
    payload = {"title": title, "body": body, "severity": sev, "source": "linkedeye", "site": "",
               "occurred_at": "", "kind": "digest", "object": "", "count": n, "titles": digest["titles"]}
    return _send(payload, f"[LinkedEye {sev.upper()}] {title}", body)


# ------------------------------------------------------------------ digest
def _expired(now):
    """Pop windows that ended; returns digests for the ones that collapsed anything. Caller holds _lock."""
    window = settings.NOTIFY_DIGEST_WINDOW
    out = []
    for sev, w in list(_state["windows"].items()):
        if now - w["start"] >= window:
            del _state["windows"][sev]
            if w["suppressed"]:
                out.append({"severity": sev, "count": w["suppressed"], "titles": w["titles"], "window": window})
    return out


def _admit(event):
    """(deliver this event individually?, digests now due)."""
    threshold = settings.NOTIFY_DIGEST_THRESHOLD
    now = _clock()
    start_timer = None
    with _lock:
        due = _expired(now)
        if threshold <= 0:
            return True, due
        w = _state["windows"].setdefault(event.severity, {"start": now, "count": 0, "suppressed": 0, "titles": []})
        w["count"] += 1
        if w["count"] <= threshold:
            return True, due
        w["suppressed"] += 1
        STATS["digested"] += 1   # _lock is held
        if len(w["titles"]) < DIGEST_SAMPLE:
            w["titles"].append(event.title)
        if w["suppressed"] == 1:
            start_timer = max(0.0, w["start"] + settings.NOTIFY_DIGEST_WINDOW - now)
    if start_timer is not None and settings.NOTIFY_ASYNC:
        t = threading.Timer(start_timer + 0.05, _timer_flush)
        t.daemon = True
        with _lock:
            _state["timers"].add(t)
        t.start()
    return False, due


def _timer_flush():
    with _lock:
        _state["timers"].discard(threading.current_thread())
    for d in _flush_due():
        _submit_digest(d)


def _flush_due(force=False):
    with _lock:
        if not force:
            return _expired(_clock())
        window = settings.NOTIFY_DIGEST_WINDOW
        out = [{"severity": sev, "count": w["suppressed"], "titles": w["titles"], "window": window}
               for sev, w in _state["windows"].items() if w["suppressed"]]
        _state["windows"].clear()
        return out


# ------------------------------------------------------------------ delivery pool
def _count(key):
    with _lock:
        STATS[key] += 1


def _run(func, arg, pooled=False):
    try:
        _count("sent" if func(arg) else "failed")
    except Exception:
        _count("failed")
        log.exception("notification delivery failed")
    finally:
        if pooled:
            connections.close_all()   # pool threads: never leave a connection open if a backend touched the DB
            with _lock:
                _state["inflight"] -= 1
                if _state["inflight"] <= 0:
                    _idle.notify_all()


def _submit(func, arg):
    if not settings.NOTIFY_ASYNC:
        _run(func, arg)
        return True
    with _lock:
        if _state["inflight"] >= max(1, settings.NOTIFY_QUEUE_SIZE):
            STATS["dropped"] += 1
            now = _clock()
            noisy = now - _state["last_drop_log"] >= 60
            if noisy:
                _state["last_drop_log"] = now
        else:
            noisy = None
            _state["inflight"] += 1
            if _state["executor"] is None:
                _state["executor"] = ThreadPoolExecutor(max_workers=max(1, settings.NOTIFY_WORKERS),
                                                        thread_name_prefix="le-notify")
            executor = _state["executor"]
    if noisy is not None:
        if noisy:
            log.warning("notification queue full (%s pending); dropping notifications (%s dropped so far)",
                        settings.NOTIFY_QUEUE_SIZE, STATS["dropped"])
        return False
    try:
        executor.submit(_run, func, arg, True)
    except RuntimeError:  # pool shut down (process exiting): deliver inline rather than lose it
        with _lock:
            _state["inflight"] -= 1
        _run(func, arg)
    return True


def _submit_digest(digest):
    _count("digests")
    return _submit(deliver_digest, digest)


def dispatch(event):
    """Digest bookkeeping + hand the event to the pool (or deliver it now when NOTIFY_ASYNC is off). Never raises."""
    try:
        individual, digests = _admit(event)
        for d in digests:
            _submit_digest(d)
        if individual:
            _submit(deliver, event)
    except Exception:
        log.exception("could not dispatch notification for %s", getattr(event, "pk", None))


def flush(timeout=30.0):
    """Send pending digests now and wait (up to `timeout` s) for queued deliveries. Returns how many are still pending."""
    for d in _flush_due(force=True):
        _submit_digest(d)
    deadline = _clock() + timeout
    with _lock:
        for t in list(_state["timers"]):
            t.cancel()
        _state["timers"].clear()
        while _state["inflight"] > 0:
            left = deadline - _clock()
            if left <= 0:
                break
            _idle.wait(left)
        return max(0, _state["inflight"])


def shutdown(timeout=10.0):
    """flush(), then stop the pool without waiting for deliveries still queued."""
    remaining = flush(timeout)
    with _lock:
        executor, _state["executor"] = _state["executor"], None
    if executor is not None:
        executor.shutdown(wait=False, cancel_futures=True)
    if remaining:
        log.warning("%s notification(s) still pending at shutdown were abandoned", remaining)
    return remaining


def stats():
    with _lock:
        return dict(STATS, pending=max(0, _state["inflight"]))


def reset():
    """Forget digest windows and counters (tests)."""
    with _lock:
        for t in _state["timers"]:
            t.cancel()
        _state.update(windows={}, timers=set(), last_drop_log=0.0)
        for k in STATS:
            STATS[k] = 0


@receiver(event_emitted, dispatch_uid="core.notify.on_event")
def _on_event(sender, event, **kwargs):
    if not (settings.NOTIFY_WEBHOOK_URLS or settings.NOTIFY_EMAILS) or event.severity not in settings.NOTIFY_SEVERITIES:
        return
    # after commit: a rolled-back event (e.g. a probe result transaction that failed) never notifies anyone
    transaction.on_commit(lambda: dispatch(event))
