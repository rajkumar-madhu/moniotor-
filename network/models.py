"""Network devices: inventory, reachability history, interfaces (from Prometheus
snmp_exporter) and subnet discovery runs."""
import ipaddress
import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone


def _tenant():
    return settings.LINKEDEYE_TENANT


HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\.?$")


def validate_host(value):
    """An IPv4/IPv6 address or a DNS hostname."""
    value = (value or "").strip()
    try:
        ipaddress.ip_address(value)
        return
    except ValueError:
        pass
    if not HOSTNAME_RE.match(value) or value.replace(".", "").isdigit():
        raise ValidationError("Enter an IP address or a hostname.")


class DeviceType(models.TextChoices):
    SWITCH = "switch", "Switch"
    ROUTER = "router", "Router"
    FIREWALL = "firewall", "Firewall"
    LOAD_BALANCER = "load_balancer", "Load Balancer"
    WIRELESS = "wireless", "Wireless"
    PHYSICAL_SERVER = "physical_server", "Physical Server"
    VIRTUAL_MACHINE = "virtual_machine", "Virtual Machine"


# device_type -> (URL name of its list page, plural page title, icon)
TYPE_PAGES = {
    DeviceType.SWITCH: ("network:switches", "Switches", "network"),
    DeviceType.ROUTER: ("network:routers", "Routers", "share"),
    DeviceType.FIREWALL: ("network:firewalls", "Firewalls", "brick"),
    DeviceType.LOAD_BALANCER: ("network:load_balancers", "Load Balancers", "layers"),
    DeviceType.WIRELESS: ("network:wireless", "Wireless", "wifi"),
    DeviceType.PHYSICAL_SERVER: ("network:physical_servers", "Physical Servers", "server"),
    DeviceType.VIRTUAL_MACHINE: ("network:virtual_machines", "Virtual Machines", "cloud"),
}


class DeviceStatus(models.TextChoices):
    UP = "up", "Up"
    DOWN = "down", "Down"
    UNKNOWN = "unknown", "Unknown"


class Environment(models.TextChoices):
    # same keys as cmdb.Environment so devices and CIs line up
    PROD = "prod", "Production"
    UAT = "uat", "UAT"
    DR = "dr", "DR"
    DEV = "dev", "Development"


class NetworkProductImage(models.Model):
    """Curated vendor product photo and datasheet URLs for device onboarding (per tenant)."""
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    vendor = models.CharField(max_length=100, db_index=True)
    device_type = models.CharField(
        max_length=20, blank=True, default="",
        help_text="Limit to this device type, or leave blank for any type.",
    )
    model_key = models.CharField(
        max_length=120, blank=True, default="",
        help_text="Match device model: exact, then startswith, then substring (case-insensitive). Leave empty for a vendor default.",
    )
    product_image_url = models.URLField(max_length=500)
    product_page_url = models.URLField(max_length=500, blank=True, default="")
    notes = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["vendor", "device_type", "model_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_key", "vendor", "device_type", "model_key"],
                name="network_productimage_unique",
            ),
        ]
        indexes = [models.Index(fields=["tenant_key", "vendor", "device_type"])]

    def __str__(self):
        return f"{self.vendor} {self.model_key or '*'} ({self.device_type or 'any'})"


class Device(models.Model):
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    name = models.CharField(max_length=150)
    device_type = models.CharField("Type", max_length=20, choices=DeviceType.choices, default=DeviceType.SWITCH, db_index=True)
    vendor = models.CharField(max_length=100, blank=True)
    model = models.CharField(max_length=100, blank=True)
    serial = models.CharField("Serial number", max_length=100, blank=True)
    firmware = models.CharField("Firmware / OS version", max_length=150, blank=True)
    mgmt_ip = models.CharField("Management IP / host", max_length=255, validators=[validate_host],
                               help_text="Address the reachability check connects to.")
    check_port = models.PositiveIntegerField("Check port", default=22, validators=[MinValueValidator(1), MaxValueValidator(65535)],
                                             help_text="TCP port used for the reachability check (22 = SSH).")
    site = models.CharField(max_length=100, blank=True, default="", db_index=True)
    role = models.CharField(max_length=120, blank=True)
    environment = models.CharField(max_length=10, choices=Environment.choices, blank=True, default=Environment.PROD)
    contact = models.CharField(max_length=150, blank=True)
    description = models.TextField(blank=True)
    product_image_url = models.URLField(
        "Product image URL", max_length=500, blank=True,
        help_text="Shown on the device overview. Can be filled from the product catalog when you pick vendor and model.")
    product_page_url = models.URLField(
        "Product page URL", max_length=500, blank=True,
        help_text="Optional datasheet or vendor product page.")
    tags = models.CharField(max_length=300, blank=True, help_text="Comma-separated, e.g. core, dc1")
    prometheus_instance = models.CharField(
        "Prometheus instance", max_length=200, blank=True,
        help_text="The snmp_exporter target's instance label in Prometheus (often the device IP or name). "
                  "Leave empty when the device is not scraped.")
    enabled = models.BooleanField("Monitoring enabled", default=True)

    # reachability state (written by the poll job)
    status = models.CharField(max_length=10, choices=DeviceStatus.choices, default=DeviceStatus.UNKNOWN, db_index=True)
    last_seen = models.DateTimeField(null=True, blank=True)
    last_checked_at = models.DateTimeField(null=True, blank=True)
    last_latency_ms = models.FloatField(null=True, blank=True)
    last_error = models.CharField(max_length=300, blank=True)
    status_changed_at = models.DateTimeField(null=True, blank=True)
    consecutive_failures = models.PositiveIntegerField(default=0)
    # the status the last reachability event was emitted for: a Down device that goes through Unknown
    # (monitoring turned off, or a check refused by the probe policy) must still recover its incident.
    last_alert_status = models.CharField(max_length=10, choices=DeviceStatus.choices, blank=True, default="")

    # metrics state (written by the Prometheus job)
    uptime_seconds = models.BigIntegerField(null=True, blank=True)
    metrics_updated_at = models.DateTimeField(null=True, blank=True)
    metrics_error = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="network_device_tenant_name_unique")]
        indexes = [models.Index(fields=["tenant_key", "device_type", "status"])]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("network:detail", args=[self.pk])

    @property
    def tag_list(self):
        return [t.strip() for t in self.tags.split(",") if t.strip()]

    @property
    def is_example(self):
        return "example" in [t.lower() for t in self.tag_list]

    @property
    def type_list_url(self):
        return reverse(TYPE_PAGES[self.device_type][0])

    @property
    def type_plural(self):
        return TYPE_PAGES[self.device_type][1]

    @property
    def type_icon(self):
        return TYPE_PAGES[self.device_type][2]

    @property
    def uptime_short(self):
        s = self.uptime_seconds
        if s is None:
            return ""
        days = int(s) // 86400
        if days:
            return f"{days} day{'s' if days != 1 else ''}"
        return f"{int(s) // 3600} h {int(s) % 3600 // 60} min"

    @property
    def uptime_display(self):
        s = self.uptime_seconds
        if s is None:
            return ""
        days, rem = divmod(int(s), 86400)
        hours, rem = divmod(rem, 3600)
        if days:
            return f"{days} day{'s' if days != 1 else ''}, {hours} hour{'s' if hours != 1 else ''}"
        return f"{hours} h {rem // 60} min"


class Interface(models.Model):
    """An interface as reported by snmp_exporter (IF-MIB) through Prometheus."""
    STATES = [("up", "Up"), ("down", "Down"), ("testing", "Testing"), ("unknown", "Unknown"), ("dormant", "Dormant"),
              ("notPresent", "Not present"), ("lowerLayerDown", "Lower layer down")]

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="interfaces")
    if_index = models.PositiveIntegerField()
    name = models.CharField(max_length=150)
    alias = models.CharField(max_length=200, blank=True)
    if_type = models.CharField(max_length=60, blank=True)
    admin_status = models.CharField(max_length=20, choices=STATES, default="unknown")
    oper_status = models.CharField(max_length=20, choices=STATES, default="unknown")
    speed_mbps = models.FloatField(null=True, blank=True)
    in_mbps = models.FloatField(null=True, blank=True)
    out_mbps = models.FloatField(null=True, blank=True)
    utilization = models.FloatField(null=True, blank=True, help_text="Busiest direction as % of interface speed")
    errors_per_s = models.FloatField(null=True, blank=True, help_text="ifInErrors + ifOutErrors, per second over 5 min")
    discards_per_s = models.FloatField(null=True, blank=True, help_text="ifInDiscards + ifOutDiscards, per second over 5 min")
    flaps_24h = models.PositiveIntegerField(null=True, blank=True, help_text="ifOperStatus changes in the last 24 hours")
    updated_at = models.DateTimeField(default=timezone.now)

    # if_type display names (network.metrics) that are not a port on the chassis
    LOGICAL_TYPES = {"Loopback", "Virtual", "Tunnel", "VLAN", "LAG", "Bridge", "MPLS", "PPP", "Other"}
    BUSY_UTIL = 80   # % — the front panel marks a port busy from here
    TONE_LABELS = {"up": "up", "busy": "busy or errors", "down": "down", "unused": "unused or admin down",
                   "unknown": "status unknown"}

    class Meta:
        ordering = ["device", "if_index"]
        constraints = [models.UniqueConstraint(fields=["device", "if_index"], name="network_interface_device_index_unique")]

    def __str__(self):
        return f"{self.device.name} {self.name}"

    @property
    def util_tone(self):
        u = self.utilization
        if u is None:
            return "mute"
        return "crit" if u >= 85 else "warn" if u >= 60 else "ok"

    @property
    def is_physical(self):
        return self.if_type not in self.LOGICAL_TYPES

    @property
    def port_tone(self):
        """Front-panel colour: unused (admin down / not present), down (admin up, no link), busy (>= BUSY_UTIL %
        or errors in the last 5 min), up, or unknown."""
        if self.admin_status == "down" or self.oper_status == "notPresent":
            return "unused"
        if self.oper_status in ("down", "lowerLayerDown"):
            return "down"
        if self.oper_status == "up":
            return "busy" if (self.utilization or 0) >= self.BUSY_UTIL or (self.errors_per_s or 0) > 0 else "up"
        return "unknown"

    @property
    def port_tone_label(self):
        return self.TONE_LABELS[self.port_tone]


class DevicePoll(models.Model):
    """One reachability check (TCP connect to mgmt_ip:check_port)."""
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="polls")
    checked_at = models.DateTimeField(default=timezone.now, db_index=True)
    status = models.CharField(max_length=10, choices=DeviceStatus.choices)
    latency_ms = models.FloatField(null=True, blank=True)
    error = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-checked_at"]
        indexes = [models.Index(fields=["device", "checked_at"])]

    def __str__(self):
        return f"{self.device_id} {self.status} @ {self.checked_at:%Y-%m-%d %H:%M}"


class DiscoveryRun(models.Model):
    STATUS = [("queued", "Queued"), ("running", "Running"), ("done", "Completed"), ("failed", "Failed"), ("cancelled", "Cancelled")]
    MAX_PORTS = 10

    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    cidr = models.CharField("Network (CIDR)", max_length=64)
    ports = models.CharField(max_length=100, default="22,443")
    site = models.CharField(max_length=100, blank=True, default="", help_text="Site given to devices approved from this run")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    requested_by_name = models.CharField(max_length=150, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="queued", db_index=True)
    total_addresses = models.PositiveIntegerField(default=0)
    scanned = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0, help_text="Addresses refused by the probe policy")
    found = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Discovery {self.cidr} ({self.get_status_display()})"

    def get_absolute_url(self):
        return reverse("network:discovery_run", args=[self.pk])

    @property
    def port_list(self):
        return [int(p) for p in self.ports.split(",") if p.strip().isdigit()]

    @property
    def active(self):
        return self.status in ("queued", "running")

    @property
    def progress(self):
        if not self.total_addresses:
            return 100 if self.status == "done" else 0
        return min(100, round(100 * (self.scanned + self.skipped) / self.total_addresses))

    @property
    def duration_s(self):
        if not self.started_at:
            return None
        return round(((self.finished_at or timezone.now()) - self.started_at).total_seconds())


class DiscoveredHost(models.Model):
    STATUS = [("new", "New"), ("approved", "Approved"), ("ignored", "Ignored"), ("existing", "Already a device")]

    run = models.ForeignKey(DiscoveryRun, on_delete=models.CASCADE, related_name="hosts")
    ip = models.GenericIPAddressField()
    open_ports = models.JSONField(default=list, blank=True)
    latency_ms = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS, default="new", db_index=True)
    device = models.ForeignKey(Device, on_delete=models.SET_NULL, null=True, blank=True, related_name="discovered_as")
    decided_by_name = models.CharField(max_length=150, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    found_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["run", "id"]
        constraints = [models.UniqueConstraint(fields=["run", "ip"], name="network_discoveredhost_run_ip_unique")]

    def __str__(self):
        return f"{self.ip} ({self.get_status_display()})"

    @property
    def ports_display(self):
        return ", ".join(str(p) for p in self.open_ports)
