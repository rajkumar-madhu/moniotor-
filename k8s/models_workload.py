"""Workloads, services and volumes of the Kubernetes inventory (workbook sheets 04, 05 and 07).

* ``Workload``    – one row per **container of one pod** of a workload, exactly as the application inventory
                    sheet lists them. A workload with three containers, or a Deployment with six pods, is
                    several rows; the key is (namespace, workload name, container, pod) so a re-import
                    updates in place. The detail page folds the rows of one workload back together.
* ``KubeService`` – one row per service **port** (the service sheet lists a multi-port service once per
                    port). Named ``KubeService`` so it never reads as a ServiceDesk or ITSM service.
* ``Volume``      – one PVC / PV pair with its capacity, status and risk. An unclaimed PV has no PVC and no
                    namespace, so ``namespace`` is nullable.

None of these models can hold a secret value: the only secret-adjacent data in the sheets are Kubernetes
Secret *names*, and those live in a different sheet this module does not read. The site comes from the
cluster (like ``Node``), so a row can never drift out of sync with the cluster it describes.
"""
from django.db import models
from django.urls import reverse

from .models import GLOBAL_SITE, HEALTH_TONES, Health, ImportedModel, TenantModel


# ----------------------------------------------------------------------------- choices
class WorkloadType(models.TextChoices):
    DEPLOYMENT = "Deployment", "Deployment"
    DAEMONSET = "DaemonSet", "DaemonSet"
    STATEFULSET = "StatefulSet", "StatefulSet"
    OTHER = "Other", "Other"


class PodStatus(models.TextChoices):
    RUNNING = "running", "Running"
    PENDING = "pending", "Pending"
    FAILED = "failed", "Failed"
    SUCCEEDED = "succeeded", "Succeeded"
    NO_POD = "no_pod", "No pod"
    UNKNOWN = "unknown", "Unknown"


POD_STATUS_TONES = {PodStatus.RUNNING: "ok", PodStatus.PENDING: "warn", PodStatus.FAILED: "crit",
                    PodStatus.SUCCEEDED: "info", PodStatus.NO_POD: "mute", PodStatus.UNKNOWN: "mute"}


class ServiceType(models.TextChoices):
    CLUSTER_IP = "ClusterIP", "ClusterIP"
    NODE_PORT = "NodePort", "NodePort"
    LOAD_BALANCER = "LoadBalancer", "LoadBalancer"
    EXTERNAL_NAME = "ExternalName", "ExternalName"
    OTHER = "Other", "Other"


class Risk(models.TextChoices):
    LOW = "low", "Low"
    MEDIUM = "medium", "Medium"
    HIGH = "high", "High"
    UNKNOWN = "unknown", "Not assessed"


RISK_TONES = {Risk.LOW: "ok", Risk.MEDIUM: "warn", Risk.HIGH: "crit", Risk.UNKNOWN: "mute"}


class SiteFromCluster(models.Model):
    """The site of a row is its cluster's site."""

    class Meta:
        abstract = True

    @property
    def site(self):
        return self.cluster.site if self.cluster_id else GLOBAL_SITE

    @property
    def site_label(self):
        return self.site or GLOBAL_SITE


# ----------------------------------------------------------------------------- workloads
class Workload(SiteFromCluster, TenantModel, ImportedModel):
    cluster = models.ForeignKey("k8s.KubeCluster", on_delete=models.CASCADE, related_name="workloads")
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, related_name="workloads")
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="k8s_workloads")
    client_label = models.CharField(max_length=150, blank=True,
                                    help_text="Client string exactly as the workbook recorded it.")
    environment = models.CharField(max_length=40, blank=True)
    project = models.CharField(max_length=253, blank=True)
    application = models.CharField(max_length=253, blank=True)

    workload_type = models.CharField(max_length=16, choices=WorkloadType.choices, default=WorkloadType.OTHER,
                                     db_index=True)
    type_raw = models.CharField(max_length=40, blank=True)
    workload_name = models.CharField(max_length=253)
    container = models.CharField(max_length=253, blank=True)
    pod_name = models.CharField(max_length=253, blank=True)
    desired_replicas = models.PositiveIntegerField(null=True, blank=True)
    ready_replicas = models.PositiveIntegerField(null=True, blank=True)
    health = models.CharField(max_length=10, choices=Health.choices, default=Health.UNKNOWN, db_index=True)
    health_raw = models.CharField(max_length=20, blank=True)

    node = models.ForeignKey("k8s.Node", on_delete=models.SET_NULL, null=True, blank=True,
                             related_name="workloads")
    node_name = models.CharField(max_length=255, blank=True, help_text="Node host name as the workbook recorded it.")

    image = models.CharField(max_length=600, blank=True)
    image_registry = models.CharField(max_length=253, blank=True, db_index=True)
    image_tag = models.CharField(max_length=128, blank=True)
    image_digest = models.CharField(max_length=100, blank=True)
    image_latest = models.BooleanField(default=False, db_index=True,
                                       help_text="The image is the moving 'latest' tag (or has no tag at all).")

    ports = models.JSONField(default=list, blank=True)
    url = models.URLField(max_length=500, blank=True)
    urls = models.JSONField(default=list, blank=True)
    storage_attached = models.JSONField(default=list, blank=True,
                                        help_text="Names of the PVCs the workload mounts.")

    pod_status = models.CharField(max_length=10, choices=PodStatus.choices, default=PodStatus.UNKNOWN,
                                  db_index=True)
    pod_status_raw = models.CharField(max_length=20, blank=True)
    pod_ip = models.GenericIPAddressField("Pod IP", null=True, blank=True)

    class Meta:
        ordering = ["namespace__name", "workload_name", "container", "pod_name"]
        verbose_name = "workload"
        constraints = [models.UniqueConstraint(
            fields=["tenant_key", "namespace", "workload_name", "container", "pod_name"],
            name="k8s_workload_tenant_key_unique")]
        indexes = [models.Index(fields=["tenant_key", "cluster"]),
                   models.Index(fields=["tenant_key", "client_label"]),
                   models.Index(fields=["tenant_key", "workload_name"])]

    def __str__(self):
        return f"{self.workload_name}/{self.container}" if self.container else self.workload_name

    def get_absolute_url(self):
        return reverse("k8s:workload_detail", args=[self.pk])

    @property
    def replicas_label(self):
        """'ready/desired'; a side the discovery did not record shows as a dash, never as zero."""
        if self.ready_replicas is None and self.desired_replicas is None:
            return "–"
        ready = "–" if self.ready_replicas is None else self.ready_replicas
        desired = "–" if self.desired_replicas is None else self.desired_replicas
        return f"{ready}/{desired}"

    @property
    def is_short(self):
        """Fewer replicas ready than wanted. Unknown on either side is *not* short."""
        return (self.ready_replicas is not None and self.desired_replicas is not None
                and self.ready_replicas < self.desired_replicas)

    @property
    def health_tone(self):
        return HEALTH_TONES.get(self.health, "mute")

    @property
    def pod_tone(self):
        return POD_STATUS_TONES.get(self.pod_status, "mute")

    @property
    def pod_status_label(self):
        return self.get_pod_status_display()

    @property
    def is_pinned(self):
        return bool(self.image_digest)

    @property
    def ports_display(self):
        out = []
        for entry in self.ports or []:
            label = str(entry.get("port")) if entry.get("port") is not None else (entry.get("name") or "")
            if entry.get("protocol"):
                label = f"{label}/{entry['protocol']}"
            if label:
                out.append(label)
        return out

    @property
    def pvc_names(self):
        return list(self.storage_attached or [])


# ----------------------------------------------------------------------------- services
class KubeService(SiteFromCluster, TenantModel, ImportedModel):
    cluster = models.ForeignKey("k8s.KubeCluster", on_delete=models.CASCADE, related_name="services")
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, related_name="services")
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="k8s_services")
    client_label = models.CharField(max_length=150, blank=True)
    environment = models.CharField(max_length=40, blank=True)
    application = models.CharField(max_length=253, blank=True)
    name = models.CharField(max_length=253)
    service_type = models.CharField(max_length=16, choices=ServiceType.choices, default=ServiceType.OTHER,
                                    db_index=True)
    type_raw = models.CharField(max_length=40, blank=True)
    cluster_ip = models.GenericIPAddressField("Cluster IP", null=True, blank=True)
    headless = models.BooleanField(default=False, help_text="ClusterIP is 'None': a headless service.")
    external_ip = models.CharField("External IP", max_length=253, blank=True)
    loadbalancer_ip = models.CharField("LoadBalancer IP", max_length=253, blank=True)
    node_port = models.PositiveIntegerField(null=True, blank=True)
    port = models.PositiveIntegerField(null=True, blank=True)
    target_port = models.CharField(max_length=63, blank=True,
                                   help_text="A number, or the container port's name.")
    protocol = models.CharField(max_length=10, blank=True)
    selector = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["namespace__name", "name", "port"]
        verbose_name = "Kubernetes service"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "namespace", "name", "port", "protocol"],
                                               name="k8s_service_tenant_key_unique")]
        indexes = [models.Index(fields=["tenant_key", "cluster"]),
                   models.Index(fields=["tenant_key", "client_label"])]

    def __str__(self):
        return self.name

    @property
    def ports_label(self):
        """'80 → 8080/TCP' — the service port, then the container port it forwards to."""
        if self.port is None and not self.target_port:
            return ""
        left = "–" if self.port is None else str(self.port)
        right = self.target_port or "–"
        proto = f"/{self.protocol}" if self.protocol else ""
        return f"{left} → {right}{proto}"

    @property
    def selector_pairs(self):
        return sorted((self.selector or {}).items())

    @property
    def selector_text(self):
        return ", ".join(f"{k}={v}" for k, v in self.selector_pairs)

    @property
    def is_exposed(self):
        """Reachable from outside the cluster (NodePort / LoadBalancer / ExternalName)."""
        return self.service_type in (ServiceType.NODE_PORT, ServiceType.LOAD_BALANCER, ServiceType.EXTERNAL_NAME)


# ----------------------------------------------------------------------------- volumes
class Volume(SiteFromCluster, TenantModel, ImportedModel):
    cluster = models.ForeignKey("k8s.KubeCluster", on_delete=models.CASCADE, related_name="volumes")
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, null=True, blank=True,
                                  related_name="volumes")
    client = models.ForeignKey("core.Client", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="k8s_volumes")
    client_label = models.CharField(max_length=150, blank=True)
    environment = models.CharField(max_length=40, blank=True)
    application = models.CharField(max_length=253, blank=True)

    pvc = models.CharField("PVC", max_length=253, blank=True)
    pvc_status = models.CharField("PVC status", max_length=20, blank=True, db_index=True)
    pv = models.CharField("PV", max_length=253, blank=True)
    pv_status = models.CharField("PV status", max_length=20, blank=True, db_index=True)
    capacity_bytes = models.BigIntegerField(null=True, blank=True)
    capacity_raw = models.CharField(max_length=40, blank=True)
    requested_bytes = models.BigIntegerField(null=True, blank=True)
    requested_raw = models.CharField(max_length=40, blank=True)
    access_mode = models.CharField(max_length=40, blank=True)
    storage_class = models.CharField(max_length=253, blank=True, db_index=True)
    reclaim_policy = models.CharField(max_length=40, blank=True)
    volume_mode = models.CharField(max_length=40, blank=True)
    provisioner = models.CharField(max_length=253, blank=True)
    workload_ref = models.TextField(blank=True, help_text="Workloads that mount it, as 'Kind/name' pairs.")
    pod = models.CharField(max_length=253, blank=True)
    mount_path = models.CharField(max_length=500, blank=True)
    age_days = models.PositiveIntegerField(null=True, blank=True)
    age_raw = models.CharField(max_length=40, blank=True)
    risk = models.CharField(max_length=10, choices=Risk.choices, default=Risk.UNKNOWN, db_index=True)
    risk_raw = models.CharField(max_length=20, blank=True)
    remarks = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["namespace__name", "pvc", "pv"]
        verbose_name = "volume"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "cluster", "namespace", "pvc", "pv"],
                                               name="k8s_volume_tenant_key_unique")]
        indexes = [models.Index(fields=["tenant_key", "cluster"]),
                   models.Index(fields=["tenant_key", "client_label"])]

    def __str__(self):
        return self.pvc or self.pv

    @property
    def is_pending(self):
        return (self.pvc_status or "").lower() == "pending"

    @property
    def is_unclaimed(self):
        """A PV nobody claims: it has no PVC, or the cluster reports it Available / Released."""
        if not self.pv:
            return False
        return not self.pvc or (self.pv_status or "").lower() in ("available", "released")

    @property
    def risk_tone(self):
        return RISK_TONES.get(self.risk, "mute")

    @property
    def workload_refs(self):
        return [part.strip() for part in (self.workload_ref or "").split(",") if part.strip()]
