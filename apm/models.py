"""APM: adapter status matrix, process status and execution logs pushed by (or read from) a customer's cron toolkit.

Site scoping: a ``Source`` (one monitored deployment) stores the site *name* in ``site`` like every other module
(and is listed in ``administration.services.SITE_NAME_FIELDS``). Every other model here (cells, runs, events,
processes, logs, retry requests) has no site of its own: it belongs to exactly one source and is scoped through
``source__site``. A rename of the site therefore follows through the source, and nothing can drift.

Everything the producer sends is stored as plain text or numbers, never interpreted, and is length-capped by
``apm.payloads`` before it gets here.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

GLOBAL_SITE = "All Sites"
EXAMPLE_MARK = "[example]"

#: adapter / process states. 3 = unknown (not evaluated / no data); "-" cells in the toolkit are not stored at all
FAILED, WARNING, HEALTHY, UNKNOWN = 0, 1, 2, 3
STATUS_CHOICES = [(FAILED, "Failed"), (WARNING, "Warning"), (HEALTHY, "Healthy"), (UNKNOWN, "Unknown")]
STATUS_LABELS = dict(STATUS_CHOICES)

LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]


def _tenant():
    return settings.LINKEDEYE_TENANT


class Source(models.Model):
    """One monitored deployment that pushes (or is polled) for APM data."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    name = models.CharField(max_length=120)
    site = models.CharField(max_length=100, default=GLOBAL_SITE, db_index=True)
    description = models.CharField(max_length=300, blank=True)
    enabled = models.BooleanField(default=True)
    expected_interval_s = models.PositiveIntegerField(
        default=300, help_text="How often the producer is expected to report. Data older than this (plus a short grace) "
                               "is shown as stale.")

    # ingest token: only a salted SHA-256 and a short visible prefix are stored; the token is shown once
    token_prefix = models.CharField(max_length=12, blank=True, db_index=True, editable=False)
    token_salt = models.CharField(max_length=32, blank=True, editable=False)
    token_hash = models.CharField(max_length=64, blank=True, editable=False)
    token_rotated_at = models.DateTimeField(null=True, blank=True, editable=False)

    # liveness bookkeeping (written by the ingest service)
    last_seen = models.DateTimeField(null=True, blank=True, editable=False)
    last_ip = models.GenericIPAddressField(null=True, blank=True, editable=False)
    last_data_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_matrix_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_table_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_log_at = models.DateTimeField(null=True, blank=True, editable=False)
    silent_since = models.DateTimeField(null=True, blank=True, editable=False)

    # optional path B: LinkedEye reads the toolkit's Redis keys itself
    redis_poll = models.BooleanField(default=False)
    redis_host = models.CharField(max_length=255, blank=True)
    redis_port = models.PositiveIntegerField(default=6379)
    redis_db = models.PositiveSmallIntegerField(default=0)
    redis_tls = models.BooleanField(default=False)
    redis_username = models.CharField(max_length=64, blank=True)
    #: onboarding.Credential id (credential vault) holding the Redis password; not a foreign key on purpose
    redis_credential_id = models.PositiveIntegerField(null=True, blank=True, editable=False)
    redis_prefix = models.CharField("Key prefix (SITE_NAME)", max_length=100, blank=True)
    redis_adapter_suffix = models.CharField(max_length=100, blank=True)
    redis_process_suffix = models.CharField(max_length=100, blank=True)
    redis_last_polled_at = models.DateTimeField(null=True, blank=True, editable=False)
    redis_last_error = models.CharField(max_length=300, blank=True, editable=False)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="apm_source_tenant_name_unique")]

    def __str__(self):
        return self.name

    @property
    def is_example(self):
        return (self.description or "").startswith(EXAMPLE_MARK)

    @property
    def has_token(self):
        return bool(self.token_hash)


class AdapterCell(models.Model):
    """Current state of one adapter: a broker (client) x exchange segment cell of the matrix."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="cells")
    client = models.CharField(max_length=40)
    segment = models.CharField(max_length=20)
    ctcl_id = models.CharField(max_length=64, blank=True)
    value = models.CharField(max_length=120, blank=True)
    status = models.PositiveSmallIntegerField(choices=STATUS_CHOICES, default=UNKNOWN)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_failure_at = models.DateTimeField(null=True, blank=True)
    failure_streak = models.PositiveIntegerField(default=0)
    failure_total = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=240, blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["client", "segment"]
        constraints = [models.UniqueConstraint(fields=["source", "client", "segment"], name="apm_cell_unique")]
        indexes = [models.Index(fields=["source", "status"])]

    def __str__(self):
        return f"{self.client} / {self.segment}"


class AdapterRun(models.Model):
    """One accepted payload (a producer run). Replays of the same executedOn do not add rows."""
    KINDS = [("matrix", "Adapter matrix"), ("table", "Process table")]
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="runs")
    kind = models.CharField(max_length=10, choices=KINDS, default="matrix")
    executed_key = models.CharField(max_length=120)
    payload_hash = models.CharField(max_length=64, blank=True)
    executed_at = models.DateTimeField()
    received_at = models.DateTimeField(default=timezone.now)
    total = models.PositiveIntegerField(default=0)
    success = models.PositiveIntegerField(default=0)
    failure = models.PositiveIntegerField(default=0)
    overall = models.BooleanField(default=True)
    duration_s = models.FloatField(null=True, blank=True)
    host = models.CharField(max_length=120, blank=True)
    script = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["-executed_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["source", "kind", "executed_key"], name="apm_run_unique")]
        indexes = [models.Index(fields=["source", "kind", "-executed_at"])]

    def __str__(self):
        return f"{self.source_id} {self.kind} {self.executed_at:%Y-%m-%d %H:%M:%S}"


class CellEvent(models.Model):
    """A change of one adapter's status (history tab, failure timeline). Text, not a foreign key: it outlives the cell."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="cell_events")
    client = models.CharField(max_length=40)
    segment = models.CharField(max_length=20)
    ts = models.DateTimeField(default=timezone.now)
    old_status = models.PositiveSmallIntegerField(null=True, blank=True)
    new_status = models.PositiveSmallIntegerField()
    ctcl_id = models.CharField(max_length=64, blank=True)
    error = models.CharField(max_length=240, blank=True)

    class Meta:
        ordering = ["-ts", "-id"]
        indexes = [models.Index(fields=["source", "client", "segment", "-ts"]), models.Index(fields=["source", "-ts"])]


class ProcessState(models.Model):
    """Current state of one monitored process on one host."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="processes")
    host = models.CharField(max_length=120, blank=True)
    name = models.CharField(max_length=120)
    pid = models.PositiveIntegerField(null=True, blank=True)
    status = models.PositiveSmallIntegerField(choices=STATUS_CHOICES, default=UNKNOWN)
    cpu_pct = models.FloatField(null=True, blank=True)
    mem_pct = models.FloatField(null=True, blank=True)
    uptime_s = models.PositiveIntegerField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    last_execution = models.DateTimeField(null=True, blank=True)
    next_execution = models.DateTimeField(null=True, blank=True)
    response_ms = models.FloatField(null=True, blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    remarks = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["host", "name"]
        constraints = [models.UniqueConstraint(fields=["source", "host", "name"], name="apm_process_unique")]


class ExecLog(models.Model):
    """One execution log line. Capped per source and aged out by the retention job."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="logs")
    ts = models.DateTimeField()
    level = models.CharField(max_length=8, default="INFO")
    process = models.CharField(max_length=80, blank=True)
    message = models.CharField(max_length=1000)
    client = models.CharField(max_length=40, blank=True)
    segment = models.CharField(max_length=20, blank=True)
    fp = models.CharField(max_length=32, editable=False)

    class Meta:
        ordering = ["-ts", "-id"]
        constraints = [models.UniqueConstraint(fields=["source", "fp"], name="apm_log_dedupe")]
        indexes = [models.Index(fields=["source", "-ts"])]


class RetryRequest(models.Model):
    """"Retry now": a request the producer's script PULLS (GET /api/apm/commands/); LinkedEye never reaches into its hosts."""
    PENDING, PICKED, DONE, EXPIRED = "pending", "picked", "done", "expired"
    STATES = [(PENDING, "Pending"), (PICKED, "Picked up"), (DONE, "Done"), (EXPIRED, "Expired")]
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="retries")
    client = models.CharField(max_length=40)
    segment = models.CharField(max_length=20)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    requested_by_name = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    picked_at = models.DateTimeField(null=True, blank=True)
    done_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=STATES, default=PENDING)
    note = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        indexes = [models.Index(fields=["source", "state"]), models.Index(fields=["source", "client", "segment"])]
