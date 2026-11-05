from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0004_client"),
    ]

    operations = [
        migrations.CreateModel(
            name="SiteLesiteProfile",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("enabled", models.BooleanField(default=True)),
                ("legacy_sitename", models.CharField(blank=True, help_text="Lesites sitename slug (e.g. fs-dr-le). Defaults from site code when empty.", max_length=100)),
                ("websocket_url", models.CharField(blank=True, max_length=500)),
                ("environment", models.CharField(blank=True, default="PROD", max_length=40)),
                ("entity_host", models.CharField(blank=True, max_length=255)),
                ("entity_port", models.CharField(blank=True, default="443", max_length=8)),
                ("is_url_secured", models.BooleanField(default=True)),
                ("analytics_prefix_url", models.URLField(blank=True, max_length=500)),
                ("location", models.CharField(blank=True, help_text="Country-state key, e.g. IN-KA", max_length=80)),
                ("country_code", models.CharField(blank=True, max_length=8)),
                ("redis_host", models.CharField(blank=True, max_length=255)),
                ("redis_port", models.CharField(blank=True, default="6379", max_length=8)),
                ("prometheus_url", models.URLField(blank=True, max_length=500)),
                ("elastic_host", models.CharField(blank=True, max_length=255)),
                ("elastic_port", models.CharField(blank=True, default="9200", max_length=8)),
                ("grafana_api", models.CharField(blank=True, max_length=500)),
                ("le_url", models.URLField(blank=True, max_length=500)),
                ("incident_url", models.URLField(blank=True, max_length=500)),
                ("incident_api", models.CharField(blank=True, max_length=500)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("site", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="lesite_profile", to="core.site")),
            ],
            options={
                "verbose_name": "site lesites profile",
            },
        ),
    ]
