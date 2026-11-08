"""Physical links between network devices, and optional per-device placement overrides for the architecture page.

Site scoping: neither model carries a site of its own. A Link joins two devices and follows them: it is visible, and
counted, only to a user who may see BOTH devices (core.access.scope_by_site on each end), and it can only be changed by
a user who may operate on both devices' sites. A Placement belongs to one device. Because the site name lives on
network.Device (already listed in administration.services.SITE_NAME_FIELDS), renaming a site moves the devices and
the links and placements follow by foreign key, so there is nothing to register for these models.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import F, Q
from django.urls import reverse

from .ports import MAX_PORT, clean_port
from .tiers import COLOURS, TIER_LABELS, TIERS

MAX_SPEED_MBPS = 10_000_000          # 10 Tbps, far above any single link
EXAMPLE_TAG = "arch-example"


def _tenant():
    return settings.LINKEDEYE_TENANT


class Medium(models.TextChoices):
    FIBRE = "fibre", "Fibre"
    COPPER = "copper", "Copper"
    DAC = "dac", "DAC / AOC cable"
    LAG = "lag", "Link aggregation (LAG)"
    OTHER = "other", "Other"


class Origin(models.TextChoices):
    MANUAL = "manual", "Entered by hand"
    LLDP = "lldp", "Found by LLDP"
    IMPORT = "import", "CSV import"


class Kind(models.TextChoices):
    UPLINK = "uplink", "Uplink"
    INTERCONNECT = "interconnect", "Interconnect"
    DOWNLINK = "downlink", "Downlink"
    WAN = "wan", "WAN"


class Link(models.Model):
    """A cable between two devices. The pair is stored in one canonical order (the lower device id is `a`), so
    A-B and B-A are the same link; `save()` swaps the ends when needed and a database check enforces it."""
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    a_device = models.ForeignKey("network.Device", on_delete=models.CASCADE, related_name="arch_links_a")
    b_device = models.ForeignKey("network.Device", on_delete=models.CASCADE, related_name="arch_links_b")
    a_port = models.CharField(max_length=MAX_PORT, blank=True, help_text="e.g. Eth1/49")
    b_port = models.CharField(max_length=MAX_PORT, blank=True, help_text="e.g. Eth1/1")
    medium = models.CharField(max_length=10, choices=Medium.choices, default=Medium.FIBRE)
    speed_mbps = models.PositiveIntegerField("Speed (Mbps)", null=True, blank=True,
                                             validators=[MinValueValidator(1), MaxValueValidator(MAX_SPEED_MBPS)],
                                             help_text="Optional, e.g. 100000 for 100G. Used for the oversubscription figure.")
    kind = models.CharField(max_length=30, blank=True,
                            help_text="uplink, interconnect, downlink, wan or any short label")
    origin = models.CharField(max_length=10, choices=Origin.choices, default=Origin.MANUAL)
    confirmed = models.BooleanField(default=True, help_text="A link found by LLDP is drawn dashed until someone confirms it.")
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["a_device__name", "a_port", "b_device__name", "b_port"]
        constraints = [
            models.UniqueConstraint(fields=["tenant_key", "a_device", "a_port", "b_device", "b_port"],
                                    name="architecture_link_unique"),
            models.CheckConstraint(check=Q(a_device__lt=F("b_device")), name="architecture_link_ordered"),
        ]
        indexes = [models.Index(fields=["tenant_key", "a_device"], name="arch_link_a_idx"),
                   models.Index(fields=["tenant_key", "b_device"], name="arch_link_b_idx")]

    def __str__(self):
        a = f"{self.a_device.name} {self.a_port}".strip()
        b = f"{self.b_device.name} {self.b_port}".strip()
        return f"{a} ↔ {b}"

    def get_absolute_url(self):
        return reverse("architecture:link_edit", args=[self.pk])

    @staticmethod
    def order_ends(a_device_id, a_port, b_device_id, b_port):
        """(a id, a port, b id, b port) in canonical order with cleaned ports."""
        a_port, b_port = clean_port(a_port), clean_port(b_port)
        if a_device_id > b_device_id:
            return b_device_id, b_port, a_device_id, a_port
        return a_device_id, a_port, b_device_id, b_port

    def normalise(self):
        """Put the ends in canonical order and clean the port text. Raises ValidationError for a self-link."""
        if self.a_device_id and self.a_device_id == self.b_device_id:
            raise ValidationError("A device cannot be linked to itself.")
        try:
            a, ap, b, bp = self.order_ends(self.a_device_id, self.a_port, self.b_device_id, self.b_port)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None
        self.a_device_id, self.a_port, self.b_device_id, self.b_port = a, ap, b, bp

    def clean(self):
        super().clean()
        if not self.a_device_id or not self.b_device_id:
            return
        self.normalise()
        if self.a_device.tenant_key != self.b_device.tenant_key or self.a_device.tenant_key != self.tenant_key:
            raise ValidationError("Both devices must belong to the same tenant as the link.")

    def save(self, *args, **kwargs):
        self.normalise()
        self.kind = " ".join((self.kind or "").split())[:30]
        super().save(*args, **kwargs)

    def other(self, device_id):
        return self.b_device_id if device_id == self.a_device_id else self.a_device_id

    @property
    def key(self):
        return str(self.pk)


class Placement(models.Model):
    """An operator override of where one device sits on the page. Empty fields mean "decide automatically"."""
    TIER_CHOICES = [(t, TIER_LABELS[t]) for t in TIERS]
    COLOUR_CHOICES = [(c, c.capitalize()) for c in COLOURS]

    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    device = models.OneToOneField("network.Device", on_delete=models.CASCADE, related_name="arch_placement")
    tier = models.CharField(max_length=10, blank=True, choices=TIER_CHOICES, help_text="Leave empty to infer the tier.")
    group = models.CharField(max_length=80, blank=True,
                             help_text="Server tier only: the card the device is drawn in, e.g. Web Servers.")
    colour = models.CharField(max_length=10, blank=True, choices=COLOUR_CHOICES,
                              help_text="Server tier only: the group's colour. Empty uses the default for the group.")
    sort_order = models.SmallIntegerField(default=0, validators=[MinValueValidator(-1000), MaxValueValidator(1000)],
                                          help_text="Lower numbers are drawn further left inside the tier.")
    hidden = models.BooleanField(default=False, help_text="Leave this device out of the drawing.")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["device__name"]

    def __str__(self):
        return f"Placement of {self.device.name}"

    def clean(self):
        super().clean()
        self.group = " ".join((self.group or "").split())
        if self.colour and self.colour not in COLOURS:
            raise ValidationError({"colour": "Choose one of the listed colours."})

    @property
    def is_empty(self):
        return not (self.tier or self.group or self.colour or self.sort_order or self.hidden)
