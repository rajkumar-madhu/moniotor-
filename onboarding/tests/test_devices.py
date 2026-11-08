"""The device (SNMP), Prometheus and targets-intake paths of the wizard.

Every network call is mocked: ``probes.checks.resolve`` answers names with documentation-range
addresses (opted in through PROBE_ALLOWED_PRIVATE_CIDRS so the real SSRF policy still runs) and
``probes.checks._open_connection`` returns a scripted connection. The base class fails any test that
opens a real socket.
"""
import json
import logging
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.urls import reverse

from administration import integration_config as ic
from cmdb.models import ConfigurationItem
from core import prometheus, secrets
from core.models import AuditEvent
from onboarding import devices, intake, prometheus_step, services
from onboarding.models import (Credential, CredentialKind, OnboardingRun, OnboardingTarget, StepLog, TargetState,
                               TargetType)
from probes import checks

from .base import OnboardingTestCase, with_key, without_key

COMMUNITY = "s3cr3t-community-zq9"
V3_AUTH = "auth-pass-phrase-77x"
V3_PRIV = "priv-pass-phrase-88y"
PROM_PASS = "prom-basic-pass-4471"

ALLOWED = override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["203.0.113.0/24"])

HOSTS = {"snmp-exporter": ["203.0.113.9"], "sw-core-01": ["203.0.113.20"], "prom.example.test": ["203.0.113.30"],
         "internal-sw": ["10.9.9.9"]}

SNMP_BODY = (
    '# HELP sysDescr system description\n'
    'sysDescr{sysDescr="Cisco IOS XE \\"17.3\\" Software\\nline two"} 1\n'
    'sysName{sysName="sw-core-01"} 1\n'
    'sysLocation{sysLocation="Mumbai DC row 4"} 1\n'
    'sysObjectID{sysObjectID="1.3.6.1.4.1.9.1.2504"} 1\n'
    'sysUpTime 123456789\n'
    'ifOperStatus{ifAlias="",ifDescr="Gi1/0/1",ifIndex="1",ifName="Gi1/0/1"} 1\n'
    'ifOperStatus{ifAlias="",ifDescr="Gi1/0/2",ifIndex="2",ifName="Gi1/0/2"} 2\n'
    'snmp_scrape_duration_seconds 0.42\n'
    'snmp_scrape_pdus_returned 83\n'
)
BUILDINFO = {"status": "success", "data": {"version": "2.53.0", "revision": "abcdef0123456789", "goVersion": "go1.22"}}


def targets_doc(n_up=3, n_down=1):
    active = []
    for i in range(n_up):
        active.append({"labels": {"instance": f"web-{i}:9100", "job": "node"}, "health": "up"})
    for i in range(n_down):
        active.append({"labels": {"instance": f"db-{i}:9100", "job": "node"}, "health": "down"})
    active.append({"labels": {"instance": "web-0:9115", "job": "blackbox"}, "health": "up"})   # same host, other job
    return {"status": "success", "data": {"activeTargets": active}}


class FakeResp:
    def __init__(self, status, body, ctype="application/json"):
        self.status, self._body, self._ctype, self._pos = status, body, ctype, 0

    def getheader(self, name, default=None):
        return {"content-type": self._ctype}.get(name.lower(), default)

    def read(self, n=-1):
        chunk = self._body[self._pos:] if n is None or n < 0 else self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


class Net:
    """A scripted network: routes are (host, path prefix) -> (status, body) or an exception to raise."""

    def __init__(self):
        self.routes, self.calls, self.default = [], [], None

    def route(self, host, prefix, status=200, body=b"", error=None, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.routes.append((host, prefix, status, body, error, ctype))

    def open(self, scheme, host, ip, port, timeout):
        net = self

        class Conn:
            def request(self, method, path, headers=None):
                net.calls.append({"scheme": scheme, "host": host, "ip": ip, "port": port, "path": path,
                                  "headers": dict(headers or {})})
                self.path = path

            def getresponse(self):
                for r_host, prefix, status, body, error, ctype in net.routes:
                    if r_host == host and self.path.startswith(prefix):
                        if error is not None:
                            raise error
                        return FakeResp(status, body, ctype)
                return FakeResp(404, b"{}")

            def close(self):
                pass

        return Conn()


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class NetCase(OnboardingTestCase):
    """OnboardingTestCase plus the scripted network, an SNMP exporter address and users."""

    def setUp(self):
        super().setUp()
        self.net = Net()
        for patcher in (mock.patch("probes.checks.resolve", side_effect=self._resolve),
                        mock.patch("probes.checks._open_connection", side_effect=self.net.open),
                        ALLOWED):
            if hasattr(patcher, "start"):
                patcher.start()
                self.addCleanup(patcher.stop)
            else:
                patcher.enable()
                self.addCleanup(patcher.disable)
        User = get_user_model()
        self.root = User.objects.create_superuser("root", password="root-pass-123456")

    def _resolve(self, host, port=443):
        if host in HOSTS:
            return list(HOSTS[host])
        import socket
        raise socket.gaierror(-2, "Name or service not known")

    def login_root(self):
        from django.test import Client
        self.c = Client()
        self.assertTrue(self.c.login(username="root", password="root-pass-123456"))
        return self.c

    def url(self, run, name):
        return reverse("onboarding:step", args=[run.pk, name])

    def everything_stored(self):
        """Every text the product keeps about a run: step logs, audit details, target facts and errors,
        the integration settings, and the cache-free vault rows (ciphertext only)."""
        from administration.models import IntegrationSetting
        parts = [e.detail for e in StepLog.objects.all()]
        parts += [json.dumps(e.details) + e.object_repr for e in AuditEvent.objects.all()]
        parts += [json.dumps(t.facts) + t.last_error for t in OnboardingTarget.objects.all()]
        parts += [json.dumps(s.values) for s in IntegrationSetting.objects.all()]
        parts += [c.name + c.notes + c.username for c in Credential.objects.all()]
        return "\n".join(parts)

    def assert_no_secret(self, *secrets_, response=None):
        text = self.everything_stored() + (response.content.decode() if response is not None else "")
        for s in secrets_:
            self.assertNotIn(s, text)
        for cred in Credential.objects.all():
            for s in secrets_:
                self.assertNotIn(s, cred.secret)


# ============================================================================= SNMP: credentials
@with_key
class SnmpCredentialTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(kind="device", step="credentials")
        self.sw = self.make_target(self.run_, "sw-core-01", target_type=TargetType.DEVICE, port=9116)
        self.srv = self.make_target(self.run_, "db-01")
        self.login(self.admin_a)

    def save(self, **extra):
        data = {"name": "mumbai-snmp", "version": "v2c", "community": COMMUNITY, "module": "if_mib",
                "apply_to": "devices", **extra}
        return self.c.post(reverse("onboarding:snmp_save", args=[self.run_.pk]), data)

    def test_v2c_community_is_encrypted_attached_to_devices_only_and_never_shown(self):
        resp = self.save()
        self.assertRedirects(resp, self.url(self.run_, "credentials"))
        cred = Credential.objects.get()
        self.assertEqual((cred.kind, cred.username, cred.site), (CredentialKind.SNMP_V2C, "", "Mumbai"))
        self.assertNotIn(COMMUNITY, cred.secret)
        with secrets.use(cred, reason="test") as plain:
            self.assertEqual(plain, COMMUNITY)
        self.sw.refresh_from_db()
        self.srv.refresh_from_db()
        self.assertEqual(self.sw.credential_id, cred.pk)
        self.assertIsNone(self.srv.credential_id)                   # the Linux server is not touched
        self.assertEqual(self.sw.facts["snmp_settings"],
                         {"version": "v2c", "module": "if_mib", "auth_profile": "public_v2", "security_level": ""})
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        self.assertNotIn(COMMUNITY, page)
        self.assertIn("••••", page)
        self.assert_no_secret(COMMUNITY, response=resp)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.snmp.credential").exists())

    def test_a_default_community_gets_a_warning(self):
        resp = self.c.post(reverse("onboarding:snmp_save", args=[self.run_.pk]),
                           {"name": "x", "version": "v2c", "community": "public", "module": "if_mib"}, follow=True)
        self.assertIn("well-known default", resp.content.decode())

    def test_v3_login_is_one_encrypted_blob_with_both_passwords(self):
        resp = self.save(version="v3", community="", username="svc-snmp", security_level="authPriv",
                         auth_protocol="SHA256", auth_password=V3_AUTH, priv_protocol="AES", priv_password=V3_PRIV)
        self.assertEqual(resp.status_code, 302)
        cred = Credential.objects.get()
        self.assertEqual((cred.kind, cred.username), (CredentialKind.SNMP_V3, "svc-snmp"))
        with secrets.use(cred, reason="test") as plain:
            blob = json.loads(plain)
        self.assertEqual(blob, {"security_level": "authPriv", "auth_protocol": "SHA256", "auth_password": V3_AUTH,
                                "priv_protocol": "AES", "priv_password": V3_PRIV})
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.facts["snmp_settings"]["security_level"], "authPriv")
        self.assertEqual(self.sw.facts["snmp_settings"]["auth_profile"], "linkedeye_v3")
        self.assert_no_secret(V3_AUTH, V3_PRIV, response=resp)

    def test_v3_validation_keeps_typed_passwords_out_of_the_re_rendered_page(self):
        resp = self.save(version="v3", community="", username="", security_level="authPriv",
                         auth_protocol="SHA", auth_password=V3_AUTH, priv_protocol="AES", priv_password="short")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("needs a security name", body)
        self.assertIn("at least 8 characters", body)
        for typed in (V3_AUTH, "short"):
            self.assertNotIn(typed, body)
        self.assertEqual(Credential.objects.count(), 0)

    def test_v3_no_auth_no_priv_needs_no_passwords(self):
        resp = self.save(version="v3", community="", username="readonly", security_level="noAuthNoPriv")
        self.assertEqual(resp.status_code, 302)
        with secrets.use(Credential.objects.get(), reason="test") as plain:
            self.assertEqual(json.loads(plain), {"security_level": "noAuthNoPriv"})

    def test_v2c_needs_a_community_and_rejects_control_characters(self):
        for bad in ("", "   ", "ab\x01cd"):
            resp = self.save(community=bad)
            self.assertEqual(resp.status_code, 200, repr(bad))
        self.assertEqual(Credential.objects.count(), 0)

    def test_module_and_profile_names_are_identifiers_only(self):
        for field in ("module", "auth_profile"):
            resp = self.save(**{field: "if_mib&target=169.254.169.254"})
            self.assertEqual(resp.status_code, 200, field)
        self.assertEqual(Credential.objects.count(), 0)

    @without_key
    def test_without_the_vault_key_nothing_is_stored_and_the_reason_is_shown(self):
        resp = self.c.post(reverse("onboarding:snmp_save", args=[self.run_.pk]),
                           {"name": "n", "version": "v2c", "community": COMMUNITY, "module": "if_mib"}, follow=True)
        self.assertEqual(Credential.objects.count(), 0)
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", resp.content.decode())
        self.assertNotIn(COMMUNITY, resp.content.decode())
        self.sw.refresh_from_db()
        self.assertIsNone(self.sw.credential_id)

    def test_apply_to_none_stores_but_does_not_attach(self):
        self.save(apply_to="none")
        self.assertEqual(Credential.objects.count(), 1)
        self.sw.refresh_from_db()
        self.assertIsNone(self.sw.credential_id)

    def test_the_exporter_profile_snippet_uses_placeholders_not_secrets(self):
        self.save()
        v2 = devices.exporter_auth_snippet("v2c")
        v3 = devices.exporter_auth_snippet("v3", username="svc", security_level="authPriv", auth_protocol="SHA256")
        self.assertIn('community: "<community string>"', v2)
        self.assertIn("<authentication password>", v3)
        self.assertIn("<privacy password>", v3)
        self.assertNotIn(COMMUNITY, v2 + v3)
        self.assertNotIn("<privacy password>", devices.exporter_auth_snippet("v3", security_level="authNoPriv"))

    def test_the_credentials_page_shows_the_snmp_panel_for_a_device_run(self):
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        self.assertIn("Network devices over SNMP", page)
        self.assertIn("The SNMP exporter address is not set", page)
        self.assertIn('name="community"', page)
        self.assertNotIn(COMMUNITY, page)

    def test_a_server_run_without_devices_has_no_snmp_panel(self):
        run = self.make_run(kind="server")
        self.make_target(run, "app-01")
        page = self.c.get(self.url(run, "credentials")).content.decode()
        self.assertNotIn("Network devices over SNMP", page)


# ============================================================================= SNMP: verify
@with_key
class SnmpVerifyTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(kind="device", step="credentials")
        self.sw = self.make_target(self.run_, "sw-core-01", target_type=TargetType.DEVICE, port=9116)
        self.login(self.admin_a)
        ic.save("snmp_exporter", {"url": "http://snmp-exporter:9116"}, self.admin_a)

    def test_url(self):
        return reverse("onboarding:snmp_test", args=[self.run_.pk])

    def test_a_successful_walk_records_sysname_sysdescr_syslocation_and_interfaces(self):
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY, ctype="text/plain")
        resp = self.c.post(self.test_url())
        self.assertRedirects(resp, self.url(self.run_, "credentials"))
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.state, TargetState.EXPORTER_FOUND)
        snmp = self.sw.facts["snmp"]
        self.assertEqual(snmp["sysName"], "sw-core-01")
        self.assertEqual(snmp["sysLocation"], "Mumbai DC row 4")
        self.assertEqual(snmp["sysObjectID"], "1.3.6.1.4.1.9.1.2504")
        self.assertEqual(snmp["interfaces"], 2)
        self.assertEqual(snmp["uptime_seconds"], 1234567)
        self.assertEqual(snmp["pdus"], 83)
        self.assertIn('Cisco IOS XE "17.3" Software', snmp["sysDescr"])
        self.assertNotIn("\n", snmp["sysDescr"])                        # device text has no control characters
        self.assertEqual(self.sw.facts["hostname"], "sw-core-01")
        self.assertEqual(self.sw.facts["exporter"], "snmp_exporter")
        self.assertTrue(self.sw.facts["dns"])
        self.assertEqual(self.sw.last_error, "")

    def test_the_exporter_is_asked_for_the_validated_address_module_and_profile(self):
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url())
        call = self.net.calls[0]
        self.assertEqual((call["host"], call["ip"], call["port"]), ("snmp-exporter", "203.0.113.9", 9116))
        self.assertIn("target=203.0.113.20", call["path"])               # the address that passed the policy
        self.assertIn("module=if_mib", call["path"])
        self.assertIn("auth=public_v2", call["path"])

    def test_the_probe_goes_through_probes_checks(self):
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        with mock.patch.object(checks, "resolve_allowed", wraps=checks.resolve_allowed) as policy:
            self.c.post(self.test_url())
        self.assertEqual([c.args[0] for c in policy.call_args_list], ["sw-core-01", "snmp-exporter"])

    def test_every_attempt_is_written_to_the_step_log_with_its_outcome(self):
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url())
        lines = list(StepLog.objects.filter(run=self.run_, step="verify", target=self.sw).order_by("id"))
        self.assertGreaterEqual(len(lines), 3)
        self.assertTrue(all(line.status == "ok" for line in lines))
        self.assertTrue(any("walked if_mib" in line.detail and line.duration_ms is not None for line in lines))

    def test_a_device_that_returns_nothing_is_unreachable_with_a_useful_reason(self):
        self.net.route("snmp-exporter", "/snmp", 200, "snmp_scrape_duration_seconds 5.0\nsnmp_scrape_pdus_returned 0\n")
        self.c.post(self.test_url())
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.state, TargetState.UNREACHABLE)
        self.assertIn("returned no data", self.sw.last_error)
        self.assertIn("UDP 161", self.sw.last_error)

    def test_an_unknown_module_is_reported_without_the_exporter_text(self):
        self.net.route("snmp-exporter", "/snmp", 400, "unknown module 'if_mib' /etc/secret/path")
        self.c.post(self.test_url())
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.state, TargetState.UNREACHABLE)
        self.assertIn("HTTP 400", self.sw.last_error)
        self.assertIn("not defined in the exporter's snmp.yml", self.sw.last_error)
        self.assertNotIn("/etc/secret", self.sw.last_error)

    def test_an_exporter_that_refuses_the_connection(self):
        self.net.route("snmp-exporter", "/snmp", error=ConnectionRefusedError())
        self.c.post(self.test_url())
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.state, TargetState.UNREACHABLE)
        self.assertIn("Connection refused", self.sw.last_error)

    def test_a_device_name_that_does_not_resolve(self):
        other = self.make_target(self.run_, "ghost-sw", target_type=TargetType.DEVICE)
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url(), {"target": other.pk})
        other.refresh_from_db()
        self.assertEqual(other.state, TargetState.UNREACHABLE)
        self.assertIn("does not resolve", other.last_error)
        self.assertEqual(self.net.calls, [])                              # the exporter was never asked

    def test_a_private_device_address_outside_the_allow_list_is_refused_by_the_ssrf_policy(self):
        bad = self.make_target(self.run_, "internal-sw", target_type=TargetType.DEVICE)
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url(), {"target": bad.pk})
        bad.refresh_from_db()
        self.assertEqual(bad.state, TargetState.UNREACHABLE)
        self.assertIn("PROBE_ALLOWED_PRIVATE_CIDRS", bad.last_error)
        self.assertNotIn("10.9.9.9", bad.last_error)                      # the resolved address is not echoed
        self.assertEqual(self.net.calls, [])

    def test_the_exporter_itself_is_held_to_the_policy(self):
        ic.save("snmp_exporter", {"url": "http://internal-sw:9116"}, self.admin_a)
        self.net.route("internal-sw", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url())
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.state, TargetState.UNREACHABLE)
        self.assertEqual(self.net.calls, [])

    def test_without_an_exporter_address_the_error_says_where_to_set_it(self):
        ic.save("snmp_exporter", {"url": ""}, self.admin_a)
        self.c.post(self.test_url())
        self.sw.refresh_from_db()
        self.assertEqual(self.sw.state, TargetState.FAILED)
        self.assertIn("SNMP exporter address is not set", self.sw.last_error)

    def test_no_secret_is_read_or_stored_while_verifying(self):
        self.c.post(reverse("onboarding:snmp_save", args=[self.run_.pk]),
                    {"name": "snmp", "version": "v2c", "community": COMMUNITY, "module": "if_mib"})
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        resp = self.c.post(self.test_url(), follow=True)
        self.assertFalse(AuditEvent.objects.filter(action="credential.use").exists())   # the vault was not opened
        self.assert_no_secret(COMMUNITY, response=resp)
        self.assertTrue(all(COMMUNITY not in json.dumps(c["headers"]) for c in self.net.calls))

    def test_a_non_snmp_credential_is_noted_not_used(self):
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", "pw-ssh-1234", "Mumbai", self.admin_a, name="ssh")
        self.sw.credential = cred
        self.sw.save()
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url())
        self.sw.refresh_from_db()
        self.assertIn("not an SNMP one", self.sw.facts["credential_note"])

    def test_the_verify_hook_contract_state_and_error_keys(self):
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        result = devices.verify_device(self.sw)
        self.assertEqual(result["state"], TargetState.EXPORTER_FOUND)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["snmp"]["sysName"], "sw-core-01")

    def test_the_results_are_shown_on_the_credentials_page(self):
        self.net.route("snmp-exporter", "/snmp", 200, SNMP_BODY)
        self.c.post(self.test_url())
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        self.assertIn("Mumbai DC row 4", page)
        self.assertIn("Exporter found", page)


class SnmpParseTests(OnboardingTestCase):
    def test_parse_handles_escapes_control_characters_and_junk(self):
        found = devices.parse_snmp_metrics(
            b'sysName{sysName="a\\\\b\\"c"} 1\nsysDescr{sysDescr="x\x07y"} 1\nnot a metric line\n'
            b'ifOperStatus{ifIndex="1"} 1\nifOperStatus{ifIndex="1"} 1\n')
        self.assertEqual(found["sysName"], 'a\\b"c')
        self.assertNotIn("\x07", found["sysDescr"])
        self.assertEqual(found["interfaces"], 1)                          # the same index twice is one interface

    def test_parse_bounds_device_text(self):
        found = devices.parse_snmp_metrics(('sysDescr{sysDescr="%s"} 1\n' % ("x" * 5000)).encode())
        self.assertEqual(len(found["sysDescr"]), devices.MAX_TEXT)

    def test_an_empty_body_has_no_series(self):
        self.assertEqual(devices.parse_snmp_metrics(b"")["series"], 0)

    def test_clean_exporter_url(self):
        self.assertEqual(devices.clean_exporter_url("snmp-exporter:9116/")[0], "http://snmp-exporter:9116")
        self.assertEqual(devices.clean_exporter_url("https://x.example/snmp-base/")[0], "https://x.example/snmp-base")
        for bad in ("", "ftp://x", "http://u:p@x", "http://x?y=1", "http://" + "a" * 400):
            self.assertTrue(devices.clean_exporter_url(bad)[1], bad)


@with_key
class SnmpRbacTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(kind="device", step="credentials")
        self.sw = self.make_target(self.run_, "sw-core-01", target_type=TargetType.DEVICE)

    def post_all(self):
        out = []
        for name, data in (("snmp_save", {"name": "n", "version": "v2c", "community": COMMUNITY, "module": "if_mib"}),
                           ("snmp_test", {}), ("snmp_exporter", {"exporter_url": "http://snmp-exporter:9116"})):
            out.append(self.c.post(reverse(f"onboarding:{name}", args=[self.run_.pk]), data).status_code)
        return out

    def test_viewers_operators_and_admins_of_other_sites_get_403_and_nothing_changes(self):
        for user in (self.viewer, self.operator, self.admin_b):
            self.login(user)
            want = 404 if user is self.admin_b else 403          # another site's run is not even visible
            self.assertEqual(self.post_all(), [want] * 3, user.username)
        self.assertEqual(Credential.objects.count(), 0)
        self.assertEqual(ic.has_override("snmp_exporter"), False)
        self.assertEqual(StepLog.objects.filter(step="verify").count(), 0)

    def test_get_is_not_allowed(self):
        self.login(self.admin_a)
        for name in ("snmp_save", "snmp_test", "snmp_exporter"):
            self.assertEqual(self.c.get(reverse(f"onboarding:{name}", args=[self.run_.pk])).status_code, 405)

    def test_anonymous_is_sent_to_login(self):
        resp = self.c.post(reverse("onboarding:snmp_save", args=[self.run_.pk]), {})
        self.assertIn(resp.status_code, (302, 401, 403))
        self.assertEqual(Credential.objects.count(), 0)

    def test_the_exporter_address_is_tenant_wide_so_a_site_admin_cannot_set_it(self):
        self.login(self.admin_a)
        resp = self.c.post(reverse("onboarding:snmp_exporter", args=[self.run_.pk]),
                           {"exporter_url": "http://snmp-exporter:9116"})
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(ic.has_override("snmp_exporter"))
        self.login_root()
        resp = self.c.post(reverse("onboarding:snmp_exporter", args=[self.run_.pk]),
                           {"exporter_url": "http://snmp-exporter:9116"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(devices.exporter_url(), "http://snmp-exporter:9116")
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.snmp.exporter").exists())

    def test_a_bad_exporter_address_is_rejected_with_a_reason(self):
        self.login_root()
        resp = self.c.post(reverse("onboarding:snmp_exporter", args=[self.run_.pk]),
                           {"exporter_url": "http://user:pw@host"}, follow=True)
        self.assertIn("Do not put a login in the address", resp.content.decode())
        self.assertFalse(ic.has_override("snmp_exporter"))

    def test_csrf_is_enforced(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.login(username="admin-a", password="onboarding-test-pass-123")
        resp = strict.post(reverse("onboarding:snmp_save", args=[self.run_.pk]), {"name": "n"})
        self.assertEqual(resp.status_code, 403)


# ============================================================================= Prometheus
class PrometheusUrlTests(OnboardingTestCase):
    def test_normalize(self):
        n = prometheus_step.normalize_url
        self.assertEqual(n("prom.example.test:9090"), ("https://prom.example.test:9090", ""))
        self.assertEqual(n("http://prom.example.test:9090/"), ("http://prom.example.test:9090", ""))
        self.assertEqual(n("https://x.example/prometheus/"), ("https://x.example/prometheus", ""))
        self.assertEqual(n("http://[2001:db8::1]:9090"), ("http://[2001:db8::1]:9090", ""))
        for bad in ("", "ftp://x", "http://u:p@x", "http://x?a=1", "http://x#f", "http://" + "a" * 400,
                    "http://x:99999", "javascript:alert(1)"):
            self.assertTrue(n(bad)[1], bad)

    def test_redact_drops_login_query_and_fragment(self):
        self.assertEqual(prometheus_step.redact("https://u:p@h.example:9090/p?q=1#f"), "https://h.example:9090/p")
        self.assertEqual(prometheus_step.redact("garbage"), "")


@with_key
class PrometheusOverviewTests(NetCase):
    URL = "https://prom.example.test:9090"

    def routes(self, build=None, targets=None):
        self.net.route("prom.example.test", "/api/v1/status/buildinfo", **(build or {"body": BUILDINFO}))
        self.net.route("prom.example.test", "/api/v1/targets", **(targets or {"body": targets_doc()}))

    def test_counts_targets_hosts_jobs_and_versions(self):
        self.routes()
        out = prometheus_step.fetch_overview(self.URL)
        self.assertTrue(out["ok"])
        self.assertEqual((out["version"], out["revision"], out["go_version"]), ("2.53.0", "abcdef012345", "go1.22"))
        self.assertEqual((out["targets"], out["up"], out["down"]), (5, 4, 1))
        self.assertEqual(out["hosts"], 4)                                  # web-0 is one host with two jobs
        self.assertEqual(dict(out["jobs"]), {"node": 4, "blackbox": 1})
        self.assertEqual([s["name"] for s in out["stages"]], ["buildinfo", "targets"])
        self.assertTrue(all(s["ok"] for s in out["stages"]))

    def test_both_documented_endpoints_are_asked_under_the_policy(self):
        self.routes()
        with mock.patch.object(checks, "resolve_allowed", wraps=checks.resolve_allowed) as policy:
            prometheus_step.fetch_overview(self.URL)
        self.assertEqual(policy.call_count, 2)
        self.assertEqual([c["path"].split("?")[0] for c in self.net.calls],
                         ["/api/v1/status/buildinfo", "/api/v1/targets"])
        self.assertEqual({c["ip"] for c in self.net.calls}, {"203.0.113.30"})

    def test_basic_auth_is_sent_and_never_returned(self):
        self.routes()
        out = prometheus_step.fetch_overview(self.URL, "ops", PROM_PASS)
        sent = self.net.calls[0]["headers"]["Authorization"]
        self.assertEqual(sent, prometheus_step.basic_header("ops", PROM_PASS))
        self.assertTrue(out["authenticated"])
        self.assertNotIn(PROM_PASS, json.dumps(out, default=str))
        self.assertNotIn(sent, json.dumps(out, default=str))

    def test_without_a_login_no_authorization_header_is_sent(self):
        self.routes()
        prometheus_step.fetch_overview(self.URL)
        self.assertTrue(all("Authorization" not in c["headers"] for c in self.net.calls))

    def test_a_refused_login_stops_after_the_first_endpoint(self):
        self.routes(build={"status": 401, "body": b"nope"})
        out = prometheus_step.fetch_overview(self.URL, "ops", "wrong")
        self.assertFalse(out["ok"])
        self.assertIn("refused the login (HTTP 401)", out["errors"][0])
        self.assertIn("user name and password", out["errors"][0])
        self.assertEqual(len(self.net.calls), 1)

    def test_missing_login_is_explained(self):
        self.routes(build={"status": 401, "body": b""})
        out = prometheus_step.fetch_overview(self.URL)
        self.assertIn("It needs a login", out["errors"][0])

    def test_an_old_prometheus_without_buildinfo_is_accepted_with_a_warning(self):
        self.routes(build={"status": 404, "body": b"404 page not found"})
        out = prometheus_step.fetch_overview(self.URL)
        self.assertTrue(out["ok"])
        self.assertEqual(out["version"], "")
        self.assertTrue(any("old Prometheus" in w for w in out["warnings"]))

    def test_not_a_prometheus_document(self):
        self.routes(build={"body": {"hello": "world"}})
        out = prometheus_step.fetch_overview(self.URL)
        self.assertFalse(out["ok"])
        self.assertIn("did not return a Prometheus document", out["errors"][0])

    def test_targets_that_are_not_a_target_list(self):
        self.routes(targets={"body": {"status": "success", "data": {"nope": 1}}})
        out = prometheus_step.fetch_overview(self.URL)
        self.assertFalse(out["ok"])
        self.assertIn("did not return a target list", out["errors"][0])

    def test_targets_that_are_not_json(self):
        self.routes(targets={"body": "<html>"})
        self.assertFalse(prometheus_step.fetch_overview(self.URL)["ok"])

    def test_an_oversized_target_list_is_refused(self):
        self.routes()
        with mock.patch.object(prometheus_step, "MAX_TARGETS_BYTES", 100):
            out = prometheus_step.fetch_overview(self.URL)
        self.assertFalse(out["ok"])
        self.assertIn("larger than 8 MB", out["errors"][0])

    def test_unreachable_and_policy_and_dns_failures_are_results_not_exceptions(self):
        self.net.route("prom.example.test", "/api", error=ConnectionRefusedError())
        self.assertIn("Connection refused", prometheus_step.fetch_overview(self.URL)["errors"][0])
        self.assertIn("PROBE_ALLOWED_PRIVATE_CIDRS", prometheus_step.fetch_overview("http://internal-sw:9090")["errors"][0])
        self.assertIn("does not resolve", prometheus_step.fetch_overview("http://nowhere.test:9090")["errors"][0])

    def test_a_redirect_is_not_followed_so_a_login_cannot_travel(self):
        self.net.route("prom.example.test", "/api", 302, b"")
        out = prometheus_step.fetch_overview(self.URL, "ops", PROM_PASS)
        self.assertFalse(out["ok"])
        self.assertIn("Redirects are not followed", out["errors"][0])
        self.assertEqual(len(self.net.calls), 1)

    def test_known_in_product_counts_matching_items(self):
        ConfigurationItem.objects.create(name="web-0", site="Mumbai", prometheus_instance="web-0:9100")
        ConfigurationItem.objects.create(name="other", site="Chennai", prometheus_instance="web-1:9100")
        self.routes()
        out = prometheus_step.fetch_overview(self.URL)
        self.assertEqual(prometheus_step.known_in_product(out)["ci"], 2)
        self.assertEqual(prometheus_step.known_in_product(out, self.admin_a)["ci"], 1)   # site-scoped to the user


@with_key
class PrometheusConnectTests(NetCase):
    URL = "https://prom.example.test:9090"

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(kind="prometheus", step="targets")
        self.net.route("prom.example.test", "/api/v1/status/buildinfo", body=BUILDINFO)
        self.net.route("prom.example.test", "/api/v1/targets", body=targets_doc())
        self.login_root()

    def post(self, action="connect", **extra):
        data = {"url": self.URL, "username": "ops", "password": PROM_PASS, "action": action, **extra}
        return self.c.post(reverse("onboarding:prometheus_connect", args=[self.run_.pk]), data)

    def test_test_connection_reports_counts_and_writes_nothing(self):
        resp = self.post("test")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Prometheus answered", body)
        self.assertIn("targets seen", body)
        self.assertIn("2.53.0", body)
        self.assertFalse(ic.prometheus_url())
        self.assertEqual(Credential.objects.count(), 0)
        self.assertEqual(self.run_.targets.count(), 0)
        self.assertNotIn(PROM_PASS, body)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.prometheus.test").exists())

    def test_verify_and_connect_saves_through_integration_config_and_the_vault(self):
        resp = self.post()
        self.assertRedirects(resp, self.url(self.run_, "targets"))
        self.assertEqual(ic.prometheus_url(), self.URL)
        cred = Credential.objects.get()
        self.assertEqual((cred.kind, cred.username, cred.site), (CredentialKind.BASIC_AUTH, "ops", ""))   # tenant-wide: no site's credentials step can reach it
        self.assertNotIn(PROM_PASS, cred.secret)
        self.assertEqual(ic.prometheus_credential_id(), cred.pk)
        target = self.run_.targets.get()
        self.assertEqual((target.hostname, target.target_type, target.port, target.state),
                         ("prom.example.test", TargetType.PROMETHEUS, 9090, TargetState.EXPORTER_FOUND))
        self.assertEqual(target.credential_id, cred.pk)
        facts = target.facts["prometheus"]
        self.assertEqual((facts["targets"], facts["up"], facts["down"], facts["hosts"]), (5, 4, 1, 4))
        self.assertEqual(target.facts["exporter_version"], "2.53.0")
        self.assert_no_secret(PROM_PASS, response=resp)
        page = self.c.get(self.url(self.run_, "targets")).content.decode()
        self.assertIn("What Prometheus already sees", page)
        self.assertNotIn(PROM_PASS, page)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.prometheus.connect").exists())

    def test_the_settings_keep_only_the_vault_row_id_never_the_password(self):
        from administration.models import IntegrationSetting
        self.post()
        values = IntegrationSetting.objects.get(slug="prometheus").values
        self.assertEqual(set(values), {"url", "credential_id", "credential_url"})
        self.assertNotIn(PROM_PASS, json.dumps(values))

    def test_connecting_without_a_login_works_and_stores_no_credential(self):
        resp = self.post(username="", password="")
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(Credential.objects.count(), 0)
        self.assertIsNone(ic.prometheus_credential_id())
        self.assertTrue(all("Authorization" not in c["headers"] for c in self.net.calls))

    def test_a_failed_check_saves_nothing(self):
        self.net.routes.clear()
        self.net.route("prom.example.test", "/api", 401, b"")
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("did not pass", resp.content.decode())
        self.assertFalse(ic.prometheus_url())
        self.assertEqual(Credential.objects.count(), 0)
        self.assertEqual(self.run_.targets.count(), 0)
        self.assertNotIn(PROM_PASS, resp.content.decode())

    def test_the_form_never_echoes_the_password_after_an_error(self):
        resp = self.post(url="ftp://nope")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("http:// or https://", resp.content.decode())
        self.assertNotIn(PROM_PASS, resp.content.decode())

    def test_a_password_without_a_user_and_a_user_without_a_password_are_refused(self):
        self.assertIn("needs the user name", self.post(username="").content.decode())
        self.assertIn("Enter the password", self.post(password="").content.decode())
        self.assertFalse(ic.prometheus_url())

    def test_a_different_connected_prometheus_is_only_replaced_when_ticked(self):
        ic.save("prometheus", {"url": "https://old.example.test:9090"}, self.root)
        resp = self.post()
        self.assertEqual(ic.prometheus_url(), "https://old.example.test:9090")
        self.assertIn("Tick “Replace", resp.content.decode())
        self.assertEqual(Credential.objects.count(), 0)
        self.post(replace="on")
        self.assertEqual(ic.prometheus_url(), self.URL)

    def test_reconnecting_the_same_prometheus_rotates_the_login_in_place(self):
        self.post()
        first = Credential.objects.get()
        self.post(password="another-pass-9981")
        self.assertEqual(Credential.objects.count(), 1)
        cred = Credential.objects.get()
        self.assertEqual(cred.pk, first.pk)
        self.assertIsNotNone(cred.rotated_at)
        self.assertEqual(self.run_.targets.count(), 1)                  # idempotent: no second target

    def test_connecting_without_a_login_drops_the_one_that_was_bound(self):
        self.post()
        self.post(username="", password="")
        self.assertEqual(Credential.objects.count(), 0)
        self.assertIsNone(ic.prometheus_credential_id())

    @without_key
    def test_without_the_vault_key_a_login_is_not_saved_and_neither_is_the_url(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", resp.content.decode())
        self.assertFalse(ic.prometheus_url())
        self.assertEqual(Credential.objects.count(), 0)
        self.assertNotIn(PROM_PASS, resp.content.decode())

    @without_key
    def test_without_the_vault_key_no_login_is_needed_to_connect(self):
        self.assertEqual(self.post(username="", password="").status_code, 302)
        self.assertEqual(ic.prometheus_url(), self.URL)

    def test_autocreate_turns_on_discovery_flags_and_discovery_can_run_now(self):
        fake = mock.Mock(ok=True, targets=5, new=5, linked=0, created=4, error="", summary="5 targets, 4 created")
        with mock.patch("telemetry.discovery.run", return_value=fake) as run:
            self.post(autocreate="on", run_discovery="on")
        run.assert_called_once_with(trigger="manual")
        self.assertTrue(ic.prometheus_autocreate_ci())
        self.assertTrue(ic.prometheus_autocreate_devices())
        self.assertTrue(ic.prometheus_autoregister_exporters())
        facts = self.run_.targets.get().facts["prometheus"]
        self.assertEqual(facts["discovery"]["created"], 4)
        self.assertIn("Discovery just ran", self.c.get(self.url(self.run_, "targets")).content.decode())

    def test_without_the_ticks_discovery_is_not_touched(self):
        with mock.patch("telemetry.discovery.run") as run:
            self.post()
        run.assert_not_called()
        self.assertFalse(ic.prometheus_autocreate_ci())

    def test_a_discovery_crash_does_not_break_the_step_and_is_not_echoed(self):
        with mock.patch("telemetry.discovery.run", side_effect=RuntimeError("boom " + PROM_PASS)):
            resp = self.post(run_discovery="on")
        self.assertEqual(resp.status_code, 302)
        facts = self.run_.targets.get().facts["prometheus"]
        self.assertEqual(facts["discovery"], {"ok": False, "error": "discovery failed (RuntimeError)"})
        self.assert_no_secret(PROM_PASS)

    def test_the_page_shows_what_is_already_in_the_product(self):
        ConfigurationItem.objects.create(name="web-0", site="Mumbai", prometheus_instance="web-0:9100")
        page = self.post("test").content.decode()
        self.assertIn("Already in LinkedEye", page)
        self.assertIn("<b>1</b> CMDB item(s)", page)


@with_key
class PrometheusVerifyHookTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(kind="prometheus")
        self.net.route("prom.example.test", "/api/v1/status/buildinfo", body=BUILDINFO)
        self.net.route("prom.example.test", "/api/v1/targets", body=targets_doc())

    def test_the_hook_uses_the_vault_login_and_reports_the_overview(self):
        cred = secrets.store(CredentialKind.BASIC_AUTH, "ops", PROM_PASS, "Mumbai", self.admin_a, name="p")
        t = self.make_target(self.run_, "prom.example.test", target_type=TargetType.PROMETHEUS, port=9090,
                             scheme="https", credential=cred)
        result = prometheus_step.verify_prometheus(t)
        self.assertEqual((result["state"], result["error"]), (TargetState.EXPORTER_FOUND, ""))
        self.assertEqual(result["prometheus"]["targets"], 5)
        self.assertEqual(self.net.calls[0]["headers"]["Authorization"], prometheus_step.basic_header("ops", PROM_PASS))
        self.assertNotIn(PROM_PASS, json.dumps(result))
        self.assertTrue(AuditEvent.objects.filter(action="credential.use").exists())

    def test_a_refused_login_is_a_result_not_an_exception(self):
        self.net.routes.clear()
        self.net.route("prom.example.test", "/api", 401, b"")
        t = self.make_target(self.run_, "prom.example.test", target_type=TargetType.PROMETHEUS, port=9090)
        result = prometheus_step.verify_prometheus(t)
        self.assertEqual(result["state"], TargetState.UNREACHABLE)
        self.assertIn("refused the login", result["error"])

    @without_key
    def test_a_login_that_cannot_be_decrypted_fails_with_the_vault_reason(self):
        cred = Credential.objects.create(name="p", kind="basic_auth", username="ops", secret="not-a-token", site="Mumbai")
        t = self.make_target(self.run_, "prom.example.test", target_type=TargetType.PROMETHEUS, port=9090, credential=cred)
        result = prometheus_step.verify_prometheus(t)
        self.assertEqual(result["state"], TargetState.FAILED)
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", result["error"])


@with_key
class PrometheusRbacTests(NetCase):
    URL = PrometheusConnectTests.URL

    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(kind="prometheus", step="targets")
        self.net.route("prom.example.test", "/api/v1/status/buildinfo", body=BUILDINFO)
        self.net.route("prom.example.test", "/api/v1/targets", body=targets_doc())

    def post(self, action="connect"):
        return self.c.post(reverse("onboarding:prometheus_connect", args=[self.run_.pk]),
                           {"url": self.URL, "username": "ops", "password": PROM_PASS, "action": action})

    def test_viewers_operators_and_other_sites_admins_get_403(self):
        for user in (self.viewer, self.operator, self.admin_b):
            self.login(user)
            self.assertEqual(self.post().status_code, 404 if user is self.admin_b else 403, user.username)
        self.assertFalse(ic.prometheus_url())
        self.assertEqual(self.net.calls, [])                              # not even a probe

    def test_a_site_admin_may_test_but_not_save_a_prometheus_every_site_shares(self):
        self.login(self.admin_a)
        resp = self.post("test")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Prometheus answered", resp.content.decode())
        self.assertIn("only an administrator for all sites can save it", resp.content.decode())
        resp = self.post("connect")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("needs the admin role on every site", resp.content.decode())
        self.assertFalse(ic.prometheus_url())
        self.assertEqual(Credential.objects.count(), 0)
        self.assertEqual(self.run_.targets.count(), 0)

    def test_get_is_not_allowed(self):
        self.login(self.admin_a)
        self.assertEqual(self.c.get(reverse("onboarding:prometheus_connect", args=[self.run_.pk])).status_code, 405)

    def test_csrf_is_enforced(self):
        from django.test import Client
        strict = Client(enforce_csrf_checks=True)
        strict.login(username="admin-a", password="onboarding-test-pass-123")
        resp = strict.post(reverse("onboarding:prometheus_connect", args=[self.run_.pk]), {"url": self.URL})
        self.assertEqual(resp.status_code, 403)

    def test_the_other_tenant_or_unseen_run_is_a_404(self):
        self.login(self.admin_b)
        other = OnboardingRun.objects.create(site="Mumbai", kind="prometheus")
        resp = self.c.post(reverse("onboarding:prometheus_connect", args=[other.pk]), {"url": self.URL})
        self.assertEqual(resp.status_code, 404)


# ============================================================================= core.prometheus uses the login
@with_key
class ProductSendsTheStoredLoginTests(NetCase):
    URL = "https://prom.example.test:9090"

    class Resp:
        def __init__(self, body=b'{"status":"success","data":{"result":[]}}'):
            self._b = body

        def read(self, *a):
            return self._b

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def setUp(self):
        super().setUp()
        prometheus._auth_memo.clear()
        self.addCleanup(prometheus._auth_memo.clear)
        self.cred = secrets.store(CredentialKind.BASIC_AUTH, "ops", PROM_PASS, "Mumbai", self.admin_a, name="prom")
        ic.save("prometheus", {"url": self.URL, "credential_id": str(self.cred.pk), "credential_url": self.URL},
                self.admin_a)

    def query(self):
        sent = []

        def fake(request, timeout=None):
            sent.append(request)
            return self.Resp()
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            prometheus.query("up")
        return sent[0]

    def test_the_header_is_attached_unredirected_and_decrypted_from_the_vault(self):
        req = self.query()
        self.assertEqual(req.get_header("Authorization"), prometheus_step.basic_header("ops", PROM_PASS))
        self.assertNotIn("Authorization", req.headers)                   # an unredirected header is not copied on redirect
        self.assertIn("Authorization", req.unredirected_hdrs)

    def test_the_vault_read_is_audited_once_a_minute_not_per_query(self):
        for _ in range(5):
            self.query()
        self.assertEqual(AuditEvent.objects.filter(action="credential.use").count(), 1)

    def test_changing_the_url_stops_the_login_following_it_to_another_server(self):
        ic.save("prometheus", {"url": "https://elsewhere.example.test:9090"}, self.admin_a)
        self.assertIsNone(ic.prometheus_credential_id())
        self.assertIsNone(self.query().get_header("Authorization"))

    def test_no_login_means_no_header(self):
        ic.save("prometheus", {"credential_id": "", "credential_url": ""}, self.admin_a)
        self.assertIsNone(self.query().get_header("Authorization"))

    def test_a_deleted_login_is_a_clear_error_not_a_crash(self):
        self.cred.delete()
        with self.assertRaises(prometheus.PrometheusError) as ctx:
            prometheus.query("up")
        self.assertIn("connect Prometheus again", str(ctx.exception))

    def test_a_missing_vault_key_is_a_clear_error_that_does_not_contain_the_key(self):
        with override_settings(LINKEDEYE_CREDENTIAL_KEY=""):
            prometheus._auth_memo.clear()
            with self.assertRaises(prometheus.PrometheusError) as ctx:
                prometheus.query("up")
        self.assertIn("LINKEDEYE_CREDENTIAL_KEY", str(ctx.exception))
        self.assertNotIn(PROM_PASS, str(ctx.exception))


# ============================================================================= targets intake
@with_key
class SingleTargetTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="targets")
        self.login(self.admin_a)
        self.url_ = reverse("onboarding:targets_single", args=[self.run_.pk])

    def test_adds_one_host_with_the_standard_port_for_its_type(self):
        resp = self.c.post(self.url_, {"hostname": "DB-01.Example.Test", "target_type": "windows"})
        self.assertRedirects(resp, self.url(self.run_, "targets"))
        t = self.run_.targets.get()
        self.assertEqual((t.hostname, t.target_type, t.port, t.state), ("db-01.example.test", "windows", 9182, "new"))
        self.assertEqual(t.exporter_kind, "windows_exporter")

    def test_address_and_port_are_kept_and_a_repeat_updates_instead_of_duplicating(self):
        self.c.post(self.url_, {"hostname": "sw-1", "address": "203.0.113.5", "target_type": "device", "port": "9116"})
        self.c.post(self.url_, {"hostname": "sw-1", "address": "203.0.113.6", "target_type": "device", "port": "9117"})
        t = self.run_.targets.get()
        self.assertEqual((t.address, t.port), ("203.0.113.6", 9117))

    def test_bad_names_are_rejected_before_they_can_reach_promql_yaml_or_a_shell(self):
        for bad in ('web";drop', "a b", "x`id`", "$(id)", "a\nb", "../etc", ""):
            resp = self.c.post(self.url_, {"hostname": bad, "target_type": "linux"})
            self.assertEqual(resp.status_code, 200, repr(bad))
        resp = self.c.post(self.url_, {"hostname": "ok", "address": "bad addr", "target_type": "linux"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.run_.targets.count(), 0)

    def test_port_range_and_type_are_validated(self):
        for data in ({"hostname": "a", "target_type": "linux", "port": "0"},
                     {"hostname": "a", "target_type": "linux", "port": "70000"},
                     {"hostname": "a", "target_type": "toaster"}):
            self.assertEqual(self.c.post(self.url_, data).status_code, 200)
        self.assertEqual(self.run_.targets.count(), 0)

    def test_the_run_limit_is_enforced(self):
        with mock.patch.object(services, "MAX_TARGETS_PER_RUN", 1):
            self.c.post(self.url_, {"hostname": "a", "target_type": "linux"})
            resp = self.c.post(self.url_, {"hostname": "b", "target_type": "linux"}, follow=True)
        self.assertEqual(self.run_.targets.count(), 1)
        self.assertIn("at most 1 targets", resp.content.decode())

    def test_audited_and_logged(self):
        self.c.post(self.url_, {"hostname": "a", "target_type": "linux"})
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.targets.add").exists())
        self.assertTrue(StepLog.objects.filter(run=self.run_, step="targets").exists())

    def test_rbac_and_method(self):
        for user in (self.viewer, self.operator, self.admin_b):
            self.login(user)
            self.assertEqual(self.c.post(self.url_, {"hostname": "a", "target_type": "linux"}).status_code,
                             404 if user is self.admin_b else 403)
        self.assertEqual(self.run_.targets.count(), 0)
        self.login(self.admin_a)
        self.assertEqual(self.c.get(self.url_).status_code, 405)


@with_key
class PreviewTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="targets")
        self.make_target(self.run_, "existing-01", port=9100)
        self.make_target(self.run_, "changed-01", port=9100)
        self.login(self.admin_a)
        self.url_ = reverse("onboarding:targets_preview", args=[self.run_.pk])

    def preview(self, hosts="", csv=None, **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {"hosts": hosts, "target_type": "linux", **extra}
        if csv is not None:
            data["csv_file"] = SimpleUploadedFile("hosts.csv", csv if isinstance(csv, bytes) else csv.encode(),
                                                  content_type="text/csv")
        return self.c.post(self.url_, data)

    def test_a_dry_run_classifies_every_row_and_writes_nothing(self):
        before = (self.run_.targets.count(), StepLog.objects.count())
        text = "new-01\nnew-02:9101\nexisting-01\nchanged-01, linux, 9200\nnew-01\nbad host!!\n10.0.3.14, device"
        resp = self.preview(text)
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertIn("Dry run: nothing has been saved", body)
        result = resp.context["preview"]
        self.assertEqual(result["counts"], {"add": 3, "update": 1, "same": 1, "over": 0})
        self.assertEqual(result["n_bad"], 1)
        self.assertEqual(result["lines"], 7)
        self.assertIn("bad host!!", body)
        self.assertEqual((self.run_.targets.count(), StepLog.objects.count()), before)
        self.assertEqual(AuditEvent.objects.filter(action="onboarding.targets.add").count(), 0)
        self.assertTrue(AuditEvent.objects.filter(action="onboarding.targets.preview").exists())

    def test_confirming_adds_exactly_what_the_preview_showed(self):
        resp = self.preview("new-01\nnew-02:9101, windows\nsw-9, device\nchanged-01, linux, 9200")
        confirm = resp.context["preview"]["confirm_text"]
        self.assertTrue(confirm.startswith("hostname,address,type,port\n"))
        self.assertIn(f'<textarea name="hosts" hidden>{confirm}</textarea>', resp.content.decode())
        self.c.post(self.url(self.run_, "targets"), {"action": "add", "hosts": confirm})
        got = {t.hostname: (t.target_type, t.port) for t in self.run_.targets.all()}
        self.assertEqual(got, {"existing-01": ("linux", 9100), "changed-01": ("linux", 9200), "new-01": ("linux", 9100),
                               "new-02": ("windows", 9101),
                               "sw-9": ("device", 9116)})

    def test_a_csv_with_a_header_row_in_any_order(self):
        resp = self.preview(csv="port,type,hostname,address\n9116,device,sw-a,203.0.113.7\n9100,linux,srv-a,\n")
        rows = {r["hostname"]: r for r in resp.context["preview"]["rows"]}
        self.assertEqual(rows["sw-a"]["address"], "203.0.113.7")
        self.assertEqual((rows["sw-a"]["target_type"], rows["sw-a"]["port"]), ("device", 9116))
        self.assertEqual(rows["srv-a"]["action"], "add")

    def test_paste_and_csv_are_merged_without_duplicates(self):
        resp = self.preview("dup-01\nonly-paste", csv="hostname\ndup-01\nonly-csv\n")
        self.assertEqual(sorted(r["hostname"] for r in resp.context["preview"]["rows"]),
                         ["dup-01", "only-csv", "only-paste"])

    def test_an_oversized_upload_is_refused_with_the_limit_not_truncated(self):
        big = ("h%05d\n" % 1) * (services.MAX_PASTE_BYTES // 7 + 10)
        resp = self.preview(csv=big)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("the limit is 256 KB", resp.content.decode())
        self.assertNotIn("preview", resp.context)
        self.assertEqual(self.run_.targets.count(), 2)

    def test_a_binary_upload_is_refused(self):
        resp = self.preview(csv=b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
        self.assertIn("does not look like a text or CSV file", resp.content.decode())

    def test_an_empty_upload_and_an_empty_form(self):
        self.assertIn("empty or is not readable", self.preview(csv="  \n ").content.decode())
        self.assertIn("Paste at least one host", self.preview("").content.decode())

    def test_an_oversized_paste_is_refused(self):
        resp = self.preview("x\n" * (services.MAX_PASTE_BYTES // 2 + 10))
        self.assertIn("too large", resp.content.decode())

    def test_rows_over_the_run_limit_are_marked_and_not_confirmed(self):
        with mock.patch.object(services, "MAX_TARGETS_PER_RUN", 3):
            resp = self.preview("n-1\nn-2\nn-3")
        result = resp.context["preview"]
        self.assertEqual(result["counts"], {"add": 1, "update": 0, "same": 0, "over": 2})
        self.assertEqual(result["confirm_text"].strip().splitlines()[1:], ["n-1,,linux,9100"])

    def test_nothing_to_do_offers_no_confirm_button(self):
        resp = self.preview("existing-01")
        self.assertNotIn("Confirm: add", resp.content.decode())

    def test_hostnames_are_html_escaped_in_the_preview(self):
        resp = self.preview("<script>alert(1)</script>\nok-1")
        body = resp.content.decode()
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_formula_like_cells_cannot_become_hosts(self):
        resp = self.preview("=cmd|' /C calc'!A0\n+1+1\n@SUM(A1)\nok-1")
        self.assertEqual([r["hostname"] for r in resp.context["preview"]["rows"]], ["ok-1"])

    def test_rbac_and_method(self):
        for user in (self.viewer, self.operator, self.admin_b):
            self.login(user)
            self.assertEqual(self.preview("a").status_code, 404 if user is self.admin_b else 403, user.username)
        self.login(self.admin_a)
        self.assertEqual(self.c.get(self.url_).status_code, 405)


# ============================================================================= pages
@with_key
class PageTests(NetCase):
    def setUp(self):
        super().setUp()
        self.run_ = self.make_run(step="scope")
        self.login(self.admin_a)

    def test_every_step_page_renders_for_admin_and_viewer(self):
        for kind in ("site", "server", "device", "prometheus", "bulk"):
            run = self.make_run(kind=kind)
            self.make_target(run, "h-1", target_type=TargetType.DEVICE if kind == "device" else TargetType.LINUX)
            for user in (self.admin_a, self.viewer):
                self.login(user)
                for step in ("scope", "targets", "credentials"):
                    resp = self.c.get(self.url(run, step))
                    self.assertEqual(resp.status_code, 200, (kind, step, user.username))

    def test_the_scope_page_offers_every_kind_and_the_create_site_hint(self):
        page = self.c.get(self.url(self.run_, "scope")).content.decode()
        for kind in ("site", "server", "device", "prometheus", "bulk"):
            self.assertIn(f'name="kind" value="{kind}"', page)
        self.assertIn("An administrator for all sites creates sites", page)
        self.login_root()
        self.assertIn("Create a site first", self.c.get(self.url(self.run_, "scope")).content.decode())

    def test_the_selected_kind_is_checked(self):
        run = self.make_run(kind="device")
        page = self.c.get(self.url(run, "scope")).content.decode()
        self.assertIn('value="device" checked', page)

    def test_the_targets_page_has_single_paste_csv_and_preview(self):
        page = self.c.get(self.url(self.run_, "targets")).content.decode()
        self.assertIn("Add one host", page)
        self.assertIn("Paste a list or upload a CSV", page)
        self.assertIn("Preview (dry run)", page)
        self.assertIn(reverse("onboarding:targets_preview", args=[self.run_.pk]), page)
        self.assertNotIn("Connect Prometheus", page)

    def test_a_prometheus_run_shows_the_connect_panel_and_no_password_value(self):
        run = self.make_run(kind="prometheus")
        page = self.c.get(self.url(run, "targets")).content.decode()
        self.assertIn("Connect Prometheus", page)
        self.assertIn('type="password"', page)
        self.assertNotIn('type="password" value', page)

    def test_a_viewer_sees_no_forms(self):
        self.login(self.viewer)
        page = self.c.get(self.url(self.run_, "targets")).content.decode()
        self.assertNotIn("Add one host", page)
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        self.assertNotIn("Hand over a login", page)

    def test_the_generic_credential_form_does_not_offer_snmp_types(self):
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        self.assertNotIn('value="snmp_v3"', page)
        self.assertIn('value="ssh_password"', page)

    def test_a_credential_can_be_attached_to_a_single_target_and_detached(self):
        t1 = self.make_target(self.run_, "a-1")
        t2 = self.make_target(self.run_, "a-2")
        cred = secrets.store(CredentialKind.SSH_PASSWORD, "svc", "pw-9988-zz", "Mumbai", self.admin_a, name="ssh")
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        self.assertIn(f'<option value="{cred.pk}">ssh</option>', page)
        self.c.post(self.url(self.run_, "credentials"), {"action": "attach", "credential": cred.pk, "targets": t1.pk})
        t1.refresh_from_db()
        t2.refresh_from_db()
        self.assertEqual((t1.credential_id, t2.credential_id), (cred.pk, None))
        self.c.post(self.url(self.run_, "credentials"), {"action": "detach", "target": t1.pk})
        t1.refresh_from_db()
        self.assertIsNone(t1.credential_id)

    def test_snmp_credentials_are_only_offered_for_devices(self):
        dev = self.make_target(self.run_, "sw", target_type=TargetType.DEVICE)
        srv = self.make_target(self.run_, "srv")
        secrets.store(CredentialKind.SNMP_V2C, "", COMMUNITY, "Mumbai", self.admin_a, name="snmp-c")
        secrets.store(CredentialKind.SSH_PASSWORD, "svc", "pw-9988-zz", "Mumbai", self.admin_a, name="ssh-c")
        page = self.c.get(self.url(self.run_, "credentials")).content.decode()
        by_row = {name: row for name, row in ((n, page.rsplit(f"<b class=\"ob-wrap\">{n}</b>", 1)[1].split("</tr>", 1)[0])
                                              for n in ("sw", "srv"))}
        self.assertIn("snmp-c", by_row["sw"])
        self.assertNotIn("ssh-c", by_row["sw"])
        self.assertIn("ssh-c", by_row["srv"])
        self.assertNotIn("snmp-c", by_row["srv"])
        self.assertNotIn(COMMUNITY, page)
        self.assertEqual((dev.credential_id, srv.credential_id), (None, None))


# ============================================================================= no real sockets
class NoSocketTests(NetCase):
    def test_a_real_connection_attempt_fails_the_test(self):
        import socket
        with self.assertRaises(AssertionError):
            socket.create_connection(("203.0.113.9", 80))

    def test_probing_never_resolves_through_the_real_resolver(self):
        run = self.make_run(kind="prometheus")
        self.login_root()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        resp = self.c.post(reverse("onboarding:prometheus_connect", args=[run.pk]),
                           {"url": "https://unknown.example.test", "action": "test"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("does not resolve", resp.content.decode())


class ModuleSurfaceTests(OnboardingTestCase):
    def test_nothing_in_the_new_modules_logs_or_formats_a_secret_value(self):
        import inspect
        for module in (devices, prometheus_step, intake):
            src = inspect.getsource(module)
            self.assertNotIn("log.info(", src)
            self.assertNotIn("print(", src)
            self.assertNotIn("logger.", src)
