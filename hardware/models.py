"""Server hardware over Redfish (Dell iDRAC, HPE iLO): READ-ONLY.

Site scoping: a ``BmcEndpoint`` belongs to exactly one ``network.Device`` (physical server) and has NO site of its own. The
device's site is the single source of truth, so the site name is deliberately NOT in ``administration.services.SITE_NAME_FIELDS``
(a rename of the site follows through the device, and nothing here can drift). Components, readings, events and control
requests belong to one endpoint and are scoped through ``endpoint__device__site`` (a test keeps it that way).

Nothing a BMC sends is trusted: names, messages and values are length-capped and stripped of control characters by
``hardware.parsers`` before they are stored, and templates escape them.

LinkedEye never writes to a BMC. ``ControlRequest`` records who asked for what and who decided; an approval is a record
and a reminder of the manual step, nothing more.
"""
from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

OK, WARNING, CRITICAL, UNKNOWN = "ok", "warning", "critical", "unknown"
STATUS_CHOICES = [(OK, "OK"), (WARNING, "Warning"), (CRITICAL, "Critical"), (UNKNOWN, "Unknown")]

#: severity order used for roll-ups (unknown never outranks a real warning)
RANK = {UNKNOWN: 0, OK: 1, WARNING: 2, CRITICAL: 3}


def _tenant():
    return settings.LINKEDEYE_TENANT


class BmcEndpoint(models.Model):
    """The Redfish management controller (iDRAC / iLO) of one physical server."""
    VERIFY_VERIFY, VERIFY_PIN, VERIFY_SKIP = "verify", "pin", "skip"
    VERIFY_CHOICES = [(VERIFY_VERIFY, "Verify against the system CA bundle"),
                      (VERIFY_PIN, "Pin the certificate fingerprint (recommended for self-signed BMCs)"),
                      (VERIFY_SKIP, "Do not verify the certificate (unsafe)")]
    DELL, HPE, GENERIC = "dell", "hpe", "generic"
    VENDOR_CHOICES = [(DELL, "Dell iDRAC"), (HPE, "HPE iLO"), (GENERIC, "Generic Redfish")]
    VENDOR_LABELS = dict(VENDOR_CHOICES)

    CONN_NEVER, CONN_OK, CONN_UNREACHABLE, CONN_AUTH = "never", "connected", "unreachable", "auth_failed"
    CONN_CHOICES = [(CONN_NEVER, "Not polled yet"), (CONN_OK, "Connected"), (CONN_UNREACHABLE, "Unreachable"),
                    (CONN_AUTH, "Auth failed")]

    HEALTH_CHOICES = [("healthy", "Healthy"), (WARNING, "Warning"), (CRITICAL, "Critical"), (UNKNOWN, "Unknown")]

    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    device = models.OneToOneField("network.Device", on_delete=models.CASCADE, related_name="bmc")
    enabled = models.BooleanField(default=True)
    #: marks the synthetic demo endpoint made by ``seed_hardware``: never polled, never emits events
    is_example = models.BooleanField(default=False, editable=False)

    # where and how to talk to it
    host = models.CharField("BMC host", max_length=255, help_text="Host name or IP address of the iDRAC / iLO (not a URL).")
    port = models.PositiveIntegerField(default=443)
    insecure_http = models.BooleanField(
        "Use plain HTTP", default=False,
        help_text="Credentials would cross the network unencrypted. Off unless a lab BMC has no TLS at all.")
    verify_mode = models.CharField(max_length=8, choices=VERIFY_CHOICES, default=VERIFY_PIN)
    pinned_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    pinned_at = models.DateTimeField(null=True, blank=True, editable=False)
    #: onboarding.Credential id (credential vault) holding the user name and password; not a foreign key on purpose
    credential_id = models.PositiveIntegerField(null=True, blank=True, editable=False)
    poll_interval_s = models.PositiveIntegerField(default=60)

    # vendor: detected from Redfish, the user may override it
    vendor = models.CharField(max_length=10, blank=True, editable=False)
    vendor_override = models.CharField(max_length=10, blank=True, choices=VENDOR_CHOICES)

    # polling state (written by the poll job)
    conn_state = models.CharField(max_length=12, choices=CONN_CHOICES, default=CONN_NEVER)
    last_polled_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_ok_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=300, blank=True, editable=False)
    consecutive_failures = models.PositiveIntegerField(default=0, editable=False)
    #: failed logins in a row; polling stops at 3 so a wrong password never locks the BMC account
    auth_failures = models.PositiveIntegerField(default=0, editable=False)
    next_poll_at = models.DateTimeField(null=True, blank=True, editable=False, db_index=True)
    refresh_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    inventory_at = models.DateTimeField(null=True, blank=True, editable=False)

    # what the last successful poll reported
    overview = models.JSONField(default=dict, blank=True, editable=False)
    sections = models.JSONField(default=dict, blank=True, editable=False)
    power_state = models.CharField(max_length=20, blank=True, editable=False)
    health = models.CharField(max_length=10, choices=HEALTH_CHOICES, default=UNKNOWN, editable=False)
    #: the state the last event was emitted for: "" | ok | warning | critical | unreachable | auth_failed
    alert_state = models.CharField(max_length=12, blank=True, default="", editable=False)
    health_changed_at = models.DateTimeField(null=True, blank=True, editable=False)

    # per-endpoint alert configuration ("Configure alerts")
    alerts_enabled = models.BooleanField("Raise events on health changes", default=True)
    temp_warn_c = models.FloatField("Temperature warning at (°C)", null=True, blank=True)
    temp_crit_c = models.FloatField("Temperature critical at (°C)", null=True, blank=True)
    fan_warn_rpm = models.FloatField("Fan warning at or below (RPM)", null=True, blank=True)
    fan_crit_rpm = models.FloatField("Fan critical at or below (RPM)", null=True, blank=True)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["device__name"]
        indexes = [models.Index(fields=["tenant_key", "enabled", "next_poll_at"], name="hw_endpoint_due_idx")]

    def __str__(self):
        return f"BMC of {self.device_id}"

    # ---- derived
    @property
    def scheme(self):
        return "http" if self.insecure_http else "https"

    @property
    def base_url(self):
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        default = 80 if self.insecure_http else 443
        return f"{self.scheme}://{host}" + ("" if self.port == default else f":{self.port}")

    @property
    def effective_vendor(self):
        return self.vendor_override or self.vendor or self.GENERIC

    @property
    def vendor_label(self):
        return self.VENDOR_LABELS.get(self.effective_vendor, "Redfish")

    @property
    def bmc_name(self):
        """What the BMC is called in the UI: iDRAC, iLO or plain BMC."""
        return {self.DELL: "iDRAC", self.HPE: "iLO"}.get(self.effective_vendor, "BMC")

    @property
    def has_credential(self):
        return bool(self.credential_id)

    @property
    def auth_paused(self):
        return self.auth_failures >= 3

    @property
    def configured(self):
        """Enough to poll: a host and a stored login (and a pinned certificate when pinning is chosen)."""
        if not (self.host and self.credential_id):
            return False
        return self.verify_mode != self.VERIFY_PIN or bool(self.pinned_sha256)


class HwComponent(models.Model):
    """The current state of one sensor / PSU / fan / drive / DIMM / CPU / NIC / battery."""
    KINDS = [("temperature", "Temperature sensor"), ("fan", "Fan"), ("psu", "Power supply"), ("voltage", "Voltage sensor"),
             ("cpu", "Processor"), ("dimm", "Memory module"), ("memory", "Memory"), ("drive", "Drive"),
             ("storage", "Storage controller"), ("nic", "Network interface"), ("battery", "Battery"),
             ("intrusion", "Chassis intrusion"), ("system", "System")]
    #: kinds that get a time series in HwReading
    SERIES = ("temperature", "fan", "psu", "voltage")
    #: kinds shown on the inventory page
    INVENTORY = ("cpu", "dimm", "drive", "storage", "nic", "psu", "battery")

    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    endpoint = models.ForeignKey(BmcEndpoint, on_delete=models.CASCADE, related_name="components")
    kind = models.CharField(max_length=12, choices=KINDS)
    key = models.CharField(max_length=200)
    name = models.CharField(max_length=160)
    reading = models.FloatField(null=True, blank=True)
    unit = models.CharField(max_length=12, blank=True)
    warn_at = models.FloatField(null=True, blank=True)
    crit_at = models.FloatField(null=True, blank=True)
    low_warn = models.FloatField(null=True, blank=True)
    low_crit = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=8, choices=STATUS_CHOICES, default=UNKNOWN)
    #: what the BMC called it ("OK", "Warning", ...) and its State ("Enabled", ...), kept for the inventory page
    health = models.CharField(max_length=24, blank=True)
    state = models.CharField(max_length=24, blank=True)
    attrs = models.JSONField(default=dict, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["kind", "position", "name"]
        constraints = [models.UniqueConstraint(fields=["endpoint", "kind", "key"], name="hw_component_unique")]
        indexes = [models.Index(fields=["endpoint", "kind"], name="hw_component_kind_idx")]

    def __str__(self):
        return f"{self.kind}: {self.name}"


class HwReading(models.Model):
    """One point of a sensor time series (temperature, fan speed, PSU output, voltage)."""
    tenant_key = models.CharField(max_length=100, default=_tenant)
    component = models.ForeignKey(HwComponent, on_delete=models.CASCADE, related_name="readings")
    ts = models.DateTimeField(default=timezone.now)
    value = models.FloatField()

    class Meta:
        ordering = ["ts"]
        indexes = [models.Index(fields=["component", "ts"], name="hw_reading_comp_ts_idx"),
                   models.Index(fields=["ts"], name="hw_reading_ts_idx")]


class HwEvent(models.Model):
    """One entry of the BMC's own log (system event log, lifecycle log, integrated management log)."""
    INFO, WARN, CRIT = "info", "warning", "critical"
    SEVERITIES = [(INFO, "Info"), (WARN, "Warning"), (CRIT, "Critical")]

    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    endpoint = models.ForeignKey(BmcEndpoint, on_delete=models.CASCADE, related_name="events")
    entry_id = models.CharField(max_length=120)
    ts = models.DateTimeField()
    severity = models.CharField(max_length=8, choices=SEVERITIES, default=INFO)
    sensor = models.CharField(max_length=120, blank=True)
    message = models.CharField(max_length=500)
    log = models.CharField(max_length=40, blank=True)

    class Meta:
        ordering = ["-ts", "-id"]
        constraints = [models.UniqueConstraint(fields=["endpoint", "entry_id"], name="hw_event_unique")]
        indexes = [models.Index(fields=["endpoint", "-ts"], name="hw_event_ep_ts_idx")]


class ControlRequest(models.Model):
    """A power or firmware action somebody wants carried out. RECORDS ONLY: LinkedEye never sends it to the BMC."""
    PENDING, APPROVED, REJECTED, EXPIRED = "pending", "approved", "rejected", "expired"
    STATES = [(PENDING, "Pending"), (APPROVED, "Approved"), (REJECTED, "Rejected"), (EXPIRED, "Expired")]
    POWER_ON, GRACEFUL, FORCE_OFF, FIRMWARE = "power_on", "graceful_shutdown", "force_off", "update_firmware"
    ACTIONS = [(POWER_ON, "Power on"), (GRACEFUL, "Graceful shutdown"), (FORCE_OFF, "Force power off"),
               (FIRMWARE, "Update firmware")]
    ACTION_LABELS = dict(ACTIONS)
    LIFETIME_DAYS = 7

    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    endpoint = models.ForeignKey(BmcEndpoint, on_delete=models.CASCADE, related_name="requests")
    action = models.CharField(max_length=24, choices=ACTIONS)
    reason = models.CharField(max_length=300)
    state = models.CharField(max_length=10, choices=STATES, default=PENDING)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    requested_by_name = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    decided_by_name = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        indexes = [models.Index(fields=["endpoint", "state"], name="hw_request_state_idx")]

    @property
    def action_label(self):
        return self.ACTION_LABELS.get(self.action, self.action)


@receiver(post_delete, sender=BmcEndpoint)
def drop_login_with_endpoint(sender, instance, **kwargs):
    """The vault row holding the BMC login goes with the endpoint, however the endpoint is deleted (the Endpoints page, the
    server device being deleted, a site clean-up): an encrypted password must not outlive the thing it was for."""
    if instance.credential_id:
        from django.apps import apps
        apps.get_model("onboarding", "Credential").objects.filter(pk=instance.credential_id, tenant_key=instance.tenant_key).delete()
