"""Forms. The BMC password is write-only: ``PasswordInput(render_value=False)``, never given an initial value, stored only in
the credential vault. The certificate fingerprint is NOT a form field: it is pinned through the confirm step in the view."""
import re

from django import forms

from core.access import can_on
from network.models import Device, DeviceType

from . import services
from .models import BmcEndpoint

HOST_RE = re.compile(r"^(?=.{1,253}$)([A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$|^[0-9A-Fa-f:]{2,45}$")


def manageable_devices(user, with_endpoint=False):
    """Physical servers the user is admin on (without a BMC endpoint yet, unless `with_endpoint`)."""
    qs = Device.objects.filter(tenant_key=services.tenant(), device_type=DeviceType.PHYSICAL_SERVER).order_by("name")
    if not with_endpoint:
        qs = qs.filter(bmc__isnull=True)
    return [d for d in qs if can_on(user, "admin", d.site)]


class EndpointForm(forms.ModelForm):
    device = forms.ChoiceField(label="Physical server", choices=())
    username = forms.CharField(label="Read-only user name", max_length=150, required=False)
    password = forms.CharField(
        label="Password", required=False, strip=False, max_length=256,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Stored encrypted in the credential vault; never shown again. Leave empty to keep the stored one.")
    confirm_unsafe = forms.BooleanField(
        required=False, label="I understand the risk",
        help_text="Needed when the certificate is not verified or plain HTTP is used: anyone on the network path could read "
                  "or alter what the BMC sends, and (for plain HTTP) read the login.")

    field_order = ["device", "host", "port", "poll_interval_s", "vendor_override", "username", "password", "verify_mode",
                   "insecure_http", "confirm_unsafe", "enabled"]

    class Meta:
        model = BmcEndpoint
        fields = ["host", "port", "verify_mode", "insecure_http", "poll_interval_s", "enabled", "vendor_override"]
        labels = {"poll_interval_s": "Poll every (seconds)", "vendor_override": "Vendor (leave on Auto to detect)",
                  "enabled": "Poll this BMC"}
        help_texts = {"poll_interval_s": "30 seconds at the fastest.", "vendor_override": "The BMC is detected from its Redfish "
                                                                                          "answer; override only if it is wrong."}

    def __init__(self, *args, user=None, device=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["vendor_override"].required = False
        self.fields["vendor_override"].choices = [("", "Auto")] + list(BmcEndpoint.VENDOR_CHOICES)
        if self.instance.pk:
            d = self.instance.device
            self.fields["device"].choices = [(str(d.pk), d.name)]
            self.fields["device"].disabled = True
            self.fields["device"].initial = str(d.pk)
            self.has_password = bool(self.instance.credential_id)
            row = services.credential_row(self.instance)
            self.fields["username"].initial = row.username if row else ""
        else:
            choices = [(str(d.pk), f"{d.name} ({d.site or 'All Sites'})") for d in manageable_devices(user)] if user else []
            self.fields["device"].choices = choices
            if device is not None:
                self.fields["device"].initial = str(device.pk)
            self.has_password = False

    def clean_device(self):
        if self.instance.pk:
            return self.instance.device
        pk = self.cleaned_data["device"]
        for d in manageable_devices(self.user):
            if str(d.pk) == pk:
                return d
        raise forms.ValidationError("Choose a physical server you administer that has no BMC endpoint yet.")

    def clean_host(self):
        host = (self.cleaned_data.get("host") or "").strip().strip("[]")
        if not HOST_RE.fullmatch(host):
            raise forms.ValidationError("Enter a host name or IP address only (no scheme, port or path).")
        return host

    def clean_port(self):
        n = self.cleaned_data.get("port")
        if n is None or not 1 <= n <= 65535:
            raise forms.ValidationError("Use a port from 1 to 65535.")
        return n

    def clean_poll_interval_s(self):
        n = self.cleaned_data.get("poll_interval_s")
        if n is None or not 30 <= n <= 86400:
            raise forms.ValidationError("Use between 30 seconds and 24 hours.")
        return n

    def clean(self):
        data = super().clean()
        creating = not self.instance.pk
        if creating and not data.get("username"):
            self.add_error("username", "Enter the read-only user name.")
        if creating and not data.get("password"):
            self.add_error("password", "Enter the password of that account.")
        if data.get("password") and not data.get("username") and not (self.instance.pk and self.fields["username"].initial):
            self.add_error("username", "Enter the user name that goes with the password.")
        unsafe = data.get("verify_mode") == BmcEndpoint.VERIFY_SKIP or data.get("insecure_http")
        if unsafe and not data.get("confirm_unsafe"):
            self.add_error("confirm_unsafe", "Tick this to accept the risk, or choose pinning (and HTTPS).")
        if self.instance.pk and self.instance.credential_id and not data.get("password"):
            # (clean() runs before the instance takes the new values.) The stored login is write-only: if the address it is sent
            # to may change without it being entered again, whoever edits the address can send it to a server of their own.
            moved = (data.get("host") != self.instance.host or data.get("port") != self.instance.port
                     or bool(data.get("insecure_http")) != bool(self.instance.insecure_http))
            if moved:
                self.add_error("password", "The address or the protocol changed: enter the password again, so the stored one is "
                                           "never sent to a different place.")
        if data.get("insecure_http") and data.get("port") == 443:
            self.add_error("port", "Plain HTTP usually listens on port 80: check the port.")
        if data.get("password"):
            from core import secrets as vault
            if not vault.available():
                self.add_error("password", vault.unavailable_reason() or "The credential vault is not configured.")
        return data


class AlertsForm(forms.ModelForm):
    class Meta:
        model = BmcEndpoint
        fields = ["alerts_enabled", "temp_warn_c", "temp_crit_c", "fan_warn_rpm", "fan_crit_rpm"]
        labels = {"alerts_enabled": "Raise an event when the hardware health changes"}
        help_texts = {
            "temp_warn_c": "Leave empty to use the BMC's own limit. A value here can only make a reading MORE severe.",
            "fan_warn_rpm": "Fans at or below this speed count as warning (RPM fans only).",
        }

    def clean(self):
        data = super().clean()
        for f in ("temp_warn_c", "temp_crit_c", "fan_warn_rpm", "fan_crit_rpm"):
            v = data.get(f)
            if v is not None and not -50 <= v <= 100000:
                self.add_error(f, "Enter a sensible number.")
        w, c = data.get("temp_warn_c"), data.get("temp_crit_c")
        if w is not None and c is not None and c < w:
            self.add_error("temp_crit_c", "The critical limit must not be below the warning limit.")
        fw, fc = data.get("fan_warn_rpm"), data.get("fan_crit_rpm")
        if fw is not None and fc is not None and fc > fw:
            self.add_error("fan_crit_rpm", "The critical speed must not be above the warning speed.")
        return data
