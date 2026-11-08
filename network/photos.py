"""Which devices still lack a real photo, and the writes that give them one (picked datasheet photo, catalog photo).

Views check permissions and CSRF; this module does the database work and the audit records. Nothing here fetches."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.conf import settings
from django.db import transaction

from core.audit import audit

from . import catalog, images, services
from .models import Device, DeviceType, NetworkProductImage

EXCLUDED_TYPES = (DeviceType.VIRTUAL_MACHINE,)   # a virtual machine has no hardware datasheet


def tenant():
    return settings.LINKEDEYE_TENANT


def has_real_photo(device):
    """A stored photo, or the older linked picture (a real photo that is merely hotlinked)."""
    return bool(device.image_id or device.product_image_url)


def model_key(vendor, model):
    return ((vendor or "").strip().lower(), (model or "").strip().lower())


def manageable(user, request=None, devices=None):
    """Devices the user may change (operate on their site), visible in their scope, VMs left out."""
    qs = services.devices_for(user, request).exclude(device_type__in=EXCLUDED_TYPES) if devices is None else devices
    sites = {}
    out = []
    for d in qs:
        if d.site not in sites:
            sites[d.site] = services.can_manage(user, "operate", d.site)
        if sites[d.site]:
            out.append(d)
    return out


@dataclass
class Group:
    vendor: str
    model: str
    devices: list = field(default_factory=list)
    types: set = field(default_factory=set)
    catalog_row: object = None
    catalog_count: int = 0          # devices in the group that a catalog photo could be applied to right now

    @property
    def count(self):
        return len(self.devices)

    @property
    def label(self):
        return f"{self.vendor} {self.model}".strip()

    @property
    def query(self):
        return urlencode({"vendor": self.vendor, "model": self.model})

    @property
    def names(self):
        return ", ".join(d.name for d in self.devices[:4]) + (f" and {self.count - 4} more" if self.count > 4 else "")


@dataclass
class Overview:
    with_photo: int = 0
    illustration_only: int = 0
    groups: list = field(default_factory=list)
    unnamed: list = field(default_factory=list)       # missing a photo AND a vendor or model, so nothing can be searched for
    total: int = 0

    @property
    def models_missing(self):
        return len(self.groups)

    @property
    def catalog_applicable(self):
        return sum(g.catalog_count for g in self.groups)


def overview(user, request=None):
    devices = manageable(user, request)
    ov = Overview(total=len(devices))
    match = catalog.matcher(tenant())
    groups = {}
    for d in devices:
        if has_real_photo(d):
            ov.with_photo += 1
            continue
        ov.illustration_only += 1
        if not (d.vendor or "").strip() or not (d.model or "").strip():
            ov.unnamed.append(d)
            continue
        g = groups.setdefault(model_key(d.vendor, d.model), Group(d.vendor.strip(), d.model.strip()))
        g.devices.append(d)
        g.types.add(d.device_type)
        row = match(d.device_type, d.vendor, d.model)
        if row is not None:
            g.catalog_count += 1
            g.catalog_row = g.catalog_row or row
    ov.groups = sorted(groups.values(), key=lambda g: (-g.count, g.label.lower()))
    return ov


def group_devices(user, request, vendor, model, *, missing_only=True):
    """The devices of one vendor + model the user may change (those still without a photo, unless `missing_only` is off)."""
    qs = services.devices_for(user, request).exclude(device_type__in=EXCLUDED_TYPES).filter(
        vendor__iexact=(vendor or "").strip(), model__iexact=(model or "").strip())
    if missing_only:
        qs = qs.filter(image__isnull=True, product_image_url="")
    return manageable(user, request, qs)


# ------------------------------------------------------------------ writes
def set_photo(request, device, image, via):
    """Give `device` this stored photo. The old stored photo is released when nothing else uses it."""
    old = device.image
    if old is not None and old.pk == image.pk:
        return False
    device.image = image
    device.save(update_fields=["image", "updated_at"])
    audit(request, "update", device, changed=["image"], via=via, photo=image.sha256[:12])
    if old is not None:
        images.release(old)
    return True


def upsert_catalog_row(request, vendor, model, image):
    """The catalog row "any type, this vendor, this model" gets this photo (created when missing). Returns (row, created)."""
    vendor, model = (vendor or "").strip(), (model or "").strip()
    row = (NetworkProductImage.objects.filter(tenant_key=tenant(), vendor__iexact=vendor, device_type="", model_key__iexact=model)
           .select_related("image").defer("image__data").first())
    if row is None:
        row = NetworkProductImage.objects.create(tenant_key=tenant(), vendor=vendor, device_type="", model_key=model, image=image)
        audit(request, "create", row, vendor=vendor, device_type="", model_key=model, photo=image.sha256[:12], via="datasheet")
        return row, True
    old = row.image
    row.image, row.product_image_url = image, ""
    row.save(update_fields=["image", "product_image_url"])
    audit(request, "update", row, vendor=vendor, device_type="", model_key=model, photo=image.sha256[:12], via="datasheet")
    if old is not None and old.pk != image.pk:
        images.release(old)
    return row, False


def apply_catalog(request, user, devices, only_row=None):
    """Give each device that has no photo yet the photo of its best catalog row (or only `only_row`'s photo to the
    devices that row is the best match for). Returns the number of devices changed."""
    match = catalog.matcher(tenant())
    changed = 0
    with transaction.atomic():
        for d in devices:
            if has_real_photo(d):
                continue
            row = match(d.device_type, d.vendor, d.model)
            if row is None or (only_row is not None and row.pk != only_row.pk):
                continue
            changed += bool(set_photo(request, d, row.image, "catalog"))
    return changed
