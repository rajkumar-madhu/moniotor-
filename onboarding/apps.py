from django.apps import AppConfig


class OnboardingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "onboarding"
    verbose_name = "Onboarding"

    def ready(self):
        from core.jobs import register

        from . import jobs
        register("onboarding.liveness", jobs.LIVENESS_INTERVAL, jobs.liveness)
        register("onboarding.housekeeping", jobs.HOUSEKEEPING_INTERVAL, jobs.housekeeping)
