from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: per-port facts for the interface detail page. Existing rows keep NULL / empty ("not reported")
    until the next metrics collection fills them in; reversing the migration only drops the four columns."""

    dependencies = [
        ("network", "0006_datasheet_photo_candidates"),
    ]

    operations = [
        migrations.AddField(
            model_name="interface",
            name="mtu",
            field=models.PositiveIntegerField(blank=True, help_text="ifMtu", null=True),
        ),
        migrations.AddField(
            model_name="interface",
            name="mac",
            field=models.CharField(blank=True, help_text="ifPhysAddress as aa:bb:cc:dd:ee:ff", max_length=17),
        ),
        migrations.AddField(
            model_name="interface",
            name="duplex",
            field=models.CharField(blank=True, help_text="dot3StatsDuplexStatus: full or half", max_length=4),
        ),
        migrations.AddField(
            model_name="interface",
            name="last_change_at",
            field=models.DateTimeField(blank=True, help_text="When ifOperStatus last changed (ifLastChange against sysUpTime)", null=True),
        ),
    ]
