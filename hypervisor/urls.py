from django.urls import path

from . import views

app_name = "hypervisor"
urlpatterns = [
    path("hypervisor/sources/", views.sources, name="sources"),
    path("hypervisor/sources/new/", views.source_new, name="source_new"),
    path("hypervisor/sources/<int:pk>/", views.source_edit, name="source_edit"),
    path("hypervisor/sources/<int:pk>/rotate/", views.source_rotate, name="source_rotate"),
    path("hypervisor/sources/<int:pk>/toggle/", views.source_toggle, name="source_toggle"),
    path("hypervisor/sources/<int:pk>/delete/", views.source_delete, name="source_delete"),
    path("hypervisor/sources/<int:pk>/test/", views.source_test, name="source_test"),
    path("hypervisor/sources/<int:pk>/pin/", views.source_pin, name="source_pin"),
    path("hypervisor/sources/<int:pk>/sync/", views.source_sync, name="source_sync"),
    path("hypervisor/datastores/", views.datastores, name="datastores"),
    path("hypervisor/datastores/live/", views.datastores_live, name="datastores_live"),
    path("hypervisor/datastores/export.csv", views.datastores_export, name="datastores_export"),
    path("hypervisor/unmatched/", views.unmatched, name="unmatched"),
    path("hypervisor/vm/<int:pk>/create-device/", views.match_create, name="match_create"),
    path("hypervisor/vm/<int:pk>/link/", views.match_link, name="match_link"),
    path("hypervisor/vm/<int:pk>/ignore/", views.match_ignore, name="match_ignore"),
    path("hypervisor/vm/<int:pk>/unignore/", views.match_unignore, name="match_unignore"),
    path("hypervisor/vm/<int:pk>/unlink/", views.match_unlink, name="match_unlink"),
    path("hypervisor/requests/", views.requests_list, name="requests"),
    path("hypervisor/requests/export.csv", views.requests_export, name="requests_export"),
    path("hypervisor/requests/<int:pk>/decide/", views.request_decide, name="request_decide"),
    path("hypervisor/device/<int:device_pk>/request/", views.vm_request, name="vm_request"),
    path("hypervisor/device/<int:device_pk>/live/", views.vm_live, name="vm_live"),
]
