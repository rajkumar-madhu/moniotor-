from django.urls import path

from . import views

app_name = "architecture"
urlpatterns = [
    path("network/architecture/", views.overview, name="overview"),
    path("network/architecture/live/", views.live, name="live"),
    path("network/architecture/links/", views.links, name="links"),
    path("network/architecture/links/export.csv", views.links_export, name="links_export"),
    path("network/architecture/links/import/", views.links_import, name="links_import"),
    path("network/architecture/links/bulk/", views.links_bulk, name="links_bulk"),
    path("network/architecture/links/suggest/", views.suggest, name="suggest"),
    path("network/architecture/links/new/", views.link_form, name="link_create"),
    path("network/architecture/links/<int:pk>/edit/", views.link_form, name="link_edit"),
    path("network/architecture/links/<int:pk>/delete/", views.link_delete, name="link_delete"),
    path("network/architecture/devices/<int:pk>/placement/", views.placement_edit, name="placement_edit"),
    path("network/architecture/devices/<int:pk>/placement/reset/", views.placement_reset, name="placement_reset"),
    path("network/architecture/example/", views.example_load, name="example_load"),
]
