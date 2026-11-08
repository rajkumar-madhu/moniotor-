from django.contrib import admin

from .models import Credential, OnboardingRun, OnboardingTarget, SDFetch, StepLog


class TargetInline(admin.TabularInline):
    model = OnboardingTarget
    extra = 0
    fields = ("hostname", "address", "target_type", "port", "state", "enabled")
    readonly_fields = ("state",)


@admin.register(OnboardingRun)
class RunAdmin(admin.ModelAdmin):
    list_display = ("id", "kind", "site", "client", "status", "step", "created_at")
    list_filter = ("kind", "status", "site")
    search_fields = ("site", "notes")
    inlines = [TargetInline]


@admin.register(Credential)
class CredentialAdmin(admin.ModelAdmin):
    """The ciphertext is `editable=False`, so no admin page can show or change it here.
    Rotation happens in the onboarding wizard, which audits it."""
    list_display = ("name", "kind", "username", "site", "created_at", "last_used_at")
    list_filter = ("kind", "site")
    search_fields = ("name", "username")
    readonly_fields = ("created_at", "rotated_at", "last_used_at")
    exclude = ("secret",)

    def has_add_permission(self, request):
        # a row made here would have no secret; credentials are created in the wizard, which encrypts
        return False


@admin.register(StepLog)
class StepLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "run", "step", "status", "detail")
    list_filter = ("step", "status")


@admin.register(SDFetch)
class SDFetchAdmin(admin.ModelAdmin):
    list_display = ("fetched_at", "source_ip", "target_count", "result")
    list_filter = ("result",)
