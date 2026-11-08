from django.apps import AppConfig


class HypervisorConfig(AppConfig):
    name = "hypervisor"
    verbose_name = "Hypervisor"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import jobs
        register("hypervisor.sync", jobs.SYNC_INTERVAL, jobs.sync)
        register("hypervisor.retention", jobs.RETENTION_INTERVAL, jobs.retention)
