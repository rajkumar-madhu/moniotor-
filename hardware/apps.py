from django.apps import AppConfig


class HardwareConfig(AppConfig):
    name = "hardware"
    verbose_name = "Server hardware"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import jobs
        register("hardware.poll", jobs.POLL_INTERVAL, jobs.poll, budget=jobs.POLL_BUDGET)
        register("hardware.retention", jobs.RETENTION_INTERVAL, jobs.retention)
