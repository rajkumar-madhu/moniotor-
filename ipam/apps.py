from django.apps import AppConfig


class IpamConfig(AppConfig):
    name = "ipam"
    verbose_name = "IPAM"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import sync
        register("ipam.sync", 3600, sync.run_job)
