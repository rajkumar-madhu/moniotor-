"""Hypervisor (vCenter), READ-ONLY: what a vSphere inventory says about the virtual machines LinkedEye already monitors.

LinkedEye reads vCenter (``hypervisor.vsphere``: GET only, plus the one documented session POST / DELETE) and records the
answer here. It never changes anything in vCenter: a "Power off" button creates a ``VmControlRequest`` that an operator
approves or rejects *inside LinkedEye*; approving records the decision and shows the manual step, nothing is sent.

Site scoping: a ``VCenterSource`` stores the site *name* in ``site`` (and is listed in
``administration.services.SITE_NAME_FIELDS``). Hosts, datastores, VM records, snapshots, events and requests carry no site
of their own: they belong to one source and are scoped through ``source__site`` (a matched VM is additionally scoped by its
device). A rename of the site therefore follows through the source and nothing can drift (a test keeps it that way).

Everything vCenter sends (names, notes, messages) is untrusted: ``hypervisor.parsers`` length-caps it and strips control
characters before it gets here, and templates escape it. None of it is ever used as a URL, a host or a path.
"""
from django.conf import settings
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.utils import timezone

GLOBAL_SITE = "All Sites"
EXAMPLE_MARK = "[example]"

TLS_VERIFY, TLS_PIN, TLS_SKIP = "verify", "pin", "skip"
TLS_CHOICES = [
    (TLS_VERIFY, "Verify the certificate (recommended)"),
    (TLS_PIN, "Pin the certificate fingerprint (SHA-256)"),
    (TLS_SKIP, "Do not verify (unsafe: anyone on the path can read the password)"),
]

#: a datastore crossing these used-space percentages raises an event (with 2 points of hysteresis on the way down)
DS_WARN_PCT, DS_CRIT_PCT = 90, 95
SNAPSHOT_REVIEW_DAYS = 7


def _tenant():
    return settings.LINKEDEYE_TENANT


class VCenterSource(models.Model):
    """One vCenter Server that LinkedEye reads. The login lives in the credential vault, never on this row."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    name = models.CharField(max_length=120)
    site = models.CharField(max_length=100, default=GLOBAL_SITE, db_index=True)
    description = models.CharField(max_length=300, blank=True)
    enabled = models.BooleanField(default=True)

    host = models.CharField("vCenter host", max_length=255, help_text="Host name or IP address only: no scheme, port or path.")
    port = models.PositiveIntegerField(default=443)
    #: onboarding.Credential id (credential vault) holding the read-only account; not a foreign key on purpose
    credential_id = models.PositiveIntegerField(null=True, blank=True, editable=False)
    tls_mode = models.CharField("Certificate check", max_length=8, choices=TLS_CHOICES, default=TLS_VERIFY)
    pinned_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    pending_sha256 = models.CharField(max_length=64, blank=True, editable=False)
    pending_seen_at = models.DateTimeField(null=True, blank=True, editable=False)
    sync_interval_s = models.PositiveIntegerField(default=300)
    use_provider = models.BooleanField(
        "Read snapshots and events with pyvmomi", default=False,
        help_text="Optional. Needs requirements-vsphere.txt; the stable REST API has no snapshots or event history.")

    # sync bookkeeping (written by hypervisor.sync)
    sync_requested_at = models.DateTimeField(null=True, blank=True, editable=False)
    sync_started_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_sync_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_ok_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=300, blank=True, editable=False)
    error_kind = models.CharField(max_length=12, blank=True, editable=False)
    consecutive_failures = models.PositiveIntegerField(default=0, editable=False)
    last_duration_ms = models.PositiveIntegerField(null=True, blank=True, editable=False)
    last_provider_error = models.CharField(max_length=300, blank=True, editable=False)
    provider_ok_at = models.DateTimeField(null=True, blank=True, editable=False)
    api_version = models.CharField(max_length=60, blank=True, editable=False)
    vm_count = models.PositiveIntegerField(default=0, editable=False)
    host_count = models.PositiveIntegerField(default=0, editable=False)
    datastore_count = models.PositiveIntegerField(default=0, editable=False)
    cluster_count = models.PositiveIntegerField(default=0, editable=False)
    alert_state = models.CharField(max_length=10, blank=True, editable=False)

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+", editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="hv_source_tenant_name_unique")]
        verbose_name = "vCenter source"

    def __str__(self):
        return self.name

    @property
    def is_example(self):
        return (self.description or "").startswith(EXAMPLE_MARK)

    @property
    def has_credential(self):
        return bool(self.credential_id)

    @property
    def base_url(self):
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"https://{host}" + ("" if int(self.port) == 443 else f":{int(self.port)}")

    @property
    def interval(self):
        return max(60, int(self.sync_interval_s or 300))

    def stale_after(self):
        return self.interval * 3 + 30

    @property
    def state(self):
        """(code, label): disabled / example / waiting / error / stale / connected."""
        if self.is_example:
            return "example", "Example data"
        if not self.enabled:
            return "disabled", "Disabled"
        if self.last_ok_at is None and not self.last_sync_at:
            return "waiting", "Waiting for the first sync"
        if self.consecutive_failures:
            return ("auth", "Authentication failed") if self.error_kind == "auth" else ("error", "Unreachable")
        if self.last_ok_at and (timezone.now() - self.last_ok_at).total_seconds() > self.stale_after():
            return "stale", "Stale"
        return "connected", "Connected"

    def vm_console_url(self, moref):
        """Link to this VM in the vCenter UI, built from the configured host and a validated moref only."""
        import re
        if not re.fullmatch(r"[A-Za-z0-9\-]{1,40}", moref or ""):
            return ""
        return f"{self.base_url}/ui/app/vm;nav=v/urn:vmomi:VirtualMachine:{moref}/summary"


class HvHost(models.Model):
    """An ESXi host as vCenter lists it (a physical-server device may be matched to it)."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(VCenterSource, on_delete=models.CASCADE, related_name="hosts")
    moref = models.CharField(max_length=40)
    name = models.CharField(max_length=255)
    connection_state = models.CharField(max_length=20, blank=True)
    power_state = models.CharField(max_length=20, blank=True)
    cluster_name = models.CharField(max_length=120, blank=True)
    cluster_moref = models.CharField(max_length=40, blank=True)
    device = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="hv_hosts")
    match_state = models.CharField(max_length=10, default="unmatched")
    match_reason = models.CharField(max_length=200, blank=True)
    seen_at = models.DateTimeField(default=timezone.now)
    missing_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["source", "moref"], name="hv_host_unique")]

    def __str__(self):
        return self.name


class Datastore(models.Model):
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(VCenterSource, on_delete=models.CASCADE, related_name="datastores")
    moref = models.CharField(max_length=40)
    name = models.CharField(max_length=255)
    ds_type = models.CharField(max_length=20, blank=True)
    capacity_bytes = models.BigIntegerField(null=True, blank=True)
    free_bytes = models.BigIntegerField(null=True, blank=True)
    accessible = models.BooleanField(null=True, blank=True)
    thin_provisioning = models.BooleanField(null=True, blank=True)
    alert_level = models.PositiveSmallIntegerField(default=0, editable=False)
    seen_at = models.DateTimeField(default=timezone.now)
    missing_since = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["source", "moref"], name="hv_datastore_unique")]

    def __str__(self):
        return self.name

    @property
    def used_bytes(self):
        if self.capacity_bytes is None or self.free_bytes is None:
            return None
        return max(0, self.capacity_bytes - self.free_bytes)

    @property
    def used_pct(self):
        if not self.capacity_bytes or self.used_bytes is None:
            return None
        return round(100.0 * self.used_bytes / self.capacity_bytes, 1)

    @property
    def free_pct(self):
        p = self.used_pct
        return None if p is None else round(100.0 - p, 1)

    @property
    def status(self):
        """(code, label): healthy / warning / critical / inaccessible / unknown."""
        if self.accessible is False:
            return "crit", "Inaccessible"
        p = self.used_pct
        if p is None:
            return "mute", "Not reported"
        if p >= DS_CRIT_PCT:
            return "crit", "Critical"
        if p >= DS_WARN_PCT:
            return "warn", "Warning"
        return "ok", "Healthy"


class VmInfo(models.Model):
    """What vCenter says about one virtual machine. ``device`` is the network.Device it was matched to (never silently)."""
    MATCHED, AMBIGUOUS, UNMATCHED, IGNORED = "matched", "ambiguous", "unmatched", "ignored"
    MATCH_STATES = [(MATCHED, "Matched"), (AMBIGUOUS, "Ambiguous"), (UNMATCHED, "Unmatched"), (IGNORED, "Ignored")]
    POWER = [("on", "Powered on"), ("off", "Powered off"), ("suspended", "Suspended"), ("unknown", "Unknown")]

    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    source = models.ForeignKey(VCenterSource, on_delete=models.CASCADE, related_name="vms")
    moref = models.CharField(max_length=40)
    name = models.CharField(max_length=255)
    device = models.OneToOneField("network.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="hv_vm")
    bios_uuid = models.CharField(max_length=64, blank=True, db_index=True)
    instance_uuid = models.CharField(max_length=64, blank=True)

    power_state = models.CharField(max_length=10, choices=POWER, default="unknown")
    cpu_count = models.PositiveIntegerField(null=True, blank=True)
    cores_per_socket = models.PositiveIntegerField(null=True, blank=True)
    memory_mib = models.PositiveBigIntegerField(null=True, blank=True)
    guest_id = models.CharField(max_length=60, blank=True)
    guest_full_name = models.CharField(max_length=200, blank=True)
    guest_family = models.CharField(max_length=20, blank=True)
    guest_hostname = models.CharField(max_length=255, blank=True)
    guest_ips = models.JSONField(default=list, blank=True)
    tools_run_state = models.CharField(max_length=20, blank=True)
    tools_version_status = models.CharField(max_length=30, blank=True)
    tools_version = models.CharField(max_length=40, blank=True)
    host_moref = models.CharField(max_length=40, blank=True)
    host_name = models.CharField(max_length=255, blank=True)
    cluster_name = models.CharField(max_length=120, blank=True)
    ha_status = models.CharField(max_length=20, blank=True)
    datastore_names = models.JSONField(default=list, blank=True)
    nics = models.JSONField(default=list, blank=True)
    disks = models.JSONField(default=list, blank=True)
    created_on = models.DateTimeField(null=True, blank=True)          # provider only (not in the stable REST API)
    description = models.CharField(max_length=500, blank=True)         # provider only

    match_state = models.CharField(max_length=10, choices=MATCH_STATES, default=UNMATCHED, db_index=True)
    match_reason = models.CharField(max_length=300, blank=True)
    match_candidates = models.JSONField(default=list, blank=True)
    matched_by = models.CharField(max_length=150, blank=True)
    matched_at = models.DateTimeField(null=True, blank=True)
    #: set when an operator unlinks the VM: the sync then leaves it alone (a wrong automatic match must not come straight back)
    auto_match_off = models.BooleanField(default=False, editable=False)

    alert_state = models.JSONField(default=dict, blank=True, editable=False)
    tools_bad_since = models.DateTimeField(null=True, blank=True, editable=False)
    placement_synced = models.JSONField(default=dict, blank=True, editable=False)
    seen_at = models.DateTimeField(default=timezone.now)
    missing_since = models.DateTimeField(null=True, blank=True)
    last_detail_at = models.DateTimeField(null=True, blank=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    snapshots_synced_at = models.DateTimeField(null=True, blank=True)
    events_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["source", "moref"], name="hv_vm_unique")]
        indexes = [models.Index(fields=["source", "match_state"])]

    def __str__(self):
        return self.name

    @property
    def memory_gb(self):
        return None if self.memory_mib is None else round(self.memory_mib / 1024, 2)

    @property
    def disk_bytes(self):
        sizes = [d.get("capacity_bytes") for d in (self.disks or []) if isinstance(d, dict)]
        sizes = [s for s in sizes if isinstance(s, int)]
        return sum(sizes) if sizes else None

    @property
    def power_label(self):
        return dict(self.POWER).get(self.power_state, "Unknown")

    @property
    def is_matched(self):
        return self.match_state == self.MATCHED and self.device_id is not None


class VmSnapshot(models.Model):
    """One snapshot (provider data: the stable REST API does not list snapshots)."""
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    vm = models.ForeignKey(VmInfo, on_delete=models.CASCADE, related_name="snapshots")
    snap_id = models.CharField(max_length=60)
    name = models.CharField(max_length=200)
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField()
    size_bytes = models.BigIntegerField(null=True, blank=True)
    is_current = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [models.UniqueConstraint(fields=["vm", "snap_id"], name="hv_snapshot_unique")]

    def __str__(self):
        return self.name

    def age_days(self, now=None):
        return max(0, ((now or timezone.now()) - self.created_at).days)

    def needs_review(self, now=None):
        return ((now or timezone.now()) - self.created_at).total_seconds() > SNAPSHOT_REVIEW_DAYS * 86400


class VmEvent(models.Model):
    """One event about a VM: observed by the sync (power state, tools, host move) or read by the provider."""
    SEVERITIES = [("info", "Info"), ("warning", "Warning"), ("error", "Error")]
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    vm = models.ForeignKey(VmInfo, on_delete=models.CASCADE, related_name="events")
    key = models.CharField(max_length=80)
    ts = models.DateTimeField(default=timezone.now)
    severity = models.CharField(max_length=8, choices=SEVERITIES, default="info")
    kind = models.CharField(max_length=80, blank=True)
    message = models.CharField(max_length=400)
    origin = models.CharField(max_length=10, default="sync")          # sync | provider

    class Meta:
        ordering = ["-ts", "-id"]
        constraints = [models.UniqueConstraint(fields=["vm", "key"], name="hv_event_unique")]
        indexes = [models.Index(fields=["vm", "-ts"])]


class VmControlRequest(models.Model):
    """"Power off", "Take snapshot"...: a REQUEST recorded in LinkedEye. Approving it records the decision and shows the
    manual step. LinkedEye never sends anything to vCenter (``hypervisor.vsphere`` has no write method)."""
    PENDING, APPROVED, REJECTED, EXPIRED = "pending", "approved", "rejected", "expired"
    STATES = [(PENDING, "Pending"), (APPROVED, "Approved"), (REJECTED, "Rejected"), (EXPIRED, "Expired")]
    ACTIONS = [
        ("power_on", "Power on"), ("power_off", "Power off"), ("restart", "Restart"), ("suspend", "Suspend"),
        ("reset", "Reset"), ("migrate", "Migrate"), ("snapshot", "Take snapshot"), ("clone", "Clone VM"),
        ("edit_settings", "Edit settings"), ("move_to_host", "Move to host"), ("download_logs", "Download logs"),
    ]
    tenant_key = models.CharField(max_length=100, default=_tenant, db_index=True)
    vm = models.ForeignKey(VmInfo, on_delete=models.CASCADE, related_name="requests")
    vm_name = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=16, choices=ACTIONS)
    reason = models.CharField(max_length=300, blank=True)
    detail = models.CharField("Target / details", max_length=200, blank=True)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    requested_by_name = models.CharField(max_length=150, blank=True)
    requested_at = models.DateTimeField(default=timezone.now)
    state = models.CharField(max_length=10, choices=STATES, default=PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_by_name = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.CharField(max_length=300, blank=True)
    manual_step = models.TextField(blank=True)

    class Meta:
        ordering = ["-requested_at", "-id"]
        indexes = [models.Index(fields=["vm", "state"]), models.Index(fields=["tenant_key", "state"])]

    @property
    def action_label(self):
        return dict(self.ACTIONS).get(self.action, self.action)


@receiver(post_delete, sender=VCenterSource)
def drop_login_with_source(sender, instance, **kwargs):
    """The vault row holding the vCenter login goes with the source, however the source is deleted: an encrypted password must
    not outlive the thing it was for."""
    if instance.credential_id:
        from django.apps import apps
        apps.get_model("onboarding", "Credential").objects.filter(pk=instance.credential_id, tenant_key=instance.tenant_key).delete()
