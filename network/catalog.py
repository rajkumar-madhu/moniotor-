"""The product catalog: your own photos by vendor / model (and optional device type).

LinkedEye ships no rows. Add them on the Product catalog page (/network/catalog/), where each photo is uploaded or
downloaded once and stored, so onboarding can attach a real photo without scraping vendor sites at runtime.
"""
from __future__ import annotations

from django.db.models import Q

from .models import NetworkProductImage


def _score(r: NetworkProductImage, m: str, dt: str) -> tuple[int, int]:
    """How well catalog row `r` fits a device of type `dt` and model `m`; (-1, ...) means not at all."""
    key = (r.model_key or "").strip()
    if key:
        if not m:
            return (-1, 0)
        ml, kl = m.lower(), key.lower()
        if ml == kl:
            return (4, len(kl))
        if ml.startswith(kl):
            return (3, len(kl))
        if kl in ml:
            return (2, len(kl))
        return (-1, 0)
    # Empty model_key: vendor (and optional type) default
    if r.device_type and dt and r.device_type == dt:
        return (1, 2)   # typed default — preferred over generic
    if not r.device_type:
        return (1, 1)   # applies to any device type
    return (-1, 0)      # wrong device_type for this row


def _pick(rows, dt: str, m: str):
    best, best_score = None, (-1, -1)
    for r in rows:
        s = _score(r, m, dt)
        if s[0] < 0:
            continue
        if s > best_score:
            best_score, best = s, r
    return best


def best_row(*, tenant_key: str, device_type: str, vendor: str, model: str, with_image: bool = False):
    """The best matching catalog row (exact model, then prefix, then substring, then typed default, then vendor default),
    or None. With `with_image`, rows that have no stored photo are ignored."""
    v = (vendor or "").strip()
    m = (model or "").strip()
    dt = (device_type or "").strip()
    if not v:
        return None
    qs = NetworkProductImage.objects.filter(tenant_key=tenant_key, vendor__iexact=v).select_related("image").defer("image__data")
    if with_image:
        qs = qs.filter(image__isnull=False)
    if dt:
        qs = qs.filter(Q(device_type="") | Q(device_type=dt))
    return _pick(list(qs), dt, m)


def matcher(tenant_key: str):
    """best_row(with_image=True) for many devices with ONE query: returns match(device_type, vendor, model) -> row or None."""
    by_vendor: dict[str, list] = {}
    for r in NetworkProductImage.objects.filter(tenant_key=tenant_key, image__isnull=False).select_related("image").defer("image__data"):
        by_vendor.setdefault((r.vendor or "").strip().lower(), []).append(r)

    def match(device_type: str, vendor: str, model: str):
        v = (vendor or "").strip().lower()
        if not v:
            return None
        dt = (device_type or "").strip()
        rows = [r for r in by_vendor.get(v, []) if not (dt and r.device_type and r.device_type != dt)]
        return _pick(rows, dt, (model or "").strip())
    return match


def resolve_product_urls(*, tenant_key: str, device_type: str, vendor: str, model: str) -> tuple[str, str]:
    """Return (image_url, page_url) of the best row's legacy hotlink fields, empty strings when nothing matches."""
    best = best_row(tenant_key=tenant_key, device_type=device_type, vendor=vendor, model=model)
    if not best:
        return "", ""
    return (best.product_image_url or "").strip(), (best.product_page_url or "").strip()
