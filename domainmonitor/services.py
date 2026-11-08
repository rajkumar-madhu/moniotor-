"""Small domain-monitoring services shared by the views."""
from core.events import emit
from core.models import Event


def failure_open(fingerprint):
    """True when the latest event of this fingerprint is a problem (critical / warning) nobody resolved."""
    last = (Event.objects.filter(source="domainmonitor", fingerprint=fingerprint)
            .order_by("-occurred_at", "-id").values_list("severity", flat=True).first())
    return last in ("critical", "warning")


def close_failure(domain, reason, hostname=None):
    """The domain stopped being monitored (deactivated, monitoring switched off, renamed to another host) while its
    'is down' event was still open: emit the resolving event so the notification and the auto-incident keyed on
    `domain:<pk>` are closed (no probe result would ever do it). `hostname` names the host that was monitored when the
    object already carries a new one. Returns None when nothing was open."""
    if domain.pk is None or not failure_open(f"domain:{domain.pk}"):
        return None
    return emit("domainmonitor", f"{hostname or domain.hostname} is no longer monitored", severity="ok",
                message=f"{reason}; its earlier failure is closed.", site=domain.site_key, obj=domain,
                fingerprint=f"domain:{domain.pk}", reason=reason, status="unknown", previous=domain.status)
