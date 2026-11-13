from django.conf import settings
from django.urls import NoReverseMatch, reverse
from django.utils.text import slugify

from .nav import NAV, SECTIONS


def _brand():
    vendor = (getattr(settings, "LINKEDEYE_BRAND_VENDOR", None) or "").strip()
    product = (getattr(settings, "LINKEDEYE_BRAND_PRODUCT", None) or "LinkedEye").strip() or "LinkedEye"
    suite = (getattr(settings, "LINKEDEYE_BRAND_SUITE", None) or "linkedeye.in").strip() or "linkedeye.in"
    eyebrow = f"{vendor} {product}".strip() if vendor else product
    title_suffix = f"{product} · {vendor}" if vendor else product
    return {
        "vendor": vendor,
        "product": product,
        "suite": suite,
        "eyebrow": eyebrow,
        "title_suffix": title_suffix,
        "copyright_url": (getattr(settings, "LINKEDEYE_BRAND_COPYRIGHT_URL", None) or "https://linkedeye.in/").strip(),
        "copyright_label": (getattr(settings, "LINKEDEYE_BRAND_COPYRIGHT_LABEL", None) or "LinkedEye.in").strip(),
        "headline": (getattr(settings, "LINKEDEYE_BRAND_HEADLINE", None) or "").strip(),
        "lead": (getattr(settings, "LINKEDEYE_BRAND_LEAD", None) or "").strip(),
        "shell_subtitle": suite,
    }


def _resolve(item, path):
    if item.get("url"):
        try:
            href = reverse(item["url"])
        except NoReverseMatch:
            href = reverse("coming_soon", args=[slugify(item["label"])])
    else:
        href = reverse("coming_soon", args=[slugify(item["label"])])
    # `key` is the stable id the browser stores pins under: the URL name for a link, the slug for a group
    key = item.get("url") or slugify(item["label"])
    return {"label": item["label"], "icon": item.get("icon"), "href": href, "active": False, "chev": item.get("chev", False),
            "key": key, "badge": 0, "badge_text": ""}


def build_nav(path, badges=None):
    """The sidebar for one request path: ``(nav, sections)``.

    ``nav`` is the flat list of top-level entries (each with its ``children``, ``active`` and ``open`` flags, as
    before); ``sections`` files the same entries under the labelled sections of core.nav.SECTIONS, with the
    section holding the current page marked ``active``. Pure: no queries."""
    from . import overview
    badges = badges or {}
    nav = []
    for item in NAV:
        entry = _resolve(item, path)
        entry["section"] = item.get("section") or SECTIONS[-1][0]
        entry["children"] = [_resolve(c, path) for c in item.get("children", [])]
        count = badges.get(item.get("badge"), 0) if item.get("badge") else 0
        entry["badge"], entry["badge_text"] = count, overview.badge_text(count)
        nav.append(entry)
    # highlight the single most specific link whose URL prefixes the current path
    links = [e for n in nav for e in [n, *n["children"]]]
    matches = [e for e in links if path == e["href"] or (e["href"] != "/" and path.startswith(e["href"]))]
    if matches:
        max(matches, key=lambda e: len(e["href"]))["active"] = True
    for entry in nav:
        entry["open"] = entry["active"] or any(c["active"] for c in entry["children"])
    sections = []
    for key, label in SECTIONS:
        items = [e for e in nav if e["section"] == key]
        if items:
            sections.append({"key": key, "label": label, "items": items, "active": any(e["open"] for e in items),
                             "links": sum(len(e["children"]) or 1 for e in items)})
    return nav, sections


def shell(request):
    brand = _brand()
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {
            "le_version": settings.LINKEDEYE_VERSION,
            "csrf_cookie_name": settings.CSRF_COOKIE_NAME,
            "le_brand": brand,
        }
    from . import overview
    from .backend_summary import dashboard_env_badge
    from .access import role_for
    from .client_scope import active_client, narrow_site_names
    from .client_sites import clients_for_user
    role = role_for(request.user)
    badges = getattr(request, "le_badges", None) or overview.nav_badges(request.user)
    nav, nav_sections = build_nav(request.path, badges)
    admin_url = overview._url("administration:index") or overview._url("admin:index")
    client = active_client(request)
    sites = getattr(request, "le_site_names", None)
    if sites is None:
        sites = overview.site_names(request.user)
    if client:
        sites = narrow_site_names(sites, request.user, client)
    le_clients = clients_for_user(request.user)
    le_client_name = ""
    if client:
        for c in le_clients:
            if str(c.pk) == str(client):
                le_client_name = c.name
                break
    return {
        "nav": nav,
        "nav_sections": nav_sections,
        "le_version": settings.LINKEDEYE_VERSION,
        "csrf_cookie_name": settings.CSRF_COOKIE_NAME,
        "le_brand": brand,
        "le_problem_count": overview.problem_count(request.user),
        "le_sites": sites,
        "le_site": request.GET.get("site", ""),
        "le_env_badge": dashboard_env_badge(request.GET.get("site", "")),
        "le_client": client,
        "le_client_name": le_client_name,
        "le_clients": le_clients,
        "le_role": "Super Admin" if request.user.is_superuser else (role or "viewer").capitalize(),
        "le_admin_url": admin_url,
        "le_profile_url": overview._url("administration:profile"),
    }
