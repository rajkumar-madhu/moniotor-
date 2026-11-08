"""Regression tests for the code-review findings in core/ (sign-in OTP, notifications, client filter, UI assets)."""
import io
import json
import re
from datetime import timedelta
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.template.loader import render_to_string
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import Client as CustomerClient, Event, Membership, Site

STATIC = Path(__file__).resolve().parent / "static" / "linkedeye"
PASSWORD = "Good-Pass-Word-9!"
LOCMEM = "django.core.mail.backends.locmem.EmailBackend"


def _code_from_mail(message=None):
    message = message or mail.outbox[-1]
    return re.search(r"\b(\d{6})\b", message.body).group(1)


@override_settings(LINKEDEYE_LOGIN_OTP=True, EMAIL_BACKEND=LOCMEM)
class LoginOtpHardeningTests(TestCase):
    """core-agent-1, core-agent-2, core-agent-3"""

    def setUp(self):
        cache.clear()
        self.site = Site.objects.create(name="HQ", code="hq")
        self.user = User.objects.create_user("otpuser", "otp@example.com", PASSWORD)
        Membership.objects.create(user=self.user, site=self.site, role="viewer")
        self.client = Client()

    def _password_step(self, client=None, username="otpuser"):
        return (client or self.client).post(reverse("login"), {"username": username, "password": PASSWORD})

    def test_code_survives_a_different_worker(self):
        """core-agent-2: the code must not live in the per-process cache."""
        r = self._password_step()
        self.assertRedirects(r, reverse("verify_otp"))
        code = _code_from_mail()
        cache.clear()   # what another gunicorn worker sees: an empty local-memory cache
        r2 = self.client.post(reverse("verify_otp"), {"otp": code})
        self.assertEqual(r2.status_code, 302)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)

    def test_code_is_stored_hashed(self):
        from core.models import LoginOtp

        self._password_step()
        code = _code_from_mail()
        row = LoginOtp.objects.get(user=self.user)
        self.assertNotIn(code, row.code_hash)
        self.assertTrue(row.code_hash)

    def test_wrong_codes_burn_the_code(self):
        """core-agent-1: five wrong guesses invalidate the code; the right one no longer works."""
        self._password_step()
        code = _code_from_mail()
        wrong = "000000" if code != "000000" else "000001"
        for _i in range(5):
            r = self.client.post(reverse("verify_otp"), {"otp": wrong})
            self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("verify_otp"), {"otp": code})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_resend_has_a_cooldown(self):
        self._password_step()
        n = len(mail.outbox)
        self.client.post(reverse("resend_otp"))
        self.assertEqual(len(mail.outbox), n, "a resend inside the cooldown must not send another mail")
        later = timezone.now() + timedelta(seconds=75)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.client.post(reverse("resend_otp"))
        self.assertEqual(len(mail.outbox), n + 1)

    def test_resend_has_an_hourly_cap(self):
        from core import login_otp

        self._password_step()
        base = timezone.now()
        for i in range(1, 12):
            with mock.patch("django.utils.timezone.now", return_value=base + timedelta(seconds=70 * i)):
                self.client.post(reverse("resend_otp"))
        # 1 sign-in mail + resends, all inside one hour: never more than the cap
        self.assertLessEqual(len(mail.outbox), login_otp.MAX_SENDS_PER_HOUR)

    def test_relogin_inside_cooldown_does_not_mail_again(self):
        self._password_step()
        n = len(mail.outbox)
        r = self._password_step(Client())
        self.assertRedirects(r, reverse("verify_otp"))
        self.assertEqual(len(mail.outbox), n)

    @override_settings(LOGIN_MAX_FAILURES_PER_IP=3)
    def test_wrong_codes_are_limited_per_address(self):
        """Several accounts behind one address share a budget of wrong codes."""
        users = []
        for i in range(3):
            u = User.objects.create_user(f"u{i}", f"u{i}@example.com", PASSWORD)
            Membership.objects.create(user=u, site=self.site, role="viewer")
            users.append(u)
        clients, codes = [], []
        for u in users:
            c = Client()
            self._password_step(c, u.username)
            codes.append(_code_from_mail())
            clients.append(c)
        for c in clients:
            c.post(reverse("verify_otp"), {"otp": "abcdef"})   # three wrong codes from one address
        r = clients[0].post(reverse("verify_otp"), {"otp": codes[0]})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("_auth_user_id", clients[0].session)

    def test_sends_are_limited_per_address(self):
        from core import login_otp

        users = []
        for i in range(3):
            u = User.objects.create_user(f"s{i}", f"s{i}@example.com", PASSWORD)
            users.append(u)
        with mock.patch.object(login_otp, "MAX_SENDS_PER_IP_PER_HOUR", 2):
            results = [self._password_step(Client(), u.username) for u in users]
        self.assertEqual([r.status_code for r in results], [302, 302, 200])
        self.assertEqual(len(mail.outbox), 2)

    def test_non_ascii_code_is_rejected_not_500(self):
        """core-agent-3"""
        self._password_step()
        r = self.client.post(reverse("verify_otp"), {"otp": "12345é"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "incorrect")

    def test_expired_code_is_refused(self):
        self._password_step()
        code = _code_from_mail()
        later = timezone.now() + timedelta(seconds=400)
        with mock.patch("django.utils.timezone.now", return_value=later):
            r = self.client.post(reverse("verify_otp"), {"otp": code})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "expired")


@override_settings(NOTIFY_EMAILS=["ops@example.com"], NOTIFY_WEBHOOK_URLS=[], NOTIFY_ASYNC=False,
                   NOTIFY_SEVERITIES=["critical", "warning"], EMAIL_BACKEND=LOCMEM)
class NotifyEmailTests(TestCase):
    def _event(self, title, message=""):
        return Event(title=title, message=message, severity="critical", source="test", site="R&D",
                     occurred_at=timezone.now())

    def test_multiline_title_still_sends(self):
        """core-agent-6"""
        from core import notify

        sent = notify.deliver(self._event("HighCPU\nBcc: x@evil.test"))
        self.assertEqual(sent, 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("\n", mail.outbox[0].subject)
        self.assertEqual(mail.outbox[0].extra_headers.get("Bcc"), None)
        self.assertEqual(mail.outbox[0].bcc, [])

    def test_multiline_digest_subject(self):
        from core import notify

        digest = {"severity": "warning", "count": 3, "titles": ["a\nb"], "window": 60}
        self.assertEqual(notify.deliver_digest(digest), 1)
        self.assertNotIn("\n", mail.outbox[0].subject)

    def test_text_part_is_not_html_escaped(self):
        """frontend-4"""
        from core import notify

        notify.deliver(self._event("Disk > 90% on db&web <prod>", "it's \"bad\""))
        body = mail.outbox[0].body
        self.assertIn("Disk > 90% on db&web <prod>", body)
        self.assertIn("it's \"bad\"", body)
        self.assertIn("Site: R&D", body)
        self.assertNotIn("&gt;", body)
        # the HTML part stays escaped
        html = mail.outbox[0].alternatives[0][0]
        self.assertIn("&lt;prod&gt;", html)

    def test_digest_and_otp_and_welcome_text_not_escaped(self):
        from core import notify_templates

        txt, _html = notify_templates.render_digest_email(
            {"severity": "warning", "count": 2, "titles": ["a & b > c"], "window": 60})
        self.assertIn("a & b > c", txt)
        self.assertNotIn("&amp;", txt)
        otp = render_to_string("notify/login_otp.txt", {"greeting_name": "A&B", "otp": "123456", "valid_minutes": 5})
        self.assertIn("A&B", otp)
        welcome = render_to_string("notify/welcome_email.txt", {
            "greeting_name": "A&B", "username": "u", "temporary_password": "p&<q>", "login_url": "http://x/?a=1&b=2",
            "password": "p&<q>", "show_credentials": True,
        })
        self.assertIn("p&<q>", welcome)


class WebhookPolicyTests(SimpleTestCase):
    """core-agent-7"""

    def test_file_scheme_refused(self):
        from core import notify

        with self.assertRaises(Exception):
            notify._post("file:///etc/hostname", {"a": 1})

    def test_loopback_and_metadata_refused(self):
        from core import notify

        for url in ("http://127.0.0.1:9/x", "http://169.254.169.254/latest/", "http://[::1]:9/x"):
            with self.subTest(url=url), mock.patch("socket.create_connection") as conn:
                with self.assertRaises(Exception):
                    notify._post(url, {"a": 1})
                conn.assert_not_called()

    def test_userinfo_refused(self):
        from core import notify

        with self.assertRaises(Exception):
            notify._post("http://user:pw@example.com/x", {"a": 1})

    def test_internal_receiver_allowed_when_listed(self):
        from core import notify

        fake = mock.MagicMock()
        fake.getresponse.return_value.status = 200
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8"]), \
                mock.patch("probes.checks.resolve", return_value=["10.1.2.3"]), \
                mock.patch.object(notify, "_connection", return_value=fake) as mk:
            self.assertEqual(notify._post("http://hooks.internal/x", {"a": 1}), 200)
        self.assertEqual(mk.call_args[0][2], "10.1.2.3")

    def test_redirect_is_not_followed(self):
        from core import notify

        fake = mock.MagicMock()
        resp = fake.getresponse.return_value
        resp.status = 302
        resp.getheader.return_value = "http://169.254.169.254/"
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.0.0.0/8"]), \
                mock.patch("probes.checks.resolve", return_value=["10.1.2.3"]), \
                mock.patch.object(notify, "_connection", return_value=fake) as mk:
            with self.assertRaises(Exception):
                notify._post("http://hooks.internal/x", {"a": 1})
        self.assertEqual(mk.call_count, 1)


class BaseTemplateTests(TestCase):
    """core-agent-8 / frontend-1"""

    def setUp(self):
        self.mum = Site.objects.create(name="Mumbai (NSE)", code="mum")
        self.pune = Site.objects.create(name="Pune", code="pun")
        self.c1 = CustomerClient.objects.create(name="Acme", code="acme")
        self.c2 = CustomerClient.objects.create(name="Globex", code="globex")
        self.mum.client, self.pune.client = self.c1, self.c2
        self.mum.save()
        self.pune.save()
        self.user = User.objects.create_superuser("root", "r@example.com", PASSWORD)
        self.client.force_login(self.user)

    def test_hidden_site_input_is_not_url_encoded(self):
        r = self.client.get("/", {"site": "Mumbai (NSE)"})
        html = r.content.decode()
        self.assertIn('name="site" value="Mumbai (NSE)"', html)
        self.assertNotIn("Mumbai%20", html)

    def test_hidden_client_input_is_plain(self):
        r = self.client.get("/", {"client": str(self.c1.pk)})
        self.assertIn(f'name="client" value="{self.c1.pk}"', r.content.decode())


class ClientParamTests(TestCase):
    """core-agent-9, AZ-06"""

    def setUp(self):
        self.site = Site.objects.create(name="Mumbai", code="mum")
        self.cl = CustomerClient.objects.create(name="Acme", code="acme")
        self.site.client = self.cl
        self.site.save()
        self.user = User.objects.create_user("v", "v@example.com", PASSWORD)
        Membership.objects.create(user=self.user, site=self.site, role="viewer")
        self.client.force_login(self.user)

    def test_unknown_client_is_not_persisted(self):
        self.client.get("/?client=999")
        self.assertNotEqual(self.client.session.get("le_client"), "999")
        from core import overview

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.session.get("le_client"), str(self.cl.pk))
        self.assertTrue(overview.site_matrix(self.user))

    def test_stale_stored_client_is_dropped(self):
        s = self.client.session
        s["le_client"] = "999"
        s.save()
        self.client.get("/")
        self.assertEqual(self.client.session.get("le_client"), str(self.cl.pk))

    def test_valid_and_unassigned_values_are_kept(self):
        other = CustomerClient.objects.create(name="Globex", code="globex")
        s2 = Site.objects.create(name="Pune", code="pun", client=other)
        Membership.objects.create(user=self.user, site=s2, role="viewer")
        self.client.get(f"/?client={other.pk}")
        self.assertEqual(self.client.session.get("le_client"), str(other.pk))
        self.client.get("/?client=unassigned")
        self.assertEqual(self.client.session.get("le_client"), "unassigned")

    def test_huge_client_id_is_not_a_500(self):
        r = self.client.get("/?client=99999999999999999999")
        self.assertEqual(r.status_code, 200)

    def test_sites_for_client_rejects_out_of_range(self):
        from core.client_sites import sites_for_client

        self.assertEqual(sites_for_client("99999999999999999999"), [])
        self.assertEqual(sites_for_client("-5"), [])

    def test_safe_int_helper(self):
        from core.client_sites import safe_pk

        self.assertEqual(safe_pk("12"), 12)
        self.assertIsNone(safe_pk("99999999999999999999"))
        self.assertIsNone(safe_pk("x"))
        self.assertIsNone(safe_pk("-1"))
        self.assertIsNone(safe_pk(""))


class LoginRedirectTests(TestCase):
    """core-agent-10"""

    def test_next_keeps_the_whole_query(self):
        r = self.client.get("/search/?q=disk&site=Mumbai&per=50")
        self.assertEqual(r.status_code, 302)
        loc = urlsplit(r["Location"])
        self.assertEqual(loc.path, "/accounts/login/")
        self.assertEqual(parse_qs(loc.query)["next"], ["/search/?q=disk&site=Mumbai&per=50"])


class SecretsRewrapTests(TestCase):
    """onboarding-15"""

    def setUp(self):
        from cryptography.fernet import Fernet

        self.old, self.new = Fernet.generate_key().decode(), Fernet.generate_key().decode()

    def _store(self, key, text):
        from core import secrets

        with override_settings(LINKEDEYE_CREDENTIAL_KEY=key):
            return secrets.store("bearer", "", text, "", None, name=f"cred-{text}")

    def test_rewrap_moves_rows_to_the_primary_key(self):
        from core import secrets

        row = self._store(self.old, "s3cret")
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=f"{self.new},{self.old}"):
            result = secrets.rewrap()
            self.assertEqual((result["rewrapped"], result["failed"]), (1, []))
            row.refresh_from_db()
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=self.new):
            self.assertEqual(secrets.decrypt(row.secret), "s3cret")

    def test_rewrap_reports_rows_that_cannot_be_read(self):
        from core import secrets
        from onboarding.models import Credential

        row = self._store(self.old, "lost")
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=self.new):
            result = secrets.rewrap()
        self.assertEqual(result["rewrapped"], 0)
        self.assertEqual(result["failed"], [row.pk])
        self.assertEqual(Credential.objects.get(pk=row.pk).secret, row.secret)   # untouched

    def test_dry_run_changes_nothing(self):
        from core import secrets

        row = self._store(self.old, "x")
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=f"{self.new},{self.old}"):
            result = secrets.rewrap(dry_run=True)
            row.refresh_from_db()
        self.assertEqual(result["rewrapped"], 1)
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=self.old):
            self.assertEqual(secrets.decrypt(row.secret), "x")

    def test_management_command(self):
        row = self._store(self.old, "y")
        out = io.StringIO()
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=f"{self.new},{self.old}"):
            call_command("rewrap_credentials", stdout=out)
        self.assertIn("1", out.getvalue())
        from core import secrets
        row.refresh_from_db()
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=self.new):
            self.assertEqual(secrets.decrypt(row.secret), "y")


# ----------------------------------------------------------------------------- front-end assets
def _luminance(hex_color):
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def lin(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def _contrast(a, b):
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _tokens(block_start):
    css = (STATIC / "app.css").read_text()
    start = css.index(block_start)
    end = css.index("\n}", start)
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\b", css[start:end]))


class CssTests(SimpleTestCase):
    def test_table_wrap_scrolls_horizontally(self):
        """frontend-3: no later .table-wrap rule may turn overflow back to hidden."""
        css = (STATIC / "app.css").read_text()
        for m in re.finditer(r"(?<![\w.-])\.table-wrap\s*\{([^}]*)\}", css):
            body = m.group(1)
            self.assertNotRegex(body, r"overflow\s*:\s*hidden", body)

    def test_light_theme_text_colours_meet_aa(self):
        """frontend-5"""
        light = _tokens('html[data-theme="light"] {')
        panel = light["--panel"]
        for name in ("--primary", "--warn", "--stone"):
            self.assertGreaterEqual(_contrast(light[name], panel), 4.5, name)
        # breadcrumbs and "View all" links sit directly on the page background, which is darker than the panel
        self.assertGreaterEqual(_contrast(light["--primary"], light["--bg"]), 4.5, "--primary on --bg")
        self.assertGreaterEqual(_contrast(light["--btn-primary"], light["--btn-primary-ink"]), 4.5)
        self.assertGreaterEqual(_contrast(light["--active-fill"], light["--active-ink"]), 4.5)
        self.assertGreaterEqual(_contrast(light["--chart-warn"], panel), 3.0)

    def test_dark_theme_button_label_meets_aa(self):
        dark = _tokens(":root {")
        self.assertGreaterEqual(_contrast(dark["--btn-primary"], dark["--btn-primary-ink"]), 4.5)
        self.assertGreaterEqual(_contrast(dark["--active-fill"], dark["--active-ink"]), 4.5)
        self.assertGreaterEqual(_contrast(dark["--primary"], dark["--panel"]), 4.5)

    def test_closed_mobile_drawer_is_not_focusable(self):
        """frontend-8"""
        css = (STATIC / "app.css").read_text()
        self.assertRegex(css, r"\.side\s*\{[^}]*visibility\s*:\s*hidden")
        self.assertRegex(css, r"\.side\.open\s*\{[^}]*visibility\s*:\s*visible")


class AppJsTests(SimpleTestCase):
    def test_refresh_rearms_when_tab_is_hidden(self):
        """frontend-7: a hidden tab at fire time must not end the refresh loop."""
        js = (STATIC / "app.js").read_text()
        self.assertNotRegex(js, r"setTimeout\(\(\) => \{ if \(!document\.hidden\) location\.reload\(\); \}")
        self.assertIn("visibilitychange", js)

    def test_mobile_drawer_closes_on_escape_and_has_skip_link(self):
        js = (STATIC / "app.js").read_text()
        self.assertIn("Escape", js)
        base = (Path(__file__).resolve().parent / "templates" / "core" / "base.html").read_text()
        self.assertIn("skip-link", base)
        self.assertIn('id="main"', base)

    def test_logs_js_rearms_and_scopes_counter(self):
        logs = (Path(__file__).resolve().parent.parent / "onboarding" / "static" / "onboarding" / "logs.js").read_text()
        self.assertNotRegex(logs, r"if \(document\.hidden\) \{\s*return;\s*\}")


class PagerTests(SimpleTestCase):
    def _render(self, per):
        from django.core.paginator import Paginator

        page = Paginator(list(range(300)), per).page(1)
        return render_to_string("core/_pager.html", {"page": page, "query": {}})

    def test_selected_option_matches_page_size(self):
        """frontend-9"""
        for per in (5, 10, 25, 100):
            html = self._render(per)
            sel = re.findall(r'<option value="(\d+)" selected', html)
            self.assertEqual(sel, [str(per)], per)
        self.assertIn('<option value="100"', self._render(10))


class HugeIdFilterTests(TestCase):
    """AZ-06: a 20-digit id in a filter must not become an HTTP 500."""

    def setUp(self):
        self.client.force_login(User.objects.create_superuser("root", "r@example.com", PASSWORD))

    def test_pages_and_exports(self):
        big = "99999999999999999999"
        for url in (f"/?client={big}", f"/ipam/subnets/export.csv?vlan={big}", f"/ipam/subnets/?vlan={big}",
                    f"/dcim/items/export.csv?rack={big}", f"/service-desk/tickets/?client={big}"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
