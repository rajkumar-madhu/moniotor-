"""Secrets never leave the vault, hostile BMC data stays inert, the seed is idempotent and silent, migrations reverse."""
import io
import json
import logging

from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.urls import reverse

from core.models import AuditEvent, Event
from hardware import services
from hardware.models import BmcEndpoint, HwComponent, HwEvent, HwReading
from hardware.tests import fixtures
from hardware.tests.base import BMC_PASS, HwCase, with_key
from network.models import Device

SECRET = "Sup3r-S3cret-BMC-pw"


@with_key
class SecretsNeverLeak(HwCase):
    def capture(self):
        records = []

        class Grab(logging.Handler):
            def emit(self, record):
                records.append(self.format(record) + " " + str(record.args) + " " + repr(record.exc_info))

        h = Grab(level=0)
        root = logging.getLogger()
        root.addHandler(h)
        self._old = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(h), root.setLevel(self._old)))
        return records

    def test_a_failed_login_through_every_path_leaks_nothing(self):
        records = self.capture()
        services.store_login(None, self.ep_m, "monitor", SECRET)
        with self.bmc(password="a-different-password") as (srv, ep):
            services.store_login(None, ep, "monitor", SECRET)
            from django.utils import timezone
            for _ in range(2):
                BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None, auth_failures=0)
                services.poll_due(workers=1)
            c = self.login(self.mum_admin)
            pages = [c.post(reverse("hardware:endpoint_test", args=[ep.pk])), c.get(reverse("hardware:endpoint_edit", args=[ep.pk])),
                     c.get(reverse("hardware:live", args=[self.srv_m.pk])), c.get(reverse("hardware:endpoints")),
                     c.get("/network/devices/%d/?tab=hardware" % self.srv_m.pk), c.get(reverse("hardware:events", args=[self.srv_m.pk]))]
            ep.refresh_from_db()
            blobs = [p.content.decode() for p in pages]
            blobs += [ep.last_error, json.dumps(ep.overview), "\n".join(records)]
            blobs += [json.dumps(a.details) + a.object_repr for a in AuditEvent.objects.all()]
            blobs += [e.title + e.message + json.dumps(e.data) for e in Event.objects.all()]
            blobs += [json.dumps(list(HwEvent.objects.values_list("message", flat=True)))]
            self.assertEqual(ep.conn_state, "auth_failed")
        for blob in blobs:
            self.assertNotIn(SECRET, blob)
            self.assertNotIn("a-different-password", blob)

    def test_credential_secret_is_ciphertext_and_decrypt_failure_is_a_fixed_message(self):
        services.store_login(None, self.ep_m, "monitor", SECRET)
        row = services.credential_row(self.ep_m)
        self.assertNotIn(SECRET, row.secret)
        with override_settings(LINKEDEYE_CREDENTIAL_KEY="x" * 10):
            with self.assertRaises(ValueError) as cm:
                services.login_of(self.ep_m)
            r = services.poll_endpoint(self.ep_m)
        self.assertNotIn(row.secret, str(cm.exception))
        self.assertEqual(r["state"], "auth_failed")
        self.ep_m.refresh_from_db()
        self.assertFalse(self.ep_m.auth_paused)                      # a vault problem is not a rejected login

    def test_polling_does_not_write_an_audit_row_every_minute(self):
        with self.bmc() as (srv, ep):
            before = AuditEvent.objects.filter(action="credential.use").count()
            for _ in range(3):
                services.poll_endpoint(ep)
            self.assertEqual(AuditEvent.objects.filter(action="credential.use").count(), before)

    def test_the_exports_and_json_have_no_credential_fields(self):
        services.store_login(None, self.ep_m, "monitor", SECRET)
        self.applied(self.ep_m)
        c = self.login(self.root)
        j = c.get(reverse("hardware:live", args=[self.srv_m.pk])).json()
        text = json.dumps(j)
        for word in ("credential", "password", "secret", "pinned"):
            self.assertNotIn(word, text.lower())
        self.assertNotIn(SECRET, c.get(reverse("hardware:export", args=[self.srv_m.pk])).content.decode())


@with_key
class Seed(HwCase):
    def run_seed(self, *args):
        out = io.StringIO()
        call_command("seed_hardware", *args, stdout=out)
        return out.getvalue()

    def test_seed_is_idempotent_marked_example_silent_and_never_polled(self):
        self.run_seed()
        self.run_seed()
        dev = Device.objects.get(name="example-srv-01")
        self.assertTrue(dev.is_example)
        ep = dev.bmc
        self.assertTrue(ep.is_example)
        self.assertEqual(BmcEndpoint.objects.filter(device=dev).count(), 1)
        n = HwComponent.objects.filter(endpoint=ep).count()
        self.assertGreater(n, 20)
        self.run_seed()
        self.assertEqual(HwComponent.objects.filter(endpoint=ep).count(), n)
        self.assertGreater(HwReading.objects.filter(component__endpoint=ep).count(), 500)
        self.assertEqual(Event.objects.filter(source="hardware").count(), 0)
        self.assertEqual(ep.credential_id, None)
        self.assertNotIn(ep.pk, [e.pk for e in services.due_endpoints()])
        j = self.login(self.root).get(reverse("hardware:live", args=[dev.pk])).json()
        self.assertEqual((j["endpoint"]["state"], j["endpoint"]["example"], j["health"]["rollup"]), ("example", True, "healthy"))
        self.assertTrue(all(e["ts"] for e in j["events"]))

    def test_seed_never_touches_a_real_device_with_the_same_name_and_remove_cleans_up(self):
        real = Device.objects.create(tenant_key=services.tenant(), name="example-srv-01", device_type="physical_server", mgmt_ip="198.51.100.9")
        self.assertIn("real device", self.run_seed())
        self.assertFalse(BmcEndpoint.objects.filter(device=real).exists())
        real.delete()
        self.run_seed()
        self.run_seed("--vendor", "hpe")
        ep = Device.objects.get(name="example-srv-01").bmc
        self.assertEqual(ep.vendor, "hpe")
        self.assertFalse(HwEvent.objects.filter(endpoint=ep, entry_id__startswith="sel:").exists())     # the Dell run left nothing behind
        self.assertFalse(HwComponent.objects.filter(endpoint=ep, name__startswith="System Board").exists())
        self.assertFalse(HwReading.objects.filter(component__endpoint=ep, value__lt=0).exists())
        self.run_seed("--remove")
        self.assertFalse(Device.objects.filter(name="example-srv-01").exists())
        self.assertEqual(BmcEndpoint.objects.filter(is_example=True).count(), 0)

    def test_the_example_refresh_button_explains_there_is_nothing_to_poll(self):
        self.run_seed()
        dev = Device.objects.get(name="example-srv-01")
        r = self.login(self.root).post(reverse("hardware:refresh", args=[dev.pk]))
        self.assertEqual(r.status_code, 409)
        self.assertIn("example", r.json()["message"])


class MigrationsReverse(TransactionTestCase):
    def test_hardware_0001_applies_and_unapplies(self):
        executor = MigrationExecutor(connection)
        try:
            executor.migrate([("hardware", None)])
            self.assertNotIn("hardware_bmcendpoint", connection.introspection.table_names())
        finally:
            executor = MigrationExecutor(connection)
            executor.migrate(executor.loader.graph.leaf_nodes())
        self.assertIn("hardware_bmcendpoint", connection.introspection.table_names())
