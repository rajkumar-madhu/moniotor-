"""Edge exposure: ingress rules, certificates and the Secret *names* workloads reference.

Imported from ``k8s/models.py`` so Django registers the models under the ``k8s`` app.

* ``Ingress``     – one ingress rule (06_Ingress_URLs). A host name appears on several rows with different
                    paths, so a row is keyed on (namespace, ingress, host, path, backend service). The
                    pages group the rows by host name.
* ``Certificate`` – a cert-manager certificate (09_Certificate_Inventory). The number of days left is
                    **computed from the expiry at read time**: the workbook's "Days Remaining" column is a
                    snapshot of the day of discovery and goes stale, so it is not stored.
* ``SecretRef``   – a Kubernetes Secret **name** a workload references (08_Credentials_References).

Data rule for ``SecretRef``: it stores names and namespaces only. The model has no field that could hold a
secret value, the importer never reads a value column, and a test fails if a field is ever added whose name
suggests one. The same goes for the rest of this module: nothing here is a credential.
"""
import math
from datetime import timedelta

from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from .models import Exposure, ImportedModel, SiteScopedModel

# Certificate expiry bands, in days left. Anything under CRITICAL_DAYS is "critical", under WARNING_DAYS is
# "expiring" (the 30-day KPI on the pages and the overview), under NOTICE_DAYS is "soon".
CRITICAL_DAYS = 7
WARNING_DAYS = 30
NOTICE_DAYS = 90


class CertStatus(models.TextChoices):
    EXPIRED = "expired", "Expired"
    CRITICAL = "critical", "Under 7 days"
    EXPIRING = "expiring", "Under 30 days"
    SOON = "soon", "Under 90 days"
    OK = "ok", "Healthy"
    UNKNOWN = "unknown", "Expiry unknown"


CERT_TONES = {CertStatus.EXPIRED: "crit", CertStatus.CRITICAL: "crit", CertStatus.EXPIRING: "warn",
              CertStatus.SOON: "info", CertStatus.OK: "ok", CertStatus.UNKNOWN: "mute"}


def status_filter(status, now=None):
    """A ``Q`` selecting the certificates in `status`, for filtering in the database.

    Mirrors :meth:`Certificate.status` exactly (same boundaries), so a filter and the chip on the row never
    disagree.
    """
    now = now or timezone.now()
    day = timedelta(days=1)
    critical, warning, notice = (now + CRITICAL_DAYS * day, now + WARNING_DAYS * day, now + NOTICE_DAYS * day)
    return {
        CertStatus.UNKNOWN: Q(expiry__isnull=True),
        CertStatus.EXPIRED: Q(expiry__lte=now),
        CertStatus.CRITICAL: Q(expiry__gt=now, expiry__lt=critical),
        CertStatus.EXPIRING: Q(expiry__gte=critical, expiry__lt=warning),
        CertStatus.SOON: Q(expiry__gte=warning, expiry__lt=notice),
        CertStatus.OK: Q(expiry__gte=notice),
    }[status]


def under_days_filter(days, now=None):
    """Certificates that expire in under `days` days, including those already expired."""
    now = now or timezone.now()
    return Q(expiry__lt=now + timedelta(days=days))


# ----------------------------------------------------------------------------- ingress
class Ingress(SiteScopedModel, ImportedModel):
    """One ingress rule. ``site`` follows the namespace's site (set by the importer)."""
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, related_name="ingresses")
    environment = models.CharField(max_length=40, blank=True)
    application = models.CharField(max_length=253, blank=True)
    name = models.CharField("Ingress", max_length=253, blank=True)
    ingress_class = models.CharField(max_length=253, blank=True, db_index=True)
    hostname = models.CharField(max_length=253, blank=True, db_index=True)
    url = models.URLField(max_length=500, blank=True)
    ingress_ip = models.GenericIPAddressField("Ingress IP", null=True, blank=True)
    ip_type = models.CharField(max_length=10, choices=Exposure.choices, default=Exposure.UNKNOWN, db_index=True)
    path = models.CharField(max_length=500, blank=True)
    backend_service = models.CharField(max_length=253, blank=True)
    backend_port = models.CharField(max_length=40, blank=True, help_text="A number, or a named port.")
    tls = models.BooleanField(default=False, db_index=True)
    tls_secret = models.CharField("TLS secret", max_length=253, blank=True,
                                  help_text="The Secret's name. Its content is never read.")
    certificate_name = models.CharField(max_length=253, blank=True)
    status = models.CharField(max_length=40, blank=True)

    certificate = models.ForeignKey("k8s.Certificate", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="ingresses")
    url_check = models.ForeignKey("urlchecker.UrlCheck", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="k8s_ingresses")
    domain = models.ForeignKey("domainmonitor.Domain", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="k8s_ingresses")

    class Meta:
        ordering = ["hostname", "path", "name"]
        verbose_name = "ingress rule"
        constraints = [models.UniqueConstraint(
            fields=["tenant_key", "namespace", "name", "hostname", "path", "backend_service"],
            name="k8s_ingress_rule_unique")]
        indexes = [models.Index(fields=["tenant_key", "hostname"]), models.Index(fields=["tenant_key", "tls"])]

    def __str__(self):
        return f"{self.hostname or self.name}{self.path}"

    def get_absolute_url(self):
        return f"{reverse('k8s:ingresses')}?q={self.hostname}"

    @property
    def check_url(self):
        """The URL a probe would use: the recorded URL, else https://host for a TLS rule. Never a guess
        for a rule without a host."""
        if self.url:
            return self.url
        if self.hostname and "*" not in self.hostname:
            return f"{'https' if self.tls else 'http'}://{self.hostname}"
        return ""

    @property
    def is_https(self):
        return self.check_url.lower().startswith("https://")

    @property
    def missing_address(self):
        return "missing" in (self.status or "").lower()

    @property
    def backend(self):
        if not self.backend_service:
            return ""
        return f"{self.backend_service}:{self.backend_port}" if self.backend_port else self.backend_service


# ----------------------------------------------------------------------------- certificates
class Certificate(SiteScopedModel, ImportedModel):
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, related_name="certificates")
    domain = models.CharField(max_length=253, blank=True, db_index=True)
    name = models.CharField(max_length=253)
    tls_secret = models.CharField("TLS secret", max_length=253, blank=True,
                                  help_text="The Secret's name. Its content is never read.")
    issuer = models.CharField(max_length=253, blank=True)
    ready = models.BooleanField(null=True, blank=True)
    valid_from = models.DateTimeField(null=True, blank=True)
    expiry = models.DateTimeField(null=True, blank=True, db_index=True)
    risk = models.CharField(max_length=40, blank=True,
                            help_text="The workbook's risk label on the day of discovery. The live state is "
                                      "computed from the expiry — see `status`.")
    remarks = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["expiry", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "namespace", "name"],
                                               name="k8s_certificate_ns_name_unique")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return f"{reverse('k8s:certificates')}?q={self.name}"

    # ---- computed at read time, never stored
    def seconds_left(self, now=None):
        if self.expiry is None:
            return None
        return (self.expiry - (now or timezone.now())).total_seconds()

    def days_remaining(self, now=None):
        """Whole days left, counted towards zero (so "expired 2 days ago" is -2); None when the expiry is
        unknown. The status says whether it has expired — a certificate 5 hours past its expiry shows 0."""
        seconds = self.seconds_left(now)
        return None if seconds is None else math.trunc(seconds / 86400)

    def status(self, now=None):
        seconds = self.seconds_left(now)
        if seconds is None:
            return CertStatus.UNKNOWN
        if seconds <= 0:
            return CertStatus.EXPIRED
        days = seconds / 86400
        if days < CRITICAL_DAYS:
            return CertStatus.CRITICAL
        if days < WARNING_DAYS:
            return CertStatus.EXPIRING
        if days < NOTICE_DAYS:
            return CertStatus.SOON
        return CertStatus.OK

    def tone(self, now=None):
        if self.ready is False:
            return "crit"
        return CERT_TONES[self.status(now)]

    def lifetime_left_pct(self, now=None):
        """Share of the validity window still ahead (0–100), or None without both dates."""
        if self.expiry is None or self.valid_from is None or self.expiry <= self.valid_from:
            return None
        total = (self.expiry - self.valid_from).total_seconds()
        left = max(0.0, min(total, self.seconds_left(now)))
        return int(round(100 * left / total))


# ----------------------------------------------------------------------------- secret references
class SecretRef(SiteScopedModel, ImportedModel):
    """A reference from a workload to a Kubernetes Secret, by NAME.

    **This model never holds a secret value and must not grow a field that could.** The workbook is a
    read-only inventory: it lists which Secret objects exist and who references them, and the importer
    reads only the name, the namespace and the referencing workload. Where the credential lives and who
    owns it is for a vault or an operator to say — not for this table.
    """
    namespace = models.ForeignKey("k8s.Namespace", on_delete=models.CASCADE, related_name="secret_refs")
    environment = models.CharField(max_length=40, blank=True)
    application = models.CharField(max_length=253, blank=True)
    secret_name = models.CharField(max_length=253)
    secret_namespace = models.CharField(max_length=253, blank=True)
    referenced_by = models.CharField(max_length=253, blank=True, help_text="For example 'Deployment api'.")
    remarks = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["secret_namespace", "application", "secret_name"]
        verbose_name = "secret reference"
        constraints = [models.UniqueConstraint(
            fields=["tenant_key", "namespace", "application", "secret_name", "referenced_by"],
            name="k8s_secretref_unique")]
        indexes = [models.Index(fields=["tenant_key", "secret_name"])]

    def __str__(self):
        return f"{self.secret_namespace}/{self.secret_name}"
