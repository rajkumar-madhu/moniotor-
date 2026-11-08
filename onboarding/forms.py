"""Onboarding forms.

The credential form is write-only by construction: the secret field is never given an initial
value, `PasswordInput(render_value=False)` cannot echo what was typed back into the page, and the
model's `secret` column is `editable=False` so no ModelForm can reach it.
"""
from django import forms

from core.models import Client
from core.secrets import MASK

from .models import CredentialKind, OnboardingRun, RunKind, TargetType
from .services import MAX_PASTE_BYTES, onboardable_sites, tenant

class SecretTextarea(forms.Textarea):
    """A multi-line secret box (SSH private key) that never renders a value, even after a failed
    validation: a bound form would otherwise write the typed key back into the page."""

    def format_value(self, value):
        return None


ENVIRONMENTS = [("prod", "Production"), ("uat", "UAT"), ("dr", "DR"), ("dev", "Development")]


class ScopeForm(forms.ModelForm):
    """Step 1: what is being onboarded, into which site and client."""
    site = forms.ChoiceField(choices=(), label="Site",
                             help_text="You can only onboard into a site you administer.")

    class Meta:
        model = OnboardingRun
        fields = ["kind", "site", "client", "environment", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3,
                   "placeholder": "Why this onboarding run exists, change reference, …"})}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        names = onboardable_sites(user) if user is not None else []
        self.fields["site"].choices = [("", "Select a site…")] + [(n, n) for n in names]
        self.fields["kind"].choices = RunKind.choices
        self.fields["environment"] = forms.ChoiceField(choices=ENVIRONMENTS, initial="prod", label="Environment")
        self.fields["client"].queryset = Client.objects.filter(tenant_key=tenant()).order_by("name")
        self.fields["client"].required = False
        self.fields["client"].empty_label = "The site's own client"
        self.fields["client"].help_text = "Left empty, the client that owns the site is used."
        if not names:
            self.fields["site"].help_text = ("You do not administer any site yet. An administrator for the "
                                             "site must start the run.")

    def clean_site(self):
        site = (self.cleaned_data.get("site") or "").strip()
        allowed = onboardable_sites(self.user) if self.user is not None else []
        if site not in allowed:
            raise forms.ValidationError("You need the admin role on that site to onboard into it.")
        return site


class TargetsForm(forms.Form):
    """Step 2: one host, a pasted list, or a CSV."""
    hosts = forms.CharField(
        label="Hosts", required=False, widget=forms.Textarea(attrs={
            "rows": 7, "placeholder": "db-01\nweb-01:9100\n10.0.3.14, device\napp-02, linux, 9100"}),
        help_text="One per line. “host”, “host:port”, “host, type” or “host, type, port”.")
    csv_file = forms.FileField(label="CSV file", required=False,
                               help_text="Columns hostname, address, type, port (a header row is detected).")
    target_type = forms.ChoiceField(label="Default type", choices=TargetType.choices,
                                    initial=TargetType.LINUX, required=False)
    port = forms.IntegerField(label="Default port", required=False, min_value=1, max_value=65535,
                              help_text="Left empty: the standard port for the type (node 9100, SNMP 9116, …).")

    def clean(self):
        data = super().clean()
        text = (data.get("hosts") or "").strip()
        if len(text.encode("utf-8", "ignore")) > MAX_PASTE_BYTES:
            raise forms.ValidationError("That list is too large; import it as a CSV file instead.")
        if not text and not data.get("csv_file"):
            raise forms.ValidationError("Paste at least one host or choose a CSV file.")
        return data


class CredentialForm(forms.Form):
    """Step 3: hand over a secret. Write-only - nothing here is ever rendered back."""
    name = forms.CharField(label="Label", max_length=120,
                           help_text="How this credential is listed. Never put the secret in the label.")
    kind = forms.ChoiceField(label="Type", choices=CredentialKind.choices, initial=CredentialKind.SSH_PASSWORD)
    username = forms.CharField(
        label="Username / key id", max_length=150, required=False,
        help_text="SSH: the login name. SNMP v3: the security name. HTTP basic: the user. "
                  "SNMP v2c and bearer tokens: leave empty and put the community or token in Secret.")
    secret = forms.CharField(
        label="Secret", strip=False, required=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password",
                                                              "placeholder": MASK}),
        help_text="Encrypted with the vault key the moment you save. It is never shown again.")
    secret_multiline = forms.CharField(
        label="Private key", required=False, strip=False,
        widget=SecretTextarea(attrs={"rows": 5, "autocomplete": "off", "spellcheck": "false",
                                     "placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----"}),
        help_text="Paste the key here instead of the single-line box for an SSH key.")
    notes = forms.CharField(label="Notes", max_length=300, required=False)
    apply_to = forms.ChoiceField(label="Apply to", required=False, initial="all", choices=[
        ("all", "Every target of this run"), ("none", "Do not attach it yet")])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # belt and braces: a secret must never arrive back in the page as an initial value
        for field in ("secret", "secret_multiline"):
            self.fields[field].widget.attrs.pop("value", None)
            self.initial.pop(field, None)

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        single, multi = data.get("secret") or "", data.get("secret_multiline") or ""
        value = multi if (kind == CredentialKind.SSH_KEY and multi.strip()) else (single or multi)
        if not (value or "").strip():
            raise forms.ValidationError("Enter the secret. Nothing is stored unencrypted.")
        if len(value) > 20000:
            raise forms.ValidationError("That secret is larger than the vault accepts (20 000 characters).")
        data["secret_value"] = value
        return data


class RotateForm(forms.Form):
    """Replace a stored secret in place. No history of values is kept."""
    secret = forms.CharField(label="New secret", strip=False, required=False,
                             widget=forms.PasswordInput(render_value=False,
                                                        attrs={"autocomplete": "new-password"}))
    secret_multiline = forms.CharField(label="New private key", required=False, strip=False,
                                       widget=SecretTextarea(attrs={"rows": 5, "autocomplete": "off", "spellcheck": "false"}))

    def clean(self):
        data = super().clean()
        value = (data.get("secret_multiline") or "").strip() or (data.get("secret") or "")
        if not (value or "").strip():
            raise forms.ValidationError("Enter the new secret.")
        data["secret_value"] = value
        return data
