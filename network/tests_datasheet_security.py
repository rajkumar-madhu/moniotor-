"""Hostile input for the datasheet photo feature: PDFs and web pages are attacker-controlled, and so are the addresses
inside them. Every file, page and address here is built from bytes in the test; no test opens a socket."""
import socket
import struct
import threading
import time
import unittest
import zlib
from datetime import timedelta
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection, transaction
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from probes import checks

from . import datasheet, datasheet_page, datasheet_pdf, images
from .datasheet import DatasheetError
from .images import clean_source_url
from .models import DeviceImage
from .tests import T, dev, users
from .tests_datasheet import HOST, PRODUCT, Item, img_bytes, needs_pypdf, pdf_of
from .tests_images import PUBLIC, FakeConn, FakeResponse, png


def build_pdf(objs, trailer=""):
    """A classic-xref PDF from {object number: body bytes}; object 1 is the catalog."""
    out, offs = bytearray(b"%PDF-1.5\n"), {}
    for n in sorted(objs):
        offs[n] = len(out)
        out += b"%d 0 obj\n" % n + objs[n] + b"\nendobj\n"
    xref, size = len(out), max(objs) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    out += b"".join((b"%010d 00000 n \n" % offs[n]) if n in offs else b"0000000000 65535 f \n" for n in range(1, size))
    out += b"trailer\n<< /Size %d /Root 1 0 R %s>>\nstartxref\n%d\n%%%%EOF\n" % (size, trailer.encode(), xref)
    return bytes(out)


PAGES = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"}


def page_with(resources=b"", extra=None, **more):
    objs = dict(PAGES)
    objs[3] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] " + resources + b" >>"
    objs.update(extra or {})
    objs.update(more)
    return build_pdf(objs)


def stream(entries, data):
    return b"<< " + entries + b" /Length %d >>\nstream\n" % len(data) + data + b"\nendstream"


@needs_pypdf
class HostilePdfTests(SimpleTestCase):
    """Whatever is in the file, extract() returns a PdfResult in bounded time and memory: never an exception."""

    def run_pdf(self, data, seconds=8):
        t = time.monotonic()
        res = datasheet_pdf.extract(data)
        self.assertLess(time.monotonic() - t, seconds)
        self.assertEqual(res.candidates, [])
        self.assertTrue(res.message)
        return res

    def test_an_object_stream_bomb_is_a_message(self):
        """A 300 MB object stream hiding in a few hundred KB: the catalog lives inside it, so the reader must inflate it."""
        c, comp = zlib.compressobj(9), bytearray()
        head = b"1 0 "
        comp += c.compress(head + b"<< /Type /Catalog /Pages 3 0 R >>")
        block = b" " * (1 << 20)
        for _ in range(300):
            comp += c.compress(block)
        comp += c.flush()
        out = bytearray(b"%PDF-1.5\n")
        off2 = len(out)
        out += b"2 0 obj\n" + stream(b"/Type /ObjStm /N 1 /First %d /Filter /FlateDecode" % len(head), bytes(comp)) + b"\nendobj\n"
        off3 = len(out)
        out += b"3 0 obj\n<< /Type /Pages /Kids [] /Count 0 >>\nendobj\n"
        off4 = len(out)
        entries = (b"\x00" + struct.pack(">I", 0) + b"\xff" + b"\x02" + struct.pack(">I", 2) + b"\x00"
                   + b"\x01" + struct.pack(">I", off2) + b"\x00" + b"\x01" + struct.pack(">I", off3) + b"\x00"
                   + b"\x01" + struct.pack(">I", off4) + b"\x00")
        out += b"4 0 obj\n" + stream(b"/Type /XRef /Size 5 /W [1 4 1] /Root 1 0 R", entries) + b"\nendobj\nstartxref\n%d\n%%%%EOF\n" % off4
        self.assertLess(len(out), 1_000_000)
        self.run_pdf(bytes(out))

    def test_reference_cycles_do_not_loop(self):
        self.run_pdf(build_pdf({**PAGES, 3: b"<< /Type /Page /Parent 2 0 R /Resources 4 0 R >>", 4: b"4 0 R"}))
        self.run_pdf(build_pdf({**PAGES, 3: b"<< /Type /Page /Parent 2 0 R /Resources 4 0 R >>", 4: b"<< /XObject 5 0 R >>",
                                5: b"<< /A 6 0 R >>", 6: stream(b"/Type /XObject /Subtype /Form /Resources 4 0 R", b"")}))
        self.run_pdf(page_with(b"/Resources << /XObject << /Im0 4 0 R >> >>",
                               {4: stream(b"/Type /XObject /Subtype /Image /Width 300 /Height 200 /ColorSpace 5 0 R /Filter /FlateDecode",
                                          zlib.compress(b"\x01" * 10)), 5: b"[5 0 R]"}))

    def test_nested_form_xobject_fan_out_is_bounded(self):
        """200 entries -> one Form -> 200 entries -> a second Form ... is ~8M lookups per walk: the walk must stop early."""
        n = 200

        def xobj_dict(target):
            return b"<< /XObject << " + b" ".join(b"/F%d %d 0 R" % (i, target) for i in range(n)) + b" >> >>"
        form = lambda res: stream(b"/Type /XObject /Subtype /Form /BBox [0 0 10 10] /Resources " + res, b"")  # noqa: E731
        objs = {1: PAGES[1], 2: b"<< /Type /Pages /Kids [" + b" ".join([b"3 0 R"] * 6) + b"] /Count 6 >>",
                3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 800] /Resources " + xobj_dict(4) + b" >>",
                4: form(xobj_dict(5)), 5: form(xobj_dict(6)), 6: form(xobj_dict(7)), 7: form(b"<< >>")}
        t = time.monotonic()
        res = datasheet_pdf.extract(build_pdf(objs))
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(res.candidates, [])

    def test_a_page_tree_that_contains_itself(self):
        self.run_pdf(build_pdf({1: PAGES[1], 2: b"<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >>", 3: b"<< /Type /Pages /Parent 2 0 R /Kids [2 0 R] /Count 1 >>"}))
        self.run_pdf(build_pdf({1: PAGES[1], 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: b"<< /Type /Pages /Parent 2 0 R /Kids [2 0 R] /Count 1 >>"}))

    def test_a_page_count_far_beyond_the_pages_that_exist(self):
        self.run_pdf(build_pdf({1: PAGES[1], 2: b"<< /Type /Pages /Kids [3 0 R] /Count 999999999 >>", 3: b"<< /Type /Page /Parent 2 0 R >>"}))

    def test_absurd_numbers_in_the_image_dictionary(self):
        body = zlib.compress(b"\x7f" * 100)
        for width in (b"1e999", b"-5", b"9" * 5000, b"99999999999", b"0"):
            image = stream(b"/Type /XObject /Subtype /Image /Width " + width + b" /Height 300 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode", body)
            self.run_pdf(page_with(b"/Resources << /XObject << /Im0 4 0 R >> >>", {4: image}))

    def test_deeply_nested_objects_are_a_message(self):
        deep = b"[" * 100_000 + b"]" * 100_000
        self.run_pdf(page_with(b"/Foo " + deep))
        self.run_pdf(b"%PDF-1.4\n1 0 obj\n" + b"[" * 2_000_000 + b"\nendobj\n")

    def test_a_decode_parameter_that_disagrees_with_the_picture(self):
        data = zlib.compress(b"\x00" + bytes(range(256)) * 6)
        image = stream(b"/Type /XObject /Subtype /Image /Width 500 /Height 300 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
                       b"/DecodeParms << /Predictor 15 /Colors 3 /Columns 99999999 >>", data)
        self.run_pdf(page_with(b"/Resources << /XObject << /Im0 4 0 R >> >>", {4: image}))

    def test_a_soft_mask_bomb_is_not_inflated_past_its_size(self):
        c, parts = zlib.compressobj(9), []
        for _ in range(200):
            parts.append(c.compress(bytes(1 << 20)))
        parts.append(c.flush())
        mask = stream(b"/Type /XObject /Subtype /Image /Width 400 /Height 300 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode", b"".join(parts))
        photo = stream(b"/Type /XObject /Subtype /Image /Width 400 /Height 300 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode /SMask 5 0 R",
                       zlib.compress(bytes((i * 7 + i // 400) % 251 for i in range(400 * 300 * 3)), 1))
        t = time.monotonic()
        res = datasheet_pdf.extract(page_with(b"/Resources << /XObject << /Im0 4 0 R >> >>", {4: photo, 5: mask}))
        self.assertLess(time.monotonic() - t, 5)
        self.assertEqual(len(res.candidates), 1)            # the picture is kept; the unreadable mask is dropped, not inflated

    def test_garbage_after_the_header(self):
        self.run_pdf(b"%PDF-1.4\n" + bytes(range(256)) * 20_000)
        self.run_pdf(b"%PDF-1.4\n" + b"1 0 obj << >> endobj\n" * 50_000)


class FileKindByContentTests(SimpleTestCase):
    """What an upload is comes from its bytes, never from its name or declared type."""

    @needs_pypdf
    def test_a_pdf_named_png_is_read_as_a_pdf(self):
        res = datasheet.find_from_upload(pdf_of([PRODUCT]), "datasheet.png")
        self.assertEqual((res.kind, len(res.items)), ("pdf", 1))

    def test_a_picture_named_pdf_is_read_as_a_picture(self):
        res = datasheet.find_from_upload(png(320, 200), "datasheet.pdf")
        self.assertEqual((res.kind, len(res.items)), ("image", 1))

    def test_a_picture_that_mentions_the_pdf_header_is_still_a_picture(self):
        body = b"k\x00%PDF-1.4"
        tail_chunk = struct.pack(">I", len(body)) + b"tEXt" + body + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF)
        data = png(320, 200, extra=tail_chunk)
        self.assertIn(b"%PDF-", data[:1024])
        self.assertEqual(datasheet.find_from_upload(data, "x.pdf").kind, "image")

    def test_html_svg_and_text_are_refused_whatever_they_are_called(self):
        for name, body in (("a.png", b"<svg onload=alert(1)>"), ("a.pdf", b"<html><script>1</script>"), ("a.pdf", b"hello"), ("a.png", b"")):
            with self.assertRaises(DatasheetError):
                datasheet.find_from_upload(body, name)


class AddressSyntaxTests(SimpleTestCase):
    BAD = ["https://[::1", "https://[bad]/", "https://[::1]:abc/", "https://example.com:abc/", "https://example.com:99999/",
           "https://user:secret@example.com/", "https://@example.com/", "https://exa mple.com/", "https://example.com/\nX", "https://example.com/\x00X",
           "https:///path", "https://", "http://example.com/", "ftp://example.com/", "file:///etc/passwd", "javascript:alert(1)", "data:text/html,x",
           "//example.com/", "https://example.com/" + "a" * 600, "https://example.com\\@evil.example/"]

    def test_malformed_addresses_are_a_sentence_never_an_exception(self):
        for url in self.BAD:
            with self.subTest(url=url[:60]), self.assertRaises(DatasheetError):
                datasheet_page.check_url(url)

    def test_a_good_address_passes(self):
        for url in ("https://www.vendor-example.com/p?id=1", "https://[2001:db8::1]:8443/x", "HTTPS://Example.com/"):
            self.assertEqual(datasheet_page.check_url(url), url)

    def test_clean_source_url_never_raises(self):
        for url in ("https://[::1", "https://example.com:abc/", "https://example.com:99999/x", "", None):
            self.assertEqual(clean_source_url(url), "")
        self.assertEqual(clean_source_url("https://u:p@example.com/a.png?token=1#x"), "https://example.com/a.png")


def literal_resolver(names):
    """DNS for the fake network: the names in `names` resolve as given; an IP literal resolves to itself (as getaddrinfo does)."""
    real = checks.resolve

    def resolve(host, port=443):
        if host in names:
            return names[host]
        return real(host, port)
    return resolve


PRIVATE_TARGETS = [
    "https://127.0.0.1/a.png", "https://localhost/a.png", "https://169.254.169.254/latest/meta-data/a.png", "https://10.1.2.3/a.png",
    "https://172.16.0.9/a.png", "https://192.168.1.1/a.png", "https://100.64.0.1/a.png", "https://100.100.100.200/a.png",
    "https://[::1]/a.png", "https://[::ffff:127.0.0.1]/a.png", "https://[::ffff:7f00:1]/a.png", "https://[::127.0.0.1]/a.png",
    "https://[64:ff9b::7f00:1]/a.png", "https://[fe80::1]/a.png", "https://[fd00::1]/a.png", "https://[2002:7f00:1::1]/a.png",
    "https://2130706433/a.png", "https://0x7f.0.0.1/a.png", "https://017700000001/a.png", "https://0/a.png",
    "//127.0.0.1/a.png", "http://127.0.0.1/a.png", "file:///etc/passwd", "data:image/png;base64,AAAA", "javascript:alert(1)",
    "https://user:pw@cdn.vendor-example.com/a.png",
]


@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=[], PROBE_TIMEOUT=5)
class SsrfFromThePageTests(SimpleTestCase):
    """The picture addresses in a hostile page: nothing is connected to except public hosts, and the page host itself."""

    def run_page(self, html):
        opened = []

        def open_connection(scheme, host, ip, port, timeout):
            opened.append((scheme, host, ip, port))
            if host == HOST:
                return FakeConn(FakeResponse(200, {"Content-Type": "text/html"}, html.encode()), [])
            raise AssertionError(f"connected to {host} ({ip})")
        with mock.patch.object(checks, "resolve", side_effect=literal_resolver({HOST: [PUBLIC]})), \
                mock.patch.object(checks, "_open_connection", side_effect=open_connection):
            with self.assertRaises(DatasheetError) as cm:
                datasheet.find_from_url(f"https://{HOST}/product", "X100")
        return opened, str(cm.exception)

    def test_private_and_special_addresses_in_the_markup_are_never_connected_to(self):
        html = "".join(f'<img src="{u}" alt="X100" width="800" height="600">' for u in PRIVATE_TARGETS)
        html += '<meta property="og:image" content="https://127.0.0.1/og.png"><link rel="image_src" href="https://10.0.0.1/x.png">'
        html += '<script type="application/ld+json">{"@type":"Product","image":["https://169.254.169.254/p.png","file:///etc/passwd"]}</script>'
        opened, message = self.run_page(html)
        self.assertEqual([o[1] for o in opened], [HOST], "only the page the operator named was contacted")
        self.assertTrue(message)

    def test_the_policy_refuses_each_of_them_by_itself(self):
        """The same addresses given straight to the fetcher (as a candidate download would): refused before any connection."""
        for url in PRIVATE_TARGETS:
            if not url.startswith("https://") or "@" in url:
                continue
            with self.subTest(url=url), mock.patch.object(checks, "_open_connection", side_effect=AssertionError("connected")):
                res = checks.fetch_bytes(url, 1000, timeout=2, https_only=True)
            self.assertFalse(res.ok)
            self.assertEqual(res.error, checks.UNREACHABLE, url)

    def test_a_redirect_from_a_public_page_into_the_private_network(self):
        for target in ("https://127.0.0.1/p", "https://internal.corp.example/p", "http://example.net/p", "https://[::ffff:10.0.0.1]/p",
                       "file:///etc/passwd", "https://169.254.169.254/latest/"):
            opened = []

            def open_connection(scheme, host, ip, port, timeout, target=target):
                opened.append(host)
                if host == HOST:
                    return FakeConn(FakeResponse(302, {"Location": target}, b""), [])
                raise AssertionError(f"followed the redirect to {host}")
            with self.subTest(target=target), mock.patch.object(checks, "resolve", side_effect=literal_resolver(
                    {HOST: [PUBLIC], "internal.corp.example": ["10.9.8.7"], "example.net": [PUBLIC]})), \
                    mock.patch.object(checks, "_open_connection", side_effect=open_connection):
                with self.assertRaises(DatasheetError) as cm:
                    datasheet.find_from_url(f"https://{HOST}/p")
            self.assertEqual(opened, [HOST])
            self.assertNotIn("10.9.8.7", str(cm.exception))
            self.assertNotIn("127.0.0.1", str(cm.exception))

    def test_dns_that_answers_private_first_is_connected_to_on_its_public_address_only(self):
        seen = []

        def open_connection(scheme, host, ip, port, timeout):
            seen.append(ip)
            return FakeConn(FakeResponse(200, {"Content-Type": "image/png"}, png(320, 200)), [])
        with mock.patch.object(checks, "resolve", return_value=["10.0.0.5", "127.0.0.1", PUBLIC]), \
                mock.patch.object(checks, "_open_connection", side_effect=open_connection):
            res = datasheet.find_from_url(f"https://{HOST}/a.png")
        self.assertEqual(seen, [PUBLIC])
        self.assertEqual(res.kind, "image")

    def test_the_error_for_an_internal_name_says_nothing_about_what_it_resolves_to(self):
        def message(dns):
            with mock.patch.object(checks, "resolve", side_effect=dns), mock.patch.object(checks, "_open_connection", side_effect=AssertionError("connected")):
                with self.assertRaises(DatasheetError) as cm:
                    datasheet.find_from_url("https://intranet.corp.example/datasheet.pdf")
            return str(cm.exception)
        private = message(lambda h, p=443: ["10.200.3.4"])
        metadata = message(lambda h, p=443: ["169.254.169.254"])
        loopback6 = message(lambda h, p=443: ["::1"])

        def nxdomain(h, p=443):
            raise socket.gaierror(-2, "Name or service not known")
        missing = message(nxdomain)
        self.assertEqual({private, metadata, loopback6, missing}, {missing}, "private, metadata, loopback and unknown names are indistinguishable")
        for leak in ("10.200", "169.254", "::1", "loopback", "169"):
            self.assertNotIn(leak, missing)


@override_settings(LINKEDEYE_TENANT=T)
class CandidateIsolationTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.mum = dev("mum-sw-01", vendor="Cisco", model="C9500")
        self.c = Client()

    def candidate(self, user, n=1):
        return images.replace_candidates(user, [Item(img_bytes(n))])[0]

    def test_someone_elses_candidate_is_indistinguishable_from_one_that_does_not_exist(self):
        mine = self.candidate(self.operator)
        self.c.force_login(self.admin)
        other = self.c.get(reverse("network:photo_candidate", args=[mine.pk]))
        ghost = self.c.get(reverse("network:photo_candidate", args=[mine.pk + 1000]))
        huge = self.c.get(reverse("network:photo_candidate", args=[10 ** 30]))
        self.assertEqual((other.status_code, ghost.status_code, huge.status_code), (404, 404, 404))
        self.assertEqual(other.content, ghost.content)

    def test_the_hash_route_never_serves_a_candidate(self):
        mine = self.candidate(self.operator)
        self.c.force_login(self.operator)
        self.assertEqual(self.c.get(reverse("network:image", args=[mine.sha256, mine.ext])).status_code, 404)
        self.c.force_login(self.admin)
        self.assertEqual(self.c.get(reverse("network:image", args=[mine.sha256, mine.ext])).status_code, 404)
        # and once the same bytes are a real photo, the hash route serves that row, not the candidate
        real, _ = images.store_image(img_bytes(1), source="upload")
        from .models import NetworkProductImage
        NetworkProductImage.objects.create(tenant_key=T, vendor="Cisco", device_type="", model_key="", image=real)
        r = self.c.get(reverse("network:image", args=[real.sha256, real.ext]))
        self.assertEqual(r.status_code, 200)

    def test_applying_someone_elses_candidate_changes_nothing_and_says_the_same_thing_as_a_missing_one(self):
        mine = self.candidate(self.admin)
        self.c.force_login(self.operator)
        url = reverse("network:photo_apply")
        a = self.c.post(url, {"device": self.mum.pk, "candidate": mine.pk}, follow=True)
        b = self.c.post(url, {"device": self.mum.pk, "candidate": mine.pk + 1000}, follow=True)
        self.assertEqual([str(m) for m in a.context["messages"]], [str(m) for m in b.context["messages"]])
        self.mum.refresh_from_db()
        self.assertIsNone(self.mum.image_id)
        self.assertTrue(DeviceImage.objects.filter(pk=mine.pk, is_candidate=True).exists(), "the owner's candidate is untouched")

    def test_ids_that_are_not_plain_numbers_are_a_not_found_never_a_server_error(self):
        self.c.force_login(self.admin)
        for bad in ("²", "٣", "1" * 40, "-1", "1.5", "0x10", "1e3", "٣"):
            with self.subTest(bad=bad):
                for url, data in ((reverse("network:photo_find"), {"device": bad}), (reverse("network:photo_find"), {"catalog": bad}),
                                  (reverse("network:photo_apply"), {"device": bad, "candidate": "1"}),
                                  (reverse("network:photo_apply"), {"device": self.mum.pk, "candidate": bad}),
                                  (reverse("network:photo_discard"), {"device": bad})):
                    r = self.c.post(url, data)
                    self.assertLess(r.status_code, 500, (url, data))
                r = self.c.get(reverse("network:photo_find"), {"device": bad})
                self.assertEqual(r.status_code, 404)

    def test_discarding_is_audited_and_touches_only_your_own(self):
        from core.models import AuditEvent
        self.candidate(self.operator)
        self.candidate(self.admin, 2)
        self.c.force_login(self.operator)
        self.c.post(reverse("network:photo_discard"), {"device": self.mum.pk})
        ev = AuditEvent.objects.get(action="delete", object_type="network.device_photo")
        self.assertEqual((ev.actor_name, ev.details["discarded"]), ("operator", 1))
        self.assertEqual(images.live_candidates(self.admin).count(), 1)

    def test_a_promoted_photo_is_never_pruned(self):
        c = self.candidate(self.operator)
        real = images.promote(c, self.operator)
        self.assertEqual(images.prune_candidates(now=timezone.now() + timedelta(days=400)), 0)
        self.assertTrue(DeviceImage.objects.filter(pk=real.pk, is_candidate=False, candidate_owner=0).exists())
        stale = self.candidate(self.operator, 2)
        self.assertEqual(images.prune_candidates(now=timezone.now() + timedelta(days=400)), 1)
        self.assertFalse(DeviceImage.objects.filter(pk=stale.pk).exists())

    def test_a_viewer_is_turned_away_before_the_upload_is_read(self):
        self.c.force_login(self.viewer)
        body = SimpleUploadedFile("d.pdf", b"%PDF-1.4\n" + b"x" * 100_000, content_type="application/pdf")
        with mock.patch("django.http.multipartparser.MultiPartParser.parse", side_effect=AssertionError("body was parsed")):
            r = self.c.post(reverse("network:photo_find"), {"device": self.mum.pk, "pdf_file": body})
        self.assertEqual(r.status_code, 403)

    def test_a_non_pdf_non_picture_upload_is_a_message_not_an_error(self):
        self.c.force_login(self.operator)
        for name, body in (("a.pdf", b"MZ\x90\x00" + b"\x00" * 500), ("a.png", b"<svg/>"), ("a.pdf", b"%PDF-1.4 garbage"), ("a.jpg", b"")):
            r = self.c.post(reverse("network:photo_find"), {"device": self.mum.pk, "pdf_file": SimpleUploadedFile(name, body)})
            self.assertEqual(r.status_code, 400, (name, r.status_code))

    def test_hostile_addresses_through_the_form_are_a_message_not_an_error(self):
        self.c.force_login(self.operator)
        for url in AddressSyntaxTests.BAD + PRIVATE_TARGETS:
            with mock.patch.object(checks, "_open_connection", side_effect=AssertionError("connected")):
                r = self.c.post(reverse("network:photo_find"), {"device": self.mum.pk, "source_url": url})
            self.assertEqual(r.status_code, 400, (url, r.status_code))


@unittest.skipUnless(connection.vendor == "postgresql", "row locks and advisory locks need PostgreSQL")
@override_settings(LINKEDEYE_TENANT=T)
class CandidateConcurrencyTests(TransactionTestCase):
    """Two requests at the same moment. The first one is held open (its transaction not yet committed) while the second
    one starts, which is exactly the interleaving a pair of workers produces."""

    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.extra = [__import__("django.contrib.auth.models", fromlist=["User"]).User.objects.create_user(f"w{i}", password="pw", is_staff=True) for i in range(6)]

    def second(self, fn):
        out = {}

        def run():
            try:
                out["value"] = fn()
            except BaseException as e:             # noqa: BLE001 - reported to the test
                out["error"] = e
            finally:
                connection.close()
        t = threading.Thread(target=run)
        t.start()
        return t, out

    def test_the_same_person_searching_twice_at_once_ends_with_one_clean_set(self):
        items = [Item(img_bytes(i + 10)) for i in range(5)]
        with transaction.atomic():
            images.replace_candidates(self.operator, items)
            t, out = self.second(lambda: images.replace_candidates(self.operator, items))
            time.sleep(0.6)
            self.assertTrue(t.is_alive(), "the second search waits for the first")
        t.join(20)
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True, candidate_owner=self.operator.pk).count(), 5)

    def test_the_tenant_cap_holds_when_searches_overlap(self):
        for u in self.extra[:4]:
            images.replace_candidates(u, [Item(img_bytes(100 + 20 * u.pk + i)) for i in range(12)])
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), 48)
        with transaction.atomic():
            images.replace_candidates(self.extra[4], [Item(img_bytes(1000 + i)) for i in range(12)])
            t, out = self.second(lambda: images.replace_candidates(self.extra[5], [Item(img_bytes(2000 + i)) for i in range(12)]))
            time.sleep(0.6)
        t.join(20)
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), DeviceImage.MAX_CANDIDATES_PER_TENANT)

    def test_two_people_picking_the_same_picture_at_once_share_one_photo(self):
        a = images.replace_candidates(self.operator, [Item(img_bytes(7))])[0]
        b = images.replace_candidates(self.admin, [Item(img_bytes(7))])[0]
        with transaction.atomic():
            first = images.promote(a, self.operator)
            t, out = self.second(lambda: images.promote(b, self.admin))
            time.sleep(0.6)
            self.assertTrue(t.is_alive())
        t.join(20)
        self.assertNotIn("error", out, out.get("error"))
        self.assertEqual(out["value"].pk, first.pk)
        self.assertEqual(DeviceImage.objects.filter(sha256=first.sha256).count(), 1)


class HostileMarkupTests(SimpleTestCase):
    def test_a_json_ld_block_made_of_many_tiny_pieces_is_collected_in_linear_time(self):
        c = datasheet_page._Collector()
        c.handle_starttag("script", [("type", "application/ld+json")])
        t = time.monotonic()
        for _ in range(400_000):
            c.handle_data("<")
        self.assertLess(time.monotonic() - t, 2)
        c.handle_endtag("script")
        self.assertEqual(len(c.ld[0]), 300_000, "the block is cut at 300,000 characters")

    def test_pathological_pages_are_parsed_inside_the_budget(self):
        MB = 1 << 20
        pages = ['<div>' * 100_000, '<!--' * 300_000, '<' * 1_400_000, '<a ' * 450_000, '<img ' + 'a="1" ' * 200_000, '&#x' * 400_000,
                 '<script type="application/ld+json">' + '<' * 1_400_000, '<figure>' * 100_000, '<img src="/a.jpg">' * 50_000,
                 '<img srcset="' + ', '.join(f'/i{i}.jpg {i}w' for i in range(5000)) + '">', '<meta property="og:image" content="' + 'a' * (2 * MB)]
        for html in pages:
            t = time.monotonic()
            datasheet_page.parse_page(html, "https://example.com/p/", "X100")
            self.assertLess(time.monotonic() - t, datasheet_page.PARSE_BUDGET + 3, html[:30])
