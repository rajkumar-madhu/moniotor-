from django import forms

from .models import UrlCheck


class UrlCheckForm(forms.ModelForm):
    class Meta:
        model = UrlCheck
        fields = ["name", "url", "check_type", "method", "expected_status", "interval_seconds", "site", "tags",
                  "expected_text", "degraded_ms", "timeout_seconds", "follow_redirects", "enabled"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. NSE Web Portal"}),
            "url": forms.URLInput(attrs={"placeholder": "https://example.com"}),
            "site": forms.TextInput(attrs={"placeholder": "Mumbai (NSE)"}),
            "tags": forms.TextInput(attrs={"placeholder": "e.g. trading, production"}),
            "expected_text": forms.TextInput(attrs={"placeholder": "Optional"}),
        }
        labels = {"check_type": "Check type", "interval_seconds": "Check interval", "site": "Site / Location",
                  "tags": "Tags (comma separated)", "degraded_ms": "Degraded above (ms)", "timeout_seconds": "Timeout (s)"}

    def clean_url(self):
        url = self.cleaned_data["url"]
        if not url.lower().startswith(("http://", "https://")):
            raise forms.ValidationError("Only http:// and https:// URLs can be checked.")
        return url
