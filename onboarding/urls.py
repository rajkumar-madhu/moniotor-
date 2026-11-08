from django.urls import path

from . import devices, intake, prometheus_step, views

app_name = "onboarding"
urlpatterns = [
    path("onboarding/", views.index, name="index"),
    path("onboarding/export.csv", views.runs_export, name="export"),
    path("onboarding/new/", views.new, name="new"),
    path("onboarding/service-discovery/", views.sd_page, name="sd_page"),
    path("onboarding/service-discovery/token/", views.sd_token, name="sd_token"),
    path("onboarding/<int:pk>/", views.run_detail, name="run"),
    path("onboarding/<int:pk>/log/", views.run_log, name="log"),
    path("onboarding/<int:pk>/poll/", views.poll, name="poll"),
    path("onboarding/<int:pk>/delete/", views.run_delete, name="run_delete"),
    path("onboarding/<int:pk>/step/<slug:name>/", views.step, name="step"),
    path("onboarding/<int:pk>/targets/<int:target_pk>/toggle/", views.target_toggle, name="target_toggle"),
    # intake: one host and the paste / CSV dry run, the SNMP device path, the Prometheus connection
    path("onboarding/<int:pk>/targets/single/", intake.add_single, name="targets_single"),
    path("onboarding/<int:pk>/targets/preview/", intake.preview, name="targets_preview"),
    path("onboarding/<int:pk>/snmp/", devices.snmp_save, name="snmp_save"),
    path("onboarding/<int:pk>/snmp/test/", devices.snmp_test, name="snmp_test"),
    path("onboarding/<int:pk>/snmp/exporter/", devices.snmp_exporter_save, name="snmp_exporter"),
    path("onboarding/<int:pk>/prometheus/", prometheus_step.prometheus_connect, name="prometheus_connect"),
    # machine-to-machine: Prometheus http_sd (bearer token, no session/CSRF)
    path("api/sd/targets.json", views.sd_targets, name="sd_targets"),
]
