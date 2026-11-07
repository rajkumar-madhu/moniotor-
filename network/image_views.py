"""Product photos: authenticated serving, and the Product catalog management pages (/network/catalog/)."""
import csv
import re

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpResponse, HttpResponseNotModified
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from core.access import can_on, require
from core.audit import audit

from . import catalog_io, images
from .forms import CatalogForm, FieldImageError
from .imaging import CONTENT_TYPES, ImageError
from .models import DeviceImage, DeviceType, NetworkProductImage
from .views import _per_page, _query

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _etag_matches(header, etag):
    """If-None-Match: the weak comparison the spec asks for on GET, `*` matches anything."""
    if not header:
        return False
    return any(tag.strip() in ("*", etag, "W/" + etag) for tag in header.split(","))


@require("view")
@require_safe
def image(request, sha, ext):
    """The bytes of one stored photo. Only a user who may see a device (or a catalog row) that uses it gets it; anything
    else is a plain 404, so the address reveals nothing. The type is pinned from what was validated at upload time,
    never from a request value, and the response cannot be framed, sniffed or run as a document."""
    ctype = CONTENT_TYPES.get(ext)
    if ctype is None or not HEX64.fullmatch(sha):
        raise Http404
    img = DeviceImage.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, sha256=sha).defer("data").first()
    if img is None or img.content_type != ctype or not images.visible_to(request.user, img):
        raise Http404
    etag = f'"{img.sha256}"'
    if _etag_matches(request.headers.get("If-None-Match"), etag):
        resp = HttpResponseNotModified()
    else:
        body = DeviceImage.objects.filter(pk=img.pk).values_list("data", flat=True).first()
        resp = HttpResponse(b"" if request.method == "HEAD" else bytes(body), content_type=img.content_type)
        resp["Content-Disposition"] = f'inline; filename="device-photo.{img.ext}"'
    resp["ETag"] = etag
    resp["Cache-Control"] = "private, max-age=31536000, immutable"   # the address is the hash of the bytes
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Content-Security-Policy"] = "sandbox; default-src 'none'"
    resp["Cross-Origin-Resource-Policy"] = "same-origin"
    resp["Vary"] = "Cookie"
    return resp


# ------------------------------------------------------------------ catalog management
def _tenant():
    return settings.LINKEDEYE_TENANT


def _admin_only(request):
    """The catalog is tenant-wide, so changing it needs the admin role on every site."""
    if not can_on(request.user, "admin", None):
        raise PermissionDenied


def _catalog_qs():
    return NetworkProductImage.objects.filter(tenant_key=_tenant()).select_related("image").defer("image__data")


def _row(pk):
    return get_object_or_404(_catalog_qs(), pk=pk)


def _catalog_context(request, **extra):
    qs = _catalog_qs()
    g = request.GET
    q = g.get("q", "").strip()
    if q:
        qs = qs.filter(Q(vendor__icontains=q) | Q(model_key__icontains=q) | Q(notes__icontains=q))
    if g.get("type") in DeviceType.values:
        qs = qs.filter(device_type=g["type"])
    state = g.get("photo", "")
    if state == "stored":
        qs = qs.filter(image__isnull=False)
    elif state == "linked":
        qs = qs.filter(image__isnull=True).exclude(product_image_url="")
    page = Paginator(qs, _per_page(request)).get_page(g.get("page"))
    total = NetworkProductImage.objects.filter(tenant_key=_tenant()).count()
    ctx = {"page": page, "query": _query(request), "types": DeviceType.choices, "total": total,
           "legacy_count": NetworkProductImage.objects.filter(tenant_key=_tenant(), image__isnull=True).exclude(product_image_url="").count(),
           "csv_columns": catalog_io.COLUMNS, "max_rows": catalog_io.MAX_ROWS, "max_bytes_mb": 2}
    ctx.update(extra)
    return ctx


@require("admin")
@require_safe
def catalog(request):
    _admin_only(request)
    return render(request, "network/catalog.html", _catalog_context(request))


@require("admin")
@require_http_methods(["GET", "POST"])
def catalog_edit(request, pk=None):
    _admin_only(request)
    row = _row(pk) if pk else None
    old_image = row.image if row else None
    form = CatalogForm(request.POST or None, request.FILES or None, instance=row)
    if request.method == "POST" and form.is_valid():
        try:
            new_image = form.resolve_image(request.user)
        except FieldImageError as e:
            form.add_error(e.field, str(e))
        else:
            obj = form.save(commit=False)
            obj.tenant_key = _tenant()
            if new_image is not None:
                obj.image, obj.product_image_url = new_image, ""
            obj.save()
            if old_image is not None and (new_image is None or new_image.pk != old_image.pk):
                images.release(old_image)
            audit(request, "update" if row else "create", obj, vendor=obj.vendor, device_type=obj.device_type,
                  model_key=obj.model_key, photo=new_image.sha256[:12] if new_image else "")
            messages.success(request, f"Saved the catalog row for {obj.vendor} {obj.model_key or '(any model)'}.")
            return redirect("network:catalog")
    vendors = sorted(set(NetworkProductImage.objects.filter(tenant_key=_tenant()).values_list("vendor", flat=True)), key=str.lower)
    return render(request, "network/catalog_form.html", {"form": form, "row": row, "vendors": vendors})


@require("admin")
@require_POST
def catalog_delete(request, pk):
    _admin_only(request)
    row = _row(pk)
    old = row.image
    audit(request, "delete", row, vendor=row.vendor, device_type=row.device_type, model_key=row.model_key)
    label = f"{row.vendor} {row.model_key or '(any model)'}"
    row.delete()
    images.release(old)      # devices that picked this photo keep it; a photo nothing uses any more is dropped
    messages.success(request, f"Removed the catalog row for {label}.")
    return redirect("network:catalog")


@require("admin")
@require_POST
def catalog_store_copy(request, pk):
    """Turn an older row that only links to a picture into a stored copy (one safe download)."""
    _admin_only(request)
    row = _row(pk)
    if row.image_id or not row.product_image_url:
        messages.info(request, "That row already has a stored photo.")
        return redirect("network:catalog")
    try:
        image, _created = images.fetch_and_store(row.product_image_url, request.user)
    except ImageError as e:
        messages.error(request, f"Could not store a copy for {row.vendor} {row.model_key or '(any model)'}: {e}")
        return redirect("network:catalog")
    row.image, row.product_image_url = image, ""
    row.save(update_fields=["image", "product_image_url"])
    audit(request, "update", row, vendor=row.vendor, model_key=row.model_key, photo=image.sha256[:12], via="store_copy")
    messages.success(request, f"Stored a copy of the photo for {row.vendor} {row.model_key or '(any model)'}.")
    return redirect("network:catalog")


@require("admin")
@require_POST
def catalog_import(request):
    _admin_only(request)
    dry_run = request.POST.get("dry_run") == "on"
    upload = request.FILES.get("csv_file")
    text = request.POST.get("csv_text", "")
    problem, report, counts = "", None, None
    if upload is not None:
        if upload.size > catalog_io.MAX_CSV_BYTES:
            problem = f"The CSV is larger than {catalog_io.MAX_CSV_BYTES // 1024} KB."
        else:
            try:
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError:
                problem = "The CSV must be UTF-8 text."
    if not problem and not text.strip():
        problem = "Choose a CSV file or paste its content."
    rows = []
    if not problem:
        rows, problem = catalog_io.parse_csv(text)
    if problem:
        messages.error(request, problem)
        return render(request, "network/catalog.html", _catalog_context(request, csv_text=text, dry_run=dry_run), status=400)
    report = catalog_io.run_import(rows, request.user, dry_run=dry_run)
    counts = catalog_io.summary(report)
    audit(request, "import", None, object_type="network.catalog", dry_run=dry_run, rows=len(report), **counts)
    return render(request, "network/catalog.html", _catalog_context(request, report=report, counts=counts, dry_run=dry_run,
                                                                     csv_text=text))


@require("admin")
@require_safe
def catalog_export(request):
    _admin_only(request)
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="network-product-catalog.csv"'
    w = csv.writer(resp)
    w.writerow(catalog_io.EXPORT_COLUMNS)
    for r in catalog_io.export_rows():
        w.writerow([catalog_io.csv_cell(v) for v in r])
    audit(request, "export", None, object_type="network.catalog")
    return resp
