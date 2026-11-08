from django.urls import path

from . import views

app_name = "onboarding"
urlpatterns = [
    path("onboarding/", views.index, name="index"),
    path("onboarding/export.csv", views.runs_export, name="export"),
    path("onboarding/new/", views.new, name="new"),
    path("onboarding/service-discovery/", views.sd_page, name="sd_page"),
    path("onboarding/service-discovery/token/", views.sd_token, name="sd_token"),
    path("onboarding/<int:pk>/", views.run_detail, name="run"),
    path("onboarding/<int:pk>/log/", views.run_log, name="log"),
    path("onboarding/<int:pk>/delete/", views.run_delete, name="run_delete"),
    path("onboarding/<int:pk>/step/<slug:name>/", views.step, name="step"),
    path("onboarding/<int:pk>/targets/<int:target_pk>/toggle/", views.target_toggle, name="target_toggle"),
    # machine-to-machine: Prometheus http_sd (bearer token, no session/CSRF)
    path("api/sd/targets.json", views.sd_targets, name="sd_targets"),
]
