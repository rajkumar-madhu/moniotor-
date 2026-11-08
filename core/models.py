"""Platform-wide models: sites and role memberships, the audit trail and the
unified event stream that every module writes to."""
from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils import timezone


class Client(models.Model):
    """Customer / tenant account for client-wise roll-ups (sites, service desk, monitoring)."""
    STATUSES = [("active", "Active"), ("pilot", "Pilot"), ("on_hold", "On hold"), ("churned", "Churned")]
    MODULES = [
        ("servicedesk", "Service Desk"),
        ("urlchecker", "URL Checker"),
        ("domains", "Domains"),
        ("network", "Network devices"),
        ("telemetry", "Monitoring"),
        ("cmdb", "CMDB & assets"),
        ("tcpcheck", "TCP / uptime"),
        ("bodeod", "BOD / EOD"),
        ("automation", "Automation"),
        ("observe", "Logs & exporters"),
        ("reports", "Reports"),
        ("agent", "Ops Agent"),
    ]

    tenant_key = models.CharField(max_length=100, db_index=True, default=settings.LINKEDEYE_TENANT)
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=60)
    status = models.CharField(max_length=10, choices=STATUSES, default="active", db_index=True)
    contact_name = models.CharField(max_length=120, blank=True)
    contact_email = models.EmailField(blank=True)
    go_live = models.DateField(null=True, blank=True)
    modules = models.JSONField(default=list, blank=True, help_text="Enabled LinkedEye modules for this client.")
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="client_tenant_name_unique"),
                       models.UniqueConstraint(fields=["tenant_key", "code"], name="client_tenant_code_unique")]

    def __str__(self):
        return self.name


class Site(models.Model):
    """A customer site / location (e.g. Mumbai (NSE), DR Site). Other modules
    reference sites by name for compatibility with the original LinkedEye data."""
    CRITICALITY = [("critical", "Critical"), ("high", "High"), ("standard", "Standard"), ("low", "Low")]

    tenant_key = models.CharField(max_length=100, db_index=True, default=settings.LINKEDEYE_TENANT)
    client = models.ForeignKey(Client, on_delete=models.SET_NULL, null=True, blank=True, related_name="sites")
    name = models.CharField(max_length=100)
    code = models.SlugField(max_length=60)
    region = models.CharField(max_length=100, blank=True)
    latitude = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True,
                                   validators=[MinValueValidator(-90), MaxValueValidator(90)])
    longitude = models.DecimalField(max_digits=8, decimal_places=5, null=True, blank=True,
                                    validators=[MinValueValidator(-180), MaxValueValidator(180)])
    timezone = models.CharField(max_length=60, default="Asia/Kolkata")
    criticality = models.CharField(max_length=10, choices=CRITICALITY, default="standard")
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="site_tenant_name_unique"),
                       models.UniqueConstraint(fields=["tenant_key", "code"], name="site_tenant_code_unique")]

    def __str__(self):
        return self.name


class SiteLesiteProfile(models.Model):
    """Optional per-site integration profile for legacy Lesites / observability URLs."""

    site = models.OneToOneField(Site, on_delete=models.CASCADE, related_name="lesite_profile")
    enabled = models.BooleanField(default=True)
    legacy_sitename = models.CharField(
        max_length=100,
        blank=True,
        help_text="Lesites sitename slug (e.g. fs-dr-le). Defaults from site code when empty.",
    )
    websocket_url = models.CharField(max_length=500, blank=True)
    environment = models.CharField(max_length=40, blank=True, default="PROD")
    entity_host = models.CharField(max_length=255, blank=True)
    entity_port = models.CharField(max_length=8, blank=True, default="443")
    is_url_secured = models.BooleanField(default=True)
    analytics_prefix_url = models.URLField(max_length=500, blank=True)
    location = models.CharField(max_length=80, blank=True, help_text="Country-state key, e.g. IN-KA")
    country_code = models.CharField(max_length=8, blank=True)
    redis_host = models.CharField(max_length=255, blank=True)
    redis_port = models.CharField(max_length=8, blank=True, default="6379")
    prometheus_url = models.URLField(max_length=500, blank=True)
    elastic_host = models.CharField(max_length=255, blank=True)
    elastic_port = models.CharField(max_length=8, blank=True, default="9200")
    grafana_api = models.CharField(max_length=500, blank=True)
    le_url = models.URLField(max_length=500, blank=True)
    incident_url = models.URLField(max_length=500, blank=True)
    incident_api = models.CharField(max_length=500, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "site lesites profile"

    def __str__(self):
        return f"Lesites profile · {self.site}"

    @property
    def sitename(self):
        """Lesites slug for templates (legacy_sitename)."""
        return (self.legacy_sitename or "").strip()


class Membership(models.Model):
    """Grants a user a role, either on one site or (site=None) on every site of the tenant."""
    ROLES = [("admin", "Admin"), ("operator", "Operator"), ("viewer", "Viewer")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="le_memberships")
    site = models.ForeignKey(Site, on_delete=models.CASCADE, null=True, blank=True, related_name="memberships")
    role = models.CharField(max_length=10, choices=ROLES, default="viewer")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "site"], name="membership_user_site_unique")]

    def __str__(self):
        return f"{self.user} · {self.site or 'All sites'} · {self.role}"


class AuditEvent(models.Model):
    """Append-only record of who changed what."""
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    actor_name = models.CharField(max_length=150, blank=True)
    action = models.CharField(max_length=60)
    object_type = models.CharField(max_length=60)
    object_id = models.CharField(max_length=64, blank=True)
    object_repr = models.CharField(max_length=200, blank=True)
    result = models.CharField(max_length=20, default="ok")
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    details = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.actor_name} {self.action} {self.object_type}:{self.object_id}"


class Event(models.Model):
    """Unified operational event: state changes from probes, alerts from
    Alertmanager, device reachability, BOD/EOD runs, automation results…"""
    SEVERITY = [("critical", "Critical"), ("warning", "Warning"), ("info", "Info"), ("ok", "Resolved")]

    tenant_key = models.CharField(max_length=100, db_index=True, default=settings.LINKEDEYE_TENANT)
    occurred_at = models.DateTimeField(default=timezone.now, db_index=True)
    severity = models.CharField(max_length=10, choices=SEVERITY, default="info")
    source = models.CharField(max_length=40, db_index=True)        # app label, e.g. "urlchecker"
    kind = models.CharField(max_length=40, default="state_change")  # state_change | alert | log | job | ...
    title = models.CharField(max_length=200)
    message = models.TextField(blank=True)
    site = models.CharField(max_length=100, blank=True, db_index=True)
    object_type = models.CharField(max_length=60, blank=True)
    object_id = models.CharField(max_length=64, blank=True)
    fingerprint = models.CharField(max_length=200, blank=True, db_index=True)
    data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-occurred_at"]
        indexes = [models.Index(fields=["tenant_key", "occurred_at"])]

    def __str__(self):
        return f"[{self.severity}] {self.title}"


class SchedulerLease(models.Model):
    """Who runs scheduler passes (manage.py run_probes) on databases without advisory locks. The holder renews
    `expires_at` from its heartbeat thread; another instance takes the lease only once it has expired."""
    name = models.CharField(max_length=100, unique=True)
    owner = models.CharField(max_length=200, blank=True)
    acquired_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.name} held by {self.owner or 'nobody'} until {self.expires_at:%Y-%m-%d %H:%M:%S}"


class JobState(models.Model):
    """Last run of each periodic job (core.jobs), so a restarted scheduler or `run_probes --once` from cron does not
    re-run hourly jobs on every start."""
    tenant_key = models.CharField(max_length=100, default=settings.LINKEDEYE_TENANT)
    name = models.CharField(max_length=100)
    last_run_at = models.DateTimeField(null=True, blank=True)
    last_duration_ms = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="jobstate_tenant_name_unique")]

    def __str__(self):
        return f"{self.name} @ {self.last_run_at}"


class LoginOtp(models.Model):
    """The pending e-mail sign-in code of one user (core.login_otp). Kept in the database, not in the per-process
    cache, so every web worker sees it. Only a keyed hash of the code is stored.

    `failures` counts wrong guesses against the current code; `send_count` / `send_window_started_at` bound how many
    codes may be mailed per hour; `last_sent_at` drives the resend cooldown."""
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="login_otp")
    code_hash = models.CharField(max_length=64, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    failures = models.PositiveSmallIntegerField(default=0)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    send_count = models.PositiveSmallIntegerField(default=0)
    send_window_started_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    def __str__(self):
        return f"login code for user {self.user_id}"


class LoginOtpCounter(models.Model):
    """Per client-address counters for the sign-in code step: wrong codes ("verify") and mails sent ("send")."""
    kind = models.CharField(max_length=10)
    value = models.CharField(max_length=191)
    count = models.PositiveIntegerField(default=0)
    window_started_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["kind", "value"], name="core_loginotpcounter_key_unique")]

    def __str__(self):
        return f"{self.kind}:{self.value} ({self.count})"
