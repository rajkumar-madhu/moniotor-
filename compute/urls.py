from django.urls import path

from . import views

app_name = "compute"
urlpatterns = [
    path("compute/", views.overview, name="overview"),
    path("compute/servers/", views.servers, name="servers"),
    path("compute/servers/export.csv", views.servers_export, name="servers_export"),
    path("compute/vms/", views.vms, name="vms"),
    path("compute/vms/export.csv", views.vms_export, name="vms_export"),
    path("compute/vms/<int:vm_pk>/placement/", views.placement_edit, name="placement_edit"),
    path("compute/placements/<int:pk>/delete/", views.placement_delete, name="placement_delete"),
    path("compute/clusters/", views.clusters, name="clusters"),
    path("compute/clusters/export.csv", views.clusters_export, name="clusters_export"),
    path("compute/clusters/new/", views.cluster_create, name="cluster_create"),
    path("compute/clusters/<int:pk>/", views.cluster_detail, name="cluster_detail"),
    path("compute/clusters/<int:pk>/edit/", views.cluster_edit, name="cluster_edit"),
    path("compute/clusters/<int:pk>/delete/", views.cluster_delete, name="cluster_delete"),
    path("compute/clusters/<int:pk>/refresh/", views.cluster_refresh, name="cluster_refresh"),
    path("compute/clusters/<int:pk>/members/add/", views.member_add, name="member_add"),
    path("compute/members/<int:pk>/edit/", views.member_edit, name="member_edit"),
    path("compute/members/<int:pk>/delete/", views.member_delete, name="member_delete"),
    path("compute/kubernetes/", views.kubernetes, name="kubernetes"),
]
