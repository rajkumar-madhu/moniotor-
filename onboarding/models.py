"""Onboarding runs, their targets, the step log, the credential vault rows and the
service-discovery fetch record.

An onboarding run is a resumable, idempotent walk through the wizard: scope → targets →
credentials → verify → enable → logs → confirm → done. Re-entering a step never duplicates a
target (hostnames are unique per run) and never loses what the previous visit recorded.
"""
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


def _tenant():
    return settings.LINKEDEYE_TENANT


# ----------------------------------------------------------------------------- vault
class CredentialKind(models.TextChoices):
    SSH_PASSWORD = "ssh_password", "SSH username and password"
    SSH_KEY = "ssh_key", "SSH private key"
    SNMP_V2C = "snmp_v2c", "SNMP v2c community"
    SNMP_V3 = "snmp_v3", "SNMP v3 credentials"
    BASIC_AUTH = "basic_auth", "HTTP basic auth"
    BEARER = "bearer", "HTTP bearer token"


class Credential(models.Model):
    """One encrypted secret. The ciphertext lives in `secret` and is never rendered.

    Everything that reads it goes through `core.secrets.use()`, which audits the read. The field is
    `editable=False` on purpose: no ModelForm, Django admin page or serializer can echo it back.
    """
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=20, choices=CredentialKind.choices, default=CredentialKind.SSH_PASSWORD)
    username = models.CharField(max_length=150, blank=True)
    # Fernet ciphertext. Write-only: never rendered, exported, logged or returned by a form.
    secret = models.TextField(editable=False)
    site = models.CharField(max_length=100, blank=True, default="", db_index=True)
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                              related_name="credentials")
    notes = models.CharField(max_length=300, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="le_credentials")
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["tenant_key", "kind"])]

    def __str__(self):
        return f"{self.name} ({self.get_kind_display()})"

    @property
    def masked(self):
        from core.secrets import mask
        return mask(self)


# ----------------------------------------------------------------------------- runs
class RunKind(models.TextChoices):
    SITE = "site", "Site"
    SERVER = "server", "Server"
    DEVICE = "device", "Network device"
    PROMETHEUS = "prometheus", "Prometheus"
    BULK = "bulk", "Bulk import"


class RunStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    VERIFYING = "verifying", "Verifying"
    INSTALLING = "installing", "Installing"
    WAITING_FOR_DATA = "waiting_for_data", "Waiting for data"
    COMPLETE = "complete", "Complete"
    FAILED = "failed", "Failed"


#: the wizard, in order. `slug` is the URL segment; `needs_targets` steps are skipped-over gracefully
#: when the run has none yet.
STEPS = [
    ("scope", "Scope", "Site, client and environment"),
    ("targets", "Targets", "Hosts, devices or a Prometheus URL"),
    ("credentials", "Credentials", "SSH, SNMP or HTTP secrets (write-only)"),
    ("verify", "Verify", "Reachability, exporter and facts"),
    ("enable", "Enable collection", "Install or point at an exporter"),
    ("logs", "Logs", "Log shipper configuration"),
    ("confirm", "Confirm", "Wait for the first samples"),
    ("done", "Done", "Everything shows"),
]
STEP_SLUGS = [s[0] for s in STEPS]
STEP_TITLES = {s[0]: s[1] for s in STEPS}

RUN_TONES = {"draft": "mute", "verifying": "warn", "installing": "warn", "waiting_for_data": "info",
             "complete": "ok", "failed": "crit"}
TARGET_TONES = {"new": "mute", "reachable": "info", "unreachable": "crit", "exporter_found": "info",
                "installing": "warn", "installed": "info", "scraping": "info", "live": "ok", "failed": "crit"}
STEPLOG_TONES = {"ok": "ok", "warn": "warn", "error": "crit", "pending": "info", "info": "info"}

#: status the run moves to when a step is entered (the state machine; see services.advance)
STEP_STATUS = {
    "scope": RunStatus.DRAFT,
    "targets": RunStatus.DRAFT,
    "credentials": RunStatus.DRAFT,
    "verify": RunStatus.VERIFYING,
    "enable": RunStatus.INSTALLING,
    "logs": RunStatus.INSTALLING,
    "confirm": RunStatus.WAITING_FOR_DATA,
    "done": RunStatus.COMPLETE,
}


class OnboardingRun(models.Model):
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    kind = models.CharField(max_length=12, choices=RunKind.choices, default=RunKind.SERVER, db_index=True)
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.DRAFT, db_index=True)
    site = models.CharField(max_length=100, blank=True, default="", db_index=True)
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="onboarding_runs")
    environment = models.CharField(max_length=10, default="prod")
    step = models.CharField(max_length=20, default="scope")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="onboarding_runs")
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["tenant_key", "status"])]

    def __str__(self):
        return f"{self.get_kind_display()} onboarding · {self.site or 'All Sites'} · #{self.pk}"

    def get_absolute_url(self):
        return reverse("onboarding:step", args=[self.pk, self.step or "scope"])

    # -- progress rail ------------------------------------------------------
    @property
    def step_index(self):
        try:
            return STEP_SLUGS.index(self.step or "scope")
        except ValueError:
            return 0

    def rail(self, current=None):
        """[(slug, title, hint, state)] for the progress rail; state is done | current | todo."""
        here = STEP_SLUGS.index(current) if current in STEP_SLUGS else self.step_index
        out = []
        for i, (slug, title, hint) in enumerate(STEPS):
            state = "current" if i == here else ("done" if i < max(here, self.step_index) else "todo")
            if i < here:
                state = "done"
            out.append((slug, title, hint, state))
        return out

    @property
    def is_finished(self):
        return self.status in (RunStatus.COMPLETE, RunStatus.FAILED)

    @property
    def tone(self):
        return RUN_TONES.get(self.status, "mute")


class TargetType(models.TextChoices):
    LINUX = "linux", "Linux server"
    WINDOWS = "windows", "Windows server"
    DEVICE = "device", "Network device (SNMP)"
    K8S_NODE = "k8s_node", "Kubernetes node"
    URL = "url", "URL / service"
    PROMETHEUS = "prometheus", "Prometheus server"


class TargetState(models.TextChoices):
    NEW = "new", "New"
    REACHABLE = "reachable", "Reachable"
    UNREACHABLE = "unreachable", "Unreachable"
    EXPORTER_FOUND = "exporter_found", "Exporter found"
    INSTALLING = "installing", "Installing"
    INSTALLED = "installed", "Installed"
    SCRAPING = "scraping", "Registered for scraping"
    LIVE = "live", "Live"
    FAILED = "failed", "Failed"


#: default metrics port per target type
DEFAULT_PORTS = {
    TargetType.LINUX: 9100,
    TargetType.WINDOWS: 9182,
    TargetType.DEVICE: 9116,
    TargetType.K8S_NODE: 8080,
    TargetType.URL: 443,
    TargetType.PROMETHEUS: 9090,
}
#: default exporter kind per target type
DEFAULT_EXPORTERS = {
    TargetType.LINUX: "node_exporter",
    TargetType.WINDOWS: "windows_exporter",
    TargetType.DEVICE: "snmp_exporter",
    TargetType.K8S_NODE: "kube-state-metrics",
    TargetType.URL: "blackbox_exporter",
    TargetType.PROMETHEUS: "prometheus",
}
#: Prometheus `job` label per target type (what the service-discovery endpoint advertises)
JOB_LABELS = {
    TargetType.LINUX: "node",
    TargetType.WINDOWS: "windows",
    TargetType.DEVICE: "snmp",
    TargetType.K8S_NODE: "kube-state-metrics",
    TargetType.URL: "blackbox",
    TargetType.PROMETHEUS: "prometheus",
}
#: states that mean "this target is ready to be scraped" (service discovery serves these)
SD_STATES = (TargetState.SCRAPING, TargetState.LIVE, TargetState.INSTALLED, TargetState.EXPORTER_FOUND)


class OnboardingTarget(models.Model):
    run = models.ForeignKey(OnboardingRun, on_delete=models.CASCADE, related_name="targets")
    hostname = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True, default="")
    target_type = models.CharField(max_length=12, choices=TargetType.choices, default=TargetType.LINUX)
    port = models.PositiveIntegerField(default=9100)
    exporter_kind = models.CharField(max_length=40, blank=True, default="")
    credential = models.ForeignKey(Credential, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="targets")
    state = models.CharField(max_length=16, choices=TargetState.choices, default=TargetState.NEW, db_index=True)
    enabled = models.BooleanField(default=True, help_text="Serve this target from the service-discovery endpoint.")
    facts = models.JSONField(default=dict, blank=True)
    last_error = models.CharField(max_length=500, blank=True, default="")
    # filled on activation, idempotently; a host already known links to the existing record
    ci = models.ForeignKey("cmdb.ConfigurationItem", on_delete=models.SET_NULL, null=True, blank=True,
                           related_name="onboarding_targets")
    device = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="onboarding_targets")
    exporter = models.ForeignKey("observe.Exporter", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="onboarding_targets")
    first_sample_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["hostname", "id"]
        constraints = [models.UniqueConstraint(fields=["run", "hostname"], name="onboarding_target_run_host_unique")]

    def __str__(self):
        return self.hostname

    @property
    def instance(self):
        """The Prometheus `instance` label this target will carry: host:port."""
        host = (self.address or self.hostname or "").strip()
        return f"{host}:{self.port}" if host else ""

    @property
    def job(self):
        return JOB_LABELS.get(self.target_type, "node")

    @property
    def metrics_url(self):
        host = (self.address or self.hostname or "").strip()
        if not host:
            return ""
        scheme = "https" if self.port == 443 else "http"
        return f"{scheme}://{host}:{self.port}/metrics"

    @property
    def site(self):
        return self.run.site

    @property
    def is_live(self):
        return self.state == TargetState.LIVE

    @property
    def tone(self):
        return TARGET_TONES.get(self.state, "mute")


class StepLog(models.Model):
    """Append-only trail of what each step did. Never holds a secret."""
    STATUSES = [("ok", "OK"), ("warn", "Warning"), ("error", "Error"), ("pending", "Pending"), ("info", "Info")]

    run = models.ForeignKey(OnboardingRun, on_delete=models.CASCADE, related_name="step_logs")
    target = models.ForeignKey(OnboardingTarget, on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="step_logs")
    step = models.CharField(max_length=24)
    status = models.CharField(max_length=10, choices=STATUSES, default="info")
    detail = models.CharField(max_length=500, blank=True, default="")
    duration_ms = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.step} {self.status}"

    @property
    def tone(self):
        return STEPLOG_TONES.get(self.status, "mute")


class SDFetch(models.Model):
    """One fetch of /api/sd/targets.json: when, from where, how many targets, and the outcome."""
    RESULTS = [("ok", "Served"), ("denied", "Denied"), ("throttled", "Rate limited")]

    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    fetched_at = models.DateTimeField(default=timezone.now, db_index=True)
    source_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=200, blank=True, default="")
    target_count = models.PositiveIntegerField(default=0)
    result = models.CharField(max_length=10, choices=RESULTS, default="ok")

    class Meta:
        ordering = ["-fetched_at", "-id"]
        verbose_name = "service-discovery fetch"
        verbose_name_plural = "service-discovery fetches"

    def __str__(self):
        return f"{self.fetched_at:%Y-%m-%d %H:%M} {self.result} ({self.target_count})"
