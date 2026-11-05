"""Defaults and port-connection rows for FinSpot-style site onboarding."""
from administration.finspot_naming import (
    entity_host,
    portal_url,
    tool_host,
    tool_url,
    websocket_url,
)
ENV_CHOICES = ("PROD", "DR", "DEV", "UAT", "ISV", "DX")

TOOL_ROWS = [
    ("LinkedEye UI", "linkedeye", "443", "Keycloak SSO (OIDC)"),
    ("Keycloak", "keycloak", "443", "Keycloak SSO"),
    ("Grafana", "grafana", "443", "Keycloak SSO"),
    ("Jenkins", "jenkins", "443", "Keycloak SSO"),
    ("Prometheus", "prometheus", "443", "Keycloak?oauth2-proxy"),
    ("AlertManager", "alertmanager", "443", "Keycloak?oauth2-proxy"),
    ("StackStorm", "stackstom", "443", "Keycloak?oauth2-proxy"),
    ("Neo4j", "neo4j", "7687", "Keycloak?oauth2-proxy"),
    ("Monitor (Nagios)", "monitor", "443", "Keycloak?oauth2-proxy"),
]


def legacy_slug_for_site(site) -> str:
    code = (getattr(site, "code", None) or "").strip().lower()
    if code:
        return code
    return (site.name or "").strip().lower().replace(" ", "-")


def default_profile_fields(site, slug: str | None = None) -> dict:
    """Suggested lesites field values from FinSpot naming (no secrets required)."""
    slug = (slug or legacy_slug_for_site(site)).strip().lower()
    portal = portal_url(slug)
    return {
        "legacy_sitename": slug,
        "websocket_url": websocket_url(slug),
        "environment": "PROD",
        "entity_host": entity_host(slug),
        "entity_port": "443",
        "is_url_secured": True,
        "analytics_prefix_url": tool_url(slug, "grafana"),
        "location": "IN-KA",
        "country_code": "IN",
        "redis_host": "",
        "redis_port": "6379",
        "prometheus_url": tool_url(slug, "prometheus"),
        "elastic_host": "analytics-es",
        "elastic_port": "9200",
        "grafana_api": "",
        "le_url": portal,
        "incident_url": "",
        "incident_api": "",
        "enabled": True,
    }


def port_connection_rows(site_slug: str) -> list[dict]:
    """Tool/service port table (matches scaffold_client_tracker_csv)."""
    slug = (site_slug or "").strip().lower()
    rows = []
    for label, svc, port, auth in TOOL_ROWS:
        if svc == "linkedeye":
            url = portal_url(slug)
            host = url.replace("https://", "").rstrip("/") if url else ""
        else:
            host = tool_host(slug, svc)
            url = f"https://{host}" if host else ""
        rows.append({"label": label, "service": svc, "host": host, "url": url, "port": port, "auth": auth})
    return rows
