import json

from django import forms
from django.contrib import admin

from .forms import MAX_JSON_CHARS, MAX_JSON_DEPTH, nested_deeper_than
from .models import Execution, Rule, Runbook, Template


class RuleAdminForm(forms.ModelForm):
    """The admin edits action_params as raw JSON, so it applies the payload limits of the console's RuleForm: a deeper
    or larger payload could not be shown on the rule pages (pretty-printing fails near 1,000 levels on Python 3.12)."""

    class Meta:
        model = Rule
        fields = "__all__"

    def clean_action_params(self):
        params = self.cleaned_data.get("action_params")
        payload = params.get("payload") if isinstance(params, dict) else None
        if payload is not None:
            if nested_deeper_than(payload, MAX_JSON_DEPTH):
                raise forms.ValidationError(f"The payload is nested too deeply (at most {MAX_JSON_DEPTH} levels).")
            # measured as the console measures typed text: escaped, one non-ASCII character would count as six
            if len(json.dumps(payload, ensure_ascii=False)) > MAX_JSON_CHARS:
                raise forms.ValidationError(f"The payload is too large (at most {MAX_JSON_CHARS // 1024} KB).")
        return params


@admin.register(Runbook)
class RunbookAdmin(admin.ModelAdmin):
    list_display = ("title", "service", "ci_name", "site", "owner", "last_reviewed")
    list_filter = ("site",)
    search_fields = ("title", "service", "ci_name", "tags", "symptoms")


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    form = RuleAdminForm
    list_display = ("name", "enabled", "match_source", "min_severity", "action", "cooldown_minutes", "last_fired_at", "fire_count")
    list_filter = ("enabled", "action", "min_severity")
    search_fields = ("name", "description", "match_source", "title_contains")


@admin.register(Execution)
class ExecutionAdmin(admin.ModelAdmin):
    list_display = ("started_at", "kind", "trigger", "status", "http_status", "rule", "triggered_by")
    list_filter = ("kind", "status")
    search_fields = ("trigger", "error", "triggered_by", "fingerprint")
    readonly_fields = [f.name for f in Execution._meta.fields]


@admin.register(Template)
class TemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "applied_count", "last_applied_at")
    list_filter = ("kind",)
    search_fields = ("name", "description")
