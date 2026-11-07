from django.apps import AppConfig


class ComputeConfig(AppConfig):
    name = "compute"
    verbose_name = "Compute & virtualization"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import jobs

        register("compute.k8s", 300, jobs.collect_k8s)
