from django.apps import AppConfig


class ApmConfig(AppConfig):
    name = "apm"
    verbose_name = "APM"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import jobs
        register("apm.redis_poll", jobs.REDIS_INTERVAL, jobs.redis_poll)
        register("apm.stale", jobs.STALE_INTERVAL, jobs.stale)
        register("apm.retention", jobs.RETENTION_INTERVAL, jobs.retention)
