from django.contrib import admin

from .models import ClientProfile, ImportRun, KubeCluster, Namespace, Node


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ("source_name", "started_at", "status", "uploaded_by", "site")
    list_filter = ("status", "site")
    search_fields = ("source_name", "source_sha256")
    readonly_fields = ("counts", "errors", "warnings", "metadata", "source_sha256")
    date_hierarchy = "started_at"


@admin.register(KubeCluster)
class KubeClusterAdmin(admin.ModelAdmin):
    list_display = ("name", "site", "kubernetes_version", "context", "discovered_at", "missing_since")
    list_filter = ("site",)
    search_fields = ("name", "context")


@admin.register(Node)
class NodeAdmin(admin.ModelAdmin):
    list_display = ("hostname", "cluster", "role", "exposure", "internal_ip", "ready", "missing_since")
    list_filter = ("role", "exposure", "ready", "cluster")
    search_fields = ("hostname", "internal_ip", "os")
    raw_id_fields = ("ci", "device", "member", "import_run")


@admin.register(Namespace)
class NamespaceAdmin(admin.ModelAdmin):
    list_display = ("name", "cluster", "client_label", "client", "environment", "confidence",
                    "mapping_status")
    list_filter = ("mapping_status", "confidence", "environment", "cluster")
    search_fields = ("name", "client_label")
    raw_id_fields = ("client", "import_run")


@admin.register(ClientProfile)
class ClientProfileAdmin(admin.ModelAdmin):
    list_display = ("workbook_name", "workbook_id", "client", "status", "health", "open_issues", "site")
    list_filter = ("status", "health", "site", "summary_only")
    search_fields = ("workbook_name", "workbook_id", "primary_namespace")
    raw_id_fields = ("client", "import_run")
