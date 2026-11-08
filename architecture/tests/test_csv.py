import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse

from architecture import csvio
from architecture.models import Link
from core.models import AuditEvent

from .fixtures import Base, link

HEADER = "a_device,a_port,b_device,b_port,medium,speed_mbps,kind,note\n"


class CsvTests(Base):
    def validate(self, text, user=None, update=False):
        return csvio.validate(text, user or self.operator, None, update)

    def test_valid_rows_create(self):
        p = self.validate(HEADER + "dc1-spine-01,Eth1/1,dc1-leaf-01,Eth1/49,fibre,100000,uplink,x\n"
                                   "dc1-leaf-01,eth0,web-01,,dac,25000,downlink,\n")
        self.assertTrue(p.ok, p.errors)
        self.assertEqual([r["action"] for r in p.rows], ["create", "create"])
        created, _ = csvio.commit(p)
        self.assertEqual(len(created), 2)
        self.assertEqual(Link.objects.get(note="x").origin, "import")

    def test_only_required_columns_and_case_and_bom(self):
        p = csvio.validate("﻿A_Device,B_Device\nDC1-SPINE-01,dc1-leaf-01\n", self.operator)
        self.assertTrue(p.ok, p.errors)

    def test_errors_are_row_level_and_nothing_is_written(self):
        text = HEADER + ("dc1-spine-01,Eth1/1,dc1-leaf-01,Eth1/49,fibre,100000,,\n"
                         "nope,,dc1-leaf-01,,,,,\n"
                         "dc1-spine-01,,dc1-spine-01,,,,,\n"
                         "dc1-spine-01,,dc1-leaf-01,,laser,,,\n"
                         "dc1-spine-01,,dc1-leaf-01,,,fast,,\n"
                         "dc1-spine-01,,dc1-leaf-01,,,0,,\n"
                         f"dc1-spine-01,{'p' * 61},dc1-leaf-01,,,,,\n"
                         "dc1-spine-01,Eth1/1,dc1-leaf-01,Eth1/49,,,,\n"
                         "dc1-spine-01,,dc1-leaf-01,,,,,\x07\n"
                         "lon-core-01,,dc1-leaf-01,,,,,\n")
        res = self.client.post  # not used; the importer is exercised directly
        p = self.validate(text, user=self.mum_op)
        lines = {line for line, _m in p.errors}
        self.assertEqual(lines, {3, 4, 5, 6, 7, 8, 9, 10, 11})
        msgs = " ".join(m for _l, m in p.errors)
        for needle in ("No device named", "itself", "Unknown medium", "speed_mbps", "60 characters", "more than once", "control characters"):
            self.assertIn(needle, msgs)
        self.assertEqual(Link.objects.count(), 0)

    def test_unknown_and_missing_columns(self):
        self.assertIn("Unknown column", self.validate("a_device,b_device,color\nx,y,z\n").errors[0][1])
        self.assertIn("needs the column", self.validate("a_device,port\nx,y\n").errors[0][1])
        self.assertIn("empty", self.validate("").errors[0][1])
        self.assertIn("no data rows", self.validate(HEADER).errors[0][1])

    def test_too_many_cells_and_too_many_rows(self):
        self.assertIn("more cells", self.validate(HEADER + "a,b,c,d,e,f,g,h,i,j\n").errors[0][1])
        rows = "".join(f"dc1-spine-01,p{i},dc1-leaf-01,q{i}\n" for i in range(csvio.MAX_ROWS + 1))
        p = self.validate("a_device,a_port,b_device,b_port\n" + rows)
        self.assertTrue(any("5,000" in m for _l, m in p.errors))

    def test_not_utf8_and_too_big(self):
        text, why = csvio.decode(b"\xff\xfe\x00bad")
        self.assertIsNone(text)
        self.assertIn("UTF-8", why)
        self.assertIsNone(csvio.decode(b"x" * (csvio.MAX_BYTES + 1))[0])

    def test_duplicate_of_existing_needs_update_flag(self):
        link(self.spine, self.leaf, "Eth1/1", "Eth1/49", speed_mbps=1000)
        text = HEADER + "dc1-leaf-01,eth1/49,dc1-spine-01,Ethernet1/1,fibre,100000,,\n"
        p = self.validate(text)
        self.assertIn("already exists", p.errors[0][1])
        p = self.validate(text, update=True)
        self.assertEqual([r["action"] for r in p.rows], ["update"])
        csvio.commit(p)
        self.assertEqual(Link.objects.get().speed_mbps, 100000)
        p = self.validate(text, update=True)
        self.assertEqual([r["action"] for r in p.rows], ["unchanged"])

    def test_site_permissions_per_row(self):
        text = HEADER + "dc1-spine-01,,dc1-leaf-01,,,,,\nlon-core-01,,dc1-leaf-01,,,,,\n"
        p = self.validate(text, user=self.mum_op)
        self.assertEqual(len(p.errors), 1)
        self.assertIn("No device named", p.errors[0][1])       # London is not even visible
        p = self.validate("a_device,b_device\ndc1-spine-01,dc1-leaf-01\n", user=self.mum_viewer)
        self.assertIn("cannot manage", p.errors[0][1])        # visible but read-only

    def test_export_is_formula_safe_and_round_trips(self):
        l = link(self.spine, self.leaf, "=cmd|' /C calc'!A0", "@x", kind="+kind", note="-1+1")
        self.login(self.operator)
        r = self.client.get(reverse("architecture:links_export"))
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        body = r.content.decode()
        self.assertTrue(body.startswith(",".join(csvio.COLUMNS)))
        self.assertIn("'=cmd", body)
        self.assertIn(",'@x,", body)
        self.assertIn(",'+kind,", body)
        self.assertIn(",'-1+1", body)
        l.delete()
        p = self.validate(body)
        self.assertTrue(p.ok, p.errors)
        csvio.commit(p)
        again = Link.objects.get()
        self.assertEqual((again.kind, again.note), ("+kind", "-1+1"))
        self.assertTrue(again.a_port.startswith("=cmd") or again.b_port.startswith("=cmd"))

    def test_export_is_scoped(self):
        link(self.spine, self.lon_sw, "a", "b")
        link(self.spine, self.leaf, "c", "d")
        self.login(self.mum_viewer)
        body = self.client.get(reverse("architecture:links_export")).content.decode()
        self.assertNotIn("lon-core-01", body)
        self.assertEqual(body.count("\n"), 2)


class ImportViewTests(Base):
    def upload(self, text, **kw):
        return self.client.post(reverse("architecture:links_import"), {"file": SimpleUploadedFile("l.csv", text.encode()), **kw})

    def test_preview_then_commit_and_audit(self):
        self.login(self.operator)
        r = self.upload(HEADER + "dc1-spine-01,Eth1/1,dc1-leaf-01,Eth1/49,fibre,100000,,\n")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["preview"].ok)
        self.assertEqual(Link.objects.count(), 0)                # dry run: nothing written yet
        token = r.context["token"]
        self.assertTrue(token)
        r = self.client.post(reverse("architecture:links_import"), {"commit": "1", "token": "wrong"})
        self.assertEqual(Link.objects.count(), 0)
        r = self.client.post(reverse("architecture:links_import"), {"commit": "1", "token": token})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Link.objects.count(), 1)
        self.assertEqual(AuditEvent.objects.get(action="import").details["created"], 1)
        r = self.client.post(reverse("architecture:links_import"), {"commit": "1", "token": token})
        self.assertEqual(Link.objects.count(), 1)               # the token is single use

    def test_invalid_file_shows_errors_and_has_no_token(self):
        self.login(self.operator)
        r = self.upload(HEADER + "nope,,dc1-leaf-01,,,,,\n")
        self.assertFalse(r.context["preview"].ok)
        self.assertEqual(r.context["token"], "")
        self.assertContains(r, "Nothing was imported")

    def test_no_file_and_binary_file(self):
        self.login(self.operator)
        r = self.client.post(reverse("architecture:links_import"), {})
        self.assertEqual(r.status_code, 200)
        r = self.client.post(reverse("architecture:links_import"), {"file": SimpleUploadedFile("x.csv", b"\xff\xfe\xfd")})
        self.assertContains(r, "UTF-8")

    def test_viewer_is_refused(self):
        self.login(self.viewer)
        self.assertEqual(self.client.get(reverse("architecture:links_import")).status_code, 403)
        self.assertEqual(self.upload(HEADER + "dc1-spine-01,,dc1-leaf-01,,,,,\n").status_code, 302)
        self.assertEqual(Link.objects.count(), 0)

    def test_commit_revalidates(self):
        self.login(self.operator)
        r = self.upload(HEADER + "dc1-spine-01,Eth1/1,dc1-leaf-01,Eth1/49,,,,\n")
        token = r.context["token"]
        link(self.spine, self.leaf, "Eth1/1", "Eth1/49")          # someone else added it meanwhile
        self.client.post(reverse("architecture:links_import"), {"commit": "1", "token": token})
        self.assertEqual(Link.objects.count(), 1)
