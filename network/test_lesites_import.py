from django.conf import settings
from django.test import TestCase

from network.lesites_import import (
    LesitesDeviceRow,
    classify_device_type,
    import_devices,
    parse_devices_workbook,
    row_to_device_fields,
)
from network.models import Device, DeviceType


class LesitesClassifyTests(TestCase):
    def test_vm_and_firewall(self):
        self.assertEqual(classify_device_type("Ubuntu22", "VM"), DeviceType.VIRTUAL_MACHINE)
        self.assertEqual(classify_device_type("", "fortigate"), DeviceType.FIREWALL)
        self.assertEqual(classify_device_type("catalyst", "switch"), DeviceType.SWITCH)

    def test_row_to_fields(self):
        row = LesitesDeviceRow(
            selecthost="Ubuntu22",
            ipaddress="10.20.1.202",
            textname="aionion-dr-core",
            pathhost="VM",
            physical_ip="10.20.1.11",
            emailid="itsupport@finspot.in",
        )
        fields = row_to_device_fields(row, site="DR Site", environment="dr")
        self.assertIsNotNone(fields)
        assert fields is not None
        self.assertEqual(fields["name"], "aionion-dr-core")
        self.assertEqual(fields["device_type"], DeviceType.VIRTUAL_MACHINE)
        self.assertEqual(fields["mgmt_ip"], "10.20.1.202")
        self.assertEqual(fields["site"], "DR Site")
        self.assertEqual(fields["environment"], "dr")
        self.assertIn("imported", fields["tags"])
        self.assertIn("physical_ip=10.20.1.11", fields["description"])


class LesitesImportTests(TestCase):
    def setUp(self):
        self.tenant = settings.LINKEDEYE_TENANT

    def test_import_creates_and_updates(self):
        rows = [
            LesitesDeviceRow(ipaddress="10.20.1.50", textname="dr-sw-01", pathhost="switch", selecthost="cisco"),
            LesitesDeviceRow(ipaddress="10.20.1.51", textname="dr-fw-01", pathhost="fortigate"),
        ]
        r1 = import_devices(rows, self.tenant, site="DR Site", environment="dr", dry_run=False)
        self.assertEqual(r1.created, 2)
        self.assertEqual(Device.objects.filter(name="dr-sw-01").count(), 1)

        rows[0].servicename = "Core"
        r2 = import_devices(rows, self.tenant, site="DR Site", environment="dr", dry_run=False)
        self.assertEqual(r2.updated, 2)
        self.assertEqual(Device.objects.get(name__iexact="dr-sw-01").role, "Core")

    def test_parse_real_workbook_if_present(self):
        path = "/Users/rajkumarmadhu/Documents/fs-dr-le.xlsx"
        try:
            rows = parse_devices_workbook(path)
        except FileNotFoundError:
            self.skipTest("fs-dr-le.xlsx not on this machine")
        except OSError:
            self.skipTest("fs-dr-le.xlsx not readable")
        self.assertGreaterEqual(len(rows), 1)
        self.assertTrue(any(r.ipaddress for r in rows))
