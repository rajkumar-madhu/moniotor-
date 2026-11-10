"""Regression tests for the verified review findings HW-1 .. HW-3 (one class per finding)."""
import http.client
import json
import math
import ssl
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from hardware import parsers, redfish, services
from hardware.models import BmcEndpoint, HwComponent
from hardware.tests import fixtures
from hardware.tests.base import BMC_PASS, BMC_USER, HwCase, loopback, with_key
from hardware.tests.fake_redfish import FakeRedfish
from onboarding.models import Credential
from onboarding.services import credentials_for


@with_key
class HW1VaultRowFollowsTheDevice(HwCase):
    """The BMC login's vault row moves with the server: the old site's admins lose it, the new site's admins get it."""

    def test_moving_the_server_moves_the_vault_row(self):
        self.store_login(self.ep_m)
        self.ep_m.refresh_from_db()
        cred = self.ep_m.credential_id
        self.assertEqual(Credential.objects.get(pk=cred).site, "Mumbai")
        self.srv_m.site = "Chennai"
        self.srv_m.save()
        self.assertEqual(Credential.objects.get(pk=cred).site, "Chennai")
        self.assertFalse(credentials_for(self.mum_admin).filter(pk=cred).exists())
        self.assertTrue(credentials_for(self.che_admin).filter(pk=cred).exists())

    def test_a_save_that_does_not_touch_the_site_changes_nothing(self):
        self.store_login(self.ep_m)
        self.ep_m.refresh_from_db()
        with self.assertNumQueries(1):
            self.srv_m.save(update_fields=["vendor"])
        self.assertEqual(Credential.objects.get(pk=self.ep_m.credential_id).site, "Mumbai")


    def test_the_finspot_site_migration_moves_the_vault_row_too(self):
        """administration.finspot_site moves devices with a bulk update, which the post_save handler never sees."""
        from administration import finspot_site
        self.store_login(self.ep_m)
        self.ep_m.refresh_from_db()
        cred = self.ep_m.credential_id
        slug = next(iter(finspot_site.LEGACY_DEVICE_SITE_FOR_SLUG))
        legacy = finspot_site.LEGACY_DEVICE_SITE_FOR_SLUG[slug]
        type(self.srv_m).objects.filter(pk=self.srv_m.pk).update(site=legacy)       # the server sits on the legacy site name
        Credential.objects.filter(pk=cred).update(site=legacy)
        _site, _created, renamed = finspot_site.ensure_finspot_site(self.srv_m.tenant_key, slug)
        self.assertGreaterEqual(renamed, 1)
        self.assertEqual(Credential.objects.get(pk=cred).site, finspot_site.site_display_name(slug))


@loopback
class HW2PinOnEveryConnection(SimpleTestCase):
    """A connection http.client re-opens by itself must be pinned too: a different certificate never receives the login."""

    def setUp(self):
        self.srv = FakeRedfish(fixtures.dell_idrac9()).start()
        self.addCleanup(self.srv.stop)

    def bmc_client(self):
        return redfish.make_client("127.0.0.1", self.srv.port, BMC_USER, BMC_PASS, "pin", self.srv.fingerprint)

    def other_certificate(self):
        """From now on every new TLS connection presents another certificate (an on-path attacker)."""
        return mock.patch.object(ssl.SSLSocket, "getpeercert", lambda self, binary_form=False: b"attacker-certificate")

    def test_an_aborted_read_does_not_leave_an_unpinned_reconnect(self):
        real_begin = http.client.HTTPResponse.begin
        calls = []

        def abort_once(resp):
            if not calls:
                calls.append(1)
                raise ConnectionAbortedError("aborted")
            return real_begin(resp)
        with self.bmc_client() as c:
            c.get("/redfish/v1/")
            with mock.patch.object(http.client.HTTPResponse, "begin", abort_once), self.assertRaises(redfish.Unreachable):
                c.get("/redfish/v1/Systems")
            with self.other_certificate(), self.assertRaises(redfish.CertificateError):
                c.get("/redfish/v1/Chassis")
        self.assertNotIn("/redfish/v1/Chassis", [p for _m, p, _a in self.srv.requests])

    def test_http_client_auto_reconnect_is_pinned(self):
        with self.bmc_client() as c:
            c.get("/redfish/v1/")
            c._conn.close()                 # what http.client does itself after a "Connection: close" answer that failed mid-body
            with self.other_certificate(), self.assertRaises(redfish.CertificateError):
                c.get("/redfish/v1/Systems")
        self.assertNotIn("/redfish/v1/Systems", [p for _m, p, _a in self.srv.requests])

    def test_the_right_certificate_still_reconnects(self):
        with self.bmc_client() as c:
            c.get("/redfish/v1/")
            c._conn.close()
            self.assertEqual(c.get("/redfish/v1/Systems")["@odata.id"], "/redfish/v1/Systems")


class HW3OddValues(HwCase):
    """One odd BMC value never makes every poll fail to store (silently, every minute)."""

    def test_a_tiny_capacity_gives_no_infinite_load(self):
        doc = {"PowerSupplies": [{"Status": {"State": "Enabled", "Health": "OK"}, "PowerOutputWatts": 500, "PowerCapacityWatts": "1e-310"}]}
        psus, _v, _b = parsers.parse_power(doc)
        load = psus[0].attrs["load_pct"]
        self.assertTrue(load is None or math.isfinite(load), load)
        json.dumps(psus[0].attrs, allow_nan=False)

    def test_a_long_intrusion_value_fits_the_health_column(self):
        comp = parsers.parse_intrusion({"PhysicalSecurity": {"IntrusionSensor": "HardwareIntrusionDetectedByVendorX"}})
        self.assertLessEqual(len(comp.health), HwComponent._meta.get_field("health").max_length)

    @with_key
    def test_the_poll_with_an_odd_value_is_stored(self):
        docs = fixtures.dell_idrac9()
        power = next(k for k in docs if k.endswith("/Power"))
        docs[power]["PowerSupplies"][0]["PowerCapacityWatts"] = "1e-310"
        with self.bmc(docs=docs) as (_srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None)
            self.assertEqual(services.poll_due(workers=1), "1 polled, 0 failed")
        ep.refresh_from_db()
        self.assertEqual(ep.conn_state, BmcEndpoint.CONN_OK)

    @with_key
    def test_a_poll_that_cannot_be_stored_is_recorded_on_the_endpoint(self):
        with self.bmc() as (_srv, ep), mock.patch.object(services, "_store_snapshot", side_effect=ValueError("boom")):
            BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None)
            self.assertEqual(services.poll_due(workers=1, now=timezone.now()), "0 polled, 1 failed")
        ep.refresh_from_db()
        self.assertNotEqual(ep.conn_state, BmcEndpoint.CONN_NEVER)
        self.assertIn("could not be stored", ep.last_error)
        self.assertEqual(ep.consecutive_failures, 1)                        # counts toward back-off and the unreachable alert
