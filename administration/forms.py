import zoneinfo

import django
from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm, UsernameField
from django.core.exceptions import ValidationError
from django.utils.text import slugify

from core.models import Client, Membership, Site, SiteLesiteProfile

User = get_user_model()

ROLE_CHOICES = Membership.ROLES
ALL_SITES = ""


def site_choices(include_all=True):
    rows = [(ALL_SITES, "All sites")] if include_all else []
    rows += [(str(s.pk), s.name) for s in Site.objects.filter(tenant_key=settings.LINKEDEYE_TENANT).order_by("name")]
    return rows


def _site_from(value):
    if value in (None, "", ALL_SITES):
        return None
    return Site.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, pk=value).first()


class _EmailMixin:
    def clean_email(self):
        email = (self.cleaned_data.get("email") or "").strip()
        if email:
            qs = User.objects.filter(email__iexact=email)
            if self.instance is not None and self.instance.pk:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise forms.ValidationError("Another user already has this e-mail address.")
        return email


class PasswordChoiceMixin:
    """Either generate a strong password (shown once) or set one that passes AUTH_PASSWORD_VALIDATORS."""

    def _init_password_fields(self):
        self.fields["password_mode"] = forms.ChoiceField(
            choices=[("generate", "Generate a strong password (shown once)"), ("set", "Set a password")],
            initial="generate", widget=forms.RadioSelect, label="Method")
        self.fields["password1"] = forms.CharField(label="New password", required=False, strip=False,
                                                   widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                                                   help_text=password_validation.password_validators_help_text_html())
        self.fields["password2"] = forms.CharField(label="Confirm password", required=False, strip=False,
                                                   widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    def _clean_password(self, cleaned, user):
        if cleaned.get("password_mode") != "set":
            return
        p1, p2 = cleaned.get("password1") or "", cleaned.get("password2") or ""
        if not p1:
            self.add_error("password1", "Enter a password, or choose to generate one.")
            return
        if p1 != p2:
            self.add_error("password2", "The two passwords do not match.")
            return
        try:
            password_validation.validate_password(p1, user)
        except forms.ValidationError as e:
            self.add_error("password1", e)


class UserCreateForm(_EmailMixin, PasswordChoiceMixin, forms.ModelForm):
    username = UsernameField(max_length=150, help_text="Letters, digits and @/./+/-/_ only.")
    site = forms.ChoiceField(label="Initial site", required=False)
    role = forms.ChoiceField(label="Initial role", choices=ROLE_CHOICES, initial="viewer")

    class Meta:
        model = User
        fields = ["username", "first_name", "last_name", "email"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance = User()
        self.fields["site"].choices = site_choices()
        self._init_password_fields()

    def clean_username(self):
        username = self.cleaned_data["username"]
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("A user with this username already exists.")
        return username

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("site") not in (None, ALL_SITES) and _site_from(cleaned.get("site")) is None:
            self.add_error("site", "Unknown site.")
        probe = User(username=cleaned.get("username") or "", first_name=cleaned.get("first_name") or "",
                     last_name=cleaned.get("last_name") or "", email=cleaned.get("email") or "")
        self._clean_password(cleaned, probe)
        return cleaned

    @property
    def site_obj(self):
        return _site_from(self.cleaned_data.get("site"))


class UserEditForm(_EmailMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "email", "is_staff"]
        labels = {"is_staff": "Django admin access (staff)"}
        help_texts = {"is_staff": "Lets the user sign in to the Django admin. Without memberships, staff users are "
                                  "operators on all sites."}


class MembershipForm(forms.Form):
    site = forms.ChoiceField(label="Site", required=False)
    role = forms.ChoiceField(label="Role", choices=ROLE_CHOICES, initial="viewer")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = site_choices()

    def clean_site(self):
        value = self.cleaned_data.get("site")
        if value in (None, ALL_SITES):
            return None
        site = _site_from(value)
        if site is None:
            raise forms.ValidationError("Unknown site.")
        return site


class SetPasswordForm(PasswordChoiceMixin, forms.Form):
    def __init__(self, user, *args, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)
        self._init_password_fields()

    def clean(self):
        cleaned = super().clean()
        self._clean_password(cleaned, self.user)
        return cleaned


def timezone_choices(current=None):
    zones = sorted(zoneinfo.available_timezones())
    if current and current not in zones:
        zones.insert(0, current)
    return [(z, z.replace("_", " ")) for z in zones]


def client_choices():
    return Client.objects.filter(tenant_key=settings.LINKEDEYE_TENANT).order_by("name")


class ClientForm(forms.ModelForm):
    modules = forms.MultipleChoiceField(required=False, choices=Client.MODULES,
                                        widget=forms.CheckboxSelectMultiple, label="Enabled modules")

    class Meta:
        model = Client
        fields = ["name", "code", "status", "contact_name", "contact_email", "go_live", "modules", "notes"]
        help_texts = {"code": "Short identifier (letters, digits, hyphens). Leave blank to derive it from the name."}
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        if self.instance.pk and isinstance(self.instance.modules, list):
            self.fields["modules"].initial = self.instance.modules

    def _scope(self):
        qs = Client.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
        return qs.exclude(pk=self.instance.pk) if self.instance.pk else qs

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Enter a name.")
        if self.instance.pk and " ".join(self.instance.name.split()) == name:
            return self.instance.name
        if self._scope().filter(name__iexact=name).exists():
            raise forms.ValidationError("A client with this name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        code = (cleaned.get("code") or "").strip().lower()
        if not code and cleaned.get("name"):
            base = slugify(cleaned["name"])[:50] or "client"
            code = base
            n = 2
            while self._scope().filter(code=code).exists():
                suffix = f"-{n}"
                code = f"{base[:50 - len(suffix)]}{suffix}"
                n += 1
        elif code and self._scope().filter(code=code).exists():
            self.add_error("code", "A client with this code already exists.")
            return cleaned
        cleaned["code"] = code
        cleaned["modules"] = cleaned.get("modules") or []
        return cleaned


class SiteForm(forms.ModelForm):
    timezone = forms.ChoiceField(label="Timezone")

    class Meta:
        model = Site
        fields = ["client", "name", "code", "region", "timezone", "criticality", "description"]
        help_texts = {"code": "Short identifier (letters, digits, hyphens). Leave blank to derive it from the name.",
                      "name": "Records in other modules refer to the site by this name."}
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["code"].required = False
        self.fields["client"].queryset = client_choices()
        self.fields["client"].required = False
        self.fields["client"].empty_label = "No client"
        current = self.instance.timezone if self.instance and self.instance.pk else None
        self.fields["timezone"].choices = timezone_choices(current)
        if not self.instance.pk:
            self.fields["timezone"].initial = getattr(settings, "TIME_ZONE", "Asia/Kolkata")

    def _scope(self):
        qs = Site.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
        return qs.exclude(pk=self.instance.pk) if self.instance.pk else qs

    def clean_name(self):
        name = " ".join((self.cleaned_data.get("name") or "").split())
        if not name:
            raise forms.ValidationError("Enter a name.")
        if self.instance.pk and " ".join(self.instance.name.split()) == name:
            # Unchanged: keep the exact stored spelling (other modules match site names exactly) and skip the
            # duplicate checks, because bootstrap_sites can create case variants such as "Mumbai" and "mumbai".
            return self.instance.name
        if name.lower() == "all sites":
            raise forms.ValidationError("“All Sites” is reserved for records that apply everywhere.")
        if self._scope().filter(name__iexact=name).exists():
            raise forms.ValidationError("A site with this name already exists.")
        return name

    def clean(self):
        cleaned = super().clean()
        code = (cleaned.get("code") or "").strip().lower()
        if not code and cleaned.get("name"):
            base = slugify(cleaned["name"])[:50] or "site"
            code, i = base, 2
            while self._scope().filter(code__iexact=code).exists():
                code, i = f"{base}-{i}", i + 1
        if code and self.instance.pk and code == (self.instance.code or "").lower():
            cleaned["code"] = self.instance.code          # unchanged
        elif code:
            if self._scope().filter(code__iexact=code).exists():
                self.add_error("code", "A site with this code already exists.")
            cleaned["code"] = code
        return cleaned


class SiteLesiteProfileForm(forms.ModelForm):
    """Lesites / portal onboarding fields plus optional site-scoped user links (memberships)."""

    users = forms.ModelMultipleChoiceField(
        queryset=User.objects.order_by("username"),
        required=False,
        widget=forms.SelectMultiple,
        help_text="Users linked to this site (viewer role) for onboarding handoff.",
    )

    class Meta:
        model = SiteLesiteProfile
        fields = [
            "legacy_sitename",
            "enabled",
            "environment",
            "websocket_url",
            "le_url",
            "is_url_secured",
            "entity_host",
            "entity_port",
            "analytics_prefix_url",
            "prometheus_url",
            "elastic_host",
            "elastic_port",
            "grafana_api",
            "country_code",
            "location",
            "redis_host",
            "redis_port",
            "incident_url",
            "incident_api",
        ]

    def __init__(self, *args, site=None, **kwargs):
        self.site = site
        super().__init__(*args, **kwargs)
        if self.instance and getattr(self.instance, "pk", None) and getattr(self.instance, "site_id", None):
            self.fields["users"].initial = list(
                Membership.objects.filter(site_id=self.instance.site_id).values_list("user_id", flat=True)
            )
        elif site is not None and getattr(site, "pk", None):
            self.fields["users"].initial = list(Membership.objects.filter(site=site).values_list("user_id", flat=True))


# ------------------------------------------------------------------ integrations (Administration › Integrations)
class OptionalHttpsURLField(forms.URLField):
    """Bare hostnames default to https; Django 5+ also gets assume_scheme."""

    def __init__(self, *, assume_scheme="https", **kwargs):
        self._assume_scheme = assume_scheme
        if django.VERSION >= (5, 0):
            kwargs.setdefault("assume_scheme", assume_scheme)
        super().__init__(**kwargs)

    def to_python(self, value):
        if django.VERSION < (5, 0) and value not in self.empty_values:
            text = str(value).strip()
            if text and "://" not in text:
                value = f"{self._assume_scheme}://{text}"
        return super().to_python(value)


class _SecretOptionalMixin:
    """Password-style fields: blank means keep the stored secret."""

    secret_fields = ()

    def clean(self):
        cleaned = super().clean()
        for name in self.secret_fields:
            if not cleaned.get(name):
                cleaned.pop(name, None)
        return cleaned


class PrometheusIntegrationForm(_SecretOptionalMixin, forms.Form):
    url = OptionalHttpsURLField(label="Server URL", required=False)
    timeout = forms.FloatField(label="Timeout (seconds)", required=False, min_value=1, max_value=120)
    # discovery (telemetry.discovery): what the product gathers and creates by itself
    site_label = forms.RegexField(
        label="Site label", required=False, regex=r"^[a-zA-Z_][a-zA-Z0-9_]*$", max_length=100,
        error_messages={"invalid": "A Prometheus label name: letters, digits and underscores."},
        help_text='Target label discovery reads the site from. Default "site"; targets without it go to "All Sites".')
    autocreate_ci = forms.BooleanField(
        label="Auto-create CMDB items for new hosts", required=False,
        help_text="Every newly discovered Linux or Windows host becomes a server configuration item.")
    autocreate_devices = forms.BooleanField(
        label="Auto-create devices for SNMP targets", required=False,
        help_text="Every newly discovered SNMP target becomes a network device (vendor and model are left blank).")
    autoregister_exporters = forms.BooleanField(
        label="Auto-register exporters", required=False,
        help_text="Every newly discovered target is added to the Exporters inventory with its scrape URL.")


class AlertmanagerIntegrationForm(_SecretOptionalMixin, forms.Form):
    secret_fields = ("token",)
    token = forms.CharField(label="Bearer token", required=False, strip=True, widget=forms.PasswordInput(render_value=False),
                            help_text="Leave blank to keep the current token.")


class LogIngestIntegrationForm(_SecretOptionalMixin, forms.Form):
    secret_fields = ("token",)
    token = forms.CharField(label="Bearer token", required=False, strip=True, widget=forms.PasswordInput(render_value=False),
                            help_text="Leave blank to keep the current token.")


class StackStormIntegrationForm(_SecretOptionalMixin, forms.Form):
    secret_fields = ("api_key",)
    url = OptionalHttpsURLField(label="Webhook URL", required=False)
    api_key = forms.CharField(label="API key", required=False, strip=True, widget=forms.PasswordInput(render_value=False),
                              help_text="Leave blank to keep the current key.")


class WebhooksIntegrationForm(forms.Form):
    MAX_URLS = 20
    SEVERITIES = ("critical", "warning", "info", "ok")

    urls = forms.CharField(label="Webhook URLs (one per line)", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    severities = forms.CharField(label="Severities (comma-separated)", required=False,
                                 help_text="Any of critical, warning, info, ok. e.g. critical, warning")

    def clean_urls(self):
        from urllib.parse import urlsplit

        seen, out = set(), []
        for line in (self.cleaned_data.get("urls") or "").splitlines():
            line = line.strip()
            if not line or line in seen:
                continue
            try:
                parts = urlsplit(line)
                host = parts.hostname
                parts.port   # raises ValueError for a malformed port
            except ValueError:
                raise forms.ValidationError(f"Not a valid URL: {line[:80]}")
            if parts.scheme not in ("http", "https") or not host:
                raise forms.ValidationError(f"Use an http:// or https:// URL with a host name: {line[:80]}")
            if parts.username or parts.password:
                raise forms.ValidationError("Do not put credentials in the URL; use the token the receiver gives you in the path.")
            seen.add(line)
            out.append(line)
        if len(out) > self.MAX_URLS:
            raise forms.ValidationError(f"At most {self.MAX_URLS} webhook URLs.")
        return "\n".join(out)

    def clean_severities(self):
        out = []
        for part in (self.cleaned_data.get("severities") or "").replace(",", " ").split():
            sev = part.strip().lower()
            if sev not in self.SEVERITIES:
                raise forms.ValidationError(f"Unknown severity “{part[:30]}”: use critical, warning, info or ok.")
            if sev not in out:
                out.append(sev)
        return ", ".join(out)


class EmailIntegrationForm(_SecretOptionalMixin, forms.Form):
    secret_fields = ("email_host_password",)
    recipients = forms.CharField(label="Recipients (one e-mail per line)", required=False, widget=forms.Textarea(attrs={"rows": 2}))
    email_host = forms.CharField(label="SMTP host", required=False)
    email_port = forms.IntegerField(label="SMTP port", required=False, min_value=1, max_value=65535)
    email_host_user = forms.CharField(label="SMTP user", required=False)
    email_host_password = forms.CharField(label="SMTP password", required=False, strip=False,
                                          widget=forms.PasswordInput(render_value=False),
                                          help_text="Leave blank to keep the current password.")
    email_use_tls = forms.BooleanField(label="Use TLS", required=False)
    default_from_email = forms.EmailField(label="From address", required=False)


class NagiosIntegrationForm(_SecretOptionalMixin, forms.Form):
    secret_fields = ("password",)
    url = OptionalHttpsURLField(label="Server URL", required=False)
    user = forms.CharField(label="User", required=False)
    password = forms.CharField(label="Password", required=False, strip=False, widget=forms.PasswordInput(render_value=False),
                               help_text="Leave blank to keep the current password.")
    timeout = forms.FloatField(label="Timeout (seconds)", required=False, min_value=1, max_value=120)


INTEGRATION_FORMS = {
    "prometheus": PrometheusIntegrationForm,
    "alertmanager": AlertmanagerIntegrationForm,
    "logs": LogIngestIntegrationForm,
    "stackstorm": StackStormIntegrationForm,
    "webhooks": WebhooksIntegrationForm,
    "email": EmailIntegrationForm,
    "nagios": NagiosIntegrationForm,
}


def integration_form(slug, data=None, initial=None):
    cls = INTEGRATION_FORMS.get(slug)
    if not cls:
        return None
    return cls(data=data, initial=initial or {})


# ------------------------------------------------------------------ sign-in throttling
class ThrottleMixin:
    """Refuses sign-ins for a username or client address after too many failures (see services.login_limits).

    The lock is checked *before* the password, so a locked account cannot be used as a password oracle. Client
    addresses come from core.audit._ip, which honours settings.TRUSTED_PROXIES."""
    throttle_message = ""

    def clean(self):
        from core.audit import _ip, audit

        from . import services
        username = self.cleaned_data.get("username")
        password = self.cleaned_data.get("password")
        ip = _ip(self.request)
        lock = services.login_lock_remaining(username, ip)
        if lock:
            kind, remaining = lock
            minutes = max(1, -(-int(remaining.total_seconds()) // 60))
            self.throttle_message = f"Too many failed sign-ins. Try again in {minutes} minute{'s' if minutes != 1 else ''}."
            try:
                audit(self.request, "login_throttled", object_type="auth.user", result="denied",
                      username=str(username or "")[:150], locked_by="username" if kind == "user" else "client address",
                      minutes=minutes)
            except Exception:
                pass
            raise ValidationError(self.throttle_message, code="throttled")
        try:
            cleaned = super().clean()
        except ValidationError as e:
            # only a wrong password counts (the admin form also says "invalid_login" to a non-staff user whose
            # password was right; user_cache is set then)
            if username and password and getattr(e, "code", None) == "invalid_login" \
                    and getattr(self, "user_cache", None) is None:
                for kind in services.login_failed(username, ip):
                    try:
                        audit(self.request, "login_locked", object_type="auth.user", result="denied",
                              username=str(username)[:150], locked_by="username" if kind == "user" else "client address",
                              minutes=int(services.login_limits()[2].total_seconds() // 60))
                    except Exception:
                        pass
            raise
        services.login_succeeded(username)
        return cleaned


class ThrottledAuthenticationForm(ThrottleMixin, AuthenticationForm):
    pass


class ThrottledAdminAuthenticationForm(ThrottleMixin, AdminAuthenticationForm):
    pass
