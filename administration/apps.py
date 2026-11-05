from django.apps import AppConfig


class AdministrationConfig(AppConfig):
    name = "administration"
    verbose_name = "Administration"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from django.contrib import admin

        from core.jobs import register

        from . import jobs, signals  # noqa: F401  (connects the sign-in audit receivers)
        from .forms import ThrottledAdminAuthenticationForm
        register("administration.heartbeat", 60, jobs.heartbeat)
        # here rather than in the URLconf, so /admin/login/ is throttled from django.setup() on, not from first import
        admin.site.login_form = ThrottledAdminAuthenticationForm
