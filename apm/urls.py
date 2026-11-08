from django.urls import path

from . import api, views

app_name = "apm"
urlpatterns = [
    path("apm/", views.index, name="index"),
    path("apm/live/", views.live, name="live"),
    path("apm/export.csv", views.export, name="export"),
    path("apm/cell/<int:pk>/", views.cell_json, name="cell"),
    path("apm/cell/<int:pk>/evidence.txt", views.cell_evidence, name="cell_evidence"),
    path("apm/cell/<int:pk>/retry/", views.cell_retry, name="cell_retry"),
    path("apm/cell/<int:pk>/rca/", views.cell_rca, name="cell_rca"),
    path("apm/sources/", views.sources, name="sources"),
    path("apm/sources/new/", views.source_new, name="source_new"),
    path("apm/sources/<int:pk>/", views.source_edit, name="source_edit"),
    path("apm/sources/<int:pk>/rotate/", views.source_rotate, name="source_rotate"),
    path("apm/sources/<int:pk>/toggle/", views.source_toggle, name="source_toggle"),
    path("apm/sources/<int:pk>/delete/", views.source_delete, name="source_delete"),
    path("apm/sources/<int:pk>/test-redis/", views.source_test_redis, name="source_test_redis"),
    # machine-to-machine (bearer token per source; no session, no CSRF)
    path("api/apm/ingest/", api.ingest, name="api_ingest"),
    path("api/apm/commands/", api.commands, name="api_commands"),
    path("api/apm/commands/<int:pk>/done/", api.command_done, name="api_command_done"),
]
