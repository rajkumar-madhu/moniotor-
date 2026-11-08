"""Kubernetes client inventory.

The estate is discovered read-only with `kubectl` (see docs/IMPORT.md) and delivered as a workbook; the
`k8s` module holds that snapshot so the console can show it next to the rest of the inventory.

Shape of the data:

* ``ImportRun``    – one import of one workbook. Imports are **idempotent and versioned**: a later run
                     updates rows in place, sets ``last_seen``, and stamps ``missing_since`` on rows that
                     the new snapshot no longer contains. Nothing is ever duplicated or deleted.
* ``KubeCluster``  – a cluster (kubectl context), optionally tied to a ``compute.Cluster``.
* ``Node``         – a cluster node with its parsed capacity, labels and taints, optionally linked to the
                     ``cmdb.ConfigurationItem`` / ``network.Device`` / ``compute.ClusterMember`` that already
                     describe the same machine.
* ``Namespace``    – a namespace and the client it was mapped to, with the confidence and the evidence of
                     that mapping so a low-confidence guess is never shown as a fact.
* ``ClientProfile``– one row per workbook client, linked to ``core.Client`` when one matches.

Nothing here can hold a secret **value**: the workbook carries Kubernetes Secret *names* only, and this
module stores names only. See docs/IMPORT.md.
"""
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


def _tenant():
    return settings.LINKEDEYE_TENANT


GLOBAL_SITE = "All Sites"
GLOBAL_SITES = ("", GLOBAL_SITE)


def is_global(site):
    return (site or "") in GLOBAL_SITES


# ----------------------------------------------------------------------------- choices
class ImportStatus(models.TextChoices):
    RUNNING = "running", "Running"
    OK = "ok", "Completed"
    FAILED = "failed", "Failed"


class NodeRole(models.TextChoices):
    CONTROL_PLANE = "control_plane", "Control plane"
    WORKER = "worker", "Worker"
    UNKNOWN = "unknown", "Unknown"


class Exposure(models.TextChoices):
    PUBLIC = "public", "Public"
    PRIVATE = "private", "Private"
    UNKNOWN = "unknown", "Unknown"


class Confidence(models.TextChoices):
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"
    UNKNOWN = "unknown", "Unknown"


class MappingStatus(models.TextChoices):
    MAPPED = "mapped", "Mapped"
    REVIEW = "review", "Review required"
    UNKNOWN = "unknown", "Not mapped"


class Health(models.TextChoices):
    """The workbook's GREEN / AMBER / RED, in the console's own vocabulary."""
    OK = "ok", "Green"
    WARN = "warn", "Amber"
    CRIT = "crit", "Red"
    UNKNOWN = "unknown", "Not assessed"


HEALTH_FROM_WORKBOOK = {"GREEN": Health.OK, "AMBER": Health.WARN, "RED": Health.CRIT}
HEALTH_TONES = {Health.OK: "ok", Health.WARN: "warn", Health.CRIT: "crit", Health.UNKNOWN: "mute"}


# ----------------------------------------------------------------------------- abstract bases
class TenantModel(models.Model):
    """tenant_key + created/updated stamps, like every other LinkedEye model."""
    tenant_key = models.CharField(max_length=64, db_index=True, default=_tenant)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class SiteScopedModel(TenantModel):
    """Records whose own row names the site ("All Sites" = every site)."""
    site = models.CharField(max_length=100, blank=True, default=GLOBAL_SITE, db_index=True)

    class Meta:
        abstract = True

    @property
    def site_label(self):
        return self.site or GLOBAL_SITE

    @property
    def is_global(self):
        return is_global(self.site)


class ImportedModel(models.Model):
    """Rows that come from a workbook import, versioned across runs.

    ``last_seen`` is the start of the newest run that still contained the row; ``missing_since`` is set when a
    later snapshot no longer has it (and cleared again if it comes back), so a disappeared node is visible as
    gone rather than silently deleted or silently kept.
    """
    import_run = models.ForeignKey("k8s.ImportRun", on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="%(class)s_rows")
    first_seen = models.DateTimeField(null=True, blank=True)
    last_seen = models.DateTimeField(null=True, blank=True, db_index=True)
    missing_since = models.DateTimeField(null=True, blank=True, db_index=True)

    class Meta:
        abstract = True

    @property
    def is_missing(self):
        return self.missing_since is not None


# ----------------------------------------------------------------------------- import runs
class ImportRun(TenantModel):
    """One import of one inventory workbook (or directory / zip of CSV sheets)."""
    source_name = models.CharField(max_length=255, help_text="File or directory name the rows came from.")
    source_sha256 = models.CharField(max_length=64, blank=True)
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=ImportStatus.choices, default=ImportStatus.RUNNING,
                              db_index=True)
    counts = models.JSONField(default=dict, blank=True, help_text="Per-sheet row / created / updated counts.")
    errors = models.JSONField(default=list, blank=True)
    warnings = models.JSONField(default=list, blank=True)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="k8s_imports")
    notes = models.TextField(blank=True)
    metadata = models.JSONField(default=dict, blank=True,
                                 help_text="Discovery metadata from 15_RAW_Discovery: the kubectl commands "
                                           "that were run and the resource counts they reported.")
    site = models.CharField(max_length=100, blank=True, default=GLOBAL_SITE,
                             help_text="Site the imported records were filed under.")

    class Meta:
        ordering = ["-started_at", "-pk"]
        indexes = [models.Index(fields=["tenant_key", "-started_at"])]
        verbose_name = "import run"

    def __str__(self):
        return f"{self.source_name} · {self.started_at:%Y-%m-%d %H:%M}"

    def get_absolute_url(self):
        return reverse("k8s:import_detail", args=[self.pk])

    @property
    def duration_ms(self):
        if not self.finished_at:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000

    @property
    def total_rows(self):
        return sum(int(c.get("rows") or 0) for c in self.counts.values() if isinstance(c, dict))

    @property
    def sheet_rows(self):
        """[(sheet, counts)] in sheet order, for the import detail page."""
        return sorted(((k, v) for k, v in (self.counts or {}).items() if isinstance(v, dict)), key=lambda kv: kv[0])

    def sheet_count(self, sheet, key="rows"):
        entry = (self.counts or {}).get(sheet)
        return int(entry.get(key) or 0) if isinstance(entry, dict) else None


# ----------------------------------------------------------------------------- clusters & nodes
class KubeCluster(SiteScopedModel, ImportedModel):
    name = models.CharField(max_length=150)
    context = models.CharField(max_length=200, blank=True, help_text="kubectl context the snapshot came from.")
    kubernetes_version = models.CharField(max_length=40, blank=True)
    compute_cluster = models.ForeignKey("compute.Cluster", on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="k8s_inventory")
    discovered_at = models.DateTimeField(null=True, blank=True,
                                          help_text="Discovery timestamp reported by the collector.")

    class Meta:
        ordering = ["name"]
        verbose_name = "Kubernetes cluster"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="k8s_cluster_tenant_name_unique")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return f"{reverse('k8s:nodes')}?cluster={self.pk}"


class Node(TenantModel, ImportedModel):
    """A cluster node. The site comes from the cluster, so it can never drift out of sync."""
    cluster = models.ForeignKey(KubeCluster, on_delete=models.CASCADE, related_name="nodes")
    hostname = models.CharField(max_length=255)
    role = models.CharField(max_length=16, choices=NodeRole.choices, default=NodeRole.UNKNOWN, db_index=True)
    role_raw = models.CharField(max_length=60, blank=True)
    internal_ip = models.GenericIPAddressField("Internal IP", null=True, blank=True)
    external_ip = models.GenericIPAddressField("External IP", null=True, blank=True)
    exposure = models.CharField(max_length=10, choices=Exposure.choices, default=Exposure.UNKNOWN, db_index=True)
    kubernetes_version = models.CharField(max_length=40, blank=True)
    os = models.CharField("OS", max_length=120, blank=True)
    kernel = models.CharField(max_length=120, blank=True)
    container_runtime = models.CharField(max_length=120, blank=True)
    architecture = models.CharField(max_length=40, blank=True)

    cpu_capacity_millicores = models.PositiveIntegerField(null=True, blank=True)
    cpu_capacity_raw = models.CharField(max_length=40, blank=True)
    cpu_allocatable_millicores = models.PositiveIntegerField(null=True, blank=True)
    cpu_allocatable_raw = models.CharField(max_length=40, blank=True)
    memory_capacity_bytes = models.BigIntegerField(null=True, blank=True)
    memory_capacity_raw = models.CharField(max_length=40, blank=True)
    memory_allocatable_bytes = models.BigIntegerField(null=True, blank=True)
    memory_allocatable_raw = models.CharField(max_length=40, blank=True)
    pod_capacity = models.PositiveIntegerField(null=True, blank=True)
    pod_capacity_raw = models.CharField(max_length=40, blank=True)

    ready = models.BooleanField(null=True, blank=True)
    schedulable = models.BooleanField(null=True, blank=True)
    age_days = models.PositiveIntegerField(null=True, blank=True)
    age_raw = models.CharField(max_length=40, blank=True)
    labels = models.JSONField(default=dict, blank=True)
    taints = models.JSONField(default=list, blank=True)
    taints_raw = models.TextField(blank=True)

    ci = models.ForeignKey("cmdb.ConfigurationItem", on_delete=models.SET_NULL, null=True, blank=True,
                            related_name="k8s_nodes", verbose_name="Configuration item")
    device = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="k8s_nodes")
    member = models.ForeignKey("compute.ClusterMember", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="k8s_nodes")

    class Meta:
        ordering = ["cluster__name", "hostname"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "cluster", "hostname"],
                                               name="k8s_node_tenant_cluster_host_unique")]
        indexes = [models.Index(fields=["tenant_key", "role"]), models.Index(fields=["tenant_key", "exposure"])]

    def __str__(self):
        return self.hostname

    def get_absolute_url(self):
        return reverse("k8s:node_detail", args=[self.pk])

    @property
    def site(self):
        return self.cluster.site if self.cluster_id else GLOBAL_SITE

    @property
    def site_label(self):
        return self.site or GLOBAL_SITE

    @property
    def cpu_cores(self):
        m = self.cpu_capacity_millicores
        return None if m is None else m / 1000.0

    @property
    def cpu_allocatable_cores(self):
        m = self.cpu_allocatable_millicores
        return None if m is None else m / 1000.0

    @property
    def is_control_plane(self):
        return self.role == NodeRole.CONTROL_PLANE

    @property
    def label_pairs(self):
        return sorted((self.labels or {}).items())

    @property
    def linked(self):
        """The inventory records this node was matched to (for the detail page)."""
        return [("Configuration item", self.ci), ("Network device", self.device), ("Cluster member", self.member)]


# ----------------------------------------------------------------------------- namespaces
class Namespace(SiteScopedModel, ImportedModel):
    cluster = models.ForeignKey(KubeCluster, on_delete=models.CASCADE, related_name="namespaces")
    name = models.CharField(max_length=253)
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="k8s_namespaces")
    client_label = models.CharField(max_length=150, blank=True,
                                     help_text="Client string exactly as the workbook recorded it.")
    environment = models.CharField(max_length=40, blank=True)
    confidence = models.CharField(max_length=10, choices=Confidence.choices, default=Confidence.UNKNOWN)
    evidence = models.CharField(max_length=300, blank=True)
    mapping_status = models.CharField(max_length=10, choices=MappingStatus.choices, default=MappingStatus.UNKNOWN,
                                       db_index=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "cluster", "name"],
                                               name="k8s_namespace_tenant_cluster_name_unique")]
        indexes = [models.Index(fields=["tenant_key", "mapping_status"])]

    def __str__(self):
        return self.name

    @property
    def needs_review(self):
        return self.mapping_status == MappingStatus.REVIEW

    @property
    def client_display(self):
        """The linked client, else the workbook's string, else nothing — never a placeholder."""
        if self.client_id:
            return self.client.name
        return self.client_label or ""


# ----------------------------------------------------------------------------- client profiles
class ClientProfile(SiteScopedModel, ImportedModel):
    """One workbook client row (02_Client_Master), enriched by the roll-up sheet (13_Client_Summary)."""
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="k8s_profiles",
                                help_text="Matching LinkedEye client, when one was found by code or name.")
    workbook_name = models.CharField(max_length=150)
    workbook_id = models.CharField(max_length=40, blank=True, help_text="Client ID in the workbook, e.g. CL-001.")
    project_names = models.JSONField(default=list, blank=True)
    environments = models.JSONField(default=list, blank=True)
    primary_namespace = models.CharField(max_length=253, blank=True)

    app_count = models.PositiveIntegerField(null=True, blank=True)
    service_count = models.PositiveIntegerField(null=True, blank=True)
    ingress_count = models.PositiveIntegerField(null=True, blank=True)
    pv_count = models.PositiveIntegerField(null=True, blank=True)
    pvc_count = models.PositiveIntegerField(null=True, blank=True)
    total_storage_bytes = models.BigIntegerField(null=True, blank=True)
    total_storage_raw = models.CharField(max_length=40, blank=True)

    primary_url = models.URLField(max_length=500, blank=True)
    monitoring_url = models.URLField(max_length=500, blank=True)
    cicd_url = models.URLField(max_length=500, blank=True)

    status = models.CharField(max_length=10, choices=Health.choices, default=Health.UNKNOWN, db_index=True)
    status_raw = models.CharField(max_length=20, blank=True)
    remarks = models.TextField(blank=True)

    # roll-up from 13_Client_Summary (a per-client-group view of the same estate)
    summary_namespaces = models.PositiveIntegerField(null=True, blank=True)
    summary_applications = models.PositiveIntegerField(null=True, blank=True)
    summary_urls = models.PositiveIntegerField(null=True, blank=True)
    summary_storage_bytes = models.BigIntegerField(null=True, blank=True)
    summary_storage_raw = models.CharField(max_length=40, blank=True)
    monitoring_tools = models.JSONField(default=list, blank=True)
    cicd_tools = models.JSONField(default=list, blank=True)
    open_issues = models.PositiveIntegerField(null=True, blank=True)
    health = models.CharField(max_length=10, choices=Health.choices, default=Health.UNKNOWN)
    health_raw = models.CharField(max_length=20, blank=True)
    summary_only = models.BooleanField(default=False,
                                        help_text="Set when the roll-up sheet named a group that the client "
                                                  "master sheet does not list (for example an 'unmapped' group).")

    class Meta:
        ordering = ["workbook_name"]
        verbose_name = "client profile"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "workbook_name"],
                                               name="k8s_clientprofile_tenant_name_unique")]

    def __str__(self):
        return self.workbook_name

    def get_absolute_url(self):
        return f"{reverse('k8s:namespaces')}?client={self.workbook_name}"

    @property
    def display_name(self):
        return self.client.name if self.client_id else self.workbook_name

    @property
    def status_tone(self):
        return HEALTH_TONES.get(self.status, "mute")

    @property
    def health_tone(self):
        return HEALTH_TONES.get(self.health, "mute")

    @property
    def worst_tone(self):
        order = ["crit", "warn", "ok", "mute"]
        tones = [self.status_tone, self.health_tone]
        return min(tones, key=order.index)

    @property
    def links(self):
        return [("Application", self.primary_url), ("Monitoring", self.monitoring_url), ("CI/CD", self.cicd_url)]


# Workloads, services and volumes (sheets 04, 05, 07) live in their own module; imported last so they can use the bases above.
from .models_workload import KubeService, Volume, Workload  # noqa: E402,F401
# Ingress rules, certificates and Secret-name references (sheets 06, 09, 08).
from .models_ingress import Certificate, Ingress, SecretRef  # noqa: E402,F401
# Monitoring / CI/CD tools and the issue register (sheets 10, 11, 12).
from .models_ops import InventoryIssue, ToolInstance  # noqa: E402,F401
