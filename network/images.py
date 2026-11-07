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
from django.db import IntegrityError, transaction

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
    parts = urlsplit((url or "").strip())
    if parts.scheme != "https" or not parts.hostname:
        return ""
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    netloc = host + (f":{parts.port}" if parts.port and parts.port != 443 else "")
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
                             https_only=True)
    if not res.ok:
        raise ImageError(f"Could not download the photo: {res.error}")
    info = inspect_image(res.body, declared_type=res.content_type)
    return res.body, info


def store_image(data, *, source, source_url="", user=None, declared_type=None, filename=None, tenant_key=None):
    """Validate `data` and store it (deduplicated by hash). Returns (DeviceImage, created). Raises ImageError."""
    info = inspect_image(data, declared_type=declared_type, filename=filename)
    data = bytes(data)
    t = tenant_key or tenant()
    digest = hashlib.sha256(data).hexdigest()
    existing = DeviceImage.objects.filter(tenant_key=t, sha256=digest).first()
    if existing:
        return existing, False
    if DeviceImage.objects.filter(tenant_key=t).count() >= MAX_IMAGES_PER_TENANT:
        raise ImageError(f"The image library is full ({MAX_IMAGES_PER_TENANT} photos). Remove photos that are no longer used.")
    try:
        with transaction.atomic():
            image = DeviceImage.objects.create(
                tenant_key=t, sha256=digest, content_type=info.content_type, width=info.width, height=info.height,
                byte_size=len(data), data=data, source=source,
                source_url=clean_source_url(source_url) if source == "fetched" else "",
                uploaded_by=user if user is not None and user.is_authenticated else None)
    except IntegrityError:           # a concurrent request stored the same bytes first
        return DeviceImage.objects.get(tenant_key=t, sha256=digest), False
    return image, True


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
    """May `user` receive these bytes? Catalog photos are tenant-wide; a device photo follows the device's site."""
    if NetworkProductImage.objects.filter(tenant_key=image.tenant_key, image=image).exists():
        return True
    return scope_by_site(Device.objects.filter(tenant_key=image.tenant_key, image=image), user).exists()
