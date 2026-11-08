"""The read-only guarantee. These tests fail the moment a write path appears in the Redfish client or is used against a BMC."""
import inspect
import re

from django.test import SimpleTestCase

from hardware import collector, redfish, services
from hardware.tests import fixtures
from hardware.tests.base import BMC_PASS, BMC_USER, HwCase, loopback, with_key
from hardware.tests.fake_redfish import FakeRedfish

WRITE_WORDS = ("post", "put", "patch", "delete", "head", "options", "trace", "connect", "session", "action", "reset")


class Structure(SimpleTestCase):
    def test_the_only_verb_is_get(self):
        self.assertEqual(redfish.METHOD, "GET")
        self.assertEqual(redfish.ALLOWED_METHODS, frozenset({"GET"}))

    def test_exactly_one_http_request_call_in_the_whole_app(self):
        """`.request(` appears once in hardware/*.py (the GET in RedfishClient._get), and nowhere else sends HTTP."""
        import hardware
        from pathlib import Path
        root = Path(hardware.__file__).parent
        hits = []
        for f in root.glob("*.py"):
            for n, line in enumerate(f.read_text().splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\.(request|putrequest|urlopen)\(", code) or re.search(r"requests\.(get|post|put|patch|delete)", code):
                    hits.append((f.name, n))
        self.assertEqual(len(hits), 1, hits)
        self.assertEqual(hits[0][0], "redfish.py")
        self.assertIn("conn.request(METHOD,", inspect.getsource(redfish.RedfishClient._get))

    def test_no_write_verb_literals_in_the_client_or_collector(self):
        for mod in (redfish, collector):
            src = re.sub(r'""".*?"""', "", inspect.getsource(mod), flags=re.S)
            src = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
            for verb in ("POST", "PUT", "PATCH", "DELETE"):
                self.assertNotRegex(src, rf'["\']{verb}["\']', f"{verb} appears in {mod.__name__}")

    def test_the_client_has_no_write_style_methods(self):
        public = [n for n, _ in inspect.getmembers(redfish.RedfishClient, inspect.isfunction) if not n.startswith("_")]
        self.assertEqual(sorted(public), ["close", "get"])
        for n in public:
            self.assertFalse(any(w in n.lower() for w in WRITE_WORDS), n)

    def test_the_collector_and_services_only_use_get(self):
        src = inspect.getsource(collector) + inspect.getsource(services)
        for name in ("client.post", "client.patch", "client.put", "client.delete", ".post(", ".patch(", ".put(", ".delete("):
            if name in (".delete(",):
                continue                    # QuerySet.delete() is a database call (retention), never HTTP
            self.assertNotIn(name, src)


@with_key
class OnTheWire(HwCase):
    def test_a_full_poll_and_a_test_connection_send_only_get(self):
        with self.bmc() as (srv, ep):
            services.poll_endpoint(ep)
            services.test_connection(ep)
            self.assertGreater(len(srv.requests), 20)
            self.assertEqual(srv.methods, {"GET"})

    def test_control_requests_never_reach_the_bmc(self):
        with self.bmc() as (srv, ep):
            req = services.create_request(ep, self.mum_op, "force_off", "hung after firmware update")
            services.decide_request(req, self.mum_admin, True, "ok")
            services.request_refresh(ep)
            self.assertEqual(srv.requests, [])

    def test_the_fake_really_would_see_a_write(self):
        import http.client
        import ssl
        with loopback, FakeRedfish(fixtures.dell_idrac9()) as srv:
            c = http.client.HTTPSConnection("127.0.0.1", srv.port, context=ssl._create_unverified_context())
            c.request("POST", "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset", body="{}")
            self.assertEqual(c.getresponse().status, 405)
            self.assertIn("POST", srv.methods)
