from django import forms

from . import registry
from .models import ScheduledReport


def report_choices(user):
    return [(r.key, f"{r.category} · {r.title}") for r in registry.REPORTS.values() if r.allowed(user)]


class ScheduleForm(forms.ModelForm):
    class Meta:
        model = ScheduledReport
        fields = ["name", "report_key", "window_days", "site", "frequency", "send_hour", "recipients", "enabled"]
        widgets = {"recipients": forms.Textarea(attrs={"rows": 3, "placeholder": "noc@example.com, cio@example.com"}),
                   "report_key": forms.Select()}

    def __init__(self, *args, user=None, sites=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["report_key"].widget.choices = report_choices(user)
        # Server-side choice list: the site names the user can see, not a free-text field with a <select> on top.
        site_field = self.fields["site"]
        self.fields["site"] = forms.ChoiceField(
            required=False, label=site_field.label, help_text=site_field.help_text,
            choices=[("", "All my sites")] + [(s, s) for s in sites])
        self.fields["window_days"].help_text = "Rows cover this many days before each send (ignored by snapshot reports)."
        self.user = user

    def clean_report_key(self):
        key = self.cleaned_data["report_key"]
        report = registry.REPORTS.get(key)
        if report is None or not report.allowed(self.user):
            raise forms.ValidationError("Choose a report you are allowed to run.")
        return key

    def clean_site(self):
        site = self.cleaned_data.get("site", "")
        if site:
            from core.access import can_on

            if not can_on(self.user, "operate", site):
                raise forms.ValidationError("You may only schedule reports for sites you can operate.")
        return site
