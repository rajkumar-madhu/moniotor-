from django.apps import AppConfig


class DcimConfig(AppConfig):
    name = "dcim"
    verbose_name = "Data centre (DCIM)"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import services
        register("dcim.readings", 300, services.run_readings)
