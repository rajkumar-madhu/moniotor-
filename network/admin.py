from django.contrib import admin

from .models import Device, DeviceImage, DevicePoll, DiscoveredHost, DiscoveryRun, Interface, NetworkProductImage


class InterfaceInline(admin.TabularInline):
    model = Interface
    extra = 0
    readonly_fields = ["if_index", "name", "alias", "if_type", "admin_status", "oper_status", "speed_mbps", "in_mbps",
                       "out_mbps", "utilization", "updated_at"]
    can_delete = False


@admin.register(DeviceImage)
class DeviceImageAdmin(admin.ModelAdmin):
    """Read-only: photos enter through the device form or the Product catalog page, which validate the bytes. The
    stored bytes are never rendered here."""
    list_display = ["sha256", "content_type", "width", "height", "byte_size", "source", "is_candidate", "tenant_key", "created_at"]
    list_filter = ["source", "is_candidate", "content_type", "tenant_key"]
    exclude = ["data"]
    readonly_fields = ["tenant_key", "sha256", "content_type", "width", "height", "byte_size", "source", "source_url", "origin",
                       "is_candidate", "candidate_owner", "uploaded_by", "created_at"]

    def has_add_permission(self, request):
        return False

    def get_queryset(self, request):
        return super().get_queryset(request).defer("data")


@admin.register(NetworkProductImage)
class NetworkProductImageAdmin(admin.ModelAdmin):
    raw_id_fields = ["image"]
    list_display = ["vendor", "device_type", "model_key", "tenant_key"]
    list_filter = ["vendor", "device_type", "tenant_key"]
    search_fields = ["vendor", "model_key", "notes"]
    ordering = ["tenant_key", "vendor", "device_type", "model_key"]


@admin.register(Device)
class DeviceAdmin(admin.ModelAdmin):
    list_display = ["name", "device_type", "vendor", "model", "mgmt_ip", "site", "status", "last_seen", "enabled"]
    list_filter = ["device_type", "status", "enabled", "environment", "site"]
    search_fields = ["name", "mgmt_ip", "vendor", "model", "serial", "tags", "product_image_url"]
    raw_id_fields = ["image"]
    readonly_fields = ["status", "last_seen", "last_checked_at", "last_latency_ms", "last_error", "status_changed_at",
                       "consecutive_failures", "uptime_seconds", "cpu_pct", "memory_pct", "temperature_c", "metrics_updated_at", "metrics_error", "created_at", "updated_at"]
    inlines = [InterfaceInline]


@admin.register(DevicePoll)
class DevicePollAdmin(admin.ModelAdmin):
    list_display = ["device", "checked_at", "status", "latency_ms", "error"]
    list_filter = ["status"]
    date_hierarchy = "checked_at"


class DiscoveredHostInline(admin.TabularInline):
    model = DiscoveredHost
    extra = 0
    readonly_fields = ["ip", "open_ports", "latency_ms", "status", "device", "decided_by_name", "decided_at"]


@admin.register(DiscoveryRun)
class DiscoveryRunAdmin(admin.ModelAdmin):
    list_display = ["id", "cidr", "ports", "status", "found", "requested_by_name", "created_at"]
    list_filter = ["status"]
    inlines = [DiscoveredHostInline]
