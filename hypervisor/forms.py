"""The source form. The vCenter password is write-only by construction: ``PasswordInput(render_value=False)`` never echoes what was
typed, it is never given an initial value, and the vault row holding it is not editable through any form."""
import ipaddress
import re

from django import forms

from core import secrets as vault
from core.access import can, tenant_wide
from core.models import Site

from . import services
from .models import GLOBAL_SITE, TLS_PIN, TLS_SKIP, VCenterSource

HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")


def admin_sites(user):
    """Site names (plus 'All Sites') the user may create or edit sources for: admin on that site / on every site."""
    names = [s.name for s in Site.objects.order_by("name") if can(user, "admin", s.name)]
    if tenant_wide(user, "admin"):
        names = [GLOBAL_SITE] + names
    return names


class SourceForm(forms.ModelForm):
    site = forms.ChoiceField(label="Site", choices=())
    username = forms.CharField(label="Read-only account", max_length=150, help_text="A vCenter account with the Read-only role is enough.")
    password = forms.CharField(
        label="Password", required=False, strip=False, max_length=256,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Stored encrypted in the credential vault; never shown again.")

    class Meta:
        model = VCenterSource
        fields = ["name", "site", "description", "enabled", "host", "port", "username", "password", "tls_mode", "sync_interval_s",
                  "use_provider"]
        labels = {"sync_interval_s": "Sync interval (seconds)", "host": "vCenter host name or IP address", "port": "Port"}
        help_texts = {"sync_interval_s": "How often LinkedEye reads vCenter. 60 to 3600 seconds; 300 is a good default."}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        names = admin_sites(user) if user is not None else []
        self.fields["site"].choices = [(n, n) for n in names]
        if self.instance.pk and self.instance.site not in names:
            self.fields["site"].choices = [(self.instance.site, self.instance.site)] + self.fields["site"].choices
        self.fields["description"].widget = forms.TextInput()
        self.fields["description"].required = False
        if self.instance.pk:
            self.fields["username"].initial = services.username_of(self.instance)
            self.fields["password"].help_text = "Leave empty to keep the stored password. Use “Rotate password” to replace it."
            self.fields["password"].widget = forms.HiddenInput()      # rotation has its own form; never echoed here
            self.fields["password"].required = False
        else:
            self.fields["password"].required = True
        self.has_password = bool(self.instance.pk and self.instance.credential_id)

    MAIN = ("name", "site", "description", "enabled")
    CONN = ("host", "port", "username", "password", "tls_mode", "sync_interval_s", "use_provider")

    @property
    def main_fields(self):
        return [self[n] for n in self.MAIN]

    @property
    def conn_fields(self):
        return [self[n] for n in self.CONN if not (n == "password" and self.instance.pk)]

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Enter a name.")
        clash = VCenterSource.objects.filter(tenant_key=services.tenant(), name__iexact=name)
        if self.instance.pk:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise forms.ValidationError("A source with this name already exists.")
        return name

    def clean_site(self):
        site = self.cleaned_data["site"]
        allowed = admin_sites(self.user) if self.user is not None else []
        if site not in allowed and not (self.instance.pk and site == self.instance.site and can(self.user, "admin", site)):
            raise forms.ValidationError("You need the admin role on that site.")
        return site

    def clean_host(self):
        host = (self.cleaned_data.get("host") or "").strip()
        try:
            ipaddress.ip_address(host.strip("[]"))
            return host.strip("[]")
        except ValueError:
            pass
        if not HOST_RE.fullmatch(host):
            raise forms.ValidationError("Enter a host name or IP address only (no scheme, port or path).")
        return host.lower()

    def clean_port(self):
        n = self.cleaned_data.get("port")
        if n is not None and not 1 <= n <= 65535:
            raise forms.ValidationError("Use a port from 1 to 65535.")
        return n

    def clean_sync_interval_s(self):
        n = self.cleaned_data["sync_interval_s"]
        if not 60 <= n <= 3600:
            raise forms.ValidationError("Use between 60 and 3600 seconds.")
        return n

    def clean(self):
        data = super().clean()
        if data.get("password") and not vault.available():
            self.add_error("password", vault.unavailable_reason() or "The credential vault is not configured.")
        if not self.instance.pk and not vault.available() and not self.errors.get("password"):
            self.add_error("password", vault.unavailable_reason() or "The credential vault is not configured.")
        mode = data.get("tls_mode")
        self.skip_warning = mode == TLS_SKIP
        self.pin_hint = mode == TLS_PIN
        return data


class RotateForm(forms.Form):
    username = forms.CharField(max_length=150, label="Read-only account")
    password = forms.CharField(strip=False, max_length=256, label="New password",
                               widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}))

    def clean(self):
        data = super().clean()
        if not vault.available():
            raise forms.ValidationError(vault.unavailable_reason() or "The credential vault is not configured.")
        return data
