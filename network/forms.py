"""Device, discovery and approval forms."""
from django import forms
from django.conf import settings
from django.core.exceptions import ValidationError
from . import discovery, images
from .catalog import best_row
from .imaging import ImageError
from .models import Device, DeviceType, Environment, NetworkProductImage

TEXT = {"class": "in"}


def site_choices(current="", blank_label="(No site)"):
    """Site names from core.Site plus any value already used by a device."""
    from core.models import Site
    t = settings.LINKEDEYE_TENANT
    names = set(Site.objects.filter(tenant_key=t).values_list("name", flat=True))
    names |= set(Device.objects.filter(tenant_key=t).order_by().values_list("site", flat=True).distinct())
    if current:
        names.add(current)
    names.discard("")
    ordered = sorted(names - {"All Sites"}, key=str.lower)
    return [("", blank_label)] + ([("All Sites", "All Sites")] if "All Sites" in names else []) + [(n, n) for n in ordered]


def normalise_tags(text):
    seen = []
    for t in (text or "").split(","):
        t = t.strip()
        if t and t.lower() not in [s.lower() for s in seen]:
            seen.append(t)
    return ", ".join(seen)


class FieldImageError(ImageError):
    """An ImageError that belongs to one form field."""

    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


IMAGE_ACCEPT = "image/png,image/jpeg,image/webp"
TYPE_LABELS = dict(DeviceType.choices)


def image_from_sources(cd, user, row=None):
    """The stored photo chosen in a form: an upload, a web address fetched once, or a catalog row's photo. None when
    none was given; FieldImageError when two were given or one was refused."""
    upload, url = cd.get("image_file"), (cd.get("image_url") or "").strip()
    if sum(bool(x) for x in (upload, url, row)) > 1:
        raise FieldImageError("image_file", "Use one way to set the photo: upload a file, fetch an address, or pick from the catalog.")
    try:
        if upload:
            return images.store_upload(upload, user)[0]
        if url:
            return images.fetch_and_store(url, user)[0]
    except ImageError as e:
        raise FieldImageError("image_file" if upload else "image_url", str(e)) from None
    return row.image if row else None


class DeviceForm(forms.ModelForm):
    site = forms.ChoiceField(required=False)
    # the photo: at most one of these is used; none keeps the current photo
    image_file = forms.FileField(
        required=False, label="Upload a photo",
        widget=forms.FileInput(attrs={"accept": IMAGE_ACCEPT}),
        help_text="PNG, JPEG or WebP, up to 2 MB and 4096 × 4096 px. SVG and GIF are not accepted.")
    image_url = forms.CharField(
        required=False, max_length=500, label="Fetch from a web address",
        widget=forms.URLInput(attrs={"placeholder": "https://…/photo.png", "autocomplete": "off", "data-prod-fetch": "1"}),
        help_text="LinkedEye downloads the photo once, now, and stores its own copy. The link is not kept or shown to viewers.")
    catalog_row = forms.ModelChoiceField(
        queryset=NetworkProductImage.objects.none(), required=False, label="Pick from the product catalog",
        empty_label="No catalog photo")
    clear_image = forms.BooleanField(required=False, label="Remove the current photo")

    GROUPS = [
        ("Identity", ["name", "device_type", "vendor", "model", "serial", "firmware", "product_page_url"]),
        ("Monitoring", ["mgmt_ip", "check_port", "prometheus_instance", "enabled"]),
        ("Placement & ownership", ["site", "role", "environment", "contact"]),
        ("Notes", ["tags", "description"]),
    ]

    class Meta:
        model = Device
        fields = ["name", "device_type", "vendor", "model", "serial", "firmware", "product_image_url", "product_page_url",
                  "mgmt_ip", "check_port", "prometheus_instance", "enabled", "site", "role", "environment", "contact",
                  "tags", "description"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "name": forms.TextInput(attrs={"placeholder": "e.g. core-sw-01", "autocomplete": "off"}),
            "mgmt_ip": forms.TextInput(attrs={"placeholder": "e.g. 10.10.1.254", "autocomplete": "off"}),
            "prometheus_instance": forms.TextInput(attrs={"placeholder": "e.g. 10.10.1.254"}),
            "tags": forms.TextInput(attrs={"placeholder": "e.g. core, dc1"}),
            "vendor": forms.TextInput(attrs={"placeholder": "e.g. Fortinet", "list": "net-vendors", "data-prod-vendor": "1"}),
            "model": forms.TextInput(attrs={"placeholder": "e.g. MX204", "data-prod-model": "1"}),
            "contact": forms.TextInput(attrs={"placeholder": "e.g. network@example.com"}),
            "role": forms.TextInput(attrs={"placeholder": "e.g. Perimeter Firewall"}),
            "product_image_url": forms.URLInput(attrs={"placeholder": "https://…/product.webp", "data-prod-url": "image"}),
            "product_page_url": forms.URLInput(attrs={"placeholder": "https://…/datasheet", "data-prod-url": "page"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = site_choices(self.instance.site if self.instance.pk else self.initial.get("site", ""))
        self.fields["environment"].choices = [("", "—")] + list(Environment.choices)
        self.fields["check_port"].widget.attrs.update({"min": 1, "max": 65535})
        self.fields["product_image_url"].required = False
        self.fields["product_page_url"].required = False
        self.fields["product_image_url"].label = "Linked image URL (legacy)"
        self.fields["product_image_url"].help_text = (
            "Only used when the device has no stored photo, and the browser then loads it from that site. "
            "Prefer uploading or fetching a photo above.")
        rows = (NetworkProductImage.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, image__isnull=False)
                .select_related("image").defer("image__data").order_by("vendor", "device_type", "model_key"))
        self.fields["catalog_row"].queryset = rows
        self.fields["catalog_row"].label_from_instance = lambda r: (
            f"{r.vendor} {r.model_key or '(any model)'}" + (f" · {TYPE_LABELS.get(r.device_type, r.device_type)}" if r.device_type else ""))
        self.product_catalog = [
            {"id": r.pk, "vendor": r.vendor, "device_type": r.device_type, "model_key": r.model_key,
             "image_url": r.image.url if r.image_id else "", "product_image_url": r.product_image_url, "product_page_url": r.product_page_url}
            for r in NetworkProductImage.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
            .select_related("image").defer("image__data")
        ]

    def resolve_image(self, user):
        """Decide what happens to the device photo, after is_valid(). Returns ("keep", None), ("set", DeviceImage) or
        ("clear", None); raises FieldImageError(field, message) when an upload or download is refused. Uploads and
        downloads are validated and stored here (network.images), so nothing reaches the database unchecked."""
        cd = self.cleaned_data
        chosen = image_from_sources(cd, user, cd.get("catalog_row"))
        if chosen is not None:
            return "set", chosen
        if cd.get("clear_image"):
            return "clear", None
        # a device that has no photo yet, and is new or whose vendor / model / type just changed, gets the matching
        # catalog photo for free (an existing photo is never replaced behind your back)
        if not self.instance.image_id and (not self.instance.pk or {"vendor", "model", "device_type"} & set(self.changed_data)):
            match = best_row(tenant_key=settings.LINKEDEYE_TENANT, device_type=cd.get("device_type") or "",
                             vendor=cd.get("vendor") or "", model=cd.get("model") or "", with_image=True)
            if match is not None and match.image_id != self.instance.image_id:
                return "set", match.image
        return "keep", None

    @property
    def groups(self):
        return [(title, [self[f] for f in names]) for title, names in self.GROUPS]

    @property
    def photo_fields(self):
        return [self["image_file"], self["image_url"], self["catalog_row"]]

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        qs = Device.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A device with this name already exists.")
        return name

    def clean_mgmt_ip(self):
        return self.cleaned_data["mgmt_ip"].strip()

    def clean_prometheus_instance(self):
        return self.cleaned_data["prometheus_instance"].strip()

    def clean_tags(self):
        return normalise_tags(self.cleaned_data["tags"])

    def clean(self):
        cleaned = super().clean()
        v = (cleaned.get("vendor") or "").strip()
        m = (cleaned.get("model") or "").strip()
        dt = cleaned.get("device_type") or ""
        if v:
            # a catalog row that still only has the legacy link fills the link fields (rows with a stored photo
            # attach that photo instead, see resolve_image)
            row = best_row(tenant_key=settings.LINKEDEYE_TENANT, device_type=dt, vendor=v, model=m)
            if row is not None and not row.image_id:
                if not (cleaned.get("product_image_url") or "").strip() and row.product_image_url:
                    cleaned["product_image_url"] = row.product_image_url
            if row is not None and not (cleaned.get("product_page_url") or "").strip() and row.product_page_url:
                cleaned["product_page_url"] = row.product_page_url
        return cleaned


class DiscoveryForm(forms.Form):
    cidr = forms.CharField(label="Network (CIDR)", max_length=64,
                           widget=forms.TextInput(attrs={"placeholder": "e.g. 10.10.20.0/24", "autocomplete": "off"}),
                           help_text="IPv4 /24 or smaller (IPv6 /120). Only addresses the probe policy allows are contacted.")
    ports = forms.CharField(label="TCP ports", max_length=100, initial="22,443",
                            widget=forms.TextInput(attrs={"placeholder": "22,443"}),
                            help_text=f"Comma-separated, at most {discovery.MAX_PORTS}. A host is listed when any port answers.")
    site = forms.ChoiceField(label="Site for approved devices", required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = site_choices(self.initial.get("site", ""))

    def clean_cidr(self):
        net = discovery.parse_cidr(self.cleaned_data["cidr"])
        allowed, _blocked = discovery.split_allowed(discovery.addresses(net))
        if not allowed:
            raise ValidationError("No address in this network is allowed by the probe policy. Private ranges must be listed in "
                                  "LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS; link-local, multicast and reserved ranges are never scanned.")
        return str(net)

    def clean_ports(self):
        return ",".join(str(p) for p in discovery.parse_ports(self.cleaned_data["ports"]))


class ApproveForm(forms.Form):
    device_type = forms.ChoiceField(label="Device type", choices=DeviceType.choices)
    site = forms.ChoiceField(required=False)
    name_prefix = forms.CharField(label="Name prefix", max_length=60, required=False,
                                  widget=forms.TextInput(attrs={"placeholder": "e.g. sw-"}),
                                  help_text="Devices are named prefix + IP address; rename them later.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = site_choices(self.initial.get("site", ""))


class CatalogForm(forms.ModelForm):
    """One product catalog row. The photo is uploaded or fetched once and stored; the row never hotlinks."""
    device_type = forms.ChoiceField(required=False, label="Device type", choices=[("", "Any type")] + list(DeviceType.choices))
    image_file = forms.FileField(required=False, label="Upload a photo", widget=forms.FileInput(attrs={"accept": IMAGE_ACCEPT}),
                                 help_text="PNG, JPEG or WebP, up to 2 MB and 4096 × 4096 px.")
    image_url = forms.CharField(required=False, max_length=500, label="Fetch from a web address",
                                widget=forms.URLInput(attrs={"placeholder": "https://…/photo.png", "autocomplete": "off"}),
                                help_text="Downloaded once through the safe fetcher (https only, public addresses only) and stored.")

    class Meta:
        model = NetworkProductImage
        fields = ["vendor", "device_type", "model_key", "product_page_url", "notes"]
        widgets = {
            "vendor": forms.TextInput(attrs={"placeholder": "e.g. Arista", "list": "cat-vendors"}),
            "model_key": forms.TextInput(attrs={"placeholder": "e.g. DCS-7280 (empty = vendor default)"}),
            "product_page_url": forms.URLInput(attrs={"placeholder": "https://…/datasheet"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        row = self.instance
        if row.pk and not row.image_id and row.product_image_url:
            # an older row that only has a hotlink: offer to fetch it once and store a copy
            self.fields["image_url"].initial = row.product_image_url
            self.fields["image_url"].help_text = ("This row only linked to the picture. Save to download it once and "
                                                  "store LinkedEye's own copy.")

    def clean_vendor(self):
        return self.cleaned_data["vendor"].strip()

    def clean_model_key(self):
        return self.cleaned_data["model_key"].strip()

    def clean(self):
        cleaned = super().clean()
        v, dt, m = cleaned.get("vendor"), cleaned.get("device_type") or "", cleaned.get("model_key") or ""
        if v:
            dup = NetworkProductImage.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, vendor__iexact=v, device_type=dt,
                                                     model_key__iexact=m)
            if self.instance.pk:
                dup = dup.exclude(pk=self.instance.pk)
            if dup.exists():
                self.add_error("model_key", "The catalog already has a row for this vendor, type and model.")
        has_image = bool(self.instance.image_id)
        if not (has_image or cleaned.get("image_file") or (cleaned.get("image_url") or "").strip()):
            self.add_error("image_file", "Add a photo: upload a file or fetch an address.")
        return cleaned

    def resolve_image(self, user):
        """The new stored photo, or None to keep the current one; raises FieldImageError. Call after is_valid()."""
        return image_from_sources(self.cleaned_data, user)
