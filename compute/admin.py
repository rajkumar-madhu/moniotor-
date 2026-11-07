from django.contrib import admin

from .models import Cluster, ClusterMember, VMPlacement


@admin.register(Cluster)
class ClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "site", "prometheus_selector", "k8s_state", "tenant_key")
    list_filter = ("kind", "k8s_state")
    search_fields = ("name", "description", "tags")


@admin.register(ClusterMember)
class ClusterMemberAdmin(admin.ModelAdmin):
    list_display = ("device", "cluster", "role", "cpu_cores", "memory_gb", "tenant_key")
    list_filter = ("role",)
    raw_id_fields = ("device", "cluster")


@admin.register(VMPlacement)
class VMPlacementAdmin(admin.ModelAdmin):
    list_display = ("vm", "host", "cluster", "power_state", "vcpus", "memory_gb", "disk_gb", "tenant_key")
    list_filter = ("power_state",)
    raw_id_fields = ("vm", "host", "cluster")
