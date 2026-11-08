"""Forms for the Kubernetes inventory module: the workbook upload and client linking."""
from pathlib import Path

from django import forms
from django.apps import apps

from .models import GLOBAL_SITE
from .readers import CSV_SUFFIX, XLSX_SUFFIXES, ZIP_SUFFIX

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ACCEPTED_SUFFIXES = (*XLSX_SUFFIXES, ZIP_SUFFIX, CSV_SUFFIX)


def site_choices():
    """"All Sites" plus the configured sites, for filing the imported records."""
    Site = apps.get_model("core", "Site")
    names = list(Site.objects.order_by("name").values_list("name", flat=True))
    return [(GLOBAL_SITE, GLOBAL_SITE)] + [(n, n) for n in names if n and n != GLOBAL_SITE]


class UploadForm(forms.Form):
    """The inventory workbook (or a zip of its sheets as CSV)."""

    file = forms.FileField(
        label="Inventory workbook",
        help_text=f"The .xlsx workbook, or a .zip of its sheets exported as CSV. "
                  f"At most {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    site = forms.ChoiceField(label="File records under", choices=(), initial=GLOBAL_SITE,
                              help_text="The site the imported clusters and namespaces belong to.")
    create_compute = forms.BooleanField(
        label="Also create a Compute cluster and its members", required=False,
        help_text="Mirrors the cluster into Compute → Clusters and adds a device for every node that is "
                  "not in the device inventory yet. Leave this off to import the inventory only.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["site"].choices = site_choices()
        self.fields["file"].widget.attrs["accept"] = ",".join(ACCEPTED_SUFFIXES)

    def clean_file(self):
        uploaded = self.cleaned_data["file"]
        if uploaded.size > MAX_UPLOAD_BYTES:
            raise forms.ValidationError(
                f"The file is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
        if not uploaded.size:
            raise forms.ValidationError("The file is empty.")
        suffix = Path(uploaded.name or "").suffix.lower()
        if suffix not in ACCEPTED_SUFFIXES:
            raise forms.ValidationError(
                f"Expected one of {', '.join(ACCEPTED_SUFFIXES)} — {uploaded.name or 'the file'} is not.")
        return uploaded


class ProfileLinkForm(forms.Form):
    """Link one workbook client to a LinkedEye client (or clear the link)."""

    client = forms.ChoiceField(label="LinkedEye client", required=False)

    def __init__(self, *args, clients=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["client"].choices = [("", "— not linked —")] + [(str(c.pk), c.name) for c in clients]
