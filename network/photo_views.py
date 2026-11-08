"""Real device photos from datasheets: the picker (/network/photos/find/), the "devices missing a photo" page
(/network/photos/) and the apply actions.

Permissions: finding and applying a photo to a device needs `operate` on the device's site; writing the product catalog
(the "use for every <model>" box, or a catalog row) is tenant-wide and needs admin, exactly like the catalog pages.
Candidates (pictures found but not yet picked) are private to the user who searched and expire after an hour."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.files.uploadhandler import MemoryFileUploadHandler
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from core.access import can, can_on, require
from core.audit import audit

from . import datasheet, datasheet_pdf, images, photos, services
from .datasheet import DatasheetError
from .imaging import CONTENT_TYPES, ImageError
from .models import Device, DeviceImage, NetworkProductImage
from .views import _next

MAX_UPLOAD = datasheet_pdf.MAX_PDF_BYTES
SEARCH_URL = "https://duckduckgo.com/?q="        # a plain link the operator's browser opens; nothing is fetched from it here


def _tenant():
    return settings.LINKEDEYE_TENANT


def _pk(value):
    """A primary key from request data, or None. Only ASCII digits, and a length that cannot overflow the database:
    str.isdigit() accepts characters such as superscript two that int() then rejects."""
    value = (value or "").strip()
    return int(value) if value.isascii() and value.isdecimal() and len(value) <= 9 else None


@dataclass
class Ctx:
    """What the picker is for: one device, every device of a model, or one catalog row."""
    kind: str                                   # "device" | "group" | "catalog"
    vendor: str = ""
    model: str = ""
    device: Device = None
    row: NetworkProductImage = None
    devices: list = field(default_factory=list)    # devices that would get the photo (group), or the model's devices (device)
    params: dict = field(default_factory=dict)     # the query that identifies this context again

    @property
    def query(self):
        return urlencode(self.params)

    @property
    def label(self):
        return f"{self.vendor} {self.model}".strip() or "device"

    @property
    def title(self):
        if self.kind == "device":
            return f"Photo for {self.device.name}"
        if self.kind == "catalog":
            return f"Photo for catalog row {self.label}"
        return f"Photo for {self.label}"

    @property
    def search_url(self):
        return SEARCH_URL + quote(f"{self.label} datasheet")

    @property
    def can_catalog_name(self):
        return bool(self.vendor and self.model)

    @property
    def prefill(self):
        """A datasheet address already on file (https only)."""
        url = (self.device.product_page_url if self.device else self.row.product_page_url if self.row else "") or ""
        return url if url.lower().startswith("https://") else ""

    @property
    def back_url(self):
        if self.kind == "device":
            return self.device.get_absolute_url()
        if self.kind == "catalog":
            return reverse("network:catalog")
        return reverse("network:photos")


def _context(request, data):
    """Resolve and authorise the context named in `data` (GET or POST). 404 when it does not exist or is out of scope,
    403 when the user may not change it."""
    user = request.user
    dev, cat = data.get("device", "").strip(), data.get("catalog", "").strip()
    vendor, model = data.get("vendor", "").strip(), data.get("model", "").strip()
    if dev:
        if _pk(dev) is None:
            raise Http404
        device = services.devices_for(user, request).filter(pk=_pk(dev)).first()
        if device is None:
            raise Http404
        if not services.can_manage(user, "operate", device.site):
            raise PermissionDenied
        peers = (photos.group_devices(user, request, device.vendor, device.model, missing_only=False)
                 if device.vendor.strip() and device.model.strip() else [device])
        return Ctx("device", device.vendor.strip(), device.model.strip(), device=device, devices=peers, params={"device": device.pk})
    if cat:
        if _pk(cat) is None:
            raise Http404
        if not can_on(user, "admin", None):
            raise PermissionDenied
        row = get_object_or_404(NetworkProductImage.objects.select_related("image").defer("image__data"), pk=_pk(cat), tenant_key=_tenant())
        return Ctx("catalog", row.vendor, row.model_key, row=row, params={"catalog": row.pk})
    if vendor and model:
        devices = photos.group_devices(user, request, vendor, model, missing_only=True)
        if not devices:
            raise Http404
        return Ctx("group", vendor, model, devices=devices, params={"vendor": vendor, "model": model})
    raise Http404


def _no_spec(request, data):
    """A request that names no device, model or catalog row at all goes to the page where you choose one."""
    if any(data.get(k, "").strip() for k in ("device", "catalog", "vendor")):
        return None
    messages.info(request, "Choose a device or a model first.")
    return redirect("network:photos")


# ------------------------------------------------------------------ the missing-photos page
@require("operate")
@require_safe
def missing(request):
    ov = photos.overview(request.user, request)
    return render(request, "network/photos.html", {
        "ov": ov, "can_admin": can_on(request.user, "admin", None),
        "pct": round(100 * ov.with_photo / ov.total) if ov.total else 0,
    })


# ------------------------------------------------------------------ the picker
class _InMemoryUploads(MemoryFileUploadHandler):
    """Keeps an upload in memory whatever its size (the view has already bounded it): a datasheet is read from memory and is
    never written to a temporary file."""

    def handle_raw_input(self, input_data, META, content_length, boundary, encoding=None):
        self.activated = content_length <= MAX_UPLOAD + 1024 * 1024


@csrf_exempt
def find(request):
    """Entry point: the CSRF check and the upload handler are set up here because Django's CSRF middleware would read the
    form (and so choose the disk-backed upload handler) before any view code runs; csrf_protect below does the check."""
    if request.method == "POST":
        if not can(request.user, "operate"):          # before a single byte of the body is read
            raise PermissionDenied
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > MAX_UPLOAD + 1024 * 1024:
            return HttpResponse("The file is larger than the 25 MB LinkedEye reads from a PDF.", status=413, content_type="text/plain")
        request.upload_handlers = [_InMemoryUploads(request)]
    return _find(request)


@csrf_protect
@require("operate")
@require_http_methods(["GET", "POST"])
def _find(request):
    data = request.POST if request.method == "POST" else request.GET
    early = _no_spec(request, data)
    if early:
        return early
    ctx = _context(request, data)
    error, blocked = "", False
    url = ""
    if request.method == "POST":
        upload, url = request.FILES.get("pdf_file"), (request.POST.get("source_url") or "").strip()
        try:
            if upload and url:
                raise DatasheetError("Use one at a time: either choose a file or paste an address.")
            if upload:
                if upload.size > MAX_UPLOAD:
                    raise DatasheetError(f"The file is {upload.size / 1024 / 1024:.0f} MB; PDFs up to 25 MB are read.")
                found = datasheet.find_from_upload(upload.read(MAX_UPLOAD + 1), getattr(upload, "name", ""))
            elif url:
                found = datasheet.find_from_url(url, ctx.model)
            else:
                raise DatasheetError("Choose the datasheet PDF you downloaded, or paste a product page, datasheet or picture address.")
            stored = images.replace_candidates(request.user, found.items)
        except DatasheetError as e:
            error, blocked = str(e), e.blocked
            audit(request, "search", None, object_type="network.device_photo", result="error", via="url" if url else "pdf",
                  error=error[:200], **{k: str(v) for k, v in ctx.params.items()})
        except ImageError as e:
            error = str(e)
        else:
            audit(request, "search", None, object_type="network.device_photo", via=found.kind, candidates=len(stored),
                  source=found.source[:120], **{k: str(v) for k, v in ctx.params.items()})
            messages.success(request, f"Found {len(stored)} possible photo{'s' if len(stored) != 1 else ''}"
                                      + (f" in {found.source}" if found.source else "") + ". Click the one that shows the real device.")
            for note in found.notes:
                messages.info(request, note)
            return redirect(f"{reverse('network:photo_find')}?{ctx.query}")
    candidates = list(images.live_candidates(request.user))
    return render(request, "network/photo_find.html", {
        "ctx": ctx, "candidates": candidates, "error": error, "blocked": blocked, "next": request.GET.get("next", ""),
        "can_admin": can_on(request.user, "admin", None), "max_pdf_mb": MAX_UPLOAD // 1024 // 1024,
        "ttl_minutes": int(DeviceImage.CANDIDATE_TTL.total_seconds() // 60),
        "source_url": (request.POST.get("source_url") or ctx.prefill) if request.method == "POST" else ctx.prefill,
    }, status=400 if error else 200)


@require("operate")
@require_safe
def candidate_image(request, pk):
    """The bytes of one of the signed-in user's own candidates. Anyone else's, or an expired one, is a plain 404."""
    cand = images.live_candidates(request.user).filter(pk=pk).first() if pk < 2 ** 31 else None
    if cand is None:
        raise Http404
    body = DeviceImage.objects.filter(pk=cand.pk).values_list("data", flat=True).first()
    resp = HttpResponse(b"" if request.method == "HEAD" else bytes(body), content_type=cand.content_type)
    resp["Content-Disposition"] = f'inline; filename="candidate.{cand.ext}"'
    resp["Cache-Control"] = "private, no-store"
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = "sandbox; default-src 'none'"
    resp["Cross-Origin-Resource-Policy"] = "same-origin"
    resp["Vary"] = "Cookie"
    return resp


@require("operate")
@require_POST
def discard(request):
    early = _no_spec(request, request.POST)
    if early:
        return early
    ctx = _context(request, request.POST)
    n = DeviceImage.objects.filter(tenant_key=_tenant(), is_candidate=True, candidate_owner=request.user.pk).delete()[0]
    if n:
        audit(request, "delete", None, object_type="network.device_photo", discarded=n, **{k: str(v) for k, v in ctx.params.items()})
    messages.info(request, f"Discarded {n} candidate photo{'s' if n != 1 else ''}.")
    return redirect(f"{reverse('network:photo_find')}?{ctx.query}")


# ------------------------------------------------------------------ applying
def _plural(n, word):
    return f"{n} {word}{'s' if n != 1 else ''}"


@require("operate")
@require_POST
def apply_photo(request):
    """Use one candidate. For a device: that device (and, with the catalog box, every device of its model that has no photo and
    the catalog row for future ones). For a model group: every device of the group. For a catalog row: the row's photo."""
    early = _no_spec(request, request.POST)
    if early:
        return early
    ctx = _context(request, request.POST)
    user = request.user
    to_catalog = request.POST.get("catalog_save") == "on" or ctx.kind == "catalog"
    if to_catalog and not can_on(user, "admin", None):
        raise PermissionDenied          # the catalog is tenant-wide: nothing is changed, not even the device
    if to_catalog and ctx.kind != "catalog" and not ctx.can_catalog_name:
        messages.error(request, "Set the vendor and model on the device first: the catalog matches on them.")
        return redirect(f"{reverse('network:photo_find')}?{ctx.query}")
    pk = request.POST.get("candidate", "")
    cand = images.live_candidates(user).filter(pk=_pk(pk)).first() if _pk(pk) is not None else None
    if cand is None:
        messages.error(request, f"That photo is no longer available: candidates are kept for {int(DeviceImage.CANDIDATE_TTL.total_seconds() // 60)} "
                                "minutes and only for the person who searched. Search again.")
        return redirect(f"{reverse('network:photo_find')}?{ctx.query}")
    try:
        with transaction.atomic():
            image = images.promote(cand, user)
            done = _apply(request, user, ctx, image, to_catalog)
    except ImageError as e:
        messages.error(request, str(e))
        return redirect(f"{reverse('network:photo_find')}?{ctx.query}")
    messages.success(request, done)
    return redirect(_next(request, ctx.back_url))


def _apply(request, user, ctx, image, to_catalog):
    """The writes; returns the sentence shown to the operator."""
    parts = []
    if ctx.kind == "device":
        photos.set_photo(request, ctx.device, image, "datasheet")
        parts.append(f"Saved the photo for “{ctx.device.name}”.")
        if to_catalog:
            others = [d for d in photos.group_devices(user, request, ctx.vendor, ctx.model, missing_only=True) if d.pk != ctx.device.pk]
            for d in others:
                photos.set_photo(request, d, image, "datasheet")
            row, created = photos.upsert_catalog_row(request, ctx.vendor, ctx.model, image)
            parts.append(f"Every {ctx.label} device now uses it: {_plural(len(others), 'other device')} updated, and the catalog "
                         f"{'row was added' if created else 'row was updated'} so new ones get it too.")
    elif ctx.kind == "group":
        for d in ctx.devices:
            photos.set_photo(request, d, image, "datasheet")
        parts.append(f"Saved the photo for {_plural(len(ctx.devices), 'device')} ({ctx.label}).")
        if to_catalog:
            row, created = photos.upsert_catalog_row(request, ctx.vendor, ctx.model, image)
            parts.append(f"The catalog {'row was added' if created else 'row was updated'}, so new {ctx.label} devices get it too.")
    else:
        row = ctx.row
        old = row.image
        row.image, row.product_image_url = image, ""
        row.save(update_fields=["image", "product_image_url"])
        audit(request, "update", row, vendor=row.vendor, model_key=row.model_key, photo=image.sha256[:12], via="datasheet")
        if old is not None and old.pk != image.pk:
            images.release(old)
        waiting = [d for d in photos.manageable(user, request) if not photos.has_real_photo(d)]
        n = photos.apply_catalog(request, user, waiting, only_row=row)
        parts.append(f"Saved the photo for the catalog row {ctx.label}."
                     + (f" {_plural(n, 'device')} without a photo now use it." if n else ""))
    return " ".join(parts)


@require("operate")
@require_POST
def apply_catalog(request):
    """One click: devices (of one vendor + model, or all) that still show an illustration get the photo the catalog already has."""
    user = request.user
    if request.POST.get("all"):
        devices = [d for d in photos.manageable(user, request) if not photos.has_real_photo(d)]
        what = "all models"
    else:
        vendor, model = request.POST.get("vendor", "").strip(), request.POST.get("model", "").strip()
        if not (vendor and model):
            raise Http404
        devices = photos.group_devices(user, request, vendor, model, missing_only=True)
        what = f"{vendor} {model}"
    n = photos.apply_catalog(request, user, devices)
    if n:
        messages.success(request, f"Applied the catalog photo to {_plural(n, 'device')} ({what}).")
    else:
        messages.info(request, f"No catalog photo matches {what}. Get one from a datasheet instead.")
    return redirect(_next(request, reverse("network:photos")))
