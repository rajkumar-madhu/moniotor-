"""URLs for the Kubernetes inventory module.

The root urls.py includes this at "", so every path is declared in full. Pages for the remaining
workbook sheets are added here with the URL names the tab strip (k8s/tabs.py) already expects:
``k8s:workloads``, ``k8s:services``, ``k8s:ingresses``, ``k8s:volumes``, ``k8s:certificates``,
``k8s:tools``, ``k8s:issues`` — add the path here and flip the tab's ``enabled`` flag.
"""
from django.urls import path

from . import views, views_workload

app_name = "k8s"
urlpatterns = [
    path("kubernetes/", views.overview, name="overview"),
    path("kubernetes/clients/", views.clients, name="clients"),
    path("kubernetes/clients/export.csv", views.clients_export, name="clients_export"),
    path("kubernetes/clients/<int:pk>/link/", views.client_link, name="client_link"),
    path("kubernetes/namespaces/", views.namespaces, name="namespaces"),
    path("kubernetes/namespaces/export.csv", views.namespaces_export, name="namespaces_export"),
    path("kubernetes/nodes/", views.nodes, name="nodes"),
    path("kubernetes/nodes/export.csv", views.nodes_export, name="nodes_export"),
    path("kubernetes/nodes/<int:pk>/", views.node_detail, name="node_detail"),
    path("kubernetes/workloads/", views_workload.workloads, name="workloads"),
    path("kubernetes/workloads/export.csv", views_workload.workloads_export, name="workloads_export"),
    path("kubernetes/workloads/<int:pk>/", views_workload.workload_detail, name="workload_detail"),
    path("kubernetes/services/", views_workload.services_list, name="services"),
    path("kubernetes/services/export.csv", views_workload.services_export, name="services_export"),
    path("kubernetes/volumes/", views_workload.volumes, name="volumes"),
    path("kubernetes/volumes/export.csv", views_workload.volumes_export, name="volumes_export"),
    path("kubernetes/import/", views.import_inventory, name="import_inventory"),
    path("kubernetes/imports/", views.imports, name="imports"),
    path("kubernetes/imports/<int:pk>/", views.import_detail, name="import_detail"),
]

# Ingresses, certificates and secret references (sheets 06, 09, 08) declare their own routes.
from .views_ingress import urlpatterns as _edge_urlpatterns  # noqa: E402

urlpatterns += _edge_urlpatterns

# Tools, monitoring coverage and the issue register (sheets 10, 11, 12) declare their own routes.
from .views_ops import urlpatterns as _ops_urlpatterns  # noqa: E402

urlpatterns += _ops_urlpatterns
