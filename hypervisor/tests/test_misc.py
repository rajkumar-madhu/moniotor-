"""Seed command, migrations, static assets, wiring."""
import re
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings

from hypervisor.models import Datastore, VCenterSource, VmEvent, VmInfo, VmSnapshot
from hypervisor.tests.base import HvCase
from network.models import Device

APP = Path(__file__).resolve().parent.parent
ROOT = APP.parent


class Seed(TestCase):
    def run_seed(self, *args):
        out = StringIO()
        call_command("seed_hypervisor", *args, stdout=out)
        return out.getvalue()

    def counts(self):
        return tuple(m.objects.count() for m in (VCenterSource, VmInfo, Datastore, VmSnapshot, VmEvent, Device))

    def test_idempotent_marked_and_removable(self):
        from core.models import Event
        self.run_seed()
        first = self.counts()
        self.run_seed()
        self.assertEqual(first, self.counts())
        src = VCenterSource.objects.get()
        self.assertTrue(src.is_example)
        self.assertFalse(src.enabled)
        self.assertEqual(src.state[0], "example")
        self.assertEqual(Event.objects.count(), 0)               # the demo never raises events
        vm = VmInfo.objects.get()
        self.assertEqual((vm.match_state, vm.device.name), ("matched", "VM-APP-01"))
        self.assertEqual(vm.snapshots.count(), 3)
        self.assertEqual(sum(1 for s in vm.snapshots.all() if s.needs_review()), 1)
        self.assertIn("vCenter source and 1 example", self.run_seed("--remove"))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0))
        self.run_seed("--remove")

    def test_a_real_device_with_the_same_name_is_never_touched(self):
        real = Device.objects.create(tenant_key="default", name="VM-APP-01", device_type="virtual_machine", mgmt_ip="10.0.0.9")
        self.run_seed()
        real.refresh_from_db()
        self.assertEqual((real.mgmt_ip, real.vendor), ("10.0.0.9", ""))
        self.assertEqual(VmInfo.objects.get().device.name, "VM-APP-01 (example)")
        self.run_seed("--remove")
        self.assertTrue(Device.objects.filter(pk=real.pk).exists())
        self.assertFalse(Device.objects.filter(name="VM-APP-01 (example)").exists())

    def test_the_example_source_is_never_synced(self):
        from hypervisor import sync
        self.run_seed()
        self.assertFalse(sync.due(VCenterSource.objects.get(), __import__("django.utils.timezone", fromlist=["now"]).now()))


class MigrationReversible(TransactionTestCase):
    def test_zero_and_forward_again(self):
        call_command("migrate", "hypervisor", "zero", verbosity=0)
        self.assertNotIn("hypervisor_vcentersource", connection.introspection.table_names())
        call_command("migrate", "hypervisor", verbosity=0)
        for t in ("vcentersource", "hvhost", "datastore", "vminfo", "vmsnapshot", "vmevent", "vmcontrolrequest"):
            self.assertIn(f"hypervisor_{t}", connection.introspection.table_names())

    def test_exactly_one_migration(self):
        self.assertEqual(sorted(p.name for p in (APP / "migrations").glob("0*.py")), ["0001_initial.py"])


class Assets(TestCase):
    def test_css_uses_tokens_not_colours(self):
        css = (APP / "static" / "hypervisor" / "page.css").read_text()
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", css))
        self.assertIsNone(re.search(r"\brgba?\(", css))

    def test_js_writes_text_only(self):
        js = (APP / "static" / "hypervisor" / "live.js").read_text()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            self.assertNotIn(bad, js)

    def test_templates_never_mark_vcenter_text_safe(self):
        for p in (APP / "templates" / "hypervisor").glob("*.html"):
            text = p.read_text()
            self.assertNotRegex(text, r"\|\s*safe\b", p.name)
            self.assertNotIn("autoescape off", text, p.name)
            self.assertNotIn(" onclick=", text, p.name)

    def test_optional_dependency_is_wired_like_apm(self):
        req = (ROOT / "requirements-vsphere.txt").read_text()
        self.assertIn("pyvmomi", req)
        self.assertIn("-r requirements.txt", req)
        self.assertNotIn("pyvmomi", (ROOT / "requirements.txt").read_text())
        self.assertIn("vsphere", (ROOT / "Dockerfile").read_text())
        self.assertIn("requirements-vsphere.txt", (ROOT / ".github" / "workflows" / "ci.yml").read_text())
        self.assertTrue((ROOT / "docs" / "HYPERVISOR.md").exists())

    def test_pyvmomi_is_only_imported_lazily(self):
        for p in APP.glob("*.py"):
            for line in p.read_text().splitlines():
                if re.match(r"^(import|from)\s+(pyVmomi|pyVim)", line):
                    self.fail(f"{p.name} imports pyvmomi at module level")
