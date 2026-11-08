import re

from django import forms
from django.apps import apps

from core.access import scope_by_site

from . import services
from .models import MAX_U, CoolingUnit, PowerFeed, Rack, RackItem, Room

CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_query(value):
    value = (value or "").strip()
    if CONTROL_RE.search(value):
        raise forms.ValidationError("Write the query on one line, without control characters.")
    if value and len(value) > 500:
        raise forms.ValidationError("The query is too long (500 characters at most).")
    return value


def _clean_text(value, label="This field"):
    value = (value or "").strip()
    if CONTROL_RE.search(value.replace("\n", "").replace("\r", "").replace("\t", "")):
        raise forms.ValidationError(f"{label} contains control characters.")
    return value


class _Base(forms.ModelForm):
    """Shared helpers: the user (for scoping), stripped names and a uniqueness check within a parent."""

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        for f in self.fields.values():
            if isinstance(f.widget, (forms.TextInput, forms.NumberInput)) and f.required:
                f.widget.attrs.setdefault("required", True)

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise forms.ValidationError("Enter a name.")
        if CONTROL_RE.search(name):
            raise forms.ValidationError("The name contains control characters.")
        return name

    def _unique(self, model, field, label, **parent):
        name = self.cleaned_data.get("name")
        if not name or any(v is None for v in parent.values()):
            return
        qs = model.objects.filter(tenant_key=services.tenant(), name__iexact=name, **parent)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            self.add_error(field, f"A {label} with this name already exists here.")


class RoomForm(_Base):
    site = forms.ChoiceField(label="Site")

    class Meta:
        model = Room
        fields = ["site", "name", "floor", "description"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. Data Hall A"}),
                   "floor": forms.TextInput(attrs={"placeholder": "e.g. Ground, L2"}),
                   "description": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        choices = services.manageable_sites(self.user)
        if self.instance.pk and self.instance.site not in choices:
            choices = [self.instance.site] + choices
        self.fields["site"].choices = [(s, s) for s in choices]
        self.fields["site"].help_text = "Racks and cooling units in the room belong to this site."

    def clean_floor(self):
        return _clean_text(self.cleaned_data.get("floor"), "Floor")

    def clean_description(self):
        return _clean_text(self.cleaned_data.get("description"), "Description")[:2000]

    def clean(self):
        data = super().clean()
        self._unique(Room, "name", "room", site=data.get("site"))
        return data


class RackForm(_Base):
    class Meta:
        model = Rack
        fields = ["room", "name", "u_height", "status", "max_power_kw", "max_cooling_kw", "notes"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. A01"}),
                   "u_height": forms.NumberInput(attrs={"min": 1, "max": MAX_U}),
                   "max_power_kw": forms.NumberInput(attrs={"step": "0.01", "min": 0.01, "placeholder": "e.g. 8"}),
                   "max_cooling_kw": forms.NumberInput(attrs={"step": "0.01", "min": 0.01, "placeholder": "e.g. 10"}),
                   "notes": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"max_power_kw": "Power budget; the rack is flagged above 80 % and an event is raised above 90 %.",
                      "max_cooling_kw": "Heat the rack position can reject (optional)."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rooms = services.rooms_for(self.user) if self.user else Room.objects.none()
        allowed = [r.pk for r in rooms if services.can_manage(self.user, "operate", r.site)]
        if self.instance.pk:
            allowed.append(self.instance.room_id)
        self.fields["room"].queryset = Room.objects.filter(pk__in=allowed).order_by("site", "name")
        self.fields["room"].label_from_instance = lambda r: f"{r.name} · {r.site}" + (f" · {r.floor}" if r.floor else "")
        self.fields["room"].empty_label = "Choose a room…"

    def clean_notes(self):
        return _clean_text(self.cleaned_data.get("notes"), "Notes")[:4000]

    def clean(self):
        data = super().clean()
        self._unique(Rack, "name", "rack", room=data.get("room"))
        height = data.get("u_height")
        if self.instance.pk and height:
            tallest = max((it.u_top for it in self.instance.items.all()), default=0)
            if tallest > height:
                self.add_error("u_height", f"Items occupy units up to U{tallest}; move them before shrinking the rack "
                                           f"below {tallest}U.")
        return data


class RackItemForm(_Base):
    class Meta:
        model = RackItem
        fields = ["name", "kind", "u_position", "u_height", "face", "device", "ci", "power_draw_w", "notes"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. db-01 or Patch panel 1"}),
                   "u_position": forms.NumberInput(attrs={"min": 1}),
                   "u_height": forms.NumberInput(attrs={"min": 1}),
                   "power_draw_w": forms.NumberInput(attrs={"min": 0, "placeholder": "e.g. 450"})}

    def __init__(self, *args, rack=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.rack = rack or self.instance.rack
        self.instance.rack = self.rack
        self.instance.tenant_key = services.tenant()
        self.fields["u_position"].widget.attrs["max"] = self.rack.u_height
        self.fields["u_height"].widget.attrs["max"] = self.rack.u_height
        Device = apps.get_model("network", "Device")
        CI = apps.get_model("cmdb", "ConfigurationItem")
        self.fields["device"].queryset = scope_by_site(Device.objects.filter(tenant_key=services.tenant()),
                                                       self.user).order_by("name")
        self.fields["device"].label_from_instance = lambda d: f"{d.name} · {d.get_device_type_display()}" + (
            f" · {d.site}" if d.site else "")
        self.fields["device"].empty_label = "Not linked"
        self.fields["device"].help_text = "Network device (switch, router, firewall, server or VM)."
        self.fields["ci"].queryset = scope_by_site(CI.objects.filter(tenant_key=services.tenant()),
                                                   self.user).order_by("name")
        self.fields["ci"].empty_label = "Not linked"

    def clean_notes(self):
        return _clean_text(self.cleaned_data.get("notes"), "Notes")

    def _site_clash(self, obj, label):
        site = getattr(obj, "site", "") or ""
        if not services.is_global(site) and not services.is_global(self.rack.site) and site != self.rack.site:
            self.add_error(label, f"{obj.name} belongs to site “{site}”, but this rack is in “{self.rack.site}”.")

    def clean(self):
        data = super().clean()
        for field, label in (("device", "device"), ("ci", "configuration item")):
            obj = data.get(field)
            if obj is None:
                continue
            other = RackItem.objects.filter(tenant_key=services.tenant(), **{field: obj}).exclude(pk=self.instance.pk)
            other = other.select_related("rack").first()
            if other is not None:
                self.add_error(field, f"This {label} is already mounted as “{other.name}” in rack {other.rack.name} "
                                      f"({other.u_label}).")
            else:
                self._site_clash(obj, field)
        return data


class PowerFeedForm(_Base):
    class Meta:
        model = PowerFeed
        fields = ["rack", "name", "kind", "capacity_kw", "measured_kw", "prometheus_query", "notes"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. PDU-A"}),
                   "capacity_kw": forms.NumberInput(attrs={"step": "0.01", "min": 0.01, "placeholder": "e.g. 7.4"}),
                   "measured_kw": forms.NumberInput(attrs={"step": "0.001", "min": 0, "placeholder": "Optional"}),
                   "prometheus_query": forms.TextInput(attrs={
                       "placeholder": 'e.g. sum(pdu_power_watts{pdu="a01-a"}) / 1000',
                       "autocapitalize": "off", "spellcheck": "false", "class": "mono"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        racks = services.racks_for(self.user) if self.user else Rack.objects.none()
        allowed = [r.pk for r in racks if services.can_manage(self.user, "operate", r.site)]
        if self.instance.pk:
            allowed.append(self.instance.rack_id)
        self.fields["rack"].queryset = Rack.objects.filter(pk__in=allowed).select_related("room").order_by(
            "site", "room__name", "name")
        self.fields["rack"].label_from_instance = lambda r: f"{r.name} · {r.room.name} · {r.site}"
        self.fields["rack"].empty_label = "Choose a rack…"

    def clean_prometheus_query(self):
        return _clean_query(self.cleaned_data.get("prometheus_query"))

    def clean_notes(self):
        return _clean_text(self.cleaned_data.get("notes"), "Notes")

    def clean(self):
        data = super().clean()
        self._unique(PowerFeed, "name", "feed", rack=data.get("rack"))
        return data


class CoolingUnitForm(_Base):
    class Meta:
        model = CoolingUnit
        fields = ["room", "name", "kind", "status", "capacity_kw", "supply_temp_c", "return_temp_c",
                  "supply_query", "return_query", "notes"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "e.g. CRAH-01"}),
                   "capacity_kw": forms.NumberInput(attrs={"step": "0.1", "min": 0.01, "placeholder": "e.g. 90"}),
                   "supply_temp_c": forms.NumberInput(attrs={"step": "0.1", "placeholder": "e.g. 18"}),
                   "return_temp_c": forms.NumberInput(attrs={"step": "0.1", "placeholder": "e.g. 30"}),
                   "supply_query": forms.TextInput(attrs={"class": "mono", "autocapitalize": "off", "spellcheck": "false",
                                                          "placeholder": 'e.g. crah_supply_celsius{unit="crah-01"}'}),
                   "return_query": forms.TextInput(attrs={"class": "mono", "autocapitalize": "off", "spellcheck": "false",
                                                          "placeholder": 'e.g. crah_return_celsius{unit="crah-01"}'})}
        help_texts = {"supply_query": "Optional PromQL returning the supply air/water temperature in °C. "
                                      "If it matches several sensors their mean is used.",
                      "return_query": "Optional PromQL returning the return air/water temperature in °C. "
                                      "If it matches several sensors their mean is used."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        rooms = services.rooms_for(self.user) if self.user else Room.objects.none()
        allowed = [r.pk for r in rooms if services.can_manage(self.user, "operate", r.site)]
        if self.instance.pk:
            allowed.append(self.instance.room_id)
        self.fields["room"].queryset = Room.objects.filter(pk__in=allowed).order_by("site", "name")
        self.fields["room"].label_from_instance = lambda r: f"{r.name} · {r.site}"
        self.fields["room"].empty_label = "Choose a room…"

    def clean_supply_query(self):
        return _clean_query(self.cleaned_data.get("supply_query"))

    def clean_return_query(self):
        return _clean_query(self.cleaned_data.get("return_query"))

    def clean_notes(self):
        return _clean_text(self.cleaned_data.get("notes"), "Notes")

    def clean(self):
        data = super().clean()
        self._unique(CoolingUnit, "name", "cooling unit", room=data.get("room"))
        s, r = data.get("supply_temp_c"), data.get("return_temp_c")
        if s is not None and r is not None and s > r:
            self.add_error("return_temp_c", "Return temperature must not be below the supply temperature "
                                            "(warm air/water returns to the unit).")
        return data


