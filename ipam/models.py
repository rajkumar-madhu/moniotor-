"""IP address management: VLANs, subnets (IPv4 and IPv6, per VRF), IP addresses and DNS zones/records.

Address arithmetic never enumerates ranges: every subnet stores its first and last address as fixed-width
hex strings (``net_start`` / ``net_end``, 32 lowercase hex digits), so containment and overlap are plain
string comparisons in the database and work the same for IPv4 and IPv6 on SQLite, PostgreSQL and MySQL.
"""
import hashlib
import ipaddress

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

GLOBAL_SITE = "All Sites"


def _tenant():
    return settings.LINKEDEYE_TENANT


def ip_key(addr):
    """Sortable fixed-width key for an address (IPv4 and IPv6 alike): 32 lowercase hex digits."""
    return f"{int(addr):032x}"


def key_int(key):
    return int(key, 16)


def is_example_text(*values):
    return any("example" in (v or "").lower() for v in values)


class Role(models.TextChoices):
    CONTAINER = "container", "Container (supernet)"
    SERVER = "server", "Servers"
    USER = "user", "Users / clients"
    MANAGEMENT = "management", "Management"
    DMZ = "dmz", "DMZ"
    TRANSIT = "transit", "Point-to-point / transit"
    LOOPBACK = "loopback", "Loopbacks"
    STORAGE = "storage", "Storage"
    VOICE = "voice", "Voice"
    WIRELESS = "wireless", "Wireless"
    OTHER = "other", "Other"


class AddressStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RESERVED = "reserved", "Reserved"
    DHCP = "dhcp", "DHCP"
    DEPRECATED = "deprecated", "Deprecated"


class Source(models.TextChoices):
    MANUAL = "manual", "Manual"
    DEVICE = "device", "Network device"
    DISCOVERY = "discovery", "Discovery"
    IMPORT = "import", "CSV import"


class VLAN(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    site = models.CharField(max_length=100, blank=True, default=GLOBAL_SITE, db_index=True)
    vid = models.PositiveSmallIntegerField("VLAN ID", validators=[MinValueValidator(1), MaxValueValidator(4094)])
    name = models.CharField(max_length=100)
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["site", "vid"]
        verbose_name = "VLAN"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "site", "vid"], name="ipam_vlan_tenant_site_vid_unique")]

    def __str__(self):
        return f"VLAN {self.vid} {self.name}"

    @property
    def is_example(self):
        return is_example_text(self.description)

    @property
    def site_label(self):
        return self.site or GLOBAL_SITE


class Subnet(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    site = models.CharField(max_length=100, blank=True, default=GLOBAL_SITE, db_index=True)
    cidr = models.CharField("CIDR", max_length=64, help_text="Network in CIDR notation, stored normalised.")
    version = models.PositiveSmallIntegerField(editable=False, db_index=True)
    prefixlen = models.PositiveSmallIntegerField(editable=False)
    net_start = models.CharField(max_length=32, editable=False, db_index=True)
    net_end = models.CharField(max_length=32, editable=False, db_index=True)
    vrf = models.CharField("VRF", max_length=100, blank=True, default="", db_index=True,
                           help_text="Routing table name; empty = global table.")
    vlan = models.ForeignKey(VLAN, on_delete=models.SET_NULL, null=True, blank=True, related_name="subnets", verbose_name="VLAN")
    gateway = models.GenericIPAddressField(null=True, blank=True)
    role = models.CharField("Purpose / role", max_length=20, choices=Role.choices, blank=True, default="")
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vrf", "version", "net_start", "prefixlen"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "vrf", "cidr"], name="ipam_subnet_tenant_vrf_cidr_unique")]
        indexes = [models.Index(fields=["tenant_key", "vrf", "version", "net_start", "net_end"])]

    def __str__(self):
        return self.cidr + (f" ({self.vrf})" if self.vrf else "")

    def get_absolute_url(self):
        return reverse("ipam:subnet_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        self.set_range()
        super().save(*args, **kwargs)

    def set_range(self):
        net = ipaddress.ip_network(self.cidr, strict=True)
        self.cidr = str(net)
        self.version = net.version
        self.prefixlen = net.prefixlen
        self.net_start = ip_key(net.network_address)
        self.net_end = ip_key(net.broadcast_address)

    @property
    def network(self):
        return ipaddress.ip_network(self.cidr)

    @property
    def is_container(self):
        return self.role == Role.CONTAINER

    @property
    def is_example(self):
        return is_example_text(self.description)

    @property
    def size(self):
        """Number of addresses in the range (an int, possibly 2**64 or more – never enumerated)."""
        return 1 << ((32 if self.version == 4 else 128) - self.prefixlen)

    @property
    def usable(self):
        """Assignable host addresses: IPv4 drops network and broadcast except on /31 and /32 (RFC 3021);
        IPv6 drops the subnet-router anycast address except on /127 and /128."""
        if self.version == 4:
            return self.size if self.prefixlen >= 31 else self.size - 2
        return self.size if self.prefixlen >= 127 else self.size - 1

    def first_host(self):
        start = key_int(self.net_start)
        if (self.version == 4 and self.prefixlen < 31) or (self.version == 6 and self.prefixlen < 127):
            return start + 1
        return start

    def last_host(self):
        end = key_int(self.net_end)
        if self.version == 4 and self.prefixlen < 31:
            return end - 1
        return end

    def contains_int(self, value):
        return key_int(self.net_start) <= value <= key_int(self.net_end)

    @property
    def site_label(self):
        return self.site or GLOBAL_SITE


class IPAddress(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    site = models.CharField(max_length=100, blank=True, default=GLOBAL_SITE, db_index=True,
                            help_text="Copied from the subnet; kept in sync when the subnet moves.")
    address = models.CharField(max_length=45, help_text="IPv4 or IPv6 address, stored normalised.")
    version = models.PositiveSmallIntegerField(editable=False, db_index=True)
    addr_key = models.CharField(max_length=32, editable=False, db_index=True)
    vrf = models.CharField("VRF", max_length=100, blank=True, default="", db_index=True)
    subnet = models.ForeignKey(Subnet, on_delete=models.SET_NULL, null=True, blank=True, related_name="addresses")
    status = models.CharField(max_length=12, choices=AddressStatus.choices, default=AddressStatus.ACTIVE, db_index=True)
    hostname = models.CharField(max_length=253, blank=True)
    description = models.CharField(max_length=300, blank=True)
    device = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="ipam_addresses")
    ci = models.ForeignKey("cmdb.ConfigurationItem", on_delete=models.SET_NULL, null=True, blank=True,
                           related_name="ipam_addresses", verbose_name="Configuration item")
    source = models.CharField(max_length=12, choices=Source.choices, default=Source.MANUAL)
    last_seen = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["vrf", "version", "addr_key"]
        verbose_name = "IP address"
        verbose_name_plural = "IP addresses"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "vrf", "address"], name="ipam_ip_tenant_vrf_address_unique")]
        indexes = [models.Index(fields=["tenant_key", "vrf", "version", "addr_key"])]

    def __str__(self):
        return self.address + (f" ({self.vrf})" if self.vrf else "")

    def get_absolute_url(self):
        return reverse("ipam:address_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        ip = ipaddress.ip_address(self.address)
        self.address = str(ip)
        self.version = ip.version
        self.addr_key = ip_key(ip)
        if self.subnet_id and self.subnet is not None:
            self.site = self.subnet.site
        super().save(*args, **kwargs)

    @property
    def ip(self):
        return ipaddress.ip_address(self.address)

    @property
    def is_example(self):
        return is_example_text(self.description)


class DNSZone(models.Model):
    KINDS = [("forward", "Forward"), ("reverse", "Reverse")]

    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    site = models.CharField(max_length=100, blank=True, default=GLOBAL_SITE, db_index=True)
    name = models.CharField(max_length=253, help_text="Zone apex without the trailing dot, e.g. corp.example.com.")
    kind = models.CharField(max_length=8, choices=KINDS, default="forward")
    default_ttl = models.PositiveIntegerField("Default TTL (s)", default=3600,
                                              validators=[MinValueValidator(1), MaxValueValidator(2592000)])
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["kind", "name"]
        verbose_name = "DNS zone"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "name"], name="ipam_zone_tenant_name_unique")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("ipam:zone_detail", args=[self.pk])

    @property
    def is_example(self):
        return is_example_text(self.description)

    @property
    def site_label(self):
        return self.site or GLOBAL_SITE


class RecordType(models.TextChoices):
    A = "A", "A"
    AAAA = "AAAA", "AAAA"
    CNAME = "CNAME", "CNAME"
    PTR = "PTR", "PTR"
    MX = "MX", "MX"
    TXT = "TXT", "TXT"
    SRV = "SRV", "SRV"


class DNSRecord(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    zone = models.ForeignKey(DNSZone, on_delete=models.CASCADE, related_name="records")
    name = models.CharField(max_length=253, help_text="Relative to the zone; @ is the zone apex.")
    rtype = models.CharField("Type", max_length=5, choices=RecordType.choices, db_index=True)
    value = models.CharField(max_length=500)
    value_hash = models.CharField(max_length=64, editable=False,
                                  help_text="SHA-256 of the value: keeps the unique index short enough for MySQL.")
    ttl = models.PositiveIntegerField("TTL (s)", null=True, blank=True,
                                      validators=[MinValueValidator(1), MaxValueValidator(2592000)],
                                      help_text="Empty = the zone's default TTL.")
    ip = models.ForeignKey(IPAddress, on_delete=models.SET_NULL, null=True, blank=True, related_name="dns_records",
                           verbose_name="IP address")
    description = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["zone", "name", "rtype", "value"]
        verbose_name = "DNS record"
        constraints = [models.UniqueConstraint(fields=["tenant_key", "zone", "name", "rtype", "value_hash"],
                                               name="ipam_record_tenant_zone_name_type_value_unique")]

    def __str__(self):
        return f"{self.fqdn} {self.rtype} {self.value}"

    def save(self, *args, **kwargs):
        self.value_hash = hashlib.sha256(self.value.encode()).hexdigest()
        if kwargs.get("update_fields") is not None and "value" in kwargs["update_fields"]:
            kwargs["update_fields"] = list(kwargs["update_fields"]) + ["value_hash"]
        super().save(*args, **kwargs)

    @property
    def fqdn(self):
        return self.zone.name if self.name == "@" else f"{self.name}.{self.zone.name}"

    @property
    def effective_ttl(self):
        return self.ttl if self.ttl is not None else self.zone.default_ttl


class SyncRun(models.Model):
    """One "sync from devices" pass (manual or the hourly ipam.sync job) and what it changed."""
    TRIGGERS = [("manual", "Manual"), ("job", "Scheduled job")]

    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    sites = models.JSONField(default=list, blank=True,
                             help_text="Sites the run was limited to (the runner's operate scope); empty = every site.")
    trigger = models.CharField(max_length=8, choices=TRIGGERS, default="manual")
    actor_name = models.CharField(max_length=150, blank=True)
    ran_at = models.DateTimeField(db_index=True)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    skipped = models.PositiveIntegerField(default=0)
    conflicts = models.PositiveIntegerField(default=0)
    new_conflicts = models.PositiveIntegerField(default=0)
    conflict_keys = models.JSONField(default=list, blank=True)
    report = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-ran_at", "-id"]

    def __str__(self):
        return f"IPAM sync {self.ran_at:%Y-%m-%d %H:%M}"

    def get_absolute_url(self):
        return reverse("ipam:sync_run", args=[self.pk])
