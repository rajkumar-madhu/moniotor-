import re

from django import forms
from django.core.exceptions import ValidationError

from . import services
from .models import Kind, Link, Placement
from .ports import MAX_PORT, clean_port, port_key

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean_label(value, label, limit):
    """A one-line label: control characters are refused, whitespace is collapsed."""
    value = (value or "").strip()
    if CONTROL_RE.search(value):
        raise ValidationError(f"{label} contains control characters.")
    value = " ".join(value.split())
    if len(value) > limit:
        raise ValidationError(f"{label} is {limit} characters at most.")
    return value


def duplicate_of(a_id, a_port, b_id, b_port, tenant_key, exclude_pk=None):
    """An existing link between the same two devices that uses the same ports (spelling-independent), or None."""
    a, ap, b, bp = Link.order_ends(a_id, a_port, b_id, b_port)
    want = (port_key(ap), port_key(bp))
    qs = Link.objects.filter(tenant_key=tenant_key, a_device_id=a, b_device_id=b)
    if exclude_pk:
        qs = qs.exclude(pk=exclude_pk)
    for link in qs:
        if (port_key(link.a_port), port_key(link.b_port)) == want:
            return link
    return None


class LinkForm(forms.ModelForm):
    class Meta:
        model = Link
        fields = ["a_device", "a_port", "b_device", "b_port", "medium", "speed_mbps", "kind", "note", "confirmed"]
        labels = {"a_device": "Device A", "b_device": "Device B", "a_port": "Port on A", "b_port": "Port on B"}
        widgets = {"a_port": forms.TextInput(attrs={"placeholder": "Eth1/49", "maxlength": MAX_PORT}),
                   "b_port": forms.TextInput(attrs={"placeholder": "Eth1/1", "maxlength": MAX_PORT}),
                   "kind": forms.TextInput(attrs={"list": "arch-kinds", "maxlength": 30}),
                   "note": forms.TextInput(attrs={"maxlength": 300})}

    def __init__(self, *args, user=None, request=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        qs = services.manageable_devices(user, request).order_by("name")
        if self.instance.pk:   # editing: keep both ends selectable even if one is outside the user's operate scope
            qs = services.device_queryset(user, request).order_by("name")
        for f in ("a_device", "b_device"):
            self.fields[f].queryset = qs
            self.fields[f].empty_label = "Choose a device"
        self.kinds = [k for k, _l in Kind.choices]

    def clean_a_port(self):
        return self._port("a_port")

    def clean_b_port(self):
        return self._port("b_port")

    def _port(self, name):
        raw = self.cleaned_data.get(name, "")
        if CONTROL_RE.search(raw or ""):
            raise ValidationError("The port name contains control characters.")
        try:
            return clean_port(raw)
        except ValueError as exc:
            raise ValidationError(str(exc)) from None

    def clean_kind(self):
        return clean_label(self.cleaned_data.get("kind"), "The kind", 30)

    def clean_note(self):
        return clean_label(self.cleaned_data.get("note"), "The note", 300)

    def clean(self):
        data = super().clean()
        a, b = data.get("a_device"), data.get("b_device")
        if not a or not b:
            return data
        if a.pk == b.pk:
            self.add_error("b_device", "A device cannot be linked to itself.")
            return data
        for field, dev in (("a_device", a), ("b_device", b)):
            if not services.can_manage_device(self.user, dev):
                self.add_error(field, f"You cannot manage links on {dev.site or 'All Sites'}.")
        if self.errors:
            return data
        tenant = services.tenant()
        if a.tenant_key != tenant or b.tenant_key != tenant:
            raise ValidationError("Both devices must belong to this tenant.")
        dup = duplicate_of(a.pk, data.get("a_port", ""), b.pk, data.get("b_port", ""), tenant, self.instance.pk)
        if dup is not None:
            raise ValidationError(f"These two ports are already linked: {dup}.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.tenant_key = services.tenant()
        if self.instance.pk is None and obj.origin != "manual":
            obj.origin = "manual"
        if commit:
            obj.save()
        return obj


class PlacementForm(forms.ModelForm):
    class Meta:
        model = Placement
        fields = ["tier", "group", "colour", "sort_order", "hidden"]
        widgets = {"group": forms.TextInput(attrs={"maxlength": 80, "placeholder": "e.g. Web Servers"})}

    def clean_group(self):
        return clean_label(self.cleaned_data.get("group"), "The group", 80)


class ImportForm(forms.Form):
    file = forms.FileField(label="CSV file", required=False)
    update_existing = forms.BooleanField(label="Update links that already exist", required=False)
