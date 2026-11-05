"""Periodic jobs (registered in AgentConfig.ready, run by `manage.py run_probes`)."""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import Thread
from . import knowledge

log = logging.getLogger("linkedeye.agent")


def reindex_knowledge():
    try:
        return knowledge.reindex()
    except Exception:
        log.exception("agent knowledge reindex failed")
        return "error"


def prune_threads():
    try:
        days = int(getattr(settings, "AGENT_RETENTION_DAYS", 90) or 90)
    except (TypeError, ValueError):
        days = 90
    days = max(1, min(730, days))
    try:
        cutoff = timezone.now() - timedelta(days=days)
        n, _detail = Thread.objects.filter(
            tenant_key=settings.LINKEDEYE_TENANT, updated_at__lt=cutoff,
        ).delete()
        return f"pruned {n} stale agent row(s)"
    except Exception:
        log.exception("agent thread retention failed")
        return "error"
