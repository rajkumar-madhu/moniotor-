"""The Source form. The Redis password is write-only by construction: ``PasswordInput(render_value=False)`` never echoes
what was typed, it is never given an initial value, and the vault row holding it is not editable through any form."""
import re

from django import forms

from core import secrets as vault
from core.access import can, tenant_wide
from core.models import Site

from . import redis_reader, services
from .models import GLOBAL_SITE, Source

HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$|^[0-9A-Fa-f:]{2,45}$")


def admin_sites(user):
    """Site names (plus 'All Sites') the user may create or edit sources for: admin on that site / on every site."""
    names = [s.name for s in Site.objects.order_by("name") if can(user, "admin", s.name)]
    if tenant_wide(user, "admin"):
        names = [GLOBAL_SITE] + names
    return names


class SourceForm(forms.ModelForm):
    site = forms.ChoiceField(label="Site", choices=())
    redis_password = forms.CharField(
        label="Redis password", required=False, strip=False, max_length=256,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Stored encrypted in the credential vault; never shown again. Leave empty to keep the stored one.")
    clear_password = forms.BooleanField(label="Remove the stored Redis password", required=False)

    class Meta:
        model = Source
        fields = ["name", "site", "description", "enabled", "expected_interval_s", "redis_poll", "redis_host", "redis_port",
                  "redis_db", "redis_tls", "redis_username", "redis_prefix", "redis_adapter_suffix", "redis_process_suffix"]
        labels = {"expected_interval_s": "Expected interval (seconds)", "redis_poll": "Read the Redis keys from LinkedEye",
                  "redis_host": "Redis host", "redis_port": "Port", "redis_db": "Database", "redis_tls": "Use TLS",
                  "redis_username": "Username (optional)", "redis_adapter_suffix": "Adapter key suffix",
                  "redis_process_suffix": "Process key suffix"}
        help_texts = {
            "redis_prefix": "The toolkit writes to <SITE_NAME>:<suffix>. Put the SITE_NAME here.",
            "redis_adapter_suffix": "Your ADAPTER_KEY_SUFFIX, e.g. the key that holds the adapter matrix.",
            "redis_process_suffix": "Your PROCESS_KEY_SUFFIX, e.g. the key that holds the process table.",
            "expected_interval_s": "How often the producer reports. Redis-polled sources: the toolkit refreshes the key on change "
                                   "or every 20 minutes, so use at least 1500.",
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        names = admin_sites(user) if user is not None else []
        self.fields["site"].choices = [(n, n) for n in names]
        if self.instance.pk and self.instance.site not in names:
            self.fields["site"].choices = [(self.instance.site, self.instance.site)] + self.fields["site"].choices
        self.fields["description"].widget = forms.TextInput()
        self.fields["redis_port"].min_value, self.fields["redis_db"].min_value = 1, 0
        self.fields["redis_poll"].help_text = ("Optional. LinkedEye connects to Redis every minute and reads the two keys, "
                                               "using the same validation as the push API.")
        self.has_password = bool(self.instance.pk and self.instance.redis_credential_id)

    MAIN = ("name", "site", "description", "expected_interval_s", "enabled")

    @property
    def main_fields(self):
        return [self[n] for n in self.MAIN]

    @property
    def redis_fields(self):
        return [f for f in self if f.name not in self.MAIN]

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Enter a name.")
        clash = Source.objects.filter(tenant_key=services.tenant(), name__iexact=name)
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

    def clean_expected_interval_s(self):
        n = self.cleaned_data["expected_interval_s"]
        if not 30 <= n <= 86400:
            raise forms.ValidationError("Use between 30 seconds and 24 hours.")
        return n

    def clean_redis_host(self):
        host = (self.cleaned_data.get("redis_host") or "").strip()
        if host and not HOST_RE.match(host):
            raise forms.ValidationError("Enter a host name or IP address only (no scheme, port or path).")
        return host

    def clean_redis_port(self):
        n = self.cleaned_data.get("redis_port")
        if n is not None and not 1 <= n <= 65535:
            raise forms.ValidationError("Use a port from 1 to 65535.")
        return n

    def clean_redis_db(self):
        n = self.cleaned_data.get("redis_db")
        if n is not None and not 0 <= n <= 255:
            raise forms.ValidationError("Use a database number from 0 to 255.")
        return n

    def _key(self, name, label):
        value = (self.cleaned_data.get(name) or "").strip()
        if value and not redis_reader.KEY_RE.match(value):
            raise forms.ValidationError(f"{label} may contain letters, digits and _ . : - / @ + = only.")
        return value

    def clean_redis_prefix(self):
        return self._key("redis_prefix", "The prefix")

    def clean_redis_adapter_suffix(self):
        return self._key("redis_adapter_suffix", "The suffix")

    def clean_redis_process_suffix(self):
        return self._key("redis_process_suffix", "The suffix")

    def clean(self):
        data = super().clean()
        if data.get("redis_poll"):
            if not data.get("redis_host"):
                self.add_error("redis_host", "Enter the Redis host to read from.")
            if not (data.get("redis_adapter_suffix") or data.get("redis_process_suffix")):
                self.add_error("redis_adapter_suffix", "Enter at least one key suffix to read.")
        if data.get("redis_password") and not vault.available():
            self.add_error("redis_password", vault.unavailable_reason() or "The credential vault is not configured.")
        return data
