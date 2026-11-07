from django.urls import path

from . import views

app_name = "dcim"
urlpatterns = [
    path("dcim/", views.overview, name="overview"),
    path("dcim/racks/", views.racks, name="racks"),
    path("dcim/racks/export.csv", views.racks_export, name="racks_export"),
    path("dcim/racks/new/", views.rack_create, name="rack_create"),
    path("dcim/racks/<int:pk>/", views.rack_detail, name="rack_detail"),
    path("dcim/racks/<int:pk>/edit/", views.rack_edit, name="rack_edit"),
    path("dcim/racks/<int:pk>/delete/", views.rack_delete, name="rack_delete"),
    path("dcim/racks/<int:pk>/items/new/", views.item_create, name="item_create"),
    path("dcim/items/export.csv", views.items_export, name="items_export"),
    path("dcim/items/<int:pk>/edit/", views.item_edit, name="item_edit"),
    path("dcim/items/<int:pk>/delete/", views.item_delete, name="item_delete"),
    path("dcim/rooms/new/", views.room_create, name="room_create"),
    path("dcim/rooms/<int:pk>/edit/", views.room_edit, name="room_edit"),
    path("dcim/rooms/<int:pk>/delete/", views.room_delete, name="room_delete"),
    path("dcim/power/", views.power, name="power"),
    path("dcim/power/export.csv", views.power_export, name="power_export"),
    path("dcim/power/feeds/new/", views.feed_create, name="feed_create"),
    path("dcim/power/feeds/<int:pk>/edit/", views.feed_edit, name="feed_edit"),
    path("dcim/power/feeds/<int:pk>/delete/", views.feed_delete, name="feed_delete"),
    path("dcim/readings/run/", views.readings_run, name="readings_run"),
    path("dcim/cooling/", views.cooling, name="cooling"),
    path("dcim/cooling/units/new/", views.cooling_create, name="cooling_create"),
    path("dcim/cooling/units/<int:pk>/edit/", views.cooling_edit, name="cooling_edit"),
    path("dcim/cooling/units/<int:pk>/delete/", views.cooling_delete, name="cooling_delete"),
]
