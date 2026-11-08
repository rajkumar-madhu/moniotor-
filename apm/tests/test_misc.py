"""Seed command, the reference client (also end to end against a live server), model conventions and the migration."""
import importlib.util
import io
import json
import logging
import ssl
import urllib.error
from pathlib import Path
from unittest import mock

from django.apps import apps
from django.core.management import call_command
from django.db import connection, models
from django.test import LiveServerTestCase, SimpleTestCase, TestCase, TransactionTestCase, override_settings

from apm import services
from apm.models import AdapterCell, AdapterRun, CellEvent, ExecLog, ProcessState, Source
from core.models import Event
from servicedesk.models import Ticket

from .base import ApmCase, matrix, table

CLIENT_PATH = Path(__file__).resolve().parent.parent / "clients" / "linkedeye_apm.py"


def load_client():
    spec = importlib.util.spec_from_file_location("linkedeye_apm_under_test", CLIENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Seed(TestCase):
    def run_seed(self, *args):
        out = io.StringIO()
        call_command("seed_apm", *args, stdout=out, stderr=out)
        return out.getvalue()

    def counts(self):
        return tuple(m.objects.count() for m in (Source, AdapterCell, AdapterRun, CellEvent, ProcessState, ExecLog))

    def test_creates_synthetic_data_idempotently(self):
        first = self.run_seed()
        self.assertIn("Ingest token (shown once): lea_", first)
        src = Source.objects.get()
        self.assertTrue(src.is_example)
        n = self.counts()
        self.assertEqual(AdapterCell.objects.filter(status=0).count(), 4)
        self.assertEqual((ProcessState.objects.count(), ExecLog.objects.count()), (4, 100))
        second = self.run_seed()
        self.assertNotIn("lea_", second)                      # the token is not shown (or changed) again
        self.assertEqual(self.counts(), n)
        token = [w for w in first.split() if w.startswith("lea_")][0]
        self.assertEqual(services.authenticate(f"Bearer {token}").pk, src.pk)

    def test_uses_only_synthetic_codes(self):
        self.run_seed()
        clients = set(AdapterCell.objects.values_list("client", flat=True))
        self.assertTrue(all(c.startswith("c0") for c in clients), clients)
        self.assertEqual(len(clients), 12)
        segments = set(AdapterCell.objects.values_list("segment", flat=True))
        self.assertEqual(segments, {"NSE", "NFO", "CDS", "BSE", "BFO", "SLBM", "MSE", "NIPO", "BSTAR", "NCOM"})

    def test_never_emits_events_or_tickets(self):
        self.run_seed()
        self.assertFalse(Event.objects.exists())
        self.assertFalse(Ticket.objects.exists())

    def test_has_history_and_a_failure_streak(self):
        self.run_seed()
        c = AdapterCell.objects.get(client="c003", segment="NSE")
        self.assertEqual((c.status, c.failure_streak, c.failure_total), (0, 3, 3))
        self.assertGreaterEqual(CellEvent.objects.filter(client="c003", segment="NSE").count(), 2)

    def test_remove(self):
        self.run_seed()
        self.assertIn("Removed", self.run_seed("--remove"))
        self.assertEqual(self.counts(), (0, 0, 0, 0, 0, 0))
        self.assertIn("No example", self.run_seed("--remove"))

    def test_never_touches_a_real_source_with_the_same_name(self):
        real = Source.objects.create(tenant_key=services.tenant(), name="Example APM (synthetic)", description="mine")
        self.assertIn("not marked as an example", self.run_seed())
        self.assertIn("not marked as an example", self.run_seed("--remove"))
        self.assertTrue(Source.objects.filter(pk=real.pk).exists())
        self.assertEqual(AdapterCell.objects.count(), 0)

    def test_rotate_token(self):
        first = self.run_seed()
        old = [w for w in first.split() if w.startswith("lea_")][0]
        new = [w for w in self.run_seed("--rotate-token").split() if w.startswith("lea_")][0]
        self.assertNotEqual(old, new)
        self.assertIsNone(services.authenticate(f"Bearer {old}"))
        self.assertIsNotNone(services.authenticate(f"Bearer {new}"))

    def test_a_seeded_source_is_fresh_not_stale(self):
        self.run_seed()
        src = Source.objects.get()
        self.assertFalse(services.is_stale(src, src.last_matrix_at))


class ClientLibrary(SimpleTestCase):
    ENV = {"LINKEDEYE_APM_URL": "https://le.example.com", "LINKEDEYE_APM_TOKEN": "lea_00000000_" + "t" * 43}

    def setUp(self):
        self.mod = load_client()
        p = mock.patch.dict("os.environ", self.ENV, clear=False)
        p.start()
        self.addCleanup(p.stop)
        s = mock.patch.object(self.mod.time, "sleep")
        self.sleep = s.start()
        self.addCleanup(s.stop)

    def fake_response(self, payload):
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return resp

    def test_never_raises_and_reports_the_error(self):
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("refused")) as op:
            out = self.mod.push_matrix(matrix())
        self.assertFalse(out["ok"])
        self.assertEqual(op.call_count, 3)                         # retried
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [1, 2])          # exponential backoff
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=RuntimeError("weird")):
            for call in (lambda: self.mod.push_table(table()), lambda: self.mod.push_logs([{"message": "x"}]),
                         lambda: self.mod.poll_commands(), lambda: self.mod.command_done(3)):
                self.assertFalse(call()["ok"])

    def test_missing_configuration_does_not_raise(self):
        with mock.patch.dict("os.environ", {"LINKEDEYE_APM_URL": "", "LINKEDEYE_APM_TOKEN": ""}):
            out = self.mod.push_matrix(matrix())
        self.assertFalse(out["ok"])
        self.assertIn("must be set", out["error"])

    def test_plain_http_is_refused_unless_explicitly_allowed(self):
        with mock.patch.dict("os.environ", {"LINKEDEYE_APM_URL": "http://le.example.com"}):
            self.assertIn("https", self.mod.push_matrix(matrix())["error"])
            with mock.patch.dict("os.environ", {"LINKEDEYE_APM_ALLOW_HTTP": "1"}), \
                    mock.patch.object(self.mod.urllib.request, "urlopen", return_value=self.fake_response({"ok": True})):
                self.assertTrue(self.mod.push_matrix(matrix())["ok"])

    def test_tls_verification_is_on(self):
        ctx = self.mod._context()
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)
        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=self.fake_response({"ok": True})) as op:
            self.mod.push_matrix(matrix())
        self.assertEqual(op.call_args.kwargs["context"].verify_mode, ssl.CERT_REQUIRED)
        self.assertGreater(op.call_args.kwargs["timeout"], 0)

    def test_request_shape_dict_and_string(self):
        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=self.fake_response({"ok": True})) as op:
            self.mod.push_matrix(matrix(), host="h", script="s.py", duration_s=1.23456)
            req = op.call_args.args[0]
            body = json.loads(req.data)
            self.assertEqual((body["type"], body["host"], body["script"], body["duration_s"]), ("matrix", "h", "s.py", 1.235))
            self.assertIn("payload", body)
            self.assertEqual(req.get_header("Authorization"), "Bearer " + self.ENV["LINKEDEYE_APM_TOKEN"])
            self.assertEqual(req.full_url, "https://le.example.com/api/apm/ingest/")
            self.mod.push_table(str(table()))
            self.assertIn("payload_str", json.loads(op.call_args.args[0].data))

    def test_nan_becomes_null_so_the_request_is_valid_json(self):
        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=self.fake_response({"ok": True})) as op:
            self.mod.push_table({"data": [{"name": "p", "cpu_percent": float("nan")}], "x": object()})
        sent = json.loads(op.call_args.args[0].data)
        self.assertIsNone(sent["payload"]["data"][0]["cpu_percent"])

    def test_no_retry_on_client_errors_and_retry_after_is_honoured(self):
        def http(code, headers=None):
            return urllib.error.HTTPError("u", code, "m", headers or {}, io.BytesIO(b'{"message": "bad"}'))
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=http(401)) as op:
            out = self.mod.push_logs([{"message": "x"}])
        self.assertEqual((op.call_count, out["ok"]), (1, False))
        self.assertIn("401", out["error"])
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=[http(429, {"Retry-After": "7"}), self.fake_response({"ok": True})]) as op:
            self.assertTrue(self.mod.push_logs([{"message": "x"}])["ok"])
        self.assertEqual(self.sleep.call_args.args[0], 7)

    def test_the_token_is_never_logged(self):
        with self.assertLogs("linkedeye_apm", level="WARNING") as logs, \
                mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")):
            self.mod.push_matrix(matrix())
        self.assertNotIn(self.ENV["LINKEDEYE_APM_TOKEN"], "\n".join(logs.output))

    def test_oversized_payload_is_refused_locally(self):
        out = self.mod.push_logs([{"message": "x" * 900}] * 1000 + [{"message": "y" * (3 * 1024 * 1024)}])
        self.assertFalse(out["ok"])

    def test_poll_and_done(self):
        with mock.patch.object(self.mod.urllib.request, "urlopen", return_value=self.fake_response({"ok": True, "commands": [{"id": 4}]})) as op:
            self.assertEqual(self.mod.poll_commands()["commands"], [{"id": 4}])
            self.assertEqual(op.call_args.args[0].get_method(), "GET")
            self.mod.command_done(4)
            self.assertEqual(op.call_args.args[0].full_url, "https://le.example.com/api/apm/commands/4/done/")
        with mock.patch.object(self.mod.urllib.request, "urlopen", side_effect=urllib.error.URLError("x")):
            self.assertEqual(self.mod.poll_commands()["commands"], [])

    def test_stdlib_only(self):
        src = CLIENT_PATH.read_text()
        imports = {line.split()[1].split(".")[0] for line in src.splitlines() if line.startswith(("import ", "from "))}
        self.assertLessEqual(imports, {"json", "logging", "math", "os", "socket", "ssl", "sys", "time", "urllib"})


class ClientEndToEnd(LiveServerTestCase):
    """The reference client against a real server, over a real socket (loopback)."""

    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.src = Source(tenant_key=services.tenant(), name="E2E", site="All Sites")
        self.token = services.issue_token(self.src)
        self.src.save()
        self.mod = load_client()
        env = {"LINKEDEYE_APM_URL": self.live_server_url, "LINKEDEYE_APM_TOKEN": self.token, "LINKEDEYE_APM_ALLOW_HTTP": "1",
               "LINKEDEYE_APM_RETRIES": "1"}
        p = mock.patch.dict("os.environ", env)
        p.start()
        self.addCleanup(p.stop)

    def test_push_matrix_table_logs_and_commands(self):
        out = self.mod.push_matrix(matrix(failing={("c001", "NSE")}), script="adapterStatus.py", duration_s=2.5)
        self.assertTrue(out["ok"], out)
        self.assertEqual((out["total"], out["failure"]), (4, 1))
        self.assertTrue(self.mod.push_matrix(str(matrix()))["ok"])                    # the str(dict) form
        self.assertTrue(self.mod.push_table(table())["ok"])
        self.assertTrue(self.mod.push_logs([{"ts": "2026-08-27 15:02:01,741", "level": "ERROR", "process": "p", "message": "boom"}])["ok"])
        self.assertEqual(AdapterCell.objects.filter(source=self.src).count(), 4)
        self.assertEqual(ProcessState.objects.filter(source=self.src).count(), 2)
        self.assertEqual(ExecLog.objects.filter(source=self.src).count(), 1)
        cell = AdapterCell.objects.get(source=self.src, client="c001", segment="NSE")
        services.request_retry(cell, None)
        cmds = self.mod.poll_commands()["commands"]
        self.assertEqual([(c["client"], c["segment"]) for c in cmds], [("c001", "NSE")])
        self.assertTrue(self.mod.command_done(cmds[0]["id"])["ok"])

    def test_a_wrong_token_is_a_clean_failure(self):
        out = self.mod.push_matrix(matrix(), token="lea_00000000_" + "x" * 43)
        self.assertEqual((out["ok"], "401" in out["error"]), (False, True))


class Conventions(TestCase):
    def test_every_model_has_a_tenant_key_and_the_tenant_default(self):
        for model in apps.get_app_config("apm").get_models():
            f = model._meta.get_field("tenant_key")
            self.assertTrue(f.db_index, model)
            self.assertEqual(f.default(), services.tenant())

    def test_only_source_stores_a_site_name_and_it_is_registered(self):
        from administration.services import SITE_NAME_FIELDS
        registered = {(a, m, f) for a, m, f, _l in SITE_NAME_FIELDS if a == "apm"}
        stored = set()
        for model in apps.get_app_config("apm").get_models():
            for f in model._meta.get_fields():
                if isinstance(f, models.CharField) and "site" in f.name.lower():
                    stored.add(("apm", model.__name__, f.name))
        self.assertEqual(stored, registered)
        self.assertEqual(stored, {("apm", "Source", "site")})

    def test_children_are_scoped_through_their_source(self):
        for model in (AdapterCell, AdapterRun, CellEvent, ProcessState, ExecLog):
            self.assertEqual(model._meta.get_field("source").remote_field.on_delete.__name__, "CASCADE")

    def test_the_credential_row_site_is_registered_too(self):
        from administration.services import SITE_NAME_FIELDS
        self.assertIn(("onboarding", "Credential", "site", "Stored credentials"), SITE_NAME_FIELDS)

    def test_jobs_are_registered(self):
        from core import jobs
        for name in ("apm.redis_poll", "apm.stale", "apm.retention"):
            self.assertIn(name, jobs.JOBS)
        self.assertEqual(jobs.JOBS["apm.redis_poll"]["interval"], 60)

    def test_no_eval_exec_pickle_or_inline_handlers_in_the_module(self):
        import re
        root = Path(__file__).resolve().parent.parent
        for path in list(root.rglob("*.py")) + list(root.rglob("*.html")) + list(root.rglob("*.js")):
            if "tests" in path.parts or "migrations" in path.parts:
                continue
            text = path.read_text()
            self.assertIsNone(re.search(r"(?<![\w.])(eval|exec)\(", text.replace("ast.literal_eval(", "")), path)
            self.assertNotIn("pickle", text, path)
            self.assertNotIn("|safe", text, path)
            self.assertNotIn("mark_safe", text, path)
            self.assertIsNone(re.search(r"\son(click|load|error|change|submit)=", text), path)
            if path.suffix == ".js":
                self.assertNotIn("innerHTML", text, path)
                self.assertNotIn("insertAdjacentHTML", text, path)
                self.assertNotIn("document.write", text, path)

    def test_css_uses_tokens_not_colours(self):
        import re
        css = (Path(__file__).resolve().parent.parent / "static" / "apm" / "page.css").read_text()
        self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", css))
        self.assertIsNone(re.search(r"\brgba?\(", css))


class MigrationReversible(TransactionTestCase):
    def test_zero_and_forward_again(self):
        call_command("migrate", "apm", "zero", verbosity=0)
        self.assertNotIn("apm_source", connection.introspection.table_names())
        call_command("migrate", "apm", verbosity=0)
        for table_name in ("apm_source", "apm_adaptercell", "apm_adapterrun", "apm_cellevent", "apm_processstate", "apm_execlog", "apm_retryrequest"):
            self.assertIn(table_name, connection.introspection.table_names())

    def test_apm_has_exactly_one_migration(self):
        names = sorted(p.name for p in (Path(__file__).resolve().parent.parent / "migrations").glob("0*.py"))
        self.assertEqual(names, ["0001_initial.py"])
