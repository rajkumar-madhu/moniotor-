"""Data centre infrastructure: rooms, racks, rack items (with elevation), power feeds and cooling units.

Site scoping: a Room stores the site name; Racks and CoolingUnits copy their room's site (kept in sync by
services.sync_room_site) so every list can be filtered with core.access.scope_by_site. RackItems and
PowerFeeds are scoped through their rack (``rack__site``).
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.urls import reverse

GLOBAL_SITE = "All Sites"
MAX_U = 60
EXAMPLE_MARK = "[example]"


def _tenant():
    return settings.LINKEDEYE_TENANT


def faces_conflict(a, b):
    """Two faces share rack space when they are equal or either is a full-depth ("both") item."""
    return a == b or Face.BOTH in (a, b)


class Face(models.TextChoices):
    FRONT = "front", "Front"
    REAR = "rear", "Rear"
    BOTH = "both", "Full depth (front and rear)"


class RackStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    PLANNED = "planned", "Planned"
    RESERVED = "reserved", "Reserved"
    DECOMMISSIONED = "decommissioned", "Decommissioned"


class ItemKind(models.TextChoices):
    SERVER = "server", "Server"
    SWITCH = "switch", "Switch"
    PDU = "pdu", "PDU"
    PATCH_PANEL = "patch_panel", "Patch panel"
    STORAGE = "storage", "Storage"
    OTHER = "other", "Other"


class FeedKind(models.TextChoices):
    PDU = "pdu", "PDU"
    UPS = "ups", "UPS"
    FEED = "feed", "Utility feed"


class CoolingKind(models.TextChoices):
    CRAC = "crac", "CRAC"
    CRAH = "crah", "CRAH"
    IN_ROW = "in_row", "In-row"
    LIQUID = "liquid", "Liquid"


class CoolingStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    STANDBY = "standby", "Standby"
    OFFLINE = "offline", "Offline"


class Room(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    site = models.CharField(max_length=100, default=GLOBAL_SITE, db_index=True)
    name = models.CharField(max_length=120)
    floor = models.CharField(max_length=40, blank=True)
    description = models.TextField(blank=True)
    # last cooling alert state written by the readings job ("" = ok, "negative" = load above cooling capacity)
    cooling_state = models.CharField(max_length=20, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["site", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "site", "name"], name="dcim_room_tenant_site_name_unique")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("dcim:racks") + f"?room={self.pk}"

    @property
    def is_example(self):
        return EXAMPLE_MARK in self.description.lower()


class Rack(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    room = models.ForeignKey(Room, on_delete=models.PROTECT, related_name="racks")
    site = models.CharField(max_length=100, default=GLOBAL_SITE, db_index=True, editable=False,
                            help_text="Copied from the room.")
    name = models.CharField(max_length=120)
    u_height = models.PositiveSmallIntegerField("Height (U)", default=42,
                                                validators=[MinValueValidator(1), MaxValueValidator(MAX_U)])
    max_power_kw = models.FloatField("Power budget (kW)", null=True, blank=True,
                                     validators=[MinValueValidator(0.01), MaxValueValidator(1000)])
    max_cooling_kw = models.FloatField("Cooling budget (kW)", null=True, blank=True,
                                       validators=[MinValueValidator(0.01), MaxValueValidator(1000)])
    status = models.CharField(max_length=16, choices=RackStatus.choices, default=RackStatus.ACTIVE, db_index=True)
    notes = models.TextField(blank=True)
    # last power alert state written by the readings job ("" = ok, "high" = above 90 % of max_power_kw)
    power_state = models.CharField(max_length=20, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["room__name", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "room", "name"], name="dcim_rack_tenant_room_name_unique")]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("dcim:rack_detail", args=[self.pk])

    def save(self, *args, **kwargs):
        if self.room_id:
            self.site = self.room.site
        super().save(*args, **kwargs)

    @property
    def is_example(self):
        return EXAMPLE_MARK in self.notes.lower()


class RackItem(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    rack = models.ForeignKey(Rack, on_delete=models.CASCADE, related_name="items")
    name = models.CharField(max_length=150)
    kind = models.CharField(max_length=16, choices=ItemKind.choices, default=ItemKind.SERVER)
    u_position = models.PositiveSmallIntegerField("Lowest U", validators=[MinValueValidator(1), MaxValueValidator(MAX_U)],
                                                  help_text="The lowest rack unit the item occupies (U1 is at the bottom).")
    u_height = models.PositiveSmallIntegerField("Height (U)", default=1,
                                                validators=[MinValueValidator(1), MaxValueValidator(MAX_U)])
    face = models.CharField(max_length=8, choices=Face.choices, default=Face.FRONT)
    device = models.ForeignKey("network.Device", on_delete=models.SET_NULL, null=True, blank=True, related_name="rack_items")
    ci = models.ForeignKey("cmdb.ConfigurationItem", on_delete=models.SET_NULL, null=True, blank=True,
                           related_name="rack_items", verbose_name="Configuration item")
    power_draw_w = models.PositiveIntegerField("Power draw (W)", null=True, blank=True,
                                               validators=[MaxValueValidator(100000)],
                                               help_text="Typical draw; used to estimate rack load when no feed is measured.")
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rack", "-u_position"]
        indexes = [models.Index(fields=["tenant_key", "rack"])]

    def __str__(self):
        return self.name

    @property
    def u_top(self):
        return (self.u_position or 0) + (self.u_height or 1) - 1

    @property
    def u_label(self):
        return f"U{self.u_position}" if self.u_height == 1 else f"U{self.u_position}–U{self.u_top}"

    def overlaps(self, other):
        return (faces_conflict(self.face, other.face)
                and self.u_position <= other.u_top and other.u_position <= self.u_top)

    def clean(self):
        super().clean()
        if not self.rack_id or not self.u_position or not self.u_height:
            return
        rack = self.rack
        if self.u_top > rack.u_height:
            raise ValidationError({"u_position": (
                f"Does not fit: a {self.u_height}U item at U{self.u_position} reaches U{self.u_top}, "
                f"but {rack.name} has {rack.u_height}U.")})
        clash = [o for o in RackItem.objects.filter(rack=rack).exclude(pk=self.pk) if self.overlaps(o)]
        if clash:
            o = clash[0]
            raise ValidationError({"u_position": (
                f"Overlaps “{o.name}” ({o.u_label}, {o.get_face_display().lower()}). "
                f"Choose free units or another face.")})


class PowerFeed(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    rack = models.ForeignKey(Rack, on_delete=models.CASCADE, related_name="feeds")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=8, choices=FeedKind.choices, default=FeedKind.PDU)
    capacity_kw = models.FloatField("Capacity (kW)", validators=[MinValueValidator(0.01), MaxValueValidator(10000)])
    measured_kw = models.FloatField("Measured load (kW)", null=True, blank=True,
                                    validators=[MinValueValidator(0), MaxValueValidator(10000)],
                                    help_text="Manual reading. A live Prometheus value takes precedence when available.")
    prometheus_query = models.CharField("Prometheus query", max_length=500, blank=True,
                                        help_text="PromQL returning the feed's load in kW (several series are summed).")
    last_value = models.FloatField(null=True, blank=True, editable=False)
    last_read_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=300, blank=True, editable=False)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rack", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "rack", "name"], name="dcim_feed_tenant_rack_name_unique")]

    def __str__(self):
        return self.name

    @property
    def live_kw(self):
        return self.last_value if self.prometheus_query and self.last_value is not None else None

    @property
    def load_kw(self):
        live = self.live_kw
        return live if live is not None else self.measured_kw

    @property
    def source(self):
        if self.live_kw is not None:
            return "live"
        return "manual" if self.measured_kw is not None else ""

    @property
    def headroom_kw(self):
        return None if self.load_kw is None else self.capacity_kw - self.load_kw

    @property
    def load_pct(self):
        return None if self.load_kw is None else 100.0 * self.load_kw / self.capacity_kw


class CoolingUnit(models.Model):
    tenant_key = models.CharField(max_length=64, default=_tenant, db_index=True)
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="cooling_units")
    site = models.CharField(max_length=100, default=GLOBAL_SITE, db_index=True, editable=False,
                            help_text="Copied from the room.")
    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=8, choices=CoolingKind.choices, default=CoolingKind.CRAH)
    status = models.CharField(max_length=8, choices=CoolingStatus.choices, default=CoolingStatus.ACTIVE,
                              help_text="Only active units count towards the room's cooling capacity.")
    capacity_kw = models.FloatField("Capacity (kW)", validators=[MinValueValidator(0.01), MaxValueValidator(10000)])
    supply_temp_c = models.FloatField("Supply temp (°C)", null=True, blank=True,
                                      validators=[MinValueValidator(-30), MaxValueValidator(90)])
    return_temp_c = models.FloatField("Return temp (°C)", null=True, blank=True,
                                      validators=[MinValueValidator(-30), MaxValueValidator(90)])
    supply_query = models.CharField("Supply temp query", max_length=500, blank=True,
                                    help_text="Optional PromQL returning the supply air/water temperature in °C.")
    return_query = models.CharField("Return temp query", max_length=500, blank=True,
                                    help_text="Optional PromQL returning the return air/water temperature in °C.")
    last_supply_c = models.FloatField(null=True, blank=True, editable=False)
    last_return_c = models.FloatField(null=True, blank=True, editable=False)
    last_read_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=300, blank=True, editable=False)
    notes = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["room__name", "name"]
        constraints = [models.UniqueConstraint(fields=["tenant_key", "room", "name"], name="dcim_cooling_tenant_room_name_unique")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.room_id:
            self.site = self.room.site
        super().save(*args, **kwargs)

    @property
    def supply_c(self):
        if self.supply_query and self.last_supply_c is not None:
            return self.last_supply_c
        return self.supply_temp_c

    @property
    def return_c(self):
        if self.return_query and self.last_return_c is not None:
            return self.last_return_c
        return self.return_temp_c

    @property
    def supply_live(self):
        return bool(self.supply_query and self.last_supply_c is not None)

    @property
    def return_live(self):
        return bool(self.return_query and self.last_return_c is not None)

    @property
    def delta_t(self):
        s, r = self.supply_c, self.return_c
        return None if s is None or r is None else r - s

    @property
    def has_queries(self):
        return bool(self.supply_query or self.return_query)
