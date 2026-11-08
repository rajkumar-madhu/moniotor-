"""Create or reconcile ``core.Site`` + Lesites profile for FinSpot legacy slugs."""
from __future__ import annotations

from django.db import transaction

from administration.finspot_naming import (
    LEGACY_DEVICE_SITE_FOR_SLUG,
    device_environment_for_slug,
    infer_slug_from_workbook_path,
    lesites_environment_for_slug,
    site_display_name,
)
from administration.lesites_defaults import default_profile_fields


def ensure_finspot_site(
    tenant_key: str,
    site_slug: str,
    *,
    client_code: str = "finspot",
    sync_device_sites: bool = True,
):
    """Return ``(site, created, devices_renamed)`` for a slug such as ``fs-dx-le``."""
    from core.models import Client, Site, SiteLesiteProfile
    from network.models import Device

    slug = (site_slug or "").strip().lower()
    if not slug:
        raise ValueError("site_slug is required")

    name = site_display_name(slug)
    legacy = LEGACY_DEVICE_SITE_FOR_SLUG.get(slug)

    with transaction.atomic():
        client, _ = Client.objects.get_or_create(
            tenant_key=tenant_key,
            code=client_code,
            defaults={"name": "FinSpot"},
        )
        site, created = Site.objects.get_or_create(
            tenant_key=tenant_key,
            code=slug,
            defaults={"name": name, "client": client, "criticality": "high"},
        )
        if site.name != name:
            site.name = name
            site.save(update_fields=["name"])
        if site.client_id is None:
            site.client = client
            site.save(update_fields=["client"])

        defaults = default_profile_fields(site, slug)
        defaults["environment"] = lesites_environment_for_slug(slug)
        defaults["legacy_sitename"] = slug
        profile, _ = SiteLesiteProfile.objects.get_or_create(site=site, defaults=defaults)
        changed = []
        for field, value in defaults.items():
            if getattr(profile, field) != value:
                setattr(profile, field, value)
                changed.append(field)
        if changed:
            profile.save(update_fields=changed + ["updated_at"])

        devices_renamed = 0
        if sync_device_sites and legacy and legacy != name:
            devices_renamed = Device.objects.filter(tenant_key=tenant_key, site=legacy).update(site=name)

    return site, created, devices_renamed


def resolve_import_site(tenant_key: str, path: str, site: str, site_code: str):
    """Pick site name for Lesites import; optional ``ensure_finspot_site`` when code/slug known."""
    if site_code:
        from core.models import Site

        found = Site.objects.filter(tenant_key=tenant_key, code__iexact=site_code).first()
        if found:
            return found.name, site_code
        ensure_finspot_site(tenant_key, site_code)
        return site_display_name(site_code), site_code
    if site:
        return site, ""
    slug = infer_slug_from_workbook_path(path)
    if slug:
        ensure_finspot_site(tenant_key, slug)
        return site_display_name(slug), slug
    return "DR Site", ""
