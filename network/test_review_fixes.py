"""Regression tests for the code-review findings in the network app (network-2..5, frontend-2)."""
from unittest import mock

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from core.models import Event, Membership, Site

from . import jobs
from .forms import DeviceForm
from .models import Device, DeviceImage, DeviceStatus, Interface
from .tests import T, dev, fail, users
from .tests_images import png


def image_row(**kw):
    data = png(8, 8)
    return DeviceImage.objects.create(tenant_key=T, sha256=kw.pop("sha", "a" * 64), content_type="image/png", width=8, height=8,
                                      byte_size=len(data), data=data, **kw)


@override_settings(LINKEDEYE_TENANT=T)
class EditKeepsPollStateTests(TestCase):
    """network-2: an edit must not write back poll-owned columns from the copy it loaded at the start of the request."""

    def test_edit_does_not_overwrite_a_down_flip_that_happened_during_the_request(self):
        _, operator, _ = users()
        d = dev("race-sw", consecutive_failures=1, status=DeviceStatus.UP)
        c = Client()
        c.force_login(operator)

        def slow_resolve(self_form, user):
            # the reachability job processes a second failed probe while the photo is being fetched
            jobs.record(Device.objects.get(pk=d.pk), fail())
            return ("keep", None)

        data = {"name": "race-sw", "device_type": "switch", "vendor": "Cisco", "mgmt_ip": "192.0.2.10", "check_port": 22,
                "site": "", "environment": "prod", "enabled": "on"}
        with mock.patch.object(DeviceForm, "resolve_image", slow_resolve), mock.patch("network.probe.tcp_probe"):
            r = c.post(reverse("network:edit", args=[d.pk]), data)
        self.assertEqual(r.status_code, 302)
        d.refresh_from_db()
        self.assertEqual(d.vendor, "Cisco")                                    # the form's own fields are saved
        self.assertEqual((d.status, d.consecutive_failures, d.last_alert_status), ("down", 2, "down"))
        self.assertTrue(Event.objects.filter(title="race-sw is down").exists())


@override_settings(LINKEDEYE_TENANT=T)
class DeleteReleasesPhotoTests(TestCase):
    def test_deleting_a_device_removes_its_unused_photo(self):
        _, _, admin = users()
        img = image_row()
        d = dev("photo-sw", image=img)
        other = image_row(sha="b" * 64)
        shared = dev("keeps-photo", mgmt_ip="192.0.2.11", image=other)
        dev("also-keeps", mgmt_ip="192.0.2.12", image=other)
        c = Client()
        c.force_login(admin)
        self.assertEqual(c.post(reverse("network:delete", args=[d.pk])).status_code, 302)
        self.assertFalse(Device.objects.filter(pk=d.pk).exists())
        self.assertFalse(DeviceImage.objects.filter(pk=img.pk).exists())
        c.post(reverse("network:delete", args=[shared.pk]))
        self.assertTrue(DeviceImage.objects.filter(pk=other.pk).exists())    # still used by another device


@override_settings(LINKEDEYE_TENANT=T)
class PortParameterTests(TestCase):
    def setUp(self):
        _, _, self.admin = users()
        self.d = dev("port-sw")
        Interface.objects.create(device=self.d, if_index=1, name="Gi1")
        self.c = Client()
        self.c.force_login(self.admin)

    def test_non_ascii_digit_or_huge_number_is_ignored_not_a_500(self):
        for value in ("²", "٣", "9" * 5000, "-1", " 1"):
            for name in ("network:detail", "network:live"):
                r = self.c.get(reverse(name, args=[self.d.pk]), {"port": value})
                self.assertEqual(r.status_code, 200, (name, value[:10]))
        r = self.c.get(reverse("network:detail", args=[self.d.pk]), {"port": "1"})
        self.assertEqual(r.status_code, 200)


@override_settings(LINKEDEYE_TENANT=T)
class SiteChoiceScopeTests(TestCase):
    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai", code="mum")
        Site.objects.create(name="SecretCustomerB", code="b")
        dev("on-b", site="SecretCustomerB")
        self.user = User.objects.create_user("siteop", password="x")
        Membership.objects.create(user=self.user, site=self.mum, role="operator")
        self.c = Client()
        self.c.force_login(self.user)

    def test_forms_list_only_the_users_sites(self):
        for url in (reverse("network:create"), reverse("network:discovery")):
            r = self.c.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, 'value="Mumbai"')
            self.assertNotContains(r, "SecretCustomerB")
            self.assertNotContains(r, '<option value="All Sites"')
            self.assertNotContains(r, "(No site)")

    def test_tenant_wide_operator_still_sees_everything(self):
        _, operator, _ = users()
        c = Client()
        c.force_login(operator)
        r = c.get(reverse("network:create"))
        self.assertContains(r, 'value="SecretCustomerB"')
        self.assertContains(r, "(No site)")


@override_settings(LINKEDEYE_TENANT=T)
class LegacyPanelFrameTests(TestCase):
    def test_panel_may_be_framed_by_its_own_site_only(self):
        _, operator, _ = users()
        d = dev("fw-panel", vendor="Fortigate", model="Fortigate 100F", device_type="firewall")
        c = Client()
        c.force_login(operator)
        r = c.get(reverse("network:legacy_panel", args=[d.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-Frame-Options"], "SAMEORIGIN")
        # every other page keeps the global DENY
        self.assertEqual(c.get(reverse("network:detail", args=[d.pk]))["X-Frame-Options"], "DENY")


NODE_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
const notices = [], badges = [];
const el = () => ({ dataset: {}, classList: { add() {}, remove() {} }, setAttribute() {}, addEventListener() {}, textContent: '',
                    querySelector() { return el(); }, querySelectorAll() { return []; }, hidden: true });
const root = el();
Object.assign(root.dataset, { url: '/x', interval: '30', status: 'up', event: '1' });
const click = [];
const toggle = el(); toggle.addEventListener = (n, f) => click.push(f);
const notice = el();
const noticeText = el();
Object.defineProperty(noticeText, 'textContent', { set(v) { notices.push(v); }, get() { return ''; } });
notice.querySelector = (sel) => (sel === 'a' ? el() : noticeText);
root.querySelector = (sel) => (sel === '.nw-live-toggle' ? toggle : sel === '.nw-live-notice-text' ? noticeText : el());
const document = { getElementById: (id) => (id === 'nw-live' ? root : id === 'nw-live-notice' ? notice : null),
                   querySelectorAll: (sel) => (sel !== '.nw-live-badge .badge' ? [] : [{ get className() { return ''; }, set className(v) { badges.push(v); }, set textContent(v) {} }]),
                   addEventListener() {}, dispatchEvent() {}, hidden: false };
const replies = JSON.parse(process.argv[2]);
const window = { fetch: true, CustomEvent: function () {} };
const ctx = { document, window, location: { href: '/' }, CustomEvent: window.CustomEvent, Date, Math, Number, String, Array, Error, JSON,
              setTimeout, clearTimeout, setInterval: () => 0,
              fetch: () => { const d = replies.shift(); return Promise.resolve({ ok: true, headers: { get: () => 'application/json' }, json: () => Promise.resolve(d) }); } };
ctx.window = Object.assign(window, ctx);
vm.createContext(ctx);
vm.runInContext(src, ctx);
(async () => {
  for (let i = 0; i < 3; i++) {
    click[0](); click[0]();                    // pause, then resume: resume polls at once
    await new Promise((r) => setTimeout(r, 30));
  }
  console.log(JSON.stringify({ notices, status: root.dataset.status, event: root.dataset.event }));
  process.exit(0);
})();
"""


class LiveNoticeScriptTests(TestCase):
    """frontend-10: the baseline moves after an announcement, and a status change does not hide a later alert."""

    def run_polls(self, replies):
        import json
        import shutil
        import subprocess
        from pathlib import Path
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        js = Path(__file__).parent / "static" / "network" / "live.js"
        out = subprocess.run([node, "-e", NODE_HARNESS, "--", str(js), json.dumps(replies)], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_each_change_is_announced_once_and_alerts_are_not_starved(self):
        down = {"status": "down", "values": {"status": "Down"}, "tones": {"status": "down"}, "latest_event": {"id": 1, "title": "t", "ago": "now"}}
        alert = dict(down, latest_event={"id": 2, "title": "Second alert", "ago": "1m ago"})
        res = self.run_polls([down, down, alert])
        self.assertEqual(res["notices"], ["Status changed to Down.", "New alert: Second alert (1m ago)."])
        self.assertEqual((res["status"], res["event"]), ("down", "2"))
