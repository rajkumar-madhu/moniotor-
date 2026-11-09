from .alerts import acknowledge, alert_detail, alert_list, unacknowledge  # noqa: F401
from .discovery import discovery, discovery_action, discovery_detail, discovery_run  # noqa: F401
from .explorer import delete_query, explorer, save_query  # noqa: F401
from .fleet import fleet  # noqa: F401
from .infra import host_detail, host_metrics, host_metrics_json, infrastructure  # noqa: F401
from .nagios import nagios_status  # noqa: F401
from .overview import overview, overview_json  # noqa: F401
from .prom import prometheus_targets  # noqa: F401
from .webhook import alertmanager_webhook  # noqa: F401
