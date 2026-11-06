"""Curated product image URLs by vendor / model (and optional device type).

Populate ``NetworkProductImage`` rows (e.g. via admin) so onboarding can suggest real photos
without scraping vendor sites at runtime.
"""
from __future__ import annotations

from django.db.models import Q

from .models import NetworkProductImage


def resolve_product_urls(*, tenant_key: str, device_type: str, vendor: str, model: str) -> tuple[str, str]:
    """Return (image_url, page_url), possibly empty strings when no catalog row matches."""
    v = (vendor or "").strip()
    m = (model or "").strip()
    dt = (device_type or "").strip()
    if not v:
        return "", ""
    qs = NetworkProductImage.objects.filter(tenant_key=tenant_key, vendor__iexact=v)
    if dt:
        qs = qs.filter(Q(device_type="") | Q(device_type=dt))
    rows = list(qs)

    def score_row(r: NetworkProductImage) -> tuple[int, int]:
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

    best = None
    best_score = (-1, -1)
    for r in rows:
        s = score_row(r)
        if s[0] < 0:
            continue
        if s > best_score:
            best_score = s
            best = r

    if not best:
        return "", ""
    return (best.product_image_url or "").strip(), (best.product_page_url or "").strip()
