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


def portal_host(site_slug: str) -> str:
    """Hostname for the LinkedEye UI (no scheme), e.g. fs-le-dr.finspot.in."""
    slug = (site_slug or "").strip().lower()
    if not slug:
        return ""
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
    base = portal_base(site_slug)
    svc = (service or "").strip().lower().replace("_", "-")
    if not base or not svc:
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
    if not s.endswith("-le"):
        s = f"{s}-le" if s else ""
    return s
