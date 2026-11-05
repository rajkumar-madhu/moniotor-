"""Render e-mail bodies for core.notify (webhooks keep the JSON payload in notify.py)."""
from django.template.loader import render_to_string
from django.utils import timezone


def _occurred_at(event):
    when = getattr(event, "occurred_at", None)
    if not when:
        return ""
    try:
        return timezone.localtime(when).strftime("%d %b %Y, %H:%M %Z")
    except (TypeError, ValueError, AttributeError):
        return str(when)


def event_context(event):
    severity = getattr(event, "severity", "") or "info"
    return {
        "title": getattr(event, "title", "") or "LinkedEye notification",
        "message": getattr(event, "message", "") or "",
        "severity": severity,
        "severity_label": severity.capitalize(),
        "source": getattr(event, "source", "") or "linkedeye",
        "site": getattr(event, "site", "") or "",
        "kind": getattr(event, "kind", "") or "",
        "object_type": getattr(event, "object_type", "") or "",
        "object_id": getattr(event, "object_id", "") or "",
        "occurred_at": _occurred_at(event),
    }


def digest_context(digest):
    n = int(digest.get("count") or 0)
    sev = digest.get("severity") or "warning"
    titles = list(digest.get("titles") or [])
    window = int(digest.get("window") or 0)
    title = f"{n} more {sev} event{'s' if n != 1 else ''} in the last {window} s"
    lines = list(titles)
    if n > len(lines):
        lines.append(f"… and {n - len(lines)} more")
    body = "Notifications were collapsed into this summary to avoid a flood:\n" + "\n".join(f"- {t}" for t in lines)
    return {
        "title": title,
        "body": body,
        "lines": lines,
        "severity": sev,
        "severity_label": sev.capitalize(),
        "count": n,
        "window": window,
    }


def render_event_email(event):
    ctx = event_context(event)
    return (
        render_to_string("notify/event_email.txt", ctx),
        render_to_string("notify/event_email.html", ctx),
    )


def render_digest_email(digest):
    ctx = digest_context(digest)
    return (
        render_to_string("notify/digest_email.txt", ctx),
        render_to_string("notify/digest_email.html", ctx),
    )
