from django.contrib import admin

from .models import CoolingUnit, PowerFeed, Rack, RackItem, Room


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "floor", "tenant_key")
    list_filter = ("site",)
    search_fields = ("name", "floor")


class RackItemInline(admin.TabularInline):
    model = RackItem
    extra = 0
    raw_id_fields = ("device", "ci")


@admin.register(Rack)
class RackAdmin(admin.ModelAdmin):
    list_display = ("name", "room", "site", "u_height", "status", "max_power_kw", "power_state")
    list_filter = ("status", "site")
    search_fields = ("name", "room__name")
    inlines = [RackItemInline]


@admin.register(PowerFeed)
class PowerFeedAdmin(admin.ModelAdmin):
    list_display = ("name", "rack", "kind", "capacity_kw", "measured_kw", "last_value", "last_read_at")
    search_fields = ("name", "rack__name")


@admin.register(CoolingUnit)
class CoolingUnitAdmin(admin.ModelAdmin):
    list_display = ("name", "room", "kind", "status", "capacity_kw", "supply_temp_c", "return_temp_c")
    list_filter = ("kind", "status")
    search_fields = ("name", "room__name")
