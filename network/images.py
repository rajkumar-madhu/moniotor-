"""Storing and fetching product photos.

Every path that puts bytes into DeviceImage goes through store_image(), which validates them with network.imaging.
Downloads go through probes.checks.fetch_bytes (SSRF policy, https only, redirects re-validated, size and time
limited), so the server never hotlinks and never connects anywhere the probe policy forbids. Callers get ImageError
with a message that is safe to show."""
import hashlib
import logging
from urllib.parse import urlsplit, urlunsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from core.access import scope_by_site
from probes import checks

from .imaging import CONTENT_TYPES, MAX_BYTES, ImageError, inspect_image
from .models import Device, DeviceImage, NetworkProductImage

log = logging.getLogger("linkedeye.network")

FETCH_TIMEOUT = 10             # seconds, connect to last byte, redirects included
MAX_IMAGES_PER_TENANT = 2000   # a runaway import or script cannot fill the database
ACCEPTED_TYPES = set(CONTENT_TYPES.values()) | {"image/jpg", "image/pjpeg", "image/x-png", "application/octet-stream",
                                                "binary/octet-stream"}
ACCEPT_HEADER = "image/png,image/jpeg,image/webp;q=0.9,*/*;q=0.1"
_url_validator = URLValidator(schemes=["https"])


def tenant():
    return settings.LINKEDEYE_TENANT


def clean_source_url(url):
    """The address recorded for a fetched image: scheme, host and path only. A query string or fragment (signed
    URLs, tokens) and any user:password@ part are never stored."""
    try:
        parts = urlsplit((url or "").strip())
        hostname, port = parts.hostname, parts.port
    except ValueError:                       # [::1, :abc, :99999
        return ""
    if parts.scheme != "https" or not hostname:
        return ""
    host = hostname if ":" not in hostname else f"[{hostname}]"
    netloc = host + (f":{port}" if port and port != 443 else "")
    out = urlunsplit(("https", netloc, parts.path or "/", "", ""))
    return out if len(out) <= 500 else ""


def validate_fetch_url(url):
    """Syntax check before any network call; returns the stripped URL."""
    url = (url or "").strip()
    if not url:
        raise ImageError("Enter an address to fetch the photo from.")
    if len(url) > 500:
        raise ImageError("That address is too long (500 characters at most).")
    if not url.lower().startswith("https://"):
        raise ImageError("Only https:// addresses can be fetched.")
    try:
        _url_validator(url)
    except ValidationError:
        raise ImageError("That is not a valid https:// address.") from None
    return url


def download(url):
    """Fetch `url` once, safely, and return the validated bytes plus their ImageInfo: (data, info). Raises ImageError."""
    url = validate_fetch_url(url)
    res = checks.fetch_bytes(url, MAX_BYTES, timeout=FETCH_TIMEOUT, accept=ACCEPT_HEADER, accepted_types=ACCEPTED_TYPES,
                             https_only=True, user_agent=f"LinkedEye/{settings.LINKEDEYE_VERSION} (product photo fetch)")
    if not res.ok:
        raise ImageError(f"Could not download the photo: {res.error}")
    info = inspect_image(res.body, declared_type=res.content_type)
    return res.body, info


def store_image(data, *, source, source_url="", user=None, declared_type=None, filename=None, tenant_key=None, origin=""):
    """Validate `data` and store it (deduplicated by hash). Returns (DeviceImage, created). Raises ImageError.
    Candidates (pictures nobody has picked yet) are never matched or counted here; see store_candidate()."""
    info = inspect_image(data, declared_type=declared_type, filename=filename)
    data = bytes(data)
    t = tenant_key or tenant()
    digest = hashlib.sha256(data).hexdigest()
    existing = DeviceImage.objects.filter(tenant_key=t, sha256=digest, candidate_owner=0).first()
    if existing:
        return existing, False
    if DeviceImage.objects.filter(tenant_key=t, candidate_owner=0).count() >= MAX_IMAGES_PER_TENANT:
        raise ImageError(f"The image library is full ({MAX_IMAGES_PER_TENANT} photos). Remove photos that are no longer used.")
    try:
        with transaction.atomic():
            image = DeviceImage.objects.create(
                tenant_key=t, sha256=digest, content_type=info.content_type, width=info.width, height=info.height,
                byte_size=len(data), data=data, source=source,
                source_url=clean_source_url(source_url) if source in ("fetched", "datasheet") else "", origin=origin[:80],
                uploaded_by=user if user is not None and user.is_authenticated else None)
    except IntegrityError:           # a concurrent request stored the same bytes first
        return DeviceImage.objects.get(tenant_key=t, sha256=digest, candidate_owner=0), False
    return image, True


# ------------------------------------------------------------------ candidates (datasheet / product page results)
def live_candidates(user, tenant_key=None):
    """This user's pending candidates, in the order they were ranked, bytes not loaded. Expired ones are not listed even
    when the prune job has not removed them yet."""
    if user is None or not user.is_authenticated:
        return DeviceImage.objects.none()
    return (DeviceImage.objects.filter(tenant_key=tenant_key or tenant(), is_candidate=True, candidate_owner=user.pk,
                                       created_at__gte=timezone.now() - DeviceImage.CANDIDATE_TTL)
            .defer("data").order_by("pk"))


def prune_candidates(now=None):
    """Delete candidates older than their time to live. Returns how many were removed."""
    cutoff = (now or timezone.now()) - DeviceImage.CANDIDATE_TTL
    return DeviceImage.objects.filter(is_candidate=True, created_at__lt=cutoff).delete()[0]


def _lock_candidates(tenant_key):
    """Serialise candidate writes of one tenant for the rest of the current transaction, so the per-tenant cap and the
    one-row-per-(owner, bytes) rule hold when two requests run at once. PostgreSQL: a transaction-scoped advisory lock
    (released at commit or rollback, nothing to clean up). SQLite (development and tests) already serialises writers."""
    if connection.vendor == "postgresql":
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ["le-photo-candidates:" + tenant_key])


def replace_candidates(user, items, tenant_key=None):
    """Make `items` this user's candidates: their previous ones are discarded first. `items` are objects with `data`,
    `origin` and `source_url`; each is validated like any photo. Returns the stored DeviceImage rows (at most the tenant
    cap; when the tenant is at the cap the oldest candidates of anyone are dropped to make room). Raises ImageError only
    when nothing could be stored."""
    t = tenant_key or tenant()
    cap = DeviceImage.MAX_CANDIDATES_PER_TENANT
    stored, last_error = [], None
    with transaction.atomic():
        _lock_candidates(t)
        prune_candidates()
        DeviceImage.objects.filter(tenant_key=t, is_candidate=True, candidate_owner=user.pk).delete()
        for item in list(items)[:cap]:
            try:
                info = inspect_image(item.data)
            except ImageError as e:
                last_error = e
                continue
            digest = hashlib.sha256(item.data).hexdigest()
            if any(s.sha256 == digest for s in stored):
                continue
            over = DeviceImage.objects.filter(tenant_key=t, is_candidate=True).count() + 1 - cap
            if over > 0:
                old = list(DeviceImage.objects.filter(tenant_key=t, is_candidate=True).order_by("created_at", "pk")
                           .values_list("pk", flat=True)[:over])
                DeviceImage.objects.filter(pk__in=old).delete()
            stored.append(DeviceImage.objects.create(
                tenant_key=t, sha256=digest, content_type=info.content_type, width=info.width, height=info.height,
                byte_size=len(item.data), data=bytes(item.data), source="datasheet", origin=(item.origin or "")[:80],
                source_url=clean_source_url(getattr(item, "source_url", "") or ""), uploaded_by=user,
                is_candidate=True, candidate_owner=user.pk))
    if not stored and last_error is not None:
        raise last_error
    return stored


def promote(candidate, user):
    """Turn the user's own candidate into a real stored photo and return it. If the same bytes are already a photo, that
    one is used and the candidate is dropped. Raises ImageError when the candidate is not the user's, has expired, or the
    library is full."""
    with transaction.atomic():
        row = (DeviceImage.objects.select_for_update().filter(pk=candidate.pk, tenant_key=tenant(), is_candidate=True,
                                                              candidate_owner=getattr(user, "pk", 0) or -1).first())
        if row is None or row.created_at < timezone.now() - DeviceImage.CANDIDATE_TTL:
            raise ImageError("That photo is no longer available: candidates are kept for one hour. Search again.")
        existing = DeviceImage.objects.filter(tenant_key=row.tenant_key, sha256=row.sha256, candidate_owner=0).first()
        if existing is not None:
            row.delete()
            return existing
        if DeviceImage.objects.filter(tenant_key=row.tenant_key, candidate_owner=0).count() >= MAX_IMAGES_PER_TENANT:
            raise ImageError(f"The image library is full ({MAX_IMAGES_PER_TENANT} photos). Remove photos that are no longer used.")
        try:
            with transaction.atomic():        # two people promoting the same bytes at once: the second one meets the first one's row
                row.is_candidate, row.candidate_owner, row.created_at = False, 0, timezone.now()
                row.save(update_fields=["is_candidate", "candidate_owner", "created_at"])
        except IntegrityError:
            existing = DeviceImage.objects.filter(tenant_key=row.tenant_key, sha256=row.sha256, candidate_owner=0).first()
            if existing is None:
                raise ImageError("That photo could not be saved. Search again.") from None
            DeviceImage.objects.filter(pk=row.pk).delete()
            return existing
        return row


def read_upload(uploaded):
    """Bytes of a Django UploadedFile, refusing anything above the limit without reading it all."""
    if uploaded.size is not None and uploaded.size > MAX_BYTES:
        raise ImageError(f"The file is {uploaded.size / 1024 / 1024:.1f} MB; the limit is {MAX_BYTES // 1024 // 1024} MB.")
    data = uploaded.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ImageError(f"The file is larger than the {MAX_BYTES // 1024 // 1024} MB limit.")
    return data


def store_upload(uploaded, user):
    data = read_upload(uploaded)
    return store_image(data, source="upload", user=user, declared_type=getattr(uploaded, "content_type", None),
                       filename=getattr(uploaded, "name", None))


def fetch_and_store(url, user=None):
    """Download `url` once and store the bytes. Returns (DeviceImage, created). Raises ImageError."""
    data, _info = download(url)
    return store_image(data, source="fetched", source_url=url, user=user)


def release(*images):
    """Delete stored photos that nothing uses any more (called after a device or catalog row stops using one)."""
    for image in {i for i in images if i is not None}:
        if not Device.objects.filter(image=image).exists() and not NetworkProductImage.objects.filter(image=image).exists():
            image.delete()


def visible_to(user, image):
    """May `user` receive these bytes? Catalog photos are tenant-wide; a device photo follows the device's site.
    A candidate is nobody's until it is picked (its owner gets it through network.photo_views.candidate_image)."""
    if image.is_candidate:
        return False
    if NetworkProductImage.objects.filter(tenant_key=image.tenant_key, image=image).exists():
        return True
    return scope_by_site(Device.objects.filter(tenant_key=image.tenant_key, image=image), user).exists()
