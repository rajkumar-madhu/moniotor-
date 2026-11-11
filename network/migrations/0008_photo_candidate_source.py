from django.db import migrations


def relabel(apps, schema_editor):
    """Photos picked in the photo finder were all stored with source "datasheet", so the device card said "from a
    datasheet" for a picture the operator uploaded or one fetched from an address. Every "datasheet" row was made by the
    finder, and its origin note still tells where the bytes came from: PDF pictures are "datasheet page N", an uploaded
    picture is "uploaded picture", and everything else (an image address, a product page) was fetched."""
    DeviceImage = apps.get_model("network", "DeviceImage")
    rows = DeviceImage.objects.filter(source="datasheet")
    rows.filter(origin="uploaded picture").update(source="upload", source_url="")
    rows.exclude(origin__startswith="datasheet page ").update(source="fetched")


class Migration(migrations.Migration):
    """Data only, no schema change. Not reversed: going back leaves the corrected labels, which the old code reads fine."""

    dependencies = [
        ("network", "0007_interface_port_details"),
    ]

    operations = [
        migrations.RunPython(relabel, migrations.RunPython.noop),
    ]
