"""Compute & virtualization.

Physical servers and virtual machines are NOT stored here: they are network.Device rows with
device_type "physical_server" / "virtual_machine". This app adds what the device inventory lacks:

* Cluster        – a VMware / Proxmox / Hyper-V / Kubernetes / other cluster, scoped to a site
* ClusterMember  – a device (host, control-plane, worker…) that belongs to a cluster
* VMPlacement    – where a VM runs (host, cluster) and what it was given (vCPU, RAM, disk, power state)

Child rows (members, placements) carry no site of their own: a member belongs to its cluster's site and a
placement to its VM's site, and every query scopes them through that parent (cluster__site / vm__site), so a
site can never drift out of sync with the record it describes.
"""
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse


def _tenant():
    return settings.LINKEDEYE_TENANT


GLOBAL_SITES = ("", "All Sites")


def is_global(site):
    return (site or "") in GLOBAL_SITES


# ----------------------------------------------------------------------------- Prometheus label selector
LABEL_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")
# a value is a plain double-quoted string: no quotes, backslashes, braces or control characters, so the
# selector can never close the {...} it is embedded in or smuggle in another matcher / expression
PAIR_RE = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"([^"\\{}\x00-\x1f\x7f]{1,120})"\s*')
MAX_PAIRS = 6


def parse_selector(value):
    """'cluster="prod-k8s", env="prod"' -> [("cluster", "prod-k8s"), ("env", "prod")].

    Only equality matchers on plain label names are accepted (no =~, !=, !~, no __name__ or other reserved
    labels, no duplicates). Raises ValidationError with a user-facing message."""
    value = (value or "").strip()
    if not value:
        return []
    if value.startswith("{") and value.endswith("}"):
        value = value[1:-1].strip()
    if len(value) > 300:
        raise ValidationError("The selector is too long (300 characters at most).")
    pairs, pos = [], 0
    while pos < len(value):
        m = PAIR_RE.match(value, pos)
        if not m:
            raise ValidationError('Use label="value" pairs separated by commas, e.g. cluster="prod-k8s". '
                                  "Only = matchers are allowed; values cannot contain quotes, backslashes or braces.")
        name, val = m.group(1), m.group(2)
        if name.startswith("__"):
            raise ValidationError(f"“{name}” is a reserved Prometheus label and cannot be used in a selector.")
        if any(name == n for n, _v in pairs):
            raise ValidationError(f"The label “{name}” appears more than once.")
        pairs.append((name, val))
        pos = m.end()
        if pos < len(value):
            if value[pos] != ",":
                raise ValidationError('Separate label="value" pairs with commas.')
            pos += 1
            if pos >= len(value) or not value[pos:].strip():
                raise ValidationError("The selector ends with a comma.")
    if len(pairs) > MAX_PAIRS:
        raise ValidationError(f"Use at most {MAX_PAIRS} label pairs.")
    return pairs


def format_selector(pairs):
    return ",".join(f'{k}="{v}"' for k, v in pairs)


def validate_selector(value):
    parse_selector(value)


# ----------------------------------------------------------------------------- choices
class ClusterKind(models.TextChoices):
    VMWARE = "vmware", "VMware vSphere"
    PROXMOX = "proxmox", "Proxmox VE"
    HYPERV = "hyperv", "Hyper-V"
    KUBERNETES = "kubernetes", "Kubernetes"
    OTHER = "other", "Other"


HYPERVISOR_KINDS = (ClusterKind.VMWARE, ClusterKind.PROXMOX, ClusterKind.HYPERV, ClusterKind.OTHER)


class MemberRole(models.TextChoices):
    HOST = "host", "Hypervisor host"
    CONTROL_PLANE = "control-plane", "Control plane"
    WORKER = "worker", "Worker"
    STORAGE = "storage", "Storage"
    OTHER = "other", "Other"


class PowerState(models.TextChoices):
    RUNNING = "running", "Running"
    STOPPED = "stopped", "Stopped"
    SUSPENDED = "suspended", "Suspended"


class K8sState(models.TextChoices):
    OK = "ok", "Healthy"
    WARNING = "warning", "Degraded"
    UNKNOWN = "unknown", "Not collected"


# ----------------------------------------------------------------------------- models
class Cluster(models.Model):
    tenant_key = models.CharField(max_length=64, db_index=True, default=_tenant)
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=12, choices=ClusterKind.choices, default=ClusterKind.VMWARE, db_index=True)
    site = models.CharField(max_length=100, blank=True, default="All Sites", db_index=True)
    description = models.TextField(blank=True, max_length=2000)
    tags = models.CharField(max_length=200, blank=True, help_text="Comma-separated, e.g. production, example")
    prometheus_selector = models.CharField(
        "Prometheus selector", max_length=300, blank=True, validators=[validate_selector],
        help_text='Label matchers that select this cluster\'s series, e.g. cluster="prod-k8s". '
                  'Only label="value" pairs are allowed.')

    # kube-state-metrics summary cached by the compute.k8s job (kubernetes clusters only)
    k8s_summary = models.JSONField(default=dict, blank=True)
    k8s_state = models.CharField(max_length=10, choices=K8sState.choices, default=K8sState.UNKNOWN)
    k8s_checked_at = models.DateTimeField(null=True, blank=True)
    k8s_error = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="compute_cluster_tenant_name_unique")]
        indexes = [models.Index(fields=["tenant_key", "kind"])]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("compute:cluster_detail", args=[self.pk])

    @property
    def is_kubernetes(self):
        return self.kind == ClusterKind.KUBERNETES

    @property
    def selector_pairs(self):
        try:
            return parse_selector(self.prometheus_selector)
        except ValidationError:
            return []

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def is_example(self):
        return "example" in [t.lower() for t in self.tag_list]


class ClusterMember(models.Model):
    tenant_key = models.CharField(max_length=64, db_index=True, default=_tenant)
    cluster = models.ForeignKey(Cluster, on_delete=models.CASCADE, related_name="members")
    device = models.ForeignKey("network.Device", on_delete=models.CASCADE, related_name="compute_memberships")
    role = models.CharField(max_length=16, choices=MemberRole.choices, default=MemberRole.HOST)
    cpu_cores = models.PositiveIntegerField(
        "Host CPU cores", null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(4096)],
        help_text="Physical cores / threads available to guests. Leave empty to use node_exporter when available.")
    memory_gb = models.DecimalField(
        "Host memory (GB)", max_digits=9, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(1), MaxValueValidator(65536)],
        help_text="Leave empty to use node_exporter when available.")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["cluster__name", "device__name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "cluster", "device"],
                                               name="compute_member_tenant_cluster_device_unique")]

    def __str__(self):
        return f"{self.device.name} in {self.cluster.name}"


class VMPlacement(models.Model):
    tenant_key = models.CharField(max_length=64, db_index=True, default=_tenant)
    vm = models.ForeignKey("network.Device", on_delete=models.CASCADE, related_name="compute_placements",
                           verbose_name="Virtual machine")
    host = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="compute_guests", verbose_name="Host")
    cluster = models.ForeignKey(Cluster, on_delete=models.SET_NULL, null=True, blank=True, related_name="placements")
    vcpus = models.PositiveIntegerField("vCPUs", null=True, blank=True,
                                        validators=[MinValueValidator(1), MaxValueValidator(768)])
    memory_gb = models.DecimalField("Memory (GB)", max_digits=9, decimal_places=2, null=True, blank=True,
                                    validators=[MinValueValidator(0.25), MaxValueValidator(24576)])
    disk_gb = models.DecimalField("Disk (GB)", max_digits=10, decimal_places=1, null=True, blank=True,
                                  validators=[MinValueValidator(0), MaxValueValidator(1048576)])
    power_state = models.CharField(max_length=10, choices=PowerState.choices, default=PowerState.RUNNING, db_index=True)
    os = models.CharField("Guest OS", max_length=120, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vm__name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "vm"], name="compute_placement_tenant_vm_unique")]

    def __str__(self):
        return f"{self.vm.name} placement"
