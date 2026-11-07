"""Telemetry records.

* Alert       – one row per Alertmanager alert fingerprint, upserted by the webhook receiver
                (POST /api/v1/alertmanager/). Prometheus / Nagios data is read live and never stored.
* SavedQuery  – PromQL expressions saved from the Metrics Explorer.
* DiscoveredTarget – one row per Prometheus scrape target (instance + job), written by telemetry.discovery
                (the `telemetry.discovery` job every 5 minutes, `manage.py discover_targets --now`, or the
                "Discover now" button). Carries what the exporter reports about the host (OS, kernel, cores, memory,
                interfaces, filesystems…) and the CMDB item / device / exporter it is linked to.
* DiscoveryRun – summary of one discovery pass (shown on the Discovery and Integrations pages).
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


def _tenant():
    return settings.LINKEDEYE_TENANT


class AlertQuerySet(models.QuerySet):
    def for_tenant(self):
        return self.filter(tenant_key=settings.LINKEDEYE_TENANT)

    def firing(self):
        return self.filter(status=Alert.FIRING)

    def resolved(self):
        return self.filter(status=Alert.RESOLVED)


class Alert(models.Model):
    FIRING, RESOLVED = "firing", "resolved"
    STATUSES = [(FIRING, "Firing"), (RESOLVED, "Resolved")]

    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    fingerprint = models.CharField(max_length=64, unique=True)
    status = models.CharField(max_length=10, choices=STATUSES, default=FIRING, db_index=True)
    alertname = models.CharField(max_length=200, db_index=True)
    severity = models.CharField(max_length=40, blank=True, db_index=True)
    instance = models.CharField(max_length=255, blank=True, db_index=True)
    job = models.CharField(max_length=200, blank=True)
    site = models.CharField(max_length=100, blank=True, db_index=True)
    summary = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)
    labels = models.JSONField(default=dict, blank=True)
    annotations = models.JSONField(default=dict, blank=True)
    receiver = models.CharField(max_length=200, blank=True)
    generator_url = models.CharField(max_length=1000, blank=True)
    starts_at = models.DateTimeField(null=True, blank=True)
    ends_at = models.DateTimeField(null=True, blank=True)
    last_received_at = models.DateTimeField(default=timezone.now, db_index=True)
    receive_count = models.PositiveIntegerField(default=1)
    fire_count = models.PositiveIntegerField(default=0)
    acknowledged_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name="+")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AlertQuerySet.as_manager()

    class Meta:
        ordering = ["-last_received_at"]
        indexes = [models.Index(fields=["tenant_key", "status", "severity"])]

    def __str__(self):
        return f"{self.alertname} ({self.instance})" if self.instance else self.alertname

    @property
    def event_fingerprint(self):
        return f"am:{self.fingerprint}"

    @property
    def is_example(self):
        return str((self.labels or {}).get("example", "")).lower() == "true"

    @property
    def acknowledged(self):
        return self.acknowledged_at is not None

    @property
    def severity_tone(self):
        return severity_tone(self.severity)

    @property
    def extra_labels(self):
        """Labels worth showing as chips (the well-known ones already have columns)."""
        skip = {"alertname", "severity", "instance", "job", "site"}
        return sorted((k, v) for k, v in (self.labels or {}).items() if k not in skip)

    @property
    def duration_seconds(self):
        if not self.starts_at:
            return None
        end = self.ends_at if self.status == self.RESOLVED and self.ends_at else timezone.now()
        return max(0, int((end - self.starts_at).total_seconds()))


def severity_tone(severity):
    s = (severity or "").lower()
    if s in ("critical", "page", "p1", "emergency", "error", "high"):
        return "crit"
    if s in ("warning", "warn", "medium", "p2", "p3"):
        return "warn"
    if s in ("info", "informational", "low", "p4", "none"):
        return "info"
    return "mute"


class SavedQuery(models.Model):
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    name = models.CharField(max_length=120)
    promql = models.TextField()
    range_key = models.CharField(max_length=8, default="1h")
    description = models.CharField(max_length=300, blank=True)
    shared = models.BooleanField(default=False, help_text="Visible to every user, not only the owner.")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True,
                              related_name="telemetry_saved_queries")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "owner", "name"], name="telemetry_savedquery_owner_name")]

    def __str__(self):
        return self.name


# ============================================================================= Prometheus discovery
class TargetKind(models.TextChoices):
    """What a scrape target is, derived from its job / scrape pool name and the metrics it exposes
    (see telemetry.discovery.detect_kind)."""
    NODE = "node", "Linux host (node_exporter)"
    WINDOWS = "windows", "Windows host (windows_exporter)"
    SNMP = "snmp", "SNMP device"
    BLACKBOX = "blackbox", "Blackbox probe"
    KUBE_STATE = "kube_state", "kube-state-metrics"
    CADVISOR = "cadvisor", "cAdvisor"
    MYSQL = "mysql", "MySQL"
    POSTGRES = "postgres", "PostgreSQL"
    REDIS = "redis", "Redis"
    NGINX = "nginx", "Nginx"
    OTHER = "other", "Other"


# kinds that are hosts (servers) and belong in the CMDB
HOST_KINDS = (TargetKind.NODE, TargetKind.WINDOWS)
KIND_ICONS = {"node": "server", "windows": "server", "snmp": "network", "blackbox": "radar", "kube_state": "hexagon",
              "cadvisor": "layers", "mysql": "database", "postgres": "database", "redis": "database", "nginx": "app",
              "other": "box"}


class DiscoveredTargetQuerySet(models.QuerySet):
    def for_tenant(self):
        return self.filter(tenant_key=settings.LINKEDEYE_TENANT)

    def present(self):
        return self.filter(missing_since__isnull=True)

    def hosts(self):
        return self.filter(kind__in=[k.value for k in HOST_KINDS])

    def unlinked_hosts(self):
        """Hosts Prometheus scrapes that are not in the CMDB yet (ignored targets excluded)."""
        return self.present().hosts().filter(ci__isnull=True).exclude(state=DiscoveredTarget.IGNORED)


class DiscoveredTarget(models.Model):
    UP, DOWN, UNKNOWN = "up", "down", "unknown"
    HEALTHS = [(UP, "Up"), (DOWN, "Down"), (UNKNOWN, "Unknown")]
    NEW, LINKED, IGNORED = "new", "linked", "ignored"
    STATES = [(NEW, "New"), (LINKED, "Linked"), (IGNORED, "Ignored")]

    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    instance = models.CharField(max_length=255, db_index=True)
    job = models.CharField(max_length=200, blank=True)
    scrape_pool = models.CharField(max_length=200, blank=True)
    labels = models.JSONField(default=dict, blank=True)
    discovered_labels = models.JSONField(default=dict, blank=True)
    kind = models.CharField(max_length=16, choices=TargetKind.choices, default=TargetKind.OTHER, db_index=True)
    health = models.CharField(max_length=8, choices=HEALTHS, default=UNKNOWN, db_index=True)
    last_error = models.CharField(max_length=500, blank=True)
    scrape_url = models.CharField(max_length=1000, blank=True)
    last_scrape = models.DateTimeField(null=True, blank=True)
    first_seen = models.DateTimeField(default=timezone.now, db_index=True)
    last_seen = models.DateTimeField(default=timezone.now)
    missing_since = models.DateTimeField(null=True, blank=True, db_index=True,
                                         help_text="Set when the target disappears from /api/v1/targets.")
    site = models.CharField(max_length=100, default="All Sites", db_index=True)
    site_manual = models.BooleanField(default=False, help_text="Site set by an operator: discovery keeps it.")
    details = models.JSONField(default=dict, blank=True)
    ci = models.ForeignKey("cmdb.ConfigurationItem", on_delete=models.SET_NULL, null=True, blank=True,
                           related_name="discovered_targets")
    device = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="discovered_targets")
    exporter = models.ForeignKey("observe.Exporter", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="discovered_targets")
    state = models.CharField(max_length=8, choices=STATES, default=NEW, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = DiscoveredTargetQuerySet.as_manager()

    class Meta:
        ordering = ["instance", "job"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "instance", "job"],
                                               name="telemetry_discoveredtarget_instance_job")]
        indexes = [models.Index(fields=["tenant_key", "kind", "state"])]

    def __str__(self):
        return f"{self.instance} ({self.job})" if self.job else self.instance

    def get_absolute_url(self):
        from django.urls import reverse
        return reverse("telemetry:discovery_detail", args=[self.pk])

    @property
    def address(self):
        """Host part of the instance label: "db-01:9100" -> "db-01", "[::1]:9100" -> "::1", a blackbox
        target such as "https://example.com" -> "example.com" (telemetry.discovery.address_of)."""
        from .discovery import address_of
        return (self.details or {}).get("address") or address_of(self.instance)

    @property
    def hostname(self):
        return (self.details or {}).get("hostname") or ""

    @property
    def display_name(self):
        return self.hostname or self.address or self.instance

    @property
    def is_host(self):
        return self.kind in HOST_KINDS

    @property
    def is_present(self):
        return self.missing_since is None

    @property
    def is_ignored(self):
        return self.state == self.IGNORED

    @property
    def linked_to(self):
        """The record this target is linked to (CI, device or exporter), or None."""
        return self.ci or self.device or self.exporter

    @property
    def icon(self):
        return KIND_ICONS.get(self.kind, "box")

    @property
    def label_items(self):
        return sorted((self.labels or {}).items())

    @property
    def discovered_label_items(self):
        return sorted((self.discovered_labels or {}).items())

    @property
    def summary_line(self):
        """OS / kernel for hosts, sysDescr for SNMP devices, otherwise the scrape pool."""
        d = self.details or {}
        if self.kind == TargetKind.SNMP:
            return d.get("sysDescr") or d.get("sysName") or ""
        parts = [d.get("os") or "", d.get("kernel") or ""]
        return " · ".join(p for p in parts if p)


class DiscoveryRun(models.Model):
    """One discovery pass. The newest row is what the Discovery page and the Integrations card show."""
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.FloatField(null=True, blank=True)
    ok = models.BooleanField(default=False)
    error = models.CharField(max_length=500, blank=True)
    trigger = models.CharField(max_length=20, default="job")   # job | manual | command
    targets = models.PositiveIntegerField(default=0)
    up = models.PositiveIntegerField(default=0)
    down = models.PositiveIntegerField(default=0)
    new = models.PositiveIntegerField(default=0)
    missing = models.PositiveIntegerField(default=0)
    linked = models.PositiveIntegerField(default=0)
    created = models.PositiveIntegerField(default=0)
    queries = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        get_latest_by = "started_at"

    def __str__(self):
        return f"discovery {self.started_at:%Y-%m-%d %H:%M} ({'ok' if self.ok else 'error'})"

    @property
    def summary(self):
        if not self.ok:
            return f"Failed: {self.error}" if self.error else "Failed"
        return (f"{self.targets} target(s), {self.up} up, {self.down} down; {self.new} new, {self.missing} missing, "
                f"{self.linked} linked, {self.created} created")
