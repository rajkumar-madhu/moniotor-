"""IPAM forms. Every form takes the requesting user so choices are limited to what they can see and the
site they pick is one they may operate on."""
import ipaddress
import re

from django import forms
from django.conf import settings

from . import dns, services
from .models import GLOBAL_SITE, AddressStatus, DNSRecord, DNSZone, IPAddress, RecordType, Role, Subnet, VLAN

VRF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$")
MIN_PREFIX = {4: 8, 6: 16}


def site_choices(user, current=""):
    names = services.site_names(user)
    if current and current not in names and current != GLOBAL_SITE:
        names = sorted(set(names) | {current}, key=str.lower)
    return [(GLOBAL_SITE, "All Sites (global)")] + [(n, n) for n in names]


def clean_vrf(value):
    value = (value or "").strip()
    if value and not VRF_RE.match(value):
        raise forms.ValidationError("Use letters, digits and _ . : - only (at most 100 characters), e.g. MGMT or cust-a.")
    return value


class _SiteMixin:
    """Site field limited to sites the user may operate on (checked again in the view for the old site)."""

    def _setup_site(self, user):
        current = self.instance.site if self.instance.pk else self.initial.get("site", "")
        self.fields["site"] = forms.ChoiceField(label="Site", choices=site_choices(user, current), required=False,
                                                initial=current or GLOBAL_SITE)

    def clean_site(self):
        site = (self.cleaned_data.get("site") or "").strip() or GLOBAL_SITE
        if not services.can_manage(self.user, "operate", site):
            raise forms.ValidationError(f"You cannot add or change IPAM records for “{site}”.")
        return site


# ------------------------------------------------------------------ VLAN
class VLANForm(_SiteMixin, forms.ModelForm):
    class Meta:
        model = VLAN
        fields = ["vid", "name", "site", "description"]
        widgets = {"vid": forms.NumberInput(attrs={"min": 1, "max": 4094, "placeholder": "1–4094"}),
                   "name": forms.TextInput(attrs={"placeholder": "e.g. SERVERS", "autocomplete": "off"}),
                   "description": forms.TextInput(attrs={"placeholder": "Optional"})}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._setup_site(user)
        self.order_fields(["vid", "name", "site", "description"])

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not name:
            raise forms.ValidationError("Enter a name.")
        return name

    def clean(self):
        data = super().clean()
        vid, site = data.get("vid"), data.get("site")
        if vid is None or not site:
            return data
        qs = VLAN.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, site=site, vid=vid)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            self.add_error("vid", f"VLAN {vid} already exists for {site}.")
        if self.instance.pk and site != GLOBAL_SITE:
            elsewhere = self.instance.subnets.exclude(site=site).values_list("cidr", flat=True)[:3]
            if elsewhere:
                self.add_error("site", f"Subnets on other sites use this VLAN ({', '.join(elsewhere)}); "
                                       "move them first or keep the VLAN global.")
        return data


# ------------------------------------------------------------------ Subnet
class SubnetForm(_SiteMixin, forms.ModelForm):
    gateway = forms.CharField(max_length=45, required=False,
                              widget=forms.TextInput(attrs={"placeholder": "Optional, e.g. 10.0.1.1", "autocomplete": "off"}))

    class Meta:
        model = Subnet
        fields = ["cidr", "vrf", "site", "role", "vlan", "gateway", "description"]
        widgets = {"cidr": forms.TextInput(attrs={"placeholder": "e.g. 10.0.1.0/24 or 2001:db8:1::/64", "autocomplete": "off",
                                                  "spellcheck": "false"}),
                   "vrf": forms.TextInput(attrs={"placeholder": "Empty = global table", "list": "ipam-vrfs"}),
                   "description": forms.TextInput(attrs={"placeholder": "Optional"})}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._setup_site(user)
        self.fields["vlan"].queryset = services.vlans_for(user).order_by("site", "vid")
        self.fields["vlan"].required = False
        self.fields["vlan"].empty_label = "— No VLAN —"
        self.fields["role"].choices = [("", "—")] + list(Role.choices)
        if self.instance.pk and self.instance.gateway:
            self.initial["gateway"] = self.instance.gateway
        self.order_fields(["cidr", "vrf", "site", "role", "vlan", "gateway", "description"])

    def clean_vrf(self):
        return clean_vrf(self.cleaned_data.get("vrf"))

    def clean_cidr(self):
        try:
            net = services.parse_network(self.cleaned_data.get("cidr"))
        except ValueError as e:
            raise forms.ValidationError(str(e))
        if net.prefixlen < MIN_PREFIX[net.version]:
            raise forms.ValidationError(f"IPv{net.version} subnets must be /{MIN_PREFIX[net.version]} or smaller.")
        return str(net)

    def clean(self):
        data = super().clean()
        cidr, site = data.get("cidr"), data.get("site")
        if not cidr or "vrf" not in data:
            return data
        net, vrf, role = ipaddress.ip_network(cidr), data["vrf"], data.get("role") or ""
        err = services.placement_error(settings.LINKEDEYE_TENANT, vrf, net, role, exclude_pk=self.instance.pk)
        if err:
            self.add_error("cidr", err)
        gw = (data.get("gateway") or "").strip()
        if gw:
            try:
                gip = services.parse_address(gw)
            except ValueError as e:
                self.add_error("gateway", str(e))
            else:
                if gip.version != net.version or gip not in net:
                    self.add_error("gateway", f"The gateway must be inside {net}.")
                elif net.version == 4 and net.prefixlen < 31 and gip in (net.network_address, net.broadcast_address):
                    self.add_error("gateway", "The gateway cannot be the network or broadcast address.")
                elif net.version == 6 and net.prefixlen < 127 and gip == net.network_address:
                    self.add_error("gateway", "The gateway cannot be the subnet-router anycast address.")
                else:
                    data["gateway"] = str(gip)
        else:
            data["gateway"] = None
        vlan = data.get("vlan")
        if vlan is not None and site and vlan.site not in (GLOBAL_SITE, "", site):
            self.add_error("vlan", f"{vlan} belongs to {vlan.site}; pick a VLAN of {site} or a global one.")
        if self.instance.pk:
            old = self.instance
            mine = old.addresses.all()
            if vrf != old.vrf and mine.exists():
                self.add_error("vrf", "This subnet has addresses: move or delete them before changing the VRF.")
            elif cidr != old.cidr:
                start, end = services.ip_key(net.network_address), services.ip_key(net.broadcast_address)
                outside = mine.exclude(version=net.version, addr_key__gte=start, addr_key__lte=end)
                if outside.exists():
                    sample = ", ".join(outside.values_list("address", flat=True)[:3])
                    self.add_error("cidr", f"{outside.count()} recorded address(es) would fall outside {net} ({sample}). "
                                           "Move or delete them first.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.gateway = self.cleaned_data.get("gateway")
        if commit:
            obj.save()
        return obj


# ------------------------------------------------------------------ IP address
def _vrf_choices():
    return [("", "Global table")] + [(v, v) for v in services.vrf_names()]


class IPAddressForm(forms.ModelForm):
    vrf = forms.ChoiceField(label="VRF", required=False)

    class Meta:
        model = IPAddress
        fields = ["address", "vrf", "status", "hostname", "device", "ci", "description"]
        widgets = {"address": forms.TextInput(attrs={"placeholder": "e.g. 10.0.1.20 or 2001:db8:1::20", "autocomplete": "off",
                                                     "spellcheck": "false"}),
                   "hostname": forms.TextInput(attrs={"placeholder": "e.g. app01.corp.example.com", "autocomplete": "off"}),
                   "description": forms.TextInput(attrs={"placeholder": "Optional"})}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.subnet = None
        choices = _vrf_choices()
        current = self.instance.vrf if self.instance.pk else self.initial.get("vrf", "")
        if current and current not in dict(choices):
            choices.append((current, current))
        self.fields["vrf"].choices = choices
        devices, cis = services.devices_for(user), services.cis_for(user)
        if devices is None:
            del self.fields["device"]
        else:
            self.fields["device"].queryset = devices.order_by("name")
            self.fields["device"].required = False
            self.fields["device"].empty_label = "— No device —"
        if cis is None:
            del self.fields["ci"]
        else:
            self.fields["ci"].queryset = cis.order_by("name")
            self.fields["ci"].required = False
            self.fields["ci"].empty_label = "— No CI —"

    def clean_address(self):
        try:
            return str(services.parse_address(self.cleaned_data.get("address")))
        except ValueError as e:
            raise forms.ValidationError(str(e))

    def clean_vrf(self):
        return clean_vrf(self.cleaned_data.get("vrf"))

    def clean_hostname(self):
        value = (self.cleaned_data.get("hostname") or "").strip()
        if not value:
            return ""
        try:
            return dns.hostname(value, "host name")
        except dns.DNSError as e:
            raise forms.ValidationError(str(e))

    def clean(self):
        data = super().clean()
        addr, vrf = data.get("address"), data.get("vrf", "")
        if not addr:
            return data
        error = address_error(self.user, addr, vrf, exclude_pk=self.instance.pk)
        if isinstance(error, str):
            self.add_error("address", error)
        else:
            self.subnet = error
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.subnet = self.subnet
        obj.site = self.subnet.site
        if commit:
            obj.save()
        return obj


def address_error(user, address, vrf, exclude_pk=None, tenant_key=None):
    """The subnet an address belongs to (a Subnet), or an error message (a str)."""
    t = tenant_key or settings.LINKEDEYE_TENANT
    ip = ipaddress.ip_address(address)
    subnet = services.subnet_for(ip, vrf, t)
    where = f"VRF “{vrf}”" if vrf else "the global table"
    if subnet is None or not services.subnets_for(user).filter(pk=subnet.pk).exists():
        return f"No subnet in {where} contains {ip}. Add the subnet first."
    net = subnet.network
    if net.version == 4 and net.prefixlen < 31:
        if ip == net.network_address:
            return f"{ip} is the network address of {net}."
        if ip == net.broadcast_address:
            return f"{ip} is the broadcast address of {net}."
    if net.version == 6 and net.prefixlen < 127 and ip == net.network_address:
        return f"{ip} is the subnet-router anycast address of {net}."
    dup = IPAddress.objects.filter(tenant_key=t, vrf=vrf, address=str(ip))
    if exclude_pk:
        dup = dup.exclude(pk=exclude_pk)
    if dup.exists():
        return f"{ip} is already recorded in {where}."
    return subnet


class ImportForm(forms.Form):
    MAX_BYTES = 2 * 1024 * 1024
    file = forms.FileField(label="CSV file", help_text="UTF-8, at most 2 MB and 5,000 rows.")
    update_existing = forms.BooleanField(label="Update addresses that already exist", required=False)

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > self.MAX_BYTES:
            raise forms.ValidationError("The file is larger than 2 MB.")
        return f


# ------------------------------------------------------------------ DNS
class DNSZoneForm(_SiteMixin, forms.ModelForm):
    class Meta:
        model = DNSZone
        fields = ["name", "kind", "site", "default_ttl", "description"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "corp.example.com, 1.10.in-addr.arpa or 10.1.0.0/16",
                                                  "autocomplete": "off", "spellcheck": "false"}),
                   "default_ttl": forms.NumberInput(attrs={"min": 1, "max": 2592000}),
                   "description": forms.TextInput(attrs={"placeholder": "Optional"})}
        help_texts = {"name": "For reverse zones you can enter the network (octet/nibble aligned)."}

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self._setup_site(user)
        self.order_fields(["name", "kind", "site", "default_ttl", "description"])

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        if not kind or not data.get("name"):
            return data
        try:
            name = dns.zone_name(data["name"], kind)
        except dns.DNSError as e:
            self.add_error("name", str(e))
            return data
        data["name"] = name
        qs = DNSZone.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, name=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
            if kind != self.instance.kind and self.instance.records.exists():
                self.add_error("kind", "This zone has records: delete them before changing forward/reverse.")
        if qs.exists():
            self.add_error("name", f"The zone {name} already exists.")
        return data


class DNSRecordForm(forms.ModelForm):
    class Meta:
        model = DNSRecord
        fields = ["name", "rtype", "value", "ttl", "description"]
        widgets = {"name": forms.TextInput(attrs={"placeholder": "@ for the apex, e.g. www", "autocomplete": "off",
                                                  "spellcheck": "false"}),
                   "value": forms.TextInput(attrs={"placeholder": "e.g. 10.0.1.20", "autocomplete": "off", "spellcheck": "false"}),
                   "ttl": forms.NumberInput(attrs={"min": 1, "max": 2592000, "placeholder": "Zone default"}),
                   "description": forms.TextInput(attrs={"placeholder": "Optional"})}

    def __init__(self, *args, zone, **kwargs):
        super().__init__(*args, **kwargs)
        self.zone = zone
        allowed = dns.REVERSE_TYPES if zone.kind == "reverse" else dns.FORWARD_TYPES
        self.fields["rtype"].choices = [(v, l) for v, l in RecordType.choices if v in allowed]
        if zone.kind == "reverse":
            self.fields["name"].widget.attrs["placeholder"] = "Address (e.g. 10.1.2.20) or relative name (20)"
            self.fields["value"].widget.attrs["placeholder"] = "Host name, e.g. app01.corp.example.com"

    def clean(self):
        data = super().clean()
        rtype, zone = data.get("rtype"), self.zone
        if not rtype:
            return data
        raw_name = (data.get("name") or "").strip()
        name = None
        if zone.kind == "reverse":
            try:
                ip = ipaddress.ip_address(raw_name)
            except ValueError:
                ip = None
            if ip is not None:
                name = dns.ptr_name(ip, zone.name)
                if name is None:
                    self.add_error("name", f"{ip} is not inside the reverse zone {zone.name}.")
                    return data
        if name is None:
            try:
                name = dns.record_name(raw_name, zone.name)
            except dns.DNSError as e:
                self.add_error("name", str(e))
                return data
        data["name"] = name
        try:
            data["value"] = dns.record_value(rtype, data.get("value"))
        except dns.DNSError as e:
            self.add_error("value", str(e))
            return data
        if rtype == "PTR" and dns.ptr_address(name, zone.name) is None:
            self.add_error("name", "A PTR name must be a complete reverse name inside this zone (e.g. the last octet).")
        others = DNSRecord.objects.filter(zone=zone, name=name)
        if self.instance.pk:
            others = others.exclude(pk=self.instance.pk)
        if rtype == "CNAME":
            if name == "@":
                self.add_error("name", "A CNAME cannot be at the zone apex.")
            elif others.exists():
                self.add_error("name", "A CNAME cannot share its name with other records.")
        elif others.filter(rtype="CNAME").exists():
            self.add_error("name", "This name already has a CNAME record; no other records may share it.")
        if others.filter(rtype=rtype, value=data["value"]).exists():
            self.add_error("value", "This record already exists.")
        return data
