from django.urls import path

from . import views

app_name = "telemetry"

urlpatterns = [
    path("monitoring/fleet/", views.fleet, name="fleet"),
    path("monitoring/overview/", views.overview, name="overview"),
    path("monitoring/overview/data.json", views.overview_json, name="overview_json"),
    path("monitoring/infrastructure/", views.infrastructure, name="infrastructure"),
    # before host_detail: <path:instance> would otherwise swallow ".../metrics/" and ".../metrics.json"
    path("monitoring/infrastructure/<path:instance>/metrics/", views.host_metrics, name="host_metrics"),
    path("monitoring/infrastructure/<path:instance>/metrics.json", views.host_metrics_json, name="host_metrics_json"),
    path("monitoring/infrastructure/<path:instance>/", views.host_detail, name="host_detail"),
    path("monitoring/discovery/", views.discovery, name="discovery"),
    path("monitoring/discovery/run/", views.discovery_run, name="discovery_run"),
    path("monitoring/discovery/action/", views.discovery_action, name="discovery_action"),
    path("monitoring/discovery/<int:pk>/", views.discovery_detail, name="discovery_detail"),
    path("monitoring/nagios/", views.nagios_status, name="nagios"),
    path("monitoring/prometheus/", views.prometheus_targets, name="prometheus"),
    path("monitoring/alerts/", views.alert_list, name="alertmanager"),
    path("monitoring/alerts/acknowledge/", views.acknowledge, name="alert_ack_bulk"),
    path("monitoring/alerts/<int:pk>/", views.alert_detail, name="alert_detail"),
    path("monitoring/alerts/<int:pk>/acknowledge/", views.acknowledge, name="alert_ack"),
    path("monitoring/alerts/<int:pk>/unacknowledge/", views.unacknowledge, name="alert_unack"),
    path("monitoring/metrics-explorer/", views.explorer, name="explorer"),
    path("monitoring/metrics-explorer/save/", views.save_query, name="query_save"),
    path("monitoring/metrics-explorer/<int:pk>/delete/", views.delete_query, name="query_delete"),
    path("api/v1/alertmanager/", views.alertmanager_webhook, name="alertmanager_webhook"),
]
