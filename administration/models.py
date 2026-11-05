"""Administration models.

Users, roles (core.Membership), sites (core.Site) and the audit trail (core.AuditEvent) live in core; this app
only stores what the admin console itself needs:

* IntegrationTest – the result of every "Test" button press on the Integrations page, so admins can see when an
  integration last worked and who checked it.
* Heartbeat – liveness records written by the `administration.heartbeat` job inside `manage.py run_probes`, so the
  web process can tell whether the scheduler is alive and remember the probe-freshness watchdog state.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class IntegrationTest(models.Model):
    INTEGRATIONS = [
        ("prometheus", "Prometheus"),
        ("notify", "Notifications"),
        ("stackstorm", "StackStorm"),
        ("agent", "Ops Agent"),
    ]

    tenant_key = models.CharField(max_length=100, db_index=True, default=settings.LINKEDEYE_TENANT)
    integration = models.CharField(max_length=30, choices=INTEGRATIONS, db_index=True)
    ok = models.BooleanField(default=False)
    message = models.CharField(max_length=500, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    tested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="+")
    tested_by_name = models.CharField(max_length=150, blank=True)
    tested_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-tested_at"]

    def __str__(self):
        return f"{self.get_integration_display()} test {'passed' if self.ok else 'failed'}"


class Heartbeat(models.Model):
    """One row per key (e.g. "scheduler"); `seen_at` is refreshed on every job run."""

    key = models.CharField(max_length=60, unique=True)
    seen_at = models.DateTimeField(default=timezone.now)
    host = models.CharField(max_length=255, blank=True)
    pid = models.PositiveIntegerField(null=True, blank=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["key"]

    def __str__(self):
        return f"{self.key} @ {self.seen_at:%Y-%m-%d %H:%M:%S}"

    @property
    def age_seconds(self):
        return max(0, int((timezone.now() - self.seen_at).total_seconds()))


class LoginThrottle(models.Model):
    """Failed sign-in counter for one username or one client address, shared by every web worker (the default
    LocMemCache is per process, so a cache-only counter would multiply the limit by the number of workers).

    `failures` counts failures since `window_started_at`; once it reaches the limit the key is refused until
    `locked_until`. See administration.services.login_throttle_*."""
    KINDS = [("user", "Username"), ("ip", "Client address")]

    kind = models.CharField(max_length=4, choices=KINDS)
    value = models.CharField(max_length=191)
    failures = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)
    locked_until = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "value"], name="administration_loginthrottle_key_unique")]

    def __str__(self):
        return f"{self.kind}:{self.value} ({self.failures} failures)"
