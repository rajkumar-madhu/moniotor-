"""Polling: state kept, readings, events de-duplicated, transitions announced once, back-off, retention, due selection."""
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from core.models import Event
from hardware import redfish, services
from hardware.models import BmcEndpoint, ControlRequest, HwComponent, HwEvent, HwReading
from hardware.tests import fixtures
from hardware.tests.base import HwCase, snapshot_of, with_key

C = "/redfish/v1/Chassis/System.Embedded.1"


def degraded(psu="Critical", temp=None):
    d = fixtures.dell_idrac9()
    if psu:
        d[C + "/Power"]["PowerSupplies"][1]["Status"] = {"Health": psu, "State": "Enabled"}
    if temp:
        d[C + "/Thermal"]["Temperatures"][0]["Status"] = {"Health": temp, "State": "Enabled"}
    return d


def hw_events():
    return list(Event.objects.filter(source="hardware").order_by("id"))


@with_key
class Apply(HwCase):
    def test_a_successful_poll_stores_everything_and_the_state(self):
        r = self.applied()
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        self.assertEqual((ep.conn_state, ep.health, ep.vendor, ep.last_error, ep.consecutive_failures), ("connected", "healthy", "dell", "", 0))
        self.assertEqual(ep.overview["service_tag"], "7F3K9H2")
        self.assertEqual(ep.sections["thermal"], "ok")
        self.assertGreater(r["components"], 20)
        self.assertEqual(HwComponent.objects.filter(endpoint=ep, kind="fan").count(), 6)
        self.assertEqual(HwReading.objects.filter(component__endpoint=ep).count(), 4 + 6 + 2 + 3)
        self.assertEqual(HwEvent.objects.filter(endpoint=ep).count(), 4)

    def test_a_second_poll_updates_in_place_and_does_not_duplicate_events(self):
        self.applied()
        n = HwComponent.objects.count()
        r = self.applied()
        self.assertEqual(HwComponent.objects.count(), n)
        self.assertEqual(r["events"], 0)
        self.assertEqual(HwEvent.objects.filter(endpoint=self.ep_m).count(), 4)
        self.assertEqual(HwReading.objects.filter(component__endpoint=self.ep_m).count(), 2 * 15)

    def test_a_part_that_disappears_from_a_section_read_ok_is_removed(self):
        self.applied()
        d = fixtures.dell_idrac9()
        del d[C + "/Thermal"]["Fans"][5]
        self.applied(docs=d)
        self.assertEqual(HwComponent.objects.filter(endpoint=self.ep_m, kind="fan").count(), 5)

    def test_a_section_that_could_not_be_read_keeps_its_parts(self):
        self.applied()
        d = fixtures.dell_idrac9()
        del d[C + "/Thermal"]
        self.applied(docs=d)
        self.assertEqual(HwComponent.objects.filter(endpoint=self.ep_m, kind="fan").count(), 6)
        self.assertEqual(BmcEndpoint.objects.get(pk=self.ep_m.pk).sections["thermal"], "missing")

    def test_health_poll_without_inventory_keeps_the_inventory(self):
        self.applied()
        services.apply_snapshot(self.ep_m, snapshot_of(inventory=False), timezone.now(), inventory=False)
        self.assertEqual(HwComponent.objects.filter(endpoint=self.ep_m, kind="drive").count(), 2)
        self.assertEqual(HwComponent.objects.filter(endpoint=self.ep_m, kind="dimm").count(), 4)

    def test_overrides_only_make_things_more_severe(self):
        self.ep_m.temp_warn_c, self.ep_m.temp_crit_c, self.ep_m.fan_warn_rpm, self.ep_m.fan_crit_rpm = 30, 33, 8250, 8100
        self.ep_m.save()
        self.applied()
        by = {c.name: c.status for c in HwComponent.objects.filter(endpoint=self.ep_m)}
        self.assertEqual(by["System Board Inlet Temp"], "ok")          # 24 < 30
        self.assertEqual(by["CPU1 Temp"], "warning")                    # 32 >= 30
        self.assertEqual(by["CPU2 Temp"], "critical")                   # 34 >= 33
        self.assertEqual(by["System Board Fan1A"], "critical")          # 8100 <= 8100
        self.assertEqual(by["System Board Fan2A"], "warning")           # 8200 <= 8250
        self.assertEqual(by["System Board Fan4A"], "ok")
        self.assertEqual(HwComponent.objects.get(endpoint=self.ep_m, name="CPU1 Temp").warn_at, 30)

    def test_readings_older_than_the_range_are_not_charted(self):
        self.applied(now=self.ago(hours=30))
        self.applied()
        from hardware import snapshot
        data = snapshot.build(self.ep_m, "24h")
        self.assertEqual(len(data["charts"]["temp"]["series"][0]["values"]), len(data["charts"]["temp"]["labels"]))
        self.assertEqual(sum(v is not None for v in data["charts"]["temp"]["series"][0]["values"]), 1)


@with_key
class Transitions(HwCase):
    def test_first_healthy_poll_announces_nothing(self):
        self.applied()
        self.assertEqual(hw_events(), [])
        self.assertEqual(BmcEndpoint.objects.get(pk=self.ep_m.pk).alert_state, "ok")

    def test_critical_then_recovery_is_one_event_each_with_the_same_fingerprint(self):
        self.applied()
        r1 = self.applied(docs=degraded())
        self.assertEqual(r1["transition"], "critical")
        self.applied(docs=degraded())                                    # still critical: no new event
        self.applied(docs=degraded())
        r2 = self.applied(docs=fixtures.dell_idrac9())
        self.assertEqual(r2["transition"], "ok")
        evs = hw_events()
        self.assertEqual([e.severity for e in evs], ["critical", "ok"])
        self.assertEqual(evs[0].fingerprint, evs[1].fingerprint)
        self.assertEqual(evs[0].fingerprint, f"hardware:{self.ep_m.pk}")
        self.assertEqual(evs[0].site, "Mumbai")
        self.assertEqual((evs[0].object_type, evs[0].object_id), ("network.device", str(self.srv_m.pk)))
        self.assertIn("PS2 Status is critical", evs[0].message)

    def test_warning_to_critical_to_warning(self):
        self.applied()
        self.applied(docs=degraded(psu=None, temp="Warning"))
        self.applied(docs=degraded(psu="Critical", temp="Warning"))
        self.applied(docs=degraded(psu=None, temp="Warning"))
        self.assertEqual([e.severity for e in hw_events()], ["warning", "critical", "warning"])

    def test_first_poll_that_is_already_bad_is_announced(self):
        self.applied(docs=degraded())
        self.assertEqual([e.severity for e in hw_events()], ["critical"])

    def test_alerts_switched_off_and_example_endpoints_emit_nothing(self):
        self.ep_m.alerts_enabled = False
        self.ep_m.save()
        self.applied(docs=degraded())
        self.ep_c.is_example = True
        self.ep_c.save()
        self.applied(ep=self.ep_c, docs=degraded())
        self.assertEqual(hw_events(), [])

    def test_unreachable_is_announced_after_three_failures_once_and_recovers(self):
        self.applied()
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        for _ in range(2):
            services.apply_failure(ep, redfish.Unreachable("Connection refused by the BMC."))
        self.assertEqual(hw_events(), [])
        for _ in range(3):
            services.apply_failure(ep, redfish.Unreachable("Connection refused by the BMC."))
        self.assertEqual([(e.severity, e.title) for e in hw_events()], [("warning", "srv-mum iDRAC is not reachable")])
        self.assertEqual(ep.conn_state, "unreachable")
        self.applied(ep=ep)
        self.assertEqual([e.severity for e in hw_events()], ["warning", "ok"])

    def test_backoff_grows_and_is_capped(self):
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        now = timezone.now()
        gaps = []
        for _ in range(8):
            services.apply_failure(ep, redfish.Unreachable("x"), now)
            gaps.append(int((ep.next_poll_at - now).total_seconds()))
        self.assertEqual(gaps[:3], [60, 120, 240])
        self.assertEqual(max(gaps), 900)

    def test_auth_failures_stop_polling_after_three_so_the_account_is_not_locked(self):
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        for _ in range(3):
            services.apply_failure(ep, redfish.AuthFailed("The BMC rejected the login (HTTP 401)."))
        self.assertTrue(ep.auth_paused)
        self.assertEqual(ep.conn_state, "auth_failed")
        self.assertEqual([e.severity for e in hw_events()], ["warning"])          # announced once, at the second failure
        ep.next_poll_at = None
        ep.credential_id = 1
        ep.save()
        self.assertNotIn(ep.pk, [e.pk for e in services.due_endpoints()])

    def test_a_missing_stored_login_does_not_count_as_a_rejected_login(self):
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        for _ in range(5):
            services.apply_failure(ep, redfish.LoginUnavailable("No login is stored for this BMC."))
        self.assertFalse(ep.auth_paused)

    def test_events_do_not_storm_while_flapping_inside_one_state(self):
        for _ in range(6):
            self.applied(docs=degraded())
        self.assertEqual(len(hw_events()), 1)


@with_key
class Jobs(HwCase):
    def test_due_endpoints_selection(self):
        now = timezone.now()
        self.store_login(self.ep_m)
        self.store_login(self.ep_c)
        BmcEndpoint.objects.filter(pk=self.ep_c.pk).update(next_poll_at=now + timedelta(minutes=5))
        self.assertEqual([e.pk for e in services.due_endpoints(now)], [self.ep_m.pk])
        BmcEndpoint.objects.filter(pk=self.ep_m.pk).update(enabled=False)
        self.assertEqual(services.due_endpoints(now), [])
        BmcEndpoint.objects.filter(pk=self.ep_m.pk).update(enabled=True, is_example=True)
        self.assertEqual(services.due_endpoints(now), [])
        BmcEndpoint.objects.filter(pk=self.ep_m.pk).update(is_example=False, credential_id=None)
        self.assertEqual(services.due_endpoints(now), [])

    def test_claim_is_a_compare_and_set(self):
        self.store_login(self.ep_m)
        ep = services.due_endpoints()[0]
        now = timezone.now()
        self.assertTrue(services.claim(ep, now))
        self.assertFalse(services.claim(ep, now))                       # a second worker with the same view loses

    def test_poll_due_against_the_fake_bmc_inline_and_threaded(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                with self.bmc() as (srv, ep):
                    BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None, inventory_at=None)
                    out = services.poll_due(workers=workers)
                    self.assertEqual(out, "1 polled, 0 failed")
                    ep.refresh_from_db()
                    self.assertEqual((ep.conn_state, ep.health), ("connected", "healthy"))
                    self.assertGreater(ep.next_poll_at, timezone.now())
                    self.assertEqual(srv.methods, {"GET"})
                    n = len(srv.requests)
                    BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None)
                    services.poll_due(workers=workers)
                    self.assertLess(len(srv.requests) - n, n)              # the inventory is not re-read on every poll

    def test_poll_due_with_a_wrong_password_and_with_an_unreachable_bmc(self):
        with self.bmc(password="other-password") as (srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None)
            self.assertEqual(services.poll_due(workers=1), "0 polled, 1 failed")
            ep.refresh_from_db()
            self.assertEqual((ep.conn_state, ep.auth_failures), ("auth_failed", 1))
        ep.refresh_from_db()
        BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None, auth_failures=0)
        services.poll_due(workers=1)                                    # the server is gone now
        ep.refresh_from_db()
        self.assertEqual(ep.conn_state, "unreachable")

    def test_no_pin_means_no_poll(self):
        with self.bmc(pin=False) as (srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None)
            services.poll_due(workers=1)
            self.assertEqual(srv.requests, [])
            ep.refresh_from_db()
            self.assertIn("pinned", ep.last_error)

    def test_a_crashing_parser_does_not_stop_other_endpoints(self):
        with self.bmc() as (srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(next_poll_at=None)
            with mock.patch("hardware.parsers.parse", side_effect=ValueError("boom")):
                self.assertEqual(services.poll_due(workers=1), "0 polled, 1 failed")
            ep.refresh_from_db()
            self.assertEqual(ep.conn_state, "unreachable")
            self.assertNotIn("boom", ep.last_error)

    def test_jobs_never_raise(self):
        from hardware import jobs
        with mock.patch("hardware.services.poll_due", side_effect=RuntimeError("x")):
            self.assertEqual(jobs.poll(), "error")
        with mock.patch("hardware.services.retention", side_effect=RuntimeError("x")):
            self.assertEqual(jobs.retention(), "error")

    def test_jobs_are_registered(self):
        from core.jobs import JOBS
        self.assertEqual((JOBS["hardware.poll"]["interval"], JOBS["hardware.retention"]["interval"]), (30, 3600))

    def test_refresh_is_rate_limited_and_queues_the_next_poll(self):
        self.store_login(self.ep_m)
        BmcEndpoint.objects.filter(pk=self.ep_m.pk).update(next_poll_at=timezone.now() + timedelta(minutes=5))
        ep = BmcEndpoint.objects.get(pk=self.ep_m.pk)
        ok, _ = services.request_refresh(ep)
        self.assertTrue(ok)
        self.assertEqual([e.pk for e in services.due_endpoints()], [self.ep_m.pk])
        ok, msg = services.request_refresh(ep)
        self.assertFalse(ok)
        self.assertIn("Try again", msg)

    def test_refresh_refuses_example_disabled_unconfigured_and_paused(self):
        for field, value in (("is_example", True), ("enabled", False), ("credential_id", None), ("auth_failures", 3)):
            ep = self.make_endpoint(self.make_device(f"x-{field}", "Mumbai"), credential_id=5)
            setattr(ep, field, value)
            ep.save()
            self.assertFalse(services.request_refresh(ep)[0], field)


@with_key
class Retention(HwCase):
    def setUp(self):
        super().setUp()
        self.applied()
        self.comp = HwComponent.objects.get(endpoint=self.ep_m, name="CPU1 Temp")
        HwReading.objects.all().delete()

    def add(self, **ago):
        return HwReading.objects.create(component=self.comp, ts=self.ago(**ago), value=30.0)

    def test_old_readings_events_are_deleted_and_recent_ones_kept(self):
        self.add(days=8)
        keep = self.add(hours=2)
        HwEvent.objects.create(endpoint=self.ep_m, entry_id="old", ts=self.ago(days=91), message="old")
        HwEvent.objects.create(endpoint=self.ep_m, entry_id="new", ts=self.ago(days=1), message="new")
        out = services.retention()
        self.assertEqual(out["readings"], 1)
        self.assertEqual(list(HwReading.objects.values_list("pk", flat=True)), [keep.pk])
        self.assertFalse(HwEvent.objects.filter(entry_id="old").exists())
        self.assertEqual(list(HwEvent.objects.filter(endpoint=self.ep_m).values_list("entry_id", flat=True)), ["new"])   # the 2025 fixtures aged out too

    def test_readings_between_one_and_seven_days_are_thinned_to_one_per_ten_minutes(self):
        base = self.ago(days=3).replace(second=0, microsecond=0)
        base -= timedelta(minutes=base.minute % 10)
        for i in range(30):
            HwReading.objects.create(component=self.comp, ts=base + timedelta(minutes=i), value=float(i))
        recent = [self.add(minutes=m) for m in (5, 6, 7)]
        services.retention()
        self.assertEqual(HwReading.objects.filter(ts__lt=self.ago(hours=24)).count(), 3)        # 30 minutes = 3 windows
        self.assertEqual(HwReading.objects.filter(ts__gte=self.ago(hours=24)).count(), 3)       # last day untouched
        n = HwReading.objects.count()
        services.retention()
        self.assertEqual(HwReading.objects.count(), n)                                          # idempotent
        del recent

    def test_pending_requests_expire(self):
        r = services.create_request(self.ep_m, self.mum_op, "power_on", "start after maintenance")
        ControlRequest.objects.filter(pk=r.pk).update(expires_at=self.ago(minutes=1))
        self.assertEqual(services.retention()["requests_expired"], 1)
        self.assertEqual(ControlRequest.objects.get(pk=r.pk).state, "expired")


@with_key
class RequestsLifecycle(HwCase):
    def test_create_approve_reject_expire(self):
        r = services.create_request(self.ep_m, self.mum_op, "graceful_shutdown", "  patch   window  ")
        self.assertEqual((r.state, r.reason, r.requested_by_name), ("pending", "patch window", "mum-op"))
        with self.assertRaises(ValueError):
            services.create_request(self.ep_m, self.mum_op, "graceful_shutdown", "same again please")
        done = services.decide_request(r, self.mum_admin, True, "go")
        self.assertEqual((done.state, done.decided_by_name, done.decision_note), ("approved", "mum-admin", "go"))
        with self.assertRaises(ValueError):
            services.decide_request(r, self.mum_admin, False)
        r2 = services.create_request(self.ep_m, self.mum_op, "force_off", "it is stuck for an hour")
        self.assertEqual(services.decide_request(r2, self.mum_admin, False).state, "rejected")
        r3 = services.create_request(self.ep_m, self.mum_op, "update_firmware", "new bmc release 7.00")
        ControlRequest.objects.filter(pk=r3.pk).update(expires_at=self.ago(hours=1))
        with self.assertRaises(ValueError):
            services.decide_request(r3, self.mum_admin, True)
        self.assertEqual(ControlRequest.objects.get(pk=r3.pk).state, "expired")

    def test_validation(self):
        for action, reason in (("format_disk", "valid reason here"), ("power_on", "no"), ("power_on", "")):
            with self.assertRaises(ValueError):
                services.create_request(self.ep_m, self.mum_op, action, reason)
