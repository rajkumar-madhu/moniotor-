"""Pages per role, site scoping and IDOR, the matching actions, the control-request workflow, the VM tabs, secrets."""
import socket
from unittest import mock

from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from administration import services as admin_services
from core.models import AuditEvent, Event
from hypervisor import services, vsphere
from hypervisor.models import Datastore, VCenterSource, VmControlRequest, VmInfo
from hypervisor.tests.base import FakeProvider, HvCase, capture_logs, provider_factory, snap
from hypervisor.tests.fake_vcenter import PASSWORD as VC_PASSWORD, FakeVCenter
from network.models import Device
from onboarding.models import Credential


@override_settings(PROMETHEUS_URL="")
class ViewCase(HvCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)
        self.src = self.make_source(self.fake)                              # Mumbai
        self.other = self.make_source(self.fake, name="vc-chennai", site="Chennai")
        self.dev = self.make_vm_device("app-01", site="Mumbai", ip="10.20.1.15")
        prov = FakeProvider({"vm-101": {"snapshots": [snap("s1", "pre-upgrade", 10, size=12 * 1024 ** 3), snap("s2", "weekly-backup", 1)]}})
        self.src.use_provider = True
        self.src.save()
        self.run_sync(self.src, provider_factory=provider_factory(prov))
        self.run_sync(self.other)
        self.vm = VmInfo.objects.get(source=self.src, moref="vm-101")
        self.vm_other = VmInfo.objects.get(source=self.other, moref="vm-101")

    def url(self, name, *a):
        return reverse(f"hypervisor:{name}", args=a)


class SourcesAdmin(ViewCase):
    def test_roles(self):
        for user, code in ((self.mum_view, 403), (self.mum_op, 403), (self.glob_view, 403), (self.mum_admin, 200), (self.root, 200)):
            self.assertEqual(self.login(user).get(self.url("sources")).status_code, code, user)

    def test_admin_sees_only_their_sites_sources(self):
        r = self.login(self.mum_admin).get(self.url("sources"))
        self.assertContains(r, "vc-mumbai")
        self.assertNotContains(r, "vc-chennai")

    def test_idor_on_every_source_action(self):
        c = self.login(self.mum_admin)
        for name in ("source_edit",):
            self.assertEqual(c.get(self.url(name, self.other.pk)).status_code, 404)
        for name in ("source_rotate", "source_toggle", "source_delete", "source_test", "source_pin", "source_sync"):
            self.assertEqual(c.post(self.url(name, self.other.pk), {"username": "x", "password": "y"}).status_code, 404, name)
        self.assertTrue(VCenterSource.objects.filter(pk=self.other.pk, enabled=True).exists())

    def test_create_stores_the_login_in_the_vault_and_never_echoes_it(self):
        c = self.login(self.mum_admin)
        r = c.post(self.url("source_new"), {"name": "vc-new", "site": "Mumbai", "host": "vc.example.test", "port": 443, "username": "ro-user",
                                            "password": "S3cret-Value-1", "tls_mode": "verify", "sync_interval_s": 300, "enabled": "on"}, follow=True)
        self.assertEqual(r.status_code, 200)
        src = VCenterSource.objects.get(name="vc-new")
        row = Credential.objects.get(pk=src.credential_id)
        self.assertEqual((row.username, row.site), ("ro-user", "Mumbai"))
        self.assertNotIn("S3cret-Value-1", row.secret)
        self.assertNotContains(r, "S3cret-Value-1")
        edit = c.get(self.url("source_edit", src.pk))
        self.assertNotContains(edit, "S3cret-Value-1")
        self.assertNotContains(edit, row.secret)
        self.assertContains(edit, "ro-user")
        audit = " ".join(str(a.details) for a in AuditEvent.objects.filter(action__startswith="hypervisor.source")) + " ".join(
            str(a.details) for a in AuditEvent.objects.filter(action__startswith="credential"))
        self.assertNotIn("S3cret-Value-1", audit)
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.source.create").exists())

    def test_create_rejects_a_site_the_user_does_not_administer_and_bad_hosts(self):
        c = self.login(self.mum_admin)
        base = {"name": "bad", "port": 443, "username": "u", "password": "p", "tls_mode": "verify", "sync_interval_s": 300}
        for extra in ({"site": "Chennai", "host": "ok.example.test"}, {"site": "Mumbai", "host": "https://vc/path"}, {"site": "Mumbai", "host": "a b"},
                      {"site": "Mumbai", "host": "vc.example.test", "sync_interval_s": 5}, {"site": "Mumbai", "host": "vc.example.test", "port": 70000}):
            r = c.post(self.url("source_new"), {**base, **extra})
            self.assertEqual(r.status_code, 200, extra)
        self.assertFalse(VCenterSource.objects.filter(name="bad").exists())

    def test_without_a_vault_key_nothing_is_stored(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=""):
            r = self.login(self.mum_admin).post(self.url("source_new"), {
                "name": "vc-nokey", "site": "Mumbai", "host": "vc.example.test", "port": 443, "username": "u", "password": "p",
                "tls_mode": "verify", "sync_interval_s": 300})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(VCenterSource.objects.filter(name="vc-nokey").exists())

    def test_rotate_replaces_the_password(self):
        old = Credential.objects.get(pk=self.src.credential_id).secret
        r = self.login(self.mum_admin).post(self.url("source_rotate", self.src.pk), {"username": "other-user", "password": "Brand-New-Pass-2"}, follow=True)
        self.assertNotContains(r, "Brand-New-Pass-2")
        row = Credential.objects.get(pk=self.src.credential_id)
        self.assertNotEqual(row.secret, old)
        self.assertEqual(row.username, "other-user")
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.source.rotate_password").exists())

    def test_edit_changing_the_host_clears_the_pin(self):
        VCenterSource.objects.filter(pk=self.src.pk).update(pinned_sha256="ab" * 32, tls_mode="pin")
        c = self.login(self.mum_admin)
        c.post(self.url("source_edit", self.src.pk), {"name": "vc-mumbai", "site": "Mumbai", "host": "other.example.test", "port": 443, "username": "svc-linkedeye-ro",
                                                       "tls_mode": "pin", "sync_interval_s": 300, "enabled": "on"})
        self.src.refresh_from_db()
        self.assertEqual((self.src.host, self.src.pinned_sha256), ("other.example.test", ""))

    def test_toggle_and_delete(self):
        c = self.login(self.mum_admin)
        c.post(self.url("source_toggle", self.src.pk))
        self.src.refresh_from_db()
        self.assertFalse(self.src.enabled)
        cred = self.src.credential_id
        c.post(self.url("source_delete", self.src.pk))
        self.assertFalse(VCenterSource.objects.filter(pk=self.src.pk).exists())
        self.assertFalse(Credential.objects.filter(pk=cred).exists())
        self.assertFalse(VmInfo.objects.filter(source_id=self.src.pk).exists())
        self.assertTrue(Device.objects.filter(pk=self.dev.pk).exists())     # the device stays

    def test_get_is_not_allowed_on_mutations(self):
        c = self.login(self.mum_admin)
        for name in ("source_toggle", "source_delete", "source_test", "source_pin", "source_sync", "source_rotate"):
            self.assertEqual(c.get(self.url(name, self.src.pk)).status_code, 405, name)

    def test_connection_test_ok_and_audited(self):
        r = self.login(self.mum_admin).post(self.url("source_test", self.src.pk), follow=True)
        self.assertContains(r, "Connected to vCenter 8.0.2.00000")
        self.assertContains(r, "5 VMs")
        a = AuditEvent.objects.filter(action="hypervisor.source.test").first()
        self.assertEqual((a.result, a.details["outcome"]), ("ok", "ok"))
        self.assertTrue(AuditEvent.objects.filter(action="credential.use").exists())

    def test_connection_test_failure_message_has_no_secret(self):
        self.fake.fail_login = True
        with capture_logs() as buf:
            r = self.login(self.mum_admin).post(self.url("source_test", self.src.pk), follow=True)
        self.assertContains(r, "rejected the account")
        self.assertNotContains(r, VC_PASSWORD)
        self.assertNotIn(VC_PASSWORD, buf.getvalue())

    def test_pin_flow_sends_no_login_until_confirmed(self):
        self.src.tls_mode = "pin"
        self.src.save()
        self.fake.requests.clear()
        c = self.login(self.mum_admin)
        r = c.post(self.url("source_test", self.src.pk), follow=True)
        self.assertContains(r, "No login was attempted")
        self.assertEqual(self.fake.requests, [])
        self.src.refresh_from_db()
        self.assertEqual(self.src.pending_sha256, self.fake.fingerprint)
        # a wrong fingerprint is refused, the right one pins
        c.post(self.url("source_pin", self.src.pk), {"fingerprint": "AA:BB"})
        self.src.refresh_from_db()
        self.assertEqual(self.src.pinned_sha256, "")
        c.post(self.url("source_pin", self.src.pk), {"fingerprint": vsphere.format_fingerprint(self.fake.fingerprint)})
        self.src.refresh_from_db()
        self.assertEqual(self.src.pinned_sha256, self.fake.fingerprint)
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.source.pin").exists())
        r = c.post(self.url("source_test", self.src.pk), follow=True)
        self.assertContains(r, "Connected to vCenter")

    def test_pin_mismatch_is_reported_and_sends_nothing(self):
        VCenterSource.objects.filter(pk=self.src.pk).update(tls_mode="pin", pinned_sha256="cd" * 32)
        self.fake.requests.clear()
        r = self.login(self.mum_admin).post(self.url("source_test", self.src.pk), follow=True)
        self.assertContains(r, "does not match the pinned")
        self.assertEqual(self.fake.requests, [])

    def test_sync_now_is_rate_limited(self):
        c = self.login(self.mum_admin)
        c.post(self.url("source_sync", self.src.pk))
        self.src.refresh_from_db()
        first = self.src.sync_requested_at
        self.assertIsNotNone(first)
        r = c.post(self.url("source_sync", self.src.pk), follow=True)
        self.assertContains(r, "Wait 30 seconds")
        self.src.refresh_from_db()
        self.assertEqual(self.src.sync_requested_at, first)
        from hypervisor import sync
        self.assertTrue(sync.due(self.src, timezone.now()))

    def test_unsafe_tls_mode_is_labelled(self):
        r = self.login(self.mum_admin).get(self.url("source_new"))
        self.assertContains(r, "Unsafe.")
        self.assertContains(r, "Do not verify")


class Datastores(ViewCase):
    def test_scoped_by_site(self):
        Datastore.objects.filter(source=self.other).update(name="CHENNAI-ONLY")
        r = self.login(self.mum_admin).get(self.url("datastores"))
        self.assertContains(r, "DS01")
        self.assertNotContains(r, "CHENNAI-ONLY")
        self.assertContains(self.login(self.che_admin).get(self.url("datastores")), "CHENNAI-ONLY")
        self.assertContains(self.login(self.glob_view).get(self.url("datastores")), "CHENNAI-ONLY")

    def test_live_json_and_csv_are_scoped_too(self):
        Datastore.objects.filter(source=self.other).update(name="CHENNAI-ONLY")
        c = self.login(self.mum_view)
        data = c.get(self.url("datastores_live")).json()
        names = {r["name"] for r in data["rows"]}
        self.assertIn("DS01", names)
        self.assertNotIn("CHENNAI-ONLY", names)
        self.assertEqual([s["name"] for s in data["sources"]], ["vc-mumbai"])
        csv = c.get(self.url("datastores_export")).content.decode()
        self.assertIn("DS01", csv)
        self.assertNotIn("CHENNAI-ONLY", csv)

    def test_status_tones(self):
        ds = Datastore.objects.get(source=self.src, name="DS01")
        self.assertEqual(ds.status[0], "ok")
        ds.free_bytes = int(ds.capacity_bytes * 0.08)
        self.assertEqual(ds.status[0], "warn")
        ds.free_bytes = int(ds.capacity_bytes * 0.03)
        self.assertEqual(ds.status[0], "crit")
        ds.accessible = False
        self.assertEqual(ds.status[1], "Inaccessible")
        ds.accessible, ds.capacity_bytes = True, None
        self.assertEqual(ds.status[1], "Not reported")

    def test_anonymous_is_sent_to_login(self):
        from django.test import Client
        self.assertEqual(Client().get(self.url("datastores")).status_code, 302)


class Unmatched(ViewCase):
    def test_page_lists_what_needs_a_decision(self):
        r = self.login(self.mum_op).get(self.url("unmatched"))
        self.assertContains(r, "orphan-01")
        self.assertNotContains(r, ">app-01<")
        self.assertContains(r, "Create device")

    def test_viewer_sees_the_page_but_no_buttons(self):
        r = self.login(self.mum_view).get(self.url("unmatched"))
        self.assertContains(r, "orphan-01")
        self.assertNotContains(r, "Create device")
        self.assertContains(r, "operator role needed")

    def test_other_sites_vms_are_not_listed_or_actionable(self):
        orphan_other = VmInfo.objects.get(source=self.other, moref="vm-104")
        self.assertNotContains(self.login(self.mum_op).get(self.url("unmatched") + "?q=orphan"), f"/vm/{orphan_other.pk}/")
        for name in ("match_create", "match_link", "match_ignore", "match_unignore", "match_unlink"):
            self.assertEqual(self.login(self.mum_admin).post(self.url(name, orphan_other.pk), {"device": self.dev.pk}).status_code, 404, name)
        orphan_other.refresh_from_db()
        self.assertEqual(orphan_other.match_state, "unmatched")

    def test_create_device_links_it_and_is_audited(self):
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        r = self.login(self.mum_op).post(self.url("match_create", vm.pk), {"next": "/hypervisor/unmatched/"}, follow=True)
        self.assertContains(r, "created and linked")
        vm.refresh_from_db()
        dev = vm.device
        self.assertEqual((dev.name, dev.device_type, dev.site, dev.mgmt_ip, dev.check_port, dev.vendor), ("orphan-01", "virtual_machine", "Mumbai", "10.99.9.9", 3389, "VMware"))
        self.assertEqual((vm.match_state, vm.matched_by), ("matched", "mum-op"))
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.match.create_device").exists())

    def test_create_device_refuses_a_name_clash_instead_of_merging(self):
        self.make_vm_device("orphan-01", ip="10.1.1.1")
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        r = self.login(self.mum_op).post(self.url("match_create", vm.pk), follow=True)
        self.assertContains(r, "already exists")
        vm.refresh_from_db()
        self.assertIsNone(vm.device_id)
        self.assertEqual(Device.objects.filter(name="orphan-01").count(), 1)

    def test_link_to_a_chosen_device(self):
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        target = self.make_vm_device("my-orphan", ip="10.5.5.5")
        self.login(self.mum_op).post(self.url("match_link", vm.pk), {"device": target.pk})
        vm.refresh_from_db()
        self.assertEqual(vm.device_id, target.pk)

    def test_link_refuses_a_device_that_is_already_used_and_other_sites_devices(self):
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        self.login(self.mum_op).post(self.url("match_link", vm.pk), {"device": self.dev.pk})        # used by vm-101
        vm.refresh_from_db()
        self.assertIsNone(vm.device_id)
        foreign = self.make_vm_device("chennai-vm", site="Chennai", ip="10.6.6.6")
        self.login(self.mum_op).post(self.url("match_link", vm.pk), {"device": foreign.pk})
        vm.refresh_from_db()
        self.assertIsNone(vm.device_id)
        self.login(self.mum_op).post(self.url("match_link", vm.pk), {"device": "x"})
        self.assertIsNone(VmInfo.objects.get(pk=vm.pk).device_id)

    def test_ignore_unignore_unlink(self):
        c = self.login(self.mum_op)
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        c.post(self.url("match_ignore", vm.pk))
        self.assertEqual(VmInfo.objects.get(pk=vm.pk).match_state, "ignored")
        self.assertNotContains(c.get(self.url("unmatched")), f"/hypervisor/vm/{vm.pk}/ignore/")
        self.assertContains(c.get(self.url("unmatched") + "?state=ignored"), f"/hypervisor/vm/{vm.pk}/unignore/")
        c.post(self.url("match_unignore", vm.pk))
        self.assertEqual(VmInfo.objects.get(pk=vm.pk).match_state, "unmatched")
        c.post(self.url("match_unlink", self.vm.pk))
        self.vm.refresh_from_db()
        self.assertEqual((self.vm.device_id, self.vm.match_state), (None, "unmatched"))

    def test_viewers_cannot_decide(self):
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        for name in ("match_create", "match_ignore"):
            r = self.login(self.mum_view).post(self.url(name, vm.pk))
            self.assertIn(r.status_code, (302, 403))
        self.assertEqual(VmInfo.objects.get(pk=vm.pk).match_state, "unmatched")

    def test_next_is_never_an_open_redirect(self):
        vm = VmInfo.objects.get(source=self.src, moref="vm-104")
        r = self.login(self.mum_op).post(self.url("match_ignore", vm.pk), {"next": "https://evil.example/"})
        self.assertEqual(r["Location"], self.url("unmatched"))


class Requests(ViewCase):
    def post_request(self, user, action="power_off", **extra):
        return self.login(user).post(self.url("vm_request", self.dev.pk), {"action": action, "reason": "patching", **extra}, follow=True)

    def test_operator_records_a_request_and_nothing_is_sent_to_vcenter(self):
        self.fake.requests.clear()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            r = self.post_request(self.mum_op)
        self.assertContains(r, "nothing was sent to vCenter")
        req = VmControlRequest.objects.get()
        self.assertEqual((req.action, req.state, req.requested_by_name, req.reason, req.vm_name), ("power_off", "pending", "mum-op", "patching", "app-01"))
        self.assertEqual(self.fake.requests, [])
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.request.create").exists())

    def test_a_duplicate_pending_request_is_not_created_twice(self):
        self.post_request(self.mum_op)
        r = self.post_request(self.mum_op)
        self.assertContains(r, "already a pending")
        self.assertEqual(VmControlRequest.objects.count(), 1)

    def test_viewers_cannot_request_and_other_sites_cannot_see_the_vm(self):
        self.post_request(self.mum_view)
        self.assertEqual(VmControlRequest.objects.count(), 0)
        r = self.login(self.che_admin).post(self.url("vm_request", self.dev.pk), {"action": "reset"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(VmControlRequest.objects.count(), 0)

    def test_validation(self):
        self.post_request(self.mum_op, action="format_disk")
        self.post_request(self.mum_op, action="snapshot")                 # needs a name
        self.assertEqual(VmControlRequest.objects.count(), 0)
        self.post_request(self.mum_op, action="snapshot", detail="before-patch")
        self.assertEqual(VmControlRequest.objects.get().detail, "before-patch")

    def test_every_listed_action_is_a_request_with_a_manual_step(self):
        for code, _label in VmControlRequest.ACTIONS:
            self.assertIn(code, services.MANUAL_STEPS)
        self.assertEqual(set(services.MANUAL_STEPS), {c for c, _l in VmControlRequest.ACTIONS})

    def test_admin_approves_and_gets_the_manual_step(self):
        self.post_request(self.mum_op)
        req = VmControlRequest.objects.get()
        self.fake.requests.clear()
        with mock.patch.object(socket, "create_connection", side_effect=AssertionError("network used")):
            r = self.login(self.mum_admin).post(self.url("request_decide", req.pk), {"decision": "approve", "note": "ok"}, follow=True)
        self.assertContains(r, "LinkedEye sent nothing to vCenter")
        req.refresh_from_db()
        self.assertEqual((req.state, req.decided_by_name, req.decision_note), ("approved", "mum-admin", "ok"))
        self.assertIn("Shut Down Guest OS", req.manual_step)
        self.assertEqual(self.fake.requests, [])
        self.assertTrue(AuditEvent.objects.filter(action="hypervisor.request.approve").exists())
        self.assertContains(self.login(self.mum_admin).get(self.url("requests") + "?state=approved"), "Manual step")

    def test_reject_and_a_decision_is_final(self):
        self.post_request(self.mum_op)
        req = VmControlRequest.objects.get()
        c = self.login(self.mum_admin)
        c.post(self.url("request_decide", req.pk), {"decision": "reject"})
        c.post(self.url("request_decide", req.pk), {"decision": "approve"})
        req.refresh_from_db()
        self.assertEqual((req.state, req.manual_step), ("rejected", ""))

    def test_operators_and_other_sites_cannot_decide(self):
        self.post_request(self.mum_op)
        req = VmControlRequest.objects.get()
        self.login(self.mum_op).post(self.url("request_decide", req.pk), {"decision": "approve"})
        self.assertEqual(self.login(self.che_admin).post(self.url("request_decide", req.pk), {"decision": "approve"}).status_code, 404)
        req.refresh_from_db()
        self.assertEqual(req.state, "pending")

    def test_list_is_scoped_and_csv_is_injection_safe(self):
        self.post_request(self.mum_op, reason="=HYPERLINK(\"http://evil\")")
        self.assertContains(self.login(self.mum_view).get(self.url("requests")), "patching" if False else "app-01")
        self.assertNotContains(self.login(self.che_admin).get(self.url("requests")), "app-01")
        csv = self.login(self.mum_view).get(self.url("requests_export")).content.decode()
        self.assertIn("'=HYPERLINK", csv)
        self.assertEqual(self.login(self.che_admin).get(self.url("requests_export")).content.decode().count("\n"), 1)

    def test_the_example_source_records_no_requests(self):
        VCenterSource.objects.filter(pk=self.src.pk).update(description="[example] demo")
        self.post_request(self.mum_op)
        self.assertEqual(VmControlRequest.objects.count(), 0)


class DevicePage(ViewCase):
    def page(self, tab="overview", user=None):
        return (self.login(user or self.mum_view)).get(reverse("network:detail", args=[self.dev.pk]) + f"?tab={tab}")

    def test_overview_shows_vcenter_data(self):
        r = self.page()
        self.assertContains(r, "VM information")
        self.assertContains(r, "esxi-host-01.example.test")
        self.assertContains(r, "Compute-01")
        self.assertContains(r, "Running (12352)")
        self.assertContains(r, "Datastore details")
        self.assertContains(r, "VMFS")
        self.assertContains(r, "61.8%")
        self.assertContains(r, "pre-upgrade")
        self.assertContains(r, "10 d: review")
        self.assertContains(r, "Open in vCenter")
        self.assertContains(r, "https://127.0.0.1:%d/ui/app/vm;nav=v/urn:vmomi:VirtualMachine:vm-101/summary" % self.fake.port)
        self.assertContains(r, "read-only in this release")

    def test_actions_are_disabled_for_viewers_and_enabled_for_operators(self):
        self.assertContains(self.page(), 'data-act="power_off" disabled')
        self.assertNotContains(self.page(user=self.mum_op), 'data-act="power_off" disabled')
        self.assertContains(self.page(user=self.mum_op), 'id="hv-req"')

    def test_other_tabs_are_filled(self):
        self.assertContains(self.page("storage"), "Virtual disks")
        self.assertContains(self.page("storage"), "100 GB")
        self.assertContains(self.page("snapshots"), "weekly-backup")
        self.assertContains(self.page("guest"), "Ubuntu Linux (64-bit)")
        self.assertContains(self.page("guest"), "app-01.example.test")
        net = self.page("interfaces")
        self.assertContains(net, "Virtual network adapters")
        self.assertContains(net, "VM Network")
        self.assertContains(self.page("events"), "Hypervisor events")

    def test_unreported_values_say_so(self):
        VmInfo.objects.filter(pk=self.vm.pk).update(cluster_name="", created_on=None, tools_run_state="", snapshots_synced_at=None)
        VmInfo.objects.get(pk=self.vm.pk).snapshots.all().delete()
        r = self.page()
        self.assertContains(r, "Not reported")
        self.assertContains(r, "Snapshots are not reported")

    def test_provider_missing_is_explained(self):
        VCenterSource.objects.filter(pk=self.src.pk).update(use_provider=False)
        VmInfo.objects.filter(pk=self.vm.pk).update(snapshots_synced_at=None)
        VmInfo.objects.get(pk=self.vm.pk).snapshots.all().delete()
        self.assertContains(self.page("snapshots"), "pyvmomi")

    def test_untrusted_values_are_escaped(self):
        VmInfo.objects.filter(pk=self.vm.pk).update(description="<script>alert(1)</script>", guest_hostname="<img src=x onerror=alert(1)>")
        for tab in ("overview", "guest"):
            html = self.page(tab).content.decode()
            self.assertNotIn("<script>alert(1)</script>", html)
            self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", self.page().content.decode())

    def test_no_secret_anywhere_on_the_page(self):
        row = Credential.objects.get(pk=self.src.credential_id)
        for tab in ("overview", "storage", "snapshots", "guest", "interfaces", "events"):
            html = self.page(tab, self.root).content.decode()
            self.assertNotIn(VC_PASSWORD, html)
            self.assertNotIn(row.secret, html)

    def test_a_vm_with_no_vcenter_record_keeps_the_old_page(self):
        plain = self.make_vm_device("plain-vm", ip="10.3.3.3")
        r = self.login(self.mum_view).get(reverse("network:detail", args=[plain.pk]) + "?tab=storage")
        self.assertContains(r, "Not connected")
        self.assertNotContains(r, "Virtual disks")
        r = self.login(self.mum_view).get(reverse("network:detail", args=[plain.pk]))
        self.assertContains(r, "Hypervisor control is read-only in release 1")

    def test_out_of_scope_users_get_no_vcenter_data(self):
        VmInfo.objects.filter(pk=self.vm.pk).update()
        # the vCenter belongs to Mumbai: a Chennai-only user cannot even open the device
        self.assertEqual(self.login(self.che_admin).get(reverse("network:detail", args=[self.dev.pk])).status_code, 404)
        # a user who sees the device but not the vCenter's site gets the plain page
        Device.objects.filter(pk=self.dev.pk).update(site="Chennai")
        r = self.login(self.che_admin).get(reverse("network:detail", args=[self.dev.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertNotContains(r, "VM information")

    def test_live_json_shape_and_scope(self):
        data = self.login(self.mum_view).get(self.url("vm_live", self.dev.pk)).json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["vm"]["power"], "Powered on")
        self.assertEqual(data["vm"]["host"], "esxi-host-01.example.test")
        self.assertEqual(data["datastore"]["used_pct"], 61.8)
        self.assertEqual(data["status"]["code"], "connected")
        self.assertIn("rev", data)
        self.assertEqual(self.login(self.che_admin).get(self.url("vm_live", self.dev.pk)).status_code, 404)
        plain = self.make_vm_device("plain-vm", ip="10.3.3.3")
        self.assertEqual(self.login(self.mum_view).get(self.url("vm_live", plain.pk)).status_code, 404)

    def test_stale_source_shows_the_banner(self):
        VCenterSource.objects.filter(pk=self.src.pk).update(last_ok_at=self.ago(hours=3), consecutive_failures=2, error_kind="network",
                                                            last_error="the connection timed out")
        r = self.page()
        self.assertContains(r, "vCenter data is stale")
        self.assertContains(r, "the connection timed out")
        data = self.login(self.mum_view).get(self.url("vm_live", self.dev.pk)).json()
        self.assertTrue(data["status"]["stale"])

    def test_header_overlay_from_vcenter(self):
        r = self.page()
        self.assertContains(r, "Cluster: Compute-01")
        self.assertContains(r, "Host: esxi-host-01.example.test")


class Wiring(ViewCase):
    def test_site_rename_follows_through_the_source(self):
        self.assertIn(("hypervisor", "VCenterSource", "site", "vCenter sources"), admin_services.SITE_NAME_FIELDS)
        refs = dict(admin_services.site_references("Mumbai"))
        self.assertEqual(refs.get("vCenter sources"), 1)
        admin_services.rename_site_references("Mumbai", "Mumbai-2")
        self.src.refresh_from_db()
        self.assertEqual(self.src.site, "Mumbai-2")

    def test_children_carry_no_site_field_of_their_own(self):
        from django.apps import apps
        for model in apps.get_app_config("hypervisor").get_models():
            names = {f.name for f in model._meta.get_fields()}
            if model is VCenterSource:
                self.assertIn("site", names)
            else:
                self.assertNotIn("site", names, model)
                self.assertIn("tenant_key", names, model)

    def test_nav_entries_resolve(self):
        from core.nav import NAV
        group = next(g for g in NAV if g["label"] == "Hypervisor")
        for child in group["children"]:
            reverse(child["url"])

    def test_every_page_is_login_protected(self):
        from django.test import Client
        c = Client()
        for name in ("sources", "datastores", "unmatched", "requests", "datastores_live", "requests_export"):
            self.assertEqual(c.get(self.url(name)).status_code, 302, name)
        self.assertEqual(c.get(self.url("vm_live", self.dev.pk)).status_code, 302)
