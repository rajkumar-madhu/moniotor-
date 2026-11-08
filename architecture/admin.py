from django.contrib import admin

from .models import Link, Placement


@admin.register(Link)
class LinkAdmin(admin.ModelAdmin):
    list_display = ("a_device", "a_port", "b_device", "b_port", "medium", "speed_mbps", "kind", "origin", "confirmed")
    list_filter = ("medium", "origin", "confirmed")
    search_fields = ("a_device__name", "b_device__name", "a_port", "b_port")
    raw_id_fields = ("a_device", "b_device")


@admin.register(Placement)
class PlacementAdmin(admin.ModelAdmin):
    list_display = ("device", "tier", "group", "colour", "sort_order", "hidden")
    list_filter = ("tier", "hidden")
    search_fields = ("device__name", "group")
    raw_id_fields = ("device",)
