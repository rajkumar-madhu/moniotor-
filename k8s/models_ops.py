"""Operations data of the Kubernetes inventory: the tools that run in the estate and the issues found in it.

Imported from ``k8s/models.py`` so Django registers the models under the ``k8s`` app.

* ``ToolInstance``   – one monitoring, CI/CD or platform tool as the discovery saw it: Prometheus, Grafana,
                       Argo CD, Harbor, Vault … (10_Monitoring and 11_CICD_Platform_Tools). A tool that runs
                       in several namespaces is several rows; the pages group them by tool.
* ``InventoryIssue`` – one problem the discovery flagged (12_Issues_Risks), keyed on the workbook's issue id.

Data rules
----------
* ``ToolInstance.credential_reference`` is the **name** of the Kubernetes Secret the tool refers to. The
  model has no user-name, password, token or key field, the importer ignores the workbook's user-name
  column, and a test fails if a field is ever added whose name suggests a secret value.
* Placeholder cells ("NOT DISCOVERED" …) are stored as empty strings and shown as a dash.
* ``first_seen`` / ``last_seen`` come from :class:`k8s.models.ImportedModel`: the first import that contained
  the row and the newest one that still does.
"""
from django.db import models
from django.urls import reverse

from .models import ImportedModel, SiteScopedModel, TenantModel  # noqa: F401  (TenantModel: re-export)


# ----------------------------------------------------------------------------- tools
class ToolCategory(models.TextChoices):
    MONITORING = "monitoring", "Monitoring"
    CICD = "cicd", "CI/CD"
    PLATFORM = "platform", "Platform"


CATEGORY_ICONS = {ToolCategory.MONITORING: "activity", ToolCategory.CICD: "code", ToolCategory.PLATFORM: "layers"}

# What a monitoring tool does for a client. The monitoring page shows which clients have each capability.
CAPABILITIES = [
    ("metrics", "Metrics", "Prometheus"),
    ("dashboards", "Dashboards", "Grafana"),
    ("alerting", "Alerting", "Alertmanager"),
    ("logs", "Logs", "Loki, Elasticsearch, Kibana, Logstash"),
    ("probes", "Probes", "Uptime probes: Blackbox exporter"),
    ("hardware", "Hardware", "Hardware and network: iLO, iDRAC and SNMP exporters"),
    ("host", "Host", "Host metrics: Node exporter"),
]

# canonical tool name -> (category, capability). A name the table does not know keeps the sheet's category.
KNOWN_TOOLS = {
    "Prometheus": ("monitoring", "metrics"),
    "Grafana": ("monitoring", "dashboards"),
    "Alertmanager": ("monitoring", "alerting"),
    "Loki": ("monitoring", "logs"),
    "Elasticsearch": ("monitoring", "logs"),
    "Kibana": ("monitoring", "logs"),
    "Logstash": ("monitoring", "logs"),
    "Blackbox exporter": ("monitoring", "probes"),
    "iLO exporter": ("monitoring", "hardware"),
    "iDRAC exporter": ("monitoring", "hardware"),
    "SNMP exporter": ("monitoring", "hardware"),
    "Node exporter": ("monitoring", "host"),
    "Argo CD": ("cicd", ""),
    "Jenkins": ("cicd", ""),
    "Gitea": ("cicd", ""),
    "SonarQube": ("cicd", ""),
    "Nexus": ("cicd", ""),
    "Harbor": ("cicd", ""),
    "Vault": ("platform", ""),
    "Keycloak": ("platform", ""),
    "LDAP": ("platform", ""),
    "MinIO": ("platform", ""),
    "pgAdmin": ("platform", ""),
}

# spelling in the workbook (lower case, letters and digits only) -> canonical name
TOOL_ALIASES = {
    "prometheus": "Prometheus", "grafana": "Grafana", "alertmanager": "Alertmanager", "loki": "Loki",
    "elasticsearch": "Elasticsearch", "elastic": "Elasticsearch", "kibana": "Kibana", "logstash": "Logstash",
    "blackbox": "Blackbox exporter", "blackboxexporter": "Blackbox exporter",
    "ilo": "iLO exporter", "iloexporter": "iLO exporter", "idrac": "iDRAC exporter",
    "idracexporter": "iDRAC exporter", "snmp": "SNMP exporter", "snmpexporter": "SNMP exporter",
    "nodeexporter": "Node exporter", "node": "Node exporter",
    "argocd": "Argo CD", "argo": "Argo CD", "jenkins": "Jenkins", "gitea": "Gitea", "sonarqube": "SonarQube",
    "sonar": "SonarQube", "nexus": "Nexus", "harbor": "Harbor",
    "vault": "Vault", "keycloak": "Keycloak", "ldap": "LDAP", "minio": "MinIO", "pgadmin": "pgAdmin",
}


def capability_of(tool):
    """'Prometheus' -> 'metrics'; '' for a tool that is not a monitoring capability."""
    return KNOWN_TOOLS.get(tool, ("", ""))[1]


class ToolInstance(SiteScopedModel, ImportedModel):
    """A tool running in a namespace. ``site`` follows the namespace's site (set by the importer)."""
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, related_name="tools")
    category = models.CharField(max_length=12, choices=ToolCategory.choices, db_index=True)
    tool = models.CharField(max_length=80, db_index=True, help_text="Normalised tool name, e.g. Prometheus.")
    tool_raw = models.CharField(max_length=80, blank=True, help_text="The tool name exactly as the sheet spelled it.")
    client_label = models.CharField(max_length=150, blank=True, db_index=True,
                                    help_text="Client string as the workbook recorded it.")
    environment = models.CharField(max_length=40, blank=True)
    service = models.CharField(max_length=253, blank=True)
    hostname = models.CharField(max_length=253, blank=True)
    url = models.URLField(max_length=500, blank=True)
    ports = models.JSONField(default=list, blank=True)
    storage = models.CharField(max_length=253, blank=True, help_text="Name of the volume claim, if any.")
    authentication = models.CharField(max_length=120, blank=True)
    credential_reference = models.CharField(
        max_length=253, blank=True,
        help_text="Name of the Kubernetes Secret the tool refers to. A name only — a value is never read.")
    status = models.CharField(max_length=40, blank=True)
    replica_count = models.PositiveIntegerField(null=True, blank=True)
    owner = models.CharField(max_length=150, blank=True)
    notes = models.CharField(max_length=300, blank=True)
    exporter = models.ForeignKey("observe.Exporter", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="k8s_tools")

    class Meta:
        ordering = ["category", "tool", "client_label", "namespace__name", "service"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "namespace", "category", "tool", "service"],
                                               name="k8s_tool_tenant_ns_cat_tool_svc_unique")]
        indexes = [models.Index(fields=["tenant_key", "category", "tool"])]

    def __str__(self):
        return f"{self.tool} · {self.service or self.namespace_id}"

    @property
    def capability(self):
        return capability_of(self.tool)

    @property
    def client_display(self):
        """The linked client, else the workbook's string — never a placeholder."""
        ns = self.namespace
        if ns.client_id:
            return ns.client.name
        return self.client_label or ns.client_label or ""

    @property
    def port_text(self):
        out = []
        for p in self.ports or []:
            label = str(p["port"]) if p.get("port") is not None else (p.get("name") or "")
            if label:
                out.append(label)
        return ", ".join(out)

    @property
    def is_unhealthy(self):
        """Anything the discovery did not call healthy, or a tool scaled to zero."""
        if self.replica_count == 0:
            return True
        return bool(self.status) and self.status.lower() not in ("healthy", "running", "ok", "ready")

    @property
    def status_tone(self):
        if self.replica_count == 0:
            return "crit"
        if not self.status:
            return "mute"
        return "ok" if not self.is_unhealthy else "warn"


# ----------------------------------------------------------------------------- issues
class Severity(models.TextChoices):
    HIGH = "high", "High"
    MEDIUM = "medium", "Medium"
    LOW = "low", "Low"


SEVERITY_ORDER = [Severity.HIGH, Severity.MEDIUM, Severity.LOW]
SEVERITY_TONES = {Severity.HIGH: "crit", Severity.MEDIUM: "warn", Severity.LOW: "info"}


class IssueStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


FINGERPRINT_PREFIX = "k8sinv:"


class InventoryIssue(SiteScopedModel, ImportedModel):
    """A problem the discovery flagged. ``site`` follows the namespace (else the cluster)."""
    cluster = models.ForeignKey("k8s.KubeCluster", on_delete=models.CASCADE, related_name="issues")
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="issues")
    issue_id = models.CharField(max_length=40, help_text="Issue id from the workbook, e.g. ISS-0001.")
    client_label = models.CharField(max_length=150, blank=True, db_index=True,
                                    help_text="Client string as the workbook recorded it.")
    environment = models.CharField(max_length=40, blank=True)
    severity = models.CharField(max_length=8, choices=Severity.choices, default=Severity.MEDIUM, db_index=True)
    category = models.CharField(max_length=60, blank=True, db_index=True, help_text="Pod, Deployment, Job …")
    resource = models.CharField(max_length=300, blank=True)
    issue = models.CharField(max_length=200, blank=True)
    impact = models.CharField(max_length=300, blank=True)
    evidence = models.CharField(max_length=500, blank=True)
    recommended_action = models.CharField(max_length=500, blank=True)
    owner = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=8, choices=IssueStatus.choices, default=IssueStatus.OPEN, db_index=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    incident = models.ForeignKey("servicedesk.Ticket", on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name="k8s_issues")

    class Meta:
        ordering = ["issue_id"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "issue_id"],
                                               name="k8s_issue_tenant_issue_id_unique")]
        indexes = [models.Index(fields=["tenant_key", "status", "severity"])]

    def __str__(self):
        return f"{self.issue_id} {self.issue}".strip()

    @property
    def fingerprint(self):
        return f"{FINGERPRINT_PREFIX}{self.issue_id}"

    @property
    def tone(self):
        return SEVERITY_TONES.get(self.severity, "mute")

    @property
    def is_open(self):
        return self.status == IssueStatus.OPEN

    @property
    def client_display(self):
        ns = self.namespace
        if ns is not None and ns.client_id:
            return ns.client.name
        return self.client_label or (ns.client_label if ns is not None else "") or ""

    def get_absolute_url(self):
        return f"{reverse('k8s:issues')}?q={self.issue_id}&status=all"
