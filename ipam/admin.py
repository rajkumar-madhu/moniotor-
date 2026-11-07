from django.contrib import admin

from .models import DNSRecord, DNSZone, IPAddress, Subnet, SyncRun, VLAN


@admin.register(VLAN)
class VLANAdmin(admin.ModelAdmin):
    list_display = ("vid", "name", "site", "tenant_key")
    list_filter = ("site",)
    search_fields = ("name", "description")


@admin.register(Subnet)
class SubnetAdmin(admin.ModelAdmin):
    list_display = ("cidr", "vrf", "site", "role", "vlan", "gateway", "tenant_key")
    list_filter = ("version", "role", "site")
    search_fields = ("cidr", "description", "vrf")


@admin.register(IPAddress)
class IPAddressAdmin(admin.ModelAdmin):
    list_display = ("address", "vrf", "status", "hostname", "subnet", "site", "source", "last_seen")
    list_filter = ("status", "source", "version")
    search_fields = ("address", "hostname", "description")
    raw_id_fields = ("subnet", "device", "ci")


@admin.register(DNSZone)
class DNSZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "site", "default_ttl", "tenant_key")
    list_filter = ("kind",)
    search_fields = ("name",)


@admin.register(DNSRecord)
class DNSRecordAdmin(admin.ModelAdmin):
    list_display = ("name", "zone", "rtype", "value", "ttl")
    list_filter = ("rtype",)
    search_fields = ("name", "value")
    raw_id_fields = ("zone", "ip")


@admin.register(SyncRun)
class SyncRunAdmin(admin.ModelAdmin):
    list_display = ("ran_at", "trigger", "actor_name", "created", "updated", "conflicts", "new_conflicts")
    readonly_fields = ("report", "conflict_keys")
