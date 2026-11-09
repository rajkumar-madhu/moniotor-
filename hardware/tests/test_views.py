"""Pages and JSON per role, site scoping and IDOR, the endpoint admin, test connection and certificate pinning, the CSV, the
control-request workflow. Everything a BMC can send is hostile text: it must come out escaped."""
import json
import re

from django.test import override_settings
from django.urls import reverse

from administration.services import SITE_NAME_FIELDS
from core.models import AuditEvent
from hardware import services
from hardware.models import BmcEndpoint, ControlRequest, HwComponent, HwEvent
from hardware.tests import fixtures
from hardware.tests.base import BMC_PASS, BMC_USER, KEY, HwCase, loopback, with_key
from onboarding.models import Credential

TAB = "/network/devices/{}/?tab=hardware"


def denied(r):
    """The platform answers a refused POST with a redirect and a message, a refused GET with 403: both mean 'not done'."""
    return r.status_code in (302, 403) and "/accounts/login" not in r.get("Location", "")


@with_key
class TabAndJson(HwCase):
    def setUp(self):
        super().setUp()
        self.store_login(self.ep_m)
        self.applied(self.ep_m)

    def test_the_tab_is_on_the_physical_server_page_with_the_new_label(self):
        r = self.login(self.mum_view).get(TAB.format(self.srv_m.pk))
        self.assertContains(r, "Hardware (iDRAC/iLO)")
        self.assertContains(r, 'id="hw-data"')
        self.assertContains(r, 'rel="noopener noreferrer"')                      # the console link
        self.assertContains(r, 'target="_blank"')
        data = json.loads(re.search(r'<script id="hw-data" type="application/json">(.*?)</script>', r.content.decode(), re.S).group(1))
        self.assertEqual(data["health"]["rollup"], "healthy")
        self.assertEqual(data["endpoint"]["console_url"], "https://192.0.2.50/")        # built from the configured host only

    def test_a_server_without_an_endpoint_offers_to_add_one_to_admins_only(self):
        d = self.make_device("srv-none", "Mumbai")
        admin = self.login(self.mum_admin).get(TAB.format(d.pk))
        self.assertContains(admin, "Add BMC endpoint")
        viewer = self.login(self.mum_view).get(TAB.format(d.pk))
        self.assertNotContains(viewer, "Add BMC endpoint")
        self.assertContains(viewer, "Ask an administrator")

    def test_other_device_types_keep_their_old_hardware_tab(self):
        sw = self.make_device("sw-1", "Mumbai", device_type="switch")
        r = self.login(self.mum_view).get(TAB.format(sw.pk))
        self.assertNotContains(r, 'id="hw"')
        self.assertContains(r, "SNMPv2-MIB")
        self.assertNotContains(r, "Hardware (iDRAC/iLO)")

    def test_live_json_per_role_and_scope(self):
        url = reverse("hardware:live", args=[self.srv_m.pk])
        r = self.login(self.mum_view).get(url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Cache-Control"], "no-store")
        j = r.json()
        self.assertEqual((j["perms"], j["endpoint"]["state"]), ({"operate": False, "admin": False}, "connected"))
        self.assertEqual(self.login(self.mum_admin).get(url).json()["perms"], {"operate": True, "admin": True})
        self.assertEqual(self.login(self.che_admin).get(url).status_code, 404)             # IDOR: another site's server
        self.assertEqual(self.login(self.root).get(url).status_code, 200)
        self.assertEqual(self.client.get(url).status_code, 302)                           # anonymous
        d = self.make_device("srv-none", "Mumbai")
        self.assertEqual(self.login(self.root).get(reverse("hardware:live", args=[d.pk])).status_code, 404)
        vm = self.make_device("vm-1", "Mumbai", device_type="virtual_machine")
        self.assertEqual(self.login(self.root).get(reverse("hardware:live", args=[vm.pk])).status_code, 404)

    def test_live_range_and_charts(self):
        for key in ("1h", "6h", "24h", "bogus"):
            j = self.login(self.mum_view).get(reverse("hardware:live", args=[self.srv_m.pk]) + f"?range={key}").json()
            self.assertEqual(j["range"], key if key != "bogus" else "1h")
            self.assertTrue(j["charts"]["temp"]["series"])
        self.assertTrue(all(isinstance(s["values"], list) for s in j["charts"]["fan"]["series"]))

    def test_tiles_say_not_reported_never_ok_when_a_section_is_missing(self):
        d = fixtures.dell_idrac9()
        del d["/redfish/v1/Chassis/System.Embedded.1/Power"]
        self.applied(self.ep_c, d)
        j = self.login(self.che_admin).get(reverse("hardware:live", args=[self.srv_c.pk])).json()
        tiles = {t["key"]: t for t in j["health"]["tiles"]}
        self.assertEqual((tiles["psu"]["value"], tiles["psu"]["tone"], tiles["psu"]["reported"]), ("Not reported", "mute", False))
        self.assertEqual(tiles["fan"]["value"], "6 / 6 OK")
        self.assertEqual(tiles["battery"]["value"], "Not reported")

    def test_no_data_yet_is_unknown_not_healthy(self):
        j = self.login(self.che_admin).get(reverse("hardware:live", args=[self.srv_c.pk])).json()
        self.assertEqual((j["health"]["rollup"], j["endpoint"]["state"]), ("unknown", "not_configured"))

    def test_hostile_text_is_escaped_on_every_page(self):
        HwEvent.objects.create(endpoint=self.ep_m, entry_id="x", ts=self.ago(minutes=1), sensor="<img src=x onerror=alert(1)>",
                               message="<script>alert('x')</script>", severity="critical")
        HwComponent.objects.filter(endpoint=self.ep_m, kind="dimm").update(name="<b>evil</b>", attrs={"part": "<script>1</script>"})
        c = self.login(self.mum_view)
        for name in ("events", "inventory"):
            html = c.get(reverse(f"hardware:{name}", args=[self.srv_m.pk])).content.decode()
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("<img src=x", html)
            self.assertNotIn("<b>evil</b>", html)
        tab = c.get(TAB.format(self.srv_m.pk)).content.decode()
        self.assertNotIn("<script>alert('x')", tab)                       # json_script escapes < > &


@with_key
class Pages(HwCase):
    def setUp(self):
        super().setUp()
        self.applied(self.ep_m)
        self.applied(self.ep_c)

    def test_csv_report_is_scoped_and_defused(self):
        HwEvent.objects.create(endpoint=self.ep_m, entry_id="f", ts=self.ago(minutes=1), sensor="=cmd|' /C calc'!A0", message="+SUM(1)", severity="info")
        r = self.login(self.mum_view).get(reverse("hardware:export", args=[self.srv_m.pk]))
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        body = r.content.decode()
        self.assertIn("section,name,reading,unit,warn_at,critical_at,status,time,message", body)
        self.assertIn("temperature,System Board Inlet Temp,24.0,°C,42.0,47.0,ok", body)
        self.assertIn("'=cmd", body)
        self.assertIn("'+SUM(1)", body)
        self.assertNotIn(",=cmd", body)
        self.assertEqual(self.login(self.mum_view).get(reverse("hardware:export", args=[self.srv_c.pk])).status_code, 404)
        self.assertTrue(AuditEvent.objects.filter(action="hardware.export").exists())

    def test_events_page_filters_and_paginates(self):
        for i in range(30):
            HwEvent.objects.create(endpoint=self.ep_m, entry_id=f"e{i}", ts=self.ago(minutes=i), severity="critical" if i % 2 else "info",
                                   message=f"message {i}", sensor="Fan")
        c = self.login(self.mum_view)
        url = reverse("hardware:events", args=[self.srv_m.pk])
        self.assertContains(c.get(url + "?per=10"), "Showing 1 to 10")
        r = c.get(url + "?severity=critical&per=100")
        self.assertContains(r, "message 1<")
        self.assertNotContains(r, "message 2<")
        self.assertContains(c.get(url + "?q=message+29"), "message 29")
        self.assertEqual(self.login(self.che_admin).get(url).status_code, 404)

    def test_inventory_page(self):
        r = self.login(self.mum_view).get(reverse("hardware:inventory", args=[self.srv_m.pk]))
        for text in ("Processors", "Memory modules", "Drives", "ST1200MM0099", "HMA84GR7CJR4N-WM", "Power supplies", "EXDISK0"):
            self.assertContains(r, text)
        self.assertEqual(self.login(self.che_admin).get(reverse("hardware:inventory", args=[self.srv_m.pk])).status_code, 404)

    def test_endpoints_list_is_scoped(self):
        r = self.login(self.mum_view).get(reverse("hardware:endpoints"))
        self.assertContains(r, "srv-mum")
        self.assertNotContains(r, "srv-che")
        self.assertNotContains(r, "Add endpoint")
        r = self.login(self.root).get(reverse("hardware:endpoints"))
        self.assertContains(r, "srv-che")
        self.assertContains(r, "Add endpoint")
        self.assertContains(self.login(self.root).get(reverse("hardware:endpoints") + "?health=critical"), "No BMC endpoints")
        self.assertContains(self.login(self.root).get(reverse("hardware:endpoints") + "?q=che"), "srv-che")

    def test_nav_resolves(self):
        from core.nav import NAV
        urls = [c["url"] for g in NAV for c in g.get("children", []) if c["url"].startswith("hardware:")]
        self.assertEqual(sorted(urls), ["hardware:endpoints", "hardware:requests"])
        for u in urls:
            self.assertEqual(self.login(self.mum_view).get(reverse(u)).status_code, 200)

    def test_pages_for_every_role_render(self):
        for user in (self.mum_view, self.mum_op, self.mum_admin, self.root):
            c = self.login(user)
            for name in ("endpoints", "requests"):
                self.assertEqual(c.get(reverse(f"hardware:{name}")).status_code, 200, (user, name))
            self.assertEqual(c.get(TAB.format(self.srv_m.pk)).status_code, 200)


@with_key
class RefreshAndVendor(HwCase):
    def setUp(self):
        super().setUp()
        self.store_login(self.ep_m)

    def test_refresh_needs_operator_and_is_rate_limited(self):
        url = reverse("hardware:refresh", args=[self.srv_m.pk])
        self.assertTrue(denied(self.login(self.mum_view).post(url)))
        self.assertEqual(self.login(self.che_admin).post(url).status_code, 404)
        c = self.login(self.mum_op)
        r = c.post(url)
        self.assertEqual((r.status_code, r.json()["ok"]), (200, True))
        r = c.post(url)
        self.assertEqual((r.status_code, r.json()["ok"]), (429, False))
        self.assertTrue(AuditEvent.objects.filter(action="hardware.refresh").exists())

    def test_vendor_override_is_admin_only_and_validated(self):
        url = reverse("hardware:vendor", args=[self.srv_m.pk])
        self.assertTrue(denied(self.login(self.mum_op).post(url, {"vendor": "hpe"})))
        self.ep_m.refresh_from_db()
        self.assertEqual(self.ep_m.vendor_override, "")
        c = self.login(self.mum_admin)
        self.assertEqual(c.post(url, {"vendor": "nonsense"}).status_code, 400)
        self.assertEqual(c.post(url, {"vendor": "hpe"}, HTTP_ACCEPT="application/json").status_code, 200)
        self.ep_m.refresh_from_db()
        self.assertEqual((self.ep_m.vendor_override, self.ep_m.effective_vendor, self.ep_m.bmc_name), ("hpe", "hpe", "iLO"))
        c.post(url, {"vendor": ""})
        self.ep_m.refresh_from_db()
        self.assertEqual(self.ep_m.vendor_override, "")
        self.assertEqual(self.login(self.che_admin).post(url, {"vendor": "dell"}).status_code, 404)


FORM = {"host": "bmc-new.example.com", "port": "443", "verify_mode": "pin", "poll_interval_s": "60", "enabled": "on",
        "username": BMC_USER, "password": BMC_PASS, "vendor_override": ""}


@with_key
class EndpointAdmin(HwCase):
    def setUp(self):
        super().setUp()
        self.new_dev = self.make_device("srv-new", "Mumbai", ip="198.51.100.77")

    def post_new(self, user, **kw):
        return self.login(user).post(reverse("hardware:endpoint_new"), {**FORM, "device": str(self.new_dev.pk), **kw})

    def test_only_site_admins_can_create_and_the_login_goes_to_the_vault(self):
        self.assertTrue(denied(self.login(self.mum_op).get(reverse("hardware:endpoint_new"))))
        self.assertTrue(denied(self.post_new(self.mum_op)))
        self.assertEqual(self.post_new(self.che_admin).status_code, 200)                      # not an admin on Mumbai: form error
        self.assertFalse(BmcEndpoint.objects.filter(device=self.new_dev).exists())
        r = self.post_new(self.mum_admin)
        self.assertEqual(r.status_code, 302)
        ep = BmcEndpoint.objects.get(device=self.new_dev)
        row = Credential.objects.get(pk=ep.credential_id)
        self.assertEqual((row.kind, row.username, row.site), ("basic_auth", BMC_USER, "Mumbai"))
        self.assertNotIn(BMC_PASS, row.secret)
        self.assertEqual(services.login_of(ep), (BMC_USER, BMC_PASS))
        self.assertTrue(AuditEvent.objects.filter(action="hardware.endpoint.create").exists())
        self.assertEqual(ep.pinned_sha256, "")

    def test_the_password_is_never_rendered_audited_or_returned(self):
        self.post_new(self.mum_admin)
        ep = BmcEndpoint.objects.get(device=self.new_dev)
        c = self.login(self.mum_admin)
        page = c.get(reverse("hardware:endpoint_edit", args=[ep.pk])).content.decode()
        self.assertNotIn(BMC_PASS, page)
        self.assertIn("A password is stored", page)
        r = c.post(reverse("hardware:endpoint_edit", args=[ep.pk]), {**FORM, "device": str(self.new_dev.pk), "password": "", "port": "99999"})
        self.assertNotIn(BMC_PASS, r.content.decode())
        for ev in AuditEvent.objects.all():
            self.assertNotIn(BMC_PASS, json.dumps(ev.details))
            self.assertNotIn(BMC_PASS, ev.object_repr)
        j = c.get(reverse("hardware:live", args=[self.new_dev.pk])).content.decode()
        self.assertNotIn(BMC_PASS, j)

    def test_validation(self):
        for field, value in (("host", "https://bmc.example.com/"), ("host", "bmc example"), ("port", "0"), ("poll_interval_s", "10"),
                             ("username", ""), ("password", "")):
            r = self.post_new(self.mum_admin, **{field: value})
            self.assertEqual(r.status_code, 200, (field, value))
            self.assertFalse(BmcEndpoint.objects.filter(device=self.new_dev).exists(), (field, value))

    def test_unsafe_choices_need_an_explicit_confirmation(self):
        self.assertEqual(self.post_new(self.mum_admin, verify_mode="skip").status_code, 200)
        self.assertEqual(self.post_new(self.mum_admin, verify_mode="pin", insecure_http="on", port="80").status_code, 200)
        self.assertFalse(BmcEndpoint.objects.filter(device=self.new_dev).exists())
        self.assertEqual(self.post_new(self.mum_admin, verify_mode="skip", confirm_unsafe="on").status_code, 302)

    def test_only_physical_servers_without_an_endpoint_can_be_chosen(self):
        vm = self.make_device("vm-9", "Mumbai", device_type="virtual_machine")
        self.assertEqual(self.login(self.mum_admin).post(reverse("hardware:endpoint_new"), {**FORM, "device": str(vm.pk)}).status_code, 200)
        self.assertEqual(self.login(self.mum_admin).post(reverse("hardware:endpoint_new"), {**FORM, "device": str(self.srv_m.pk)}).status_code, 200)
        self.assertEqual(BmcEndpoint.objects.filter(device=vm).count(), 0)

    def test_edit_rotate_password_and_changing_the_address_drops_the_pin(self):
        self.post_new(self.mum_admin)
        ep = BmcEndpoint.objects.get(device=self.new_dev)
        BmcEndpoint.objects.filter(pk=ep.pk).update(pinned_sha256="c" * 64, auth_failures=3)
        c = self.login(self.mum_admin)
        url = reverse("hardware:endpoint_edit", args=[ep.pk])
        self.assertEqual(self.login(self.che_admin).get(url).status_code, 404)
        self.assertTrue(denied(self.login(self.mum_op).get(url)))
        r = c.post(url, {**FORM, "password": "new-secret-pass", "username": "ro2"})
        self.assertEqual(r.status_code, 302)
        ep.refresh_from_db()
        self.assertEqual((ep.pinned_sha256, ep.auth_failures), ("c" * 64, 0))                # same address: pin kept, paused polling resumes
        self.assertEqual(services.login_of(ep), ("ro2", "new-secret-pass"))
        c.post(url, {**FORM, "host": "other.example.com", "password": "", "username": "ro2"})
        ep.refresh_from_db()
        self.assertEqual((ep.host, ep.pinned_sha256), ("bmc-new.example.com", "c" * 64))     # refused: see test_review_security
        c.post(url, {**FORM, "host": "other.example.com", "password": "newer-secret-pass", "username": "ro2"})
        ep.refresh_from_db()
        self.assertEqual((ep.host, ep.pinned_sha256), ("other.example.com", ""))
        self.assertEqual(services.login_of(ep), ("ro2", "newer-secret-pass"))
        self.assertTrue(AuditEvent.objects.filter(action="hardware.endpoint.credential").exists())

    def test_toggle_and_delete_remove_the_vault_row(self):
        self.post_new(self.mum_admin)
        ep = BmcEndpoint.objects.get(device=self.new_dev)
        cred = ep.credential_id
        c = self.login(self.mum_admin)
        self.assertEqual(self.login(self.che_admin).post(reverse("hardware:endpoint_toggle", args=[ep.pk])).status_code, 404)
        c.post(reverse("hardware:endpoint_toggle", args=[ep.pk]))
        ep.refresh_from_db()
        self.assertFalse(ep.enabled)
        self.assertTrue(denied(self.login(self.mum_op).post(reverse("hardware:endpoint_delete", args=[ep.pk]))))
        self.assertTrue(BmcEndpoint.objects.filter(pk=ep.pk).exists())
        c.post(reverse("hardware:endpoint_delete", args=[ep.pk]))
        self.assertFalse(BmcEndpoint.objects.filter(pk=ep.pk).exists())
        self.assertFalse(Credential.objects.filter(pk=cred).exists())
        self.assertTrue(type(self.new_dev).objects.filter(pk=self.new_dev.pk).exists())       # the server stays
        self.assertTrue(AuditEvent.objects.filter(action="hardware.endpoint.delete").exists())

    def test_the_device_site_is_the_only_site(self):
        for model in (BmcEndpoint, HwComponent, HwEvent, ControlRequest):
            names = [f.name for f in model._meta.get_fields()]
            self.assertNotIn("site", names, model)
        self.assertEqual([e for e in SITE_NAME_FIELDS if e[0] == "hardware"], [])                # nothing to rename: it follows the device
        self.post_new(self.mum_admin)
        ep = BmcEndpoint.objects.get(device=self.new_dev)
        self.new_dev.site = "Chennai"
        self.new_dev.save()
        self.assertEqual(self.login(self.mum_admin).get(reverse("hardware:endpoint_edit", args=[ep.pk])).status_code, 404)
        self.assertEqual(self.login(self.che_admin).get(reverse("hardware:endpoint_edit", args=[ep.pk])).status_code, 200)
        services.sync_credential_site(ep)
        self.assertEqual(Credential.objects.get(pk=ep.credential_id).site, "Chennai")

    def test_a_site_rename_follows_through_the_device(self):
        from administration.services import rename_site_references
        rename_site_references("Mumbai", "Mumbai DC")
        self.srv_m.refresh_from_db()
        self.assertEqual(self.srv_m.site, "Mumbai DC")
        self.assertEqual(services.endpoints_for(self.root).get(pk=self.ep_m.pk).device.site, "Mumbai DC")

    def test_the_vault_not_configured_is_a_form_error_not_a_crash(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=""):
            r = self.post_new(self.mum_admin)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "credential vault")
        self.assertFalse(BmcEndpoint.objects.filter(device=self.new_dev).exists())


@with_key
class TestConnectionAndPinning(HwCase):
    def test_tofu_pinning_flow_never_sends_the_login_to_an_unpinned_server(self):
        with self.bmc(pin=False) as (srv, ep):
            c = self.login(self.mum_admin)
            r = c.post(reverse("hardware:endpoint_test", args=[ep.pk]))
            self.assertContains(r, srv.fingerprint)
            self.assertContains(r, "Pin this certificate")
            self.assertEqual(srv.requests, [])                                  # handshake only: no HTTP, so no login on the wire
            # a viewer cannot pin; a wrong fingerprint is refused; the right one is accepted
            self.assertTrue(denied(self.login(self.mum_op).post(reverse("hardware:endpoint_pin", args=[ep.pk]), {"fingerprint": srv.fingerprint})))
            c.post(reverse("hardware:endpoint_pin", args=[ep.pk]), {"fingerprint": "d" * 64})
            ep.refresh_from_db()
            self.assertEqual(ep.pinned_sha256, "")
            c.post(reverse("hardware:endpoint_pin", args=[ep.pk]), {"fingerprint": srv.fingerprint.upper()})
            ep.refresh_from_db()
            self.assertEqual(ep.pinned_sha256, srv.fingerprint)
            r = c.post(reverse("hardware:endpoint_test", args=[ep.pk]))
            self.assertContains(r, "Test result: <span class=\"t-ok\">passed</span>")
            self.assertContains(r, "dell")
            self.assertEqual(srv.methods, {"GET"})
            self.assertTrue(AuditEvent.objects.filter(action="hardware.endpoint.pin").exists())
            self.assertTrue(AuditEvent.objects.filter(action="hardware.endpoint.test").exists())

    def test_a_changed_certificate_is_a_mismatch_with_a_confirm_step_and_no_login_sent(self):
        with self.bmc() as (srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(pinned_sha256="e" * 64)
            r = self.login(self.mum_admin).post(reverse("hardware:endpoint_test", args=[ep.pk]))
            self.assertContains(r, "does not match the pinned fingerprint")
            self.assertContains(r, srv.fingerprint)
            self.assertEqual(srv.requests, [])

    def test_wrong_login_is_reported_and_the_password_is_not_echoed(self):
        with self.bmc(password="the-real-one") as (srv, ep):
            r = self.login(self.mum_admin).post(reverse("hardware:endpoint_test", args=[ep.pk]))
            self.assertContains(r, "rejected the login")
            self.assertNotContains(r, BMC_PASS)

    def test_verify_mode_with_a_self_signed_certificate_stops_before_the_login(self):
        with self.bmc() as (srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(verify_mode="verify")
            r = self.login(self.mum_admin).post(reverse("hardware:endpoint_test", args=[ep.pk]))
            self.assertContains(r, "not trusted")
            self.assertEqual(srv.requests, [])

    def test_unreachable_host_and_policy_refusal(self):
        r = self.login(self.mum_admin).post(reverse("hardware:endpoint_test", args=[self.ep_m.pk]))      # 192.0.2.50: documentation range
        self.assertContains(r, "not passed")
        self.assertContains(r, "LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS")

    def test_test_and_pin_are_post_only_and_admin_only(self):
        for name in ("endpoint_test", "endpoint_pin"):
            url = reverse(f"hardware:{name}", args=[self.ep_m.pk])
            self.assertEqual(self.login(self.mum_admin).get(url).status_code, 405)
            self.assertTrue(denied(self.login(self.mum_view).post(url)))
            self.assertEqual(self.login(self.che_admin).post(url).status_code, 404)

    def test_a_successful_test_resumes_paused_polling(self):
        with self.bmc() as (srv, ep):
            BmcEndpoint.objects.filter(pk=ep.pk).update(auth_failures=3)
            self.login(self.mum_admin).post(reverse("hardware:endpoint_test", args=[ep.pk]))
            ep.refresh_from_db()
            self.assertEqual(ep.auth_failures, 0)


@with_key
class ControlRequestPages(HwCase):
    def req(self, user, action="power_on", reason="start it after the maintenance window", device=None):
        return self.login(user).post(reverse("hardware:request_new", args=[(device or self.srv_m).pk]), {"action": action, "reason": reason})

    def test_operator_requests_viewer_cannot_and_other_sites_get_404(self):
        self.assertTrue(denied(self.req(self.mum_view)))
        self.assertEqual(ControlRequest.objects.count(), 0)
        self.assertEqual(self.req(self.che_admin).status_code, 404)
        self.assertEqual(self.req(self.mum_op).status_code, 302)
        r = ControlRequest.objects.get()
        self.assertEqual((r.state, r.action, r.requested_by_name, r.endpoint_id), ("pending", "power_on", "mum-op", self.ep_m.pk))
        self.assertTrue(AuditEvent.objects.filter(action="hardware.request.create", details__reason="start it after the maintenance window").exists())

    def test_bad_input_is_refused_with_a_message_not_an_error(self):
        self.req(self.mum_op, action="reboot_everything")
        self.req(self.mum_op, reason="x")
        self.assertEqual(ControlRequest.objects.count(), 0)

    def test_decide_is_admin_on_the_device_site_and_records_only(self):
        self.req(self.mum_op, action="force_off", reason="stuck since the firmware update")
        r = ControlRequest.objects.get()
        url = reverse("hardware:request_decide", args=[r.pk])
        self.assertTrue(denied(self.login(self.mum_op).post(url, {"decision": "approve"})))
        r.refresh_from_db()
        self.assertEqual(r.state, "pending")
        self.assertEqual(self.login(self.che_admin).post(url, {"decision": "approve"}).status_code, 404)
        self.assertEqual(self.login(self.mum_admin).get(url).status_code, 405)
        resp = self.login(self.mum_admin).post(url, {"decision": "approve", "note": "ok go"}, follow=True)
        self.assertContains(resp, "LinkedEye does not carry it out")
        r.refresh_from_db()
        self.assertEqual((r.state, r.decided_by_name, r.decision_note), ("approved", "mum-admin", "ok go"))
        again = self.login(self.mum_admin).post(url, {"decision": "reject"}, follow=True)
        self.assertContains(again, "already approved")
        self.assertTrue(AuditEvent.objects.filter(action="hardware.request.approve").exists())

    def test_reject_and_the_list_pages(self):
        self.req(self.mum_op)
        self.req(self.che_admin, device=self.srv_c)
        r = ControlRequest.objects.get(endpoint=self.ep_m)
        self.login(self.mum_admin).post(reverse("hardware:request_decide", args=[r.pk]), {"decision": "reject", "note": "not now"})
        c = self.login(self.mum_admin)
        self.assertNotContains(c.get(reverse("hardware:requests")), "srv-che")
        self.assertContains(c.get(reverse("hardware:requests") + "?state=rejected"), "not now")
        self.assertContains(self.login(self.root).get(reverse("hardware:requests")), "srv-che")
        self.assertNotContains(self.login(self.mum_view).get(reverse("hardware:requests") + "?state=pending"), "Approve</button>")
        self.assertContains(c.get(reverse("hardware:requests")), "No requests")

    def test_alerts_form_is_admin_only_and_validated(self):
        url = reverse("hardware:alerts", args=[self.srv_m.pk])
        self.assertTrue(denied(self.login(self.mum_op).get(url)))
        self.assertEqual(self.login(self.che_admin).get(url).status_code, 404)
        c = self.login(self.mum_admin)
        bad = c.post(url, {"alerts_enabled": "on", "temp_warn_c": "50", "temp_crit_c": "40"})
        self.assertEqual(bad.status_code, 200)
        ok = c.post(url, {"alerts_enabled": "on", "temp_warn_c": "40", "temp_crit_c": "50", "fan_warn_rpm": "3000", "fan_crit_rpm": "1500"})
        self.assertEqual(ok.status_code, 302)
        self.ep_m.refresh_from_db()
        self.assertEqual((self.ep_m.temp_warn_c, self.ep_m.fan_crit_rpm), (40.0, 1500.0))
        c.post(url, {"temp_warn_c": "", "temp_crit_c": ""})
        self.ep_m.refresh_from_db()
        self.assertFalse(self.ep_m.alerts_enabled)
        self.assertTrue(AuditEvent.objects.filter(action="hardware.alerts").exists())


class StaticContract(HwCase):
    """Things the browser check found and that only show up at 400 px: keep them from coming back."""

    def css(self):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "static" / "hardware" / "page.css").read_text()

    def test_wide_tables_scroll_inside_their_wrapper(self):
        # app.css sets .table-wrap{overflow:hidden} late; without an own rule a wide table (and the absolutely positioned
        # .sr labels inside it) pushes the whole page sideways on a phone. Every table of this app sits in a .table-wrap.
        rule = re.search(r"\.pg-hw \.table-wrap \{([^}]*)\}", self.css())
        self.assertIsNotNone(rule)
        self.assertIn("overflow-x: auto", rule.group(1))
        self.assertIn("position: relative", rule.group(1))

    def test_no_table_outside_a_table_wrap(self):
        from pathlib import Path
        folder = Path(__file__).resolve().parents[1] / "templates" / "hardware"
        for page in folder.glob("*.html"):
            text = page.read_text()
            self.assertEqual(text.count("<table"), text.count('class="table-wrap'), page.name)

    def test_auth_failed_banner_does_not_say_it_twice(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "static" / "hardware" / "hardware.js").read_text()
        self.assertIn("rejected the login') < 0", js)
