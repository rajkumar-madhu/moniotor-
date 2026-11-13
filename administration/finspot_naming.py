"""FinSpot public hostname conventions (legacy portal, lesites, tool ingresses).

Examples (fs-dr-le):
  portal     https://fs-le-dr.finspot.in/
  lesites    https://fs-le-dr.finspot.in/lesites/?site=fs-dr-le  (same host as portal)
  admin GLE  https://gle.finspot.in/lesites/?site=fs-dr-le       (global admin UI)
  grafana    https://grafana-fs-le-dr.finspot.in/
  entity     domain-fs-le-dr.finspot.in:443
"""
import re

DOMAIN = "finspot.in"
ADMIN_PORTAL_HOST = "gle.finspot.in"

_FS_ENV_LE = re.compile(r"^fs-(?P<env>[a-z0-9]+)-le$")
_FS_LE_REST = re.compile(r"^fs-le-(?P<rest>.+)$")

# Worker / portal hostname is fs-le-dev1 (172.16.0.74), not fs-le-dev.
FINSPOT_PORTAL_HOST_OVERRIDES: dict[str, str] = {
    "fs-le-dev-finspot": f"fs-le-dev1.{DOMAIN}",
}

FINSPOT_TOOL_HOST_OVERRIDES: dict[tuple[str, str], str] = {
    # Dev stack uses fs-le-dev1-prometheus, not prometheus-fs-le-dev.
    ("fs-le-dev-finspot", "prometheus"): f"fs-le-dev1-prometheus.{DOMAIN}",
}


def portal_host(site_slug: str) -> str:
    """Hostname for the LinkedEye UI (no scheme), e.g. fs-le-dr.finspot.in."""
    slug = (site_slug or "").strip().lower()
    if not slug:
        return ""
    if slug in FINSPOT_PORTAL_HOST_OVERRIDES:
        return FINSPOT_PORTAL_HOST_OVERRIDES[slug]
    m = _FS_ENV_LE.match(slug)
    if m:
        return f"fs-le-{m.group('env')}.{DOMAIN}"
    m = _FS_LE_REST.match(slug)
    if m:
        rest = m.group("rest")
        if rest.endswith("-finspot"):
            rest = rest[: -len("-finspot")]
        return f"fs-le-{rest}.{DOMAIN}"
    return f"{slug}.{DOMAIN}"


def portal_url(site_slug: str) -> str:
    host = portal_host(site_slug)
    return f"https://{host}" if host else ""


def lesites_url(site_slug: str, *, admin: bool = False) -> str:
    """Lesites UI on the site portal, or on gle.finspot.in when admin=True."""
    slug = (site_slug or "").strip()
    if not slug:
        return ""
    host = ADMIN_PORTAL_HOST if admin else portal_host(slug)
    if not host:
        return ""
    return f"https://{host}/lesites/?site={slug}"


def admin_lesites_url(site_slug: str) -> str:
    return lesites_url(site_slug, admin=True)


def portal_base(site_slug: str) -> str:
    """Portal hostname without domain suffix, e.g. fs-le-dr."""
    host = portal_host(site_slug)
    return host.removesuffix(f".{DOMAIN}") if host else ""


def tool_host(site_slug: str, service: str) -> str:
    """Per-tool ingress prefix pattern: grafana-fs-le-dr.finspot.in from fs-dr-le."""
    slug = (site_slug or "").strip().lower()
    svc = (service or "").strip().lower().replace("_", "-")
    if not slug or not svc:
        return ""
    key = (slug, svc)
    if key in FINSPOT_TOOL_HOST_OVERRIDES:
        return FINSPOT_TOOL_HOST_OVERRIDES[key]
    base = portal_base(slug)
    if not base:
        return ""
    return f"{svc}-{base}.{DOMAIN}"


def entity_host(site_slug: str) -> str:
    """Neo4j / entity ingress host, e.g. domain-fs-le-dr.finspot.in."""
    base = portal_base(site_slug)
    return f"domain-{base}.{DOMAIN}" if base else ""


def websocket_url(site_slug: str) -> str:
    host = portal_host(site_slug)
    return f"wss://{host}/websocket/ws/" if host else ""


def tool_url(site_slug: str, service: str) -> str:
    host = tool_host(site_slug, service)
    return f"https://{host}" if host else ""


def default_site_slug_from_workbook_stem(stem: str) -> str:
    """Normalize workbook stem (e.g. fs-dr-le, fs_dr_le Restore) to legacy sitename."""
    s = (stem or "").strip().lower().replace("_", "-")
    s = re.sub(r"(?i)[-\s]*restore[-\s]*devices(?:\s+\d+)?$", "", s).strip("- ")
    # Lesites dev portal slug; workbooks are often named fs-le-dev without -finspot.
    if s in ("fs-le-dev", "fs-le-dev-finspot"):
        return "fs-le-dev-finspot"
    # fs-le-* portal slugs (fs-le-isv, fs-le-dev-finspot) must not become fs-le-*-le.
    if _FS_LE_REST.match(s):
        return s
    if not s.endswith("-le"):
        s = f"{s}-le" if s else ""
    return s


# Canonical console site names and legacy inventory labels (pre–Site row).
FINSPOT_SITE_LABELS: dict[str, str] = {
    "fs-dx-le": "FinSpot DX DR",
    "fs-dr-le": "FinSpot DR",
    "fs-le-dev-finspot": "FinSpot Dev",
}
LEGACY_DEVICE_SITE_FOR_SLUG: dict[str, str] = {
    "fs-dx-le": "FinSpot DX",
    "fs-dr-le": "DR Site",
    "fs-le-dev-finspot": "FinSpot Dev",
}


def site_display_name(site_slug: str) -> str:
    """Human site name for ``core.Site.name`` from a Lesites slug (e.g. fs-dx-le)."""
    slug = (site_slug or "").strip().lower()
    if slug in FINSPOT_SITE_LABELS:
        return FINSPOT_SITE_LABELS[slug]
    m = _FS_ENV_LE.match(slug)
    if m:
        return f"FinSpot {m.group('env').upper()}"
    return slug.replace("-", " ").title() if slug else ""


def lesites_environment_for_slug(site_slug: str) -> str:
    """Lesites profile environment token from slug (DR, DX, PROD, …)."""
    slug = (site_slug or "").strip().lower()
    if slug in ("fs-le-dev-finspot",):
        return "DEV"
    m = _FS_ENV_LE.match(slug)
    if not m:
        return "PROD"
    env = m.group("env").upper()
    # DX DR stacks use the DX token in Lesites; DR uses DR.
    return env if env in ("PROD", "DR", "DEV", "UAT", "ISV", "DX") else "PROD"


def device_environment_for_slug(site_slug: str) -> str:
    """``network.Device.environment`` key from a Lesites slug."""
    slug = (site_slug or "").strip().lower()
    if slug in ("fs-le-dev-finspot",):
        return "dev"
    m = _FS_ENV_LE.match(slug)
    if not m:
        return "prod"
    env = m.group("env")
    if env in ("dr", "uat", "dev"):
        return env
    if env == "dx":
        return "dr"
    return "prod"


def infer_slug_from_workbook_path(path: str) -> str:
    """Legacy sitename slug from an .xlsx path (filename stem)."""
    from pathlib import Path

    return default_site_slug_from_workbook_stem(Path(path).stem)
