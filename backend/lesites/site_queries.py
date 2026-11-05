"""Explicit lesite column lists (avoids SELECT * and legacy Redmine columns)."""

LESITE_COLUMNS = (
    'lesite.id',
    'lesite.sitename',
    'lesite.location',
    'lesite.websocket_url',
    'lesite.entity_host',
    'lesite.entity_port',
    'lesite.is_URLSecured',
    'lesite.environment',
    'lesite.analytics_Prefix_URL',
    'lesite.redis_host',
    'lesite.redis_port',
    'lesite.is_enable',
    'lesite.prometheus_url',
    'lesite.le_url',
    'lesite.lat',
    'lesite.lng',
    'lesite.status',
    'lesite.grafana_api',
    'lesite.elastic_host',
    'lesite.elastic_port',
    'lesite.incident_url',
    'lesite.incident_api',
)

LESITE_SELECT = ', '.join(LESITE_COLUMNS)

# Legacy columns/tables removed with Redmine module (drop_redmine_schema command).
REMOVED_SITE_KEYS = frozenset({'redmine_url'})
