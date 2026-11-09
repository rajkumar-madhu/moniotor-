from django.conf import settings
from django.test import TestCase

from administration.finspot_site import ensure_finspot_site
from core.models import Client, Site, SiteLesiteProfile
from network.models import Device


class FinspotSiteEnsureTests(TestCase):
    def setUp(self):
        self.tenant = settings.LINKEDEYE_TENANT
        Device.objects.create(
            tenant_key=self.tenant,
            name="GM-SW",
            device_type="switch",
            mgmt_ip="10.10.100.251",
            site="FinSpot DX",
            tags="fs-dx-le,imported",
        )

    def test_ensure_fs_dx_le(self):
        site, created, renamed = ensure_finspot_site(self.tenant, "fs-dx-le")
        self.assertTrue(created)
        self.assertEqual(site.name, "FinSpot DX DR")
        self.assertEqual(site.code, "fs-dx-le")
        self.assertEqual(renamed, 1)
        self.assertEqual(Device.objects.get(name="GM-SW").site, "FinSpot DX DR")
        profile = SiteLesiteProfile.objects.get(site=site)
        self.assertEqual(profile.legacy_sitename, "fs-dx-le")
        self.assertEqual(profile.environment, "DX")
        self.assertEqual(profile.le_url, "https://fs-le-dx.finspot.in")
        self.assertTrue(Client.objects.filter(code="finspot").exists())
