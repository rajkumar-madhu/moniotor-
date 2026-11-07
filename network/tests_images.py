"""Stored product photos: header parser, safe fetch, authenticated serving, upload RBAC, product catalog (form, CSV),
fallback illustrations and the device card. No test opens a socket and no real vendor image is used: every image is a
tiny synthetic PNG / JPEG / WebP built here from bytes."""
import hashlib
import socket
import struct
import time
import zlib
from io import StringIO
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import prometheus
from core.models import AuditEvent, Membership, Site
from probes import checks

from . import art, catalog_io, imaging, images, live, metrics, vm_metrics
from .imaging import ImageError, inspect_image
from .models import Device, DeviceImage, Interface, NetworkProductImage
from .tests import T, dev, prom_row, users


# ------------------------------------------------------------------------------------------ synthetic images
def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


def png(w=1, h=1, tail=b"", extra=b"", color=(255, 91, 46)):
    rows = min(h, 8)                       # the parser never inflates; a few rows keep the file small
    raw = b"".join(b"\x00" + bytes(color) * w for _ in range(rows)) if w <= 64 else b"\x00" * 16
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) + extra
            + _chunk(b"IDAT", zlib.compress(raw)) + _chunk(b"IEND", b"") + tail)


def jpeg(w=1, h=1, tail=b"", scan=b"\x00\x01\x02\xff\x00\x03"):
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    sos = b"\xff\xda" + struct.pack(">HBBBBBB", 8, 1, 1, 0, 0, 0x3F, 0)
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + sos + scan + b"\xff\xd9" + tail


def webp(w=1, h=1, tail=b"", kind="lossless"):
    if kind == "lossless":
        body = b"\x2f" + struct.pack("<I", (w - 1) | ((h - 1) << 14))
        chunk = b"VP8L" + struct.pack("<I", len(body)) + body + b"\x00"       # odd length: one pad byte
    elif kind == "lossy":
        body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", w, h)
        chunk = b"VP8 " + struct.pack("<I", len(body)) + body
    elif kind == "animated":
        vp8x = b"VP8X" + struct.pack("<I", 10) + bytes([0x02, 0, 0, 0]) + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
        chunk = vp8x + b"ANIM" + struct.pack("<I", 6) + b"\x00" * 6
    else:                                                                       # extended, still
        vp8x = b"VP8X" + struct.pack("<I", 10) + bytes([0, 0, 0, 0]) + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
        body = b"\x2f" + struct.pack("<I", (w - 1) | ((h - 1) << 14))
        chunk = vp8x + b"VP8L" + struct.pack("<I", len(body)) + body + b"\x00"
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk + tail


SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script></svg>'


def upload(data, name="photo.png", ctype="image/png"):
    return SimpleUploadedFile(name, data, content_type=ctype)


# ------------------------------------------------------------------------------------------ parser
class ParserTests(SimpleTestCase):
    def test_each_format_reports_type_and_dimensions(self):
        for data, kind, size in ((png(40, 30), "image/png", (40, 30)), (jpeg(640, 480), "image/jpeg", (640, 480)),
                                 (webp(800, 600), "image/webp", (800, 600)), (webp(320, 200, kind="lossy"), "image/webp", (320, 200)),
                                 (webp(100, 50, kind="extended"), "image/webp", (100, 50))):
            info = inspect_image(data)
            self.assertEqual((info.content_type, (info.width, info.height)), (kind, size))
        self.assertEqual([inspect_image(x).ext for x in (png(), jpeg(), webp())], ["png", "jpg", "webp"])

    def test_dimension_cap(self):
        inspect_image(png(4096, 4096))
        inspect_image(jpeg(4096, 1))
        inspect_image(webp(1, 4096))
        for bad in (png(4097, 10), png(10, 4097), jpeg(5000, 5000), webp(4097, 1), webp(70000 % 16384 + 4100, 2)):
            with self.assertRaisesMessage(ImageError, "limit is 4096"):
                inspect_image(bad)
        with self.assertRaises(ImageError):
            inspect_image(png(0, 5))
        with self.assertRaises(ImageError):
            inspect_image(jpeg(0, 5))

    def test_truncated_input(self):
        for good in (png(8, 8), jpeg(8, 8), webp(8, 8), webp(8, 8, kind="lossy")):
            for cut in (len(good) - 1, len(good) // 2, 20, 12, 9, 4):
                with self.assertRaises(ImageError, msg=f"{good[:4]!r} cut at {cut}"):
                    inspect_image(good[:cut])

    def test_empty_and_oversized_files(self):
        with self.assertRaisesMessage(ImageError, "empty"):
            inspect_image(b"")
        with self.assertRaisesMessage(ImageError, "limit is 2 MB"):
            inspect_image(png(1, 1, tail=b"\x00" * (imaging.MAX_BYTES + 1)))

    def test_svg_html_and_gif_are_refused_with_a_reason(self):
        for data in (SVG, b"\xef\xbb\xbf  " + SVG, b"<!DOCTYPE html><html><script>alert(1)</script></html>", b"<svg/>"):
            with self.assertRaisesMessage(ImageError, "SVG and HTML"):
                inspect_image(data)
        with self.assertRaisesMessage(ImageError, "GIF"):
            inspect_image(b"GIF89a/*" + b"\x00" * 20 + b"*/=alert(1);")
        with self.assertRaisesMessage(ImageError, "not a PNG, JPEG or WebP"):
            inspect_image(b"BM" + b"\x00" * 40)
        with self.assertRaises(ImageError):
            inspect_image(b"PK\x03\x04" + b"\x00" * 40)

    def test_declared_type_and_file_name_must_agree_with_the_bytes(self):
        inspect_image(png(), declared_type="image/png", filename="a.png")
        inspect_image(jpeg(), declared_type="image/pjpeg", filename="A.JPEG")
        inspect_image(png(), declared_type="application/octet-stream", filename="no-extension")
        with self.assertRaisesMessage(ImageError, "contents are image/jpeg"):
            inspect_image(jpeg(), declared_type="image/png")
        with self.assertRaisesMessage(ImageError, "contents are image/png"):
            inspect_image(png(), filename="photo.jpg")
        with self.assertRaises(ImageError):
            inspect_image(png(), declared_type="text/html")
        with self.assertRaises(ImageError):
            inspect_image(webp(), declared_type="image/svg+xml")

    def test_polyglots_and_damaged_containers(self):
        html = b"<html><script>alert(document.cookie)</script></html>"
        polyglots = {
            "png + html": png(tail=html), "png + zip": png(tail=b"PK\x03\x04" + b"\x00" * 30),
            "jpeg + html": jpeg(tail=html), "jpeg + jpeg": jpeg(tail=jpeg()), "webp + html": webp(tail=html),
            "png second IEND": png(tail=_chunk(b"IEND", b"")),
        }
        for name, data in polyglots.items():
            with self.assertRaisesMessage(ImageError, "extra data", msg=name) if name != "webp + html" else self.assertRaises(ImageError):
                inspect_image(data)
        # trailing NUL / whitespace padding is not content
        inspect_image(jpeg(tail=b"\x00\x00\n"))
        bad_crc = bytearray(png(4, 4))
        bad_crc[29] ^= 0xFF                       # inside IHDR's CRC
        with self.assertRaisesMessage(ImageError, "checksum"):
            inspect_image(bytes(bad_crc))
        with self.assertRaises(ImageError):       # IDAT before IHDR
            inspect_image(b"\x89PNG\r\n\x1a\n" + _chunk(b"IDAT", b"x") + _chunk(b"IEND", b""))
        with self.assertRaises(ImageError):       # no image data
            inspect_image(b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + _chunk(b"IEND", b""))
        with self.assertRaises(ImageError):       # JPEG segment that overruns the file
            inspect_image(b"\xff\xd8\xff\xe0\xff\xff" + b"\x00" * 10)
        with self.assertRaises(ImageError):       # JPEG without a frame header
            inspect_image(b"\xff\xd8\xff\xe0" + struct.pack(">H", 2) + b"\xff\xd9")
        with self.assertRaises(ImageError):       # RIFF size that disagrees with the file
            inspect_image(webp()[:4] + struct.pack("<I", 9999) + webp()[8:])

    def test_animated_webp_is_refused(self):
        with self.assertRaisesMessage(ImageError, "Animated"):
            inspect_image(webp(10, 10, kind="animated"))


# ------------------------------------------------------------------------------------------ fake network
class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", chunk=65536, read_error=None):
        self.status, self._headers, self._body, self._chunk, self._read_error = status, headers or {}, body, chunk, read_error
        self._pos = 0

    def getheader(self, name, default=None):
        return {k.lower(): v for k, v in self._headers.items()}.get(name.lower(), default)

    def read(self, n=-1):
        if self._read_error:
            raise self._read_error
        n = min(n, self._chunk) if n and n > 0 else self._chunk
        out = self._body[self._pos:self._pos + n]
        self._pos += len(out)
        return out


class FakeConn:
    def __init__(self, response, log):
        self._response, self._log = response, log
        self._probe_sock = None

    def request(self, method, path, headers=None):
        self._log.append((method, path, dict(headers or {})))

    def getresponse(self):
        return self._response

    def close(self):
        pass


class FakeNet:
    """Replaces DNS and the connection factory in probes.checks: nothing here can open a socket."""

    def __init__(self, dns, routes):
        self.dns, self.routes, self.requests, self.opened = dns, routes, [], []
        self.patches = [mock.patch.object(checks, "resolve", side_effect=self._resolve),
                        mock.patch.object(checks, "_open_connection", side_effect=self._open)]

    def _resolve(self, host, port=443):
        self.dns_seen = getattr(self, "dns_seen", []) + [(host, port)]
        if host not in self.dns:
            raise socket.gaierror(-2, "Name or service not known")
        return self.dns[host]

    def _open(self, scheme, host, ip, port, timeout):
        self.opened.append((scheme, host, ip, port))
        route = self.routes[(host, port)]
        return FakeConn(route() if callable(route) else route, self.requests)

    def __enter__(self):
        for p in self.patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self.patches:
            p.stop()


IMG = png(8, 6)
OK_HEADERS = {"Content-Type": "image/png"}
PUBLIC = "93.184.216.34"


@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=[], PROBE_TIMEOUT=5)
class FetchTests(TestCase):
    def fetch(self, url, dns=None, routes=None, **kw):
        dns = dns if dns is not None else {"cdn.example.com": [PUBLIC]}
        routes = routes if routes is not None else {("cdn.example.com", 443): FakeResponse(200, OK_HEADERS, IMG)}
        with FakeNet(dns, routes) as net:
            self.net = net
            return checks.fetch_bytes(url, imaging.MAX_BYTES, timeout=kw.pop("timeout", 5), **kw)

    def test_success_sends_a_plain_identity_request(self):
        r = self.fetch("https://cdn.example.com/p/a.png?v=3")
        self.assertTrue(r.ok, r.error)
        self.assertEqual((r.body, r.content_type, r.ip), (IMG, "image/png", PUBLIC))
        method, path, headers = self.net.requests[0]
        self.assertEqual((method, path, headers["Accept-Encoding"]), ("GET", "/p/a.png?v=3", "identity"))
        self.assertNotIn("Cookie", headers)
        self.assertNotIn("Authorization", headers)

    def test_private_and_loopback_addresses_are_refused(self):
        for ip in ("10.1.2.3", "127.0.0.1", "192.168.0.9", "169.254.169.254", "::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
                   "100.64.0.1", "100.100.100.200", "64:ff9b::a00:1", "fd00::1", "fe80::1", "0.0.0.0", "198.18.0.1", "192.0.0.8"):
            r = self.fetch("https://cdn.example.com/a.png", dns={"cdn.example.com": [ip]})
            self.assertFalse(r.ok, ip)
            self.assertIn("Blocked by probe policy", r.error)
            self.assertNotIn(ip, r.error, "the error must not reveal what a host name resolves to")
            self.assertEqual(self.net.opened, [], "no connection may be opened to a refused address")

    def test_refusals_do_not_reveal_whether_an_internal_name_exists(self):
        """A caller must not be able to tell 'resolves to a private address' from 'does not resolve' (internal DNS probing)."""
        private = self.fetch("https://intranet.corp.example/a.png", dns={"intranet.corp.example": ["10.9.8.7"]})
        missing = self.fetch("https://no-such-host.example/a.png", dns={})
        self.assertEqual(private.error, missing.error)
        self.assertNotIn("10.9.8.7", private.error)
        # a mix of private and public answers connects only to the public one (the address is pinned after the check)
        mixed = self.fetch("https://cdn.example.com/a.png", dns={"cdn.example.com": ["10.0.0.5", PUBLIC]})
        self.assertTrue(mixed.ok, mixed.error)
        self.assertEqual([o[2] for o in self.net.opened], [PUBLIC])

    def test_the_server_declared_type_is_truncated_in_the_message(self):
        r = self.fetch("https://cdn.example.com/a.png", accepted_types=images.ACCEPTED_TYPES, routes={
            ("cdn.example.com", 443): FakeResponse(200, {"Content-Type": "x/" + "a" * 5000}, IMG)})
        self.assertFalse(r.ok)
        self.assertLess(len(r.error), 200)

    def test_a_private_range_can_be_allowed_by_the_operator_only(self):
        with override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=["10.1.0.0/16"]):
            r = self.fetch("https://cdn.example.com/a.png", dns={"cdn.example.com": ["10.1.2.3"]})
        self.assertTrue(r.ok, r.error)

    def test_redirect_to_a_private_address_is_refused(self):
        routes = {("cdn.example.com", 443): FakeResponse(302, {"Location": "https://internal.example.com/secret.png"}),
                  ("internal.example.com", 443): FakeResponse(200, OK_HEADERS, IMG)}
        r = self.fetch("https://cdn.example.com/a.png", dns={"cdn.example.com": [PUBLIC], "internal.example.com": ["10.0.0.7"]},
                       routes=routes)
        self.assertFalse(r.ok)
        self.assertIn("Blocked by probe policy", r.error)
        self.assertEqual([o[1] for o in self.net.opened], ["cdn.example.com"])

    def test_redirect_to_http_or_another_port_is_refused_and_a_safe_redirect_is_followed(self):
        for location, text in (("http://cdn.example.com/a.png", "only https"), ("https://cdn.example.com:8443/a.png", "only port 443"),
                               ("https://user:pw@cdn.example.com/a.png", "user name or password")):
            routes = {("cdn.example.com", 443): FakeResponse(301, {"Location": location})}
            r = self.fetch("https://cdn.example.com/a.png", routes=routes)
            self.assertFalse(r.ok, location)
            self.assertIn(text, r.error)
        calls = []
        routes = {("cdn.example.com", 443): lambda: calls.append(1) or (FakeResponse(302, {"Location": "/real.png"}) if len(calls) == 1
                                                                       else FakeResponse(200, OK_HEADERS, IMG))}
        r = self.fetch("https://cdn.example.com/a.png", routes=routes)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.final_url, "https://cdn.example.com/real.png")

    def test_redirect_loop_stops(self):
        routes = {("cdn.example.com", 443): lambda: FakeResponse(302, {"Location": "/again"})}
        r = self.fetch("https://cdn.example.com/a.png", routes=routes)
        self.assertFalse(r.ok)
        self.assertIn("too many redirects", r.error)

    def test_non_https_and_credential_urls_are_refused_before_any_lookup(self):
        for url in ("http://cdn.example.com/a.png", "ftp://cdn.example.com/a.png", "https://u:p@cdn.example.com/a.png",
                    "https://cdn.example.com:8443/a.png", "file:///etc/passwd", "https:///nohost"):
            r = self.fetch(url)
            self.assertFalse(r.ok, url)
            self.assertEqual(self.net.opened, [])

    def test_oversize_by_header_and_by_stream(self):
        big = b"\x00" * (imaging.MAX_BYTES + 10)
        r = self.fetch("https://cdn.example.com/a.png", routes={("cdn.example.com", 443): FakeResponse(
            200, {**OK_HEADERS, "Content-Length": str(len(big))}, big)})
        self.assertIn("larger than", r.error)
        r = self.fetch("https://cdn.example.com/a.png", routes={("cdn.example.com", 443): FakeResponse(200, OK_HEADERS, big)})
        self.assertIn("larger than", r.error)             # no Content-Length: refused as soon as the limit is passed
        self.assertEqual(r.body, b"")

    def test_slow_body_times_out(self):
        r = self.fetch("https://cdn.example.com/a.png", routes={("cdn.example.com", 443): FakeResponse(
            200, OK_HEADERS, IMG, read_error=socket.timeout())})
        self.assertFalse(r.ok)
        self.assertIn("Timed out", r.error)
        # the overall deadline also stops a peer that keeps sending small chunks
        trickle = FakeResponse(200, OK_HEADERS, IMG, chunk=1)
        real_read = trickle.read
        trickle.read = lambda n=-1: (time.sleep(0.1), real_read(n))[1]
        r = self.fetch("https://cdn.example.com/a.png", timeout=0.3, routes={("cdn.example.com", 443): trickle})
        self.assertFalse(r.ok, "a peer that trickles bytes must hit the overall deadline")
        self.assertIn("Timed out", r.error)

    def test_wrong_content_type_status_and_encoding(self):
        accepted = images.ACCEPTED_TYPES
        for headers, text in (({"Content-Type": "text/html"}, "not an accepted image type"),
                              ({"Content-Type": "image/svg+xml"}, "not an accepted image type"),
                              ({}, "no content type"),
                              ({**OK_HEADERS, "Content-Encoding": "gzip"}, "compressed")):
            r = self.fetch("https://cdn.example.com/a.png", accepted_types=accepted,
                           routes={("cdn.example.com", 443): FakeResponse(200, headers, IMG)})
            self.assertFalse(r.ok, headers)
            self.assertIn(text, r.error)
        r = self.fetch("https://cdn.example.com/a.png", routes={("cdn.example.com", 443): FakeResponse(404, OK_HEADERS, b"x")})
        self.assertIn("HTTP 404", r.error)

    def test_dns_failure_is_reported(self):
        r = self.fetch("https://nope.example.com/a.png", dns={})
        self.assertFalse(r.ok)
        self.assertIn("could not be resolved", r.error)

    def test_the_tls_handshake_runs_under_the_deadline_watchdog(self):
        """The socket must be reachable by the watchdog before the handshake starts, or a peer that trickles TLS records
        holds the caller open for several socket timeouts."""
        calls = []
        raw, tls = mock.MagicMock(name="raw"), mock.MagicMock(name="tls")
        tls.do_handshake.side_effect = lambda: calls.append(("handshake", conn._probe_sock))
        ctx = mock.MagicMock()
        ctx.wrap_socket.return_value = tls
        with mock.patch.object(checks.socket, "create_connection", return_value=raw):
            conn = checks._PinnedHTTPSConnection("cdn.example.com", PUBLIC, 443, 5, ctx)
            conn.connect()
        self.assertEqual(ctx.wrap_socket.call_args.kwargs["do_handshake_on_connect"], False)
        self.assertEqual(ctx.wrap_socket.call_args.kwargs["server_hostname"], "cdn.example.com")
        self.assertEqual(calls, [("handshake", tls)], "_probe_sock must already be the TLS socket during the handshake")
        self.assertIs(conn.sock, tls)


@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=[])
class DownloadAndStoreTests(TestCase):
    def net(self, body=IMG, headers=None, status=200, dns=None):
        return FakeNet(dns or {"cdn.example.com": [PUBLIC]},
                       {("cdn.example.com", 443): FakeResponse(status, headers or OK_HEADERS, body)})

    def test_fetched_image_is_validated_stored_and_never_linked(self):
        with self.net():
            image, created = images.fetch_and_store("https://cdn.example.com/x/y.png?token=SECRET#frag")
        self.assertTrue(created)
        self.assertEqual((image.source, image.content_type, image.width, image.height), ("fetched", "image/png", 8, 6))
        self.assertEqual(image.source_url, "https://cdn.example.com/x/y.png")      # query string and fragment are not kept
        self.assertEqual(bytes(image.data), IMG)
        self.assertEqual(image.sha256, hashlib.sha256(IMG).hexdigest())

    def test_server_lies_are_caught_by_the_bytes(self):
        for body in (SVG, b"<html>x</html>", png(tail=b"<script>"), b"GIF89a" + b"\x00" * 20):
            with self.net(body=body, headers={"Content-Type": "image/png"}):
                with self.assertRaises(ImageError):
                    images.fetch_and_store("https://cdn.example.com/a.png")
        with self.net(body=jpeg(), headers={"Content-Type": "image/png"}):          # the server claims PNG, the bytes are JPEG
            with self.assertRaisesMessage(ImageError, "contents are image/jpeg"):
                images.fetch_and_store("https://cdn.example.com/a.png")
        self.assertEqual(DeviceImage.objects.count(), 0)

    def test_policy_refusals_surface_as_image_errors(self):
        with self.net(dns={"cdn.example.com": ["10.9.9.9"]}):
            with self.assertRaisesMessage(ImageError, "Blocked by probe policy"):
                images.fetch_and_store("https://cdn.example.com/a.png")
        for url in ("http://cdn.example.com/a.png", "", "https://", "javascript:alert(1)", "https://x" + "a" * 600):
            with self.assertRaises(ImageError, msg=url):
                images.download(url)

    def test_dedupe_by_hash_per_tenant(self):
        a, created_a = images.store_image(IMG, source="upload")
        b, created_b = images.store_image(IMG, source="fetched", source_url="https://cdn.example.com/other.png")
        self.assertEqual((a.pk, created_a, created_b), (b.pk, True, False))
        self.assertEqual(DeviceImage.objects.count(), 1)
        other, created = images.store_image(IMG, source="upload", tenant_key="other-tenant")
        self.assertTrue(created)
        self.assertNotEqual(other.pk, a.pk)
        with self.assertRaises(IntegrityError), transaction.atomic():              # the database enforces it too
            DeviceImage.objects.create(tenant_key=T, sha256=a.sha256, content_type="image/png", width=1, height=1, byte_size=1, data=b"x")

    def test_database_rejects_a_bad_row_even_if_the_parser_was_skipped(self):
        for kwargs in ({"content_type": "image/svg+xml"}, {"width": 5000}, {"byte_size": 3 * 1024 * 1024}, {"height": 0}):
            fields = dict(tenant_key=T, sha256=hashlib.sha256(str(kwargs).encode()).hexdigest(), content_type="image/png",
                          width=1, height=1, byte_size=10, data=b"x", **{})
            fields.update(kwargs)
            with self.assertRaises(IntegrityError, msg=str(kwargs)), transaction.atomic():
                DeviceImage.objects.create(**fields)

    def test_library_cap(self):
        with mock.patch.object(images, "MAX_IMAGES_PER_TENANT", 1):
            images.store_image(IMG, source="upload")
            with self.assertRaisesMessage(ImageError, "library is full"):
                images.store_image(png(2, 2), source="upload")
            images.store_image(IMG, source="upload")          # an existing photo is still found

    def test_release_keeps_photos_that_are_still_used(self):
        image, _ = images.store_image(IMG, source="upload")
        d = dev("keep-01", image=image)
        images.release(image)
        self.assertTrue(DeviceImage.objects.filter(pk=image.pk).exists())
        d.delete()
        images.release(image)
        self.assertFalse(DeviceImage.objects.filter(pk=image.pk).exists())

    def test_clean_source_url(self):
        self.assertEqual(images.clean_source_url("https://u:p@cdn.example.com:443/a/b.png?sig=1#x"), "https://cdn.example.com/a/b.png")
        self.assertEqual(images.clean_source_url("https://cdn.example.com:8443"), "https://cdn.example.com:8443/")
        self.assertEqual(images.clean_source_url("http://cdn.example.com/a.png"), "")


# ------------------------------------------------------------------------------------------ serving
@override_settings(LINKEDEYE_TENANT=T)
class ServingTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.mum = Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        self.dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        self.img, _ = images.store_image(IMG, source="upload")
        self.dev = dev("mum-sw-01", site="Mumbai", image=self.img)
        self.url = self.img.url
        self.c = Client()
        self.c.force_login(self.viewer)

    def test_url_shape(self):
        self.assertEqual(self.url, f"/network/images/{self.img.sha256}.png")

    def test_login_required(self):
        r = Client().get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])

    def test_headers_and_body(self):
        r = self.c.get(self.url)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, IMG)
        self.assertEqual(r["Content-Type"], "image/png")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["Content-Security-Policy"], "sandbox; default-src 'none'")
        self.assertEqual(r["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(r["ETag"], f'"{self.img.sha256}"')
        self.assertEqual(r["Content-Disposition"], 'inline; filename="device-photo.png"')
        self.assertEqual(r["Cross-Origin-Resource-Policy"], "same-origin")
        self.assertIn("Cookie", r["Vary"])

    def test_a_maximum_size_photo_round_trips_through_the_database(self):
        """BinaryField comes back as bytes on SQLite and as memoryview on PostgreSQL; a 2 MB row must be served byte for byte
        either way, and the list queries that defer the column must not read it."""
        filler = imaging.MAX_BYTES - len(png(1, 1)) - 12      # a chunk adds 12 bytes of framing
        big = png(1, 1, extra=_chunk(b"tEXt", b"k\x00" + b"v" * (filler - 2)))
        self.assertEqual(len(big), imaging.MAX_BYTES)
        stored, created = images.store_image(big, source="upload")
        self.assertTrue(created)
        self.assertEqual(stored.byte_size, imaging.MAX_BYTES)
        raw = DeviceImage.objects.filter(pk=stored.pk).values_list("data", flat=True).get()
        self.assertIsInstance(raw, (bytes, memoryview))
        self.assertEqual(bytes(raw), big)
        Device.objects.filter(pk=self.dev.pk).update(image=stored)
        r = self.c.get(stored.url)
        self.assertEqual((r.status_code, len(r.content), r["Content-Length"]), (200, imaging.MAX_BYTES, str(imaging.MAX_BYTES)))
        self.assertEqual(r.content, big)
        with self.assertRaises(ImageError):                  # one byte more is refused before it reaches the database
            images.store_image(big + b"\x00", source="upload")
        self.assertEqual(DeviceImage.objects.filter(pk=stored.pk).defer("data").get().byte_size, imaging.MAX_BYTES)

    def test_the_type_comes_from_the_stored_row_not_the_request(self):
        r = self.c.get(self.url + "?type=text/html&name=evil.html", HTTP_ACCEPT="text/html")
        self.assertEqual((r["Content-Type"], r["Content-Disposition"]), ("image/png", 'inline; filename="device-photo.png"'))
        jpg, _ = images.store_image(jpeg(), source="upload")
        Device.objects.filter(pk=self.dev.pk).update(image=jpg)
        r = self.c.get(jpg.url)
        self.assertEqual((r["Content-Type"], r["Content-Disposition"]), ("image/jpeg", 'inline; filename="device-photo.jpg"'))

    def test_conditional_get_returns_304_with_the_same_headers(self):
        etag = f'"{self.img.sha256}"'
        for header in (etag, "W/" + etag, f'"nope", {etag}', "*"):
            r = self.c.get(self.url, HTTP_IF_NONE_MATCH=header)
            self.assertEqual(r.status_code, 304, header)
            self.assertEqual(r.content, b"")
            self.assertEqual(r["ETag"], etag)
            self.assertEqual(r["X-Content-Type-Options"], "nosniff")
            self.assertIn("immutable", r["Cache-Control"])
        self.assertEqual(self.c.get(self.url, HTTP_IF_NONE_MATCH='"other"').status_code, 200)

    def test_wrong_extension_unknown_and_malformed_hashes_are_404(self):
        sha = self.img.sha256
        for path in (f"/network/images/{sha}.jpg", f"/network/images/{sha}.webp", f"/network/images/{sha}.svg", f"/network/images/{sha}.png/",
                     f"/network/images/{'0' * 64}.png", f"/network/images/{sha[:63]}.png", f"/network/images/{sha.upper()}.png",
                     f"/network/images/../{sha}.png", f"/network/images/{sha}",
                     f"/network/images/{sha}.png%0A", f"/network/images/{sha}%0A.png", f"/network/images/%20{sha}.png"):
            self.assertEqual(self.c.get(path).status_code, 404, path)

    def test_the_view_itself_rejects_a_hash_with_a_trailing_newline(self):
        """`$` matches before a trailing newline; the view must not rely on the URL pattern alone."""
        from . import image_views
        from django.http import Http404
        rf = __import__("django.test", fromlist=["RequestFactory"]).RequestFactory()
        request = rf.get("/x")
        request.user = self.viewer
        with self.assertRaises(Http404):
            image_views.image(request, self.img.sha256 + "\n", "png")

    def test_only_get_and_head(self):
        self.assertEqual(self.c.post(self.url).status_code, 405)
        r = self.c.head(self.url)
        self.assertEqual((r.status_code, r.content), (200, b""))

    def test_site_scoping_for_device_photos(self):
        Membership.objects.create(user=self.viewer, site=self.dr, role="viewer")
        self.assertEqual(self.c.get(self.url).status_code, 404)                       # sees DR only; the photo belongs to Mumbai
        self.assertEqual(self.c.get(self.url, HTTP_IF_NONE_MATCH=f'"{self.img.sha256}"').status_code, 404)   # a cached ETag opens nothing
        Membership.objects.create(user=self.viewer, site=self.mum, role="viewer")
        self.c.force_login(User.objects.get(pk=self.viewer.pk))
        self.assertEqual(self.c.get(self.url).status_code, 200)

    def test_all_sites_devices_and_tenant_wide_users(self):
        Device.objects.filter(pk=self.dev.pk).update(site="All Sites")
        Membership.objects.create(user=self.viewer, site=self.dr, role="viewer")
        self.assertEqual(self.c.get(self.url).status_code, 200)

    def test_catalog_photos_are_tenant_wide_and_unattached_photos_are_not_served(self):
        Membership.objects.create(user=self.viewer, site=self.dr, role="viewer")
        Device.objects.filter(pk=self.dev.pk).update(image=None)
        self.assertEqual(self.c.get(self.url).status_code, 404)                      # attached to nothing
        NetworkProductImage.objects.create(tenant_key=T, vendor="Acme", image=self.img)
        self.assertEqual(self.c.get(self.url).status_code, 200)

    def test_other_tenants_photos_are_invisible(self):
        foreign, _ = images.store_image(png(3, 3), source="upload", tenant_key="other-tenant")
        Device.objects.create(tenant_key="other-tenant", name="x", mgmt_ip="192.0.2.5", image=foreign)
        self.assertEqual(self.c.get(foreign.url).status_code, 404)


# ------------------------------------------------------------------------------------------ device form: upload RBAC
def form_data(**kw):
    data = {"name": "edge-sw-01", "device_type": "switch", "mgmt_ip": "192.0.2.10", "check_port": "22", "site": "Mumbai",
            "environment": "prod"}
    data.update(kw)
    return data


@override_settings(LINKEDEYE_TENANT=T)
class DeviceFormPhotoTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.mum = Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        self.dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        self.create_url = reverse("network:create")
        self.c = Client()

    def post(self, user, data=None, url=None, **files):
        self.c.force_login(user)
        return self.c.post(url or self.create_url, {**(data or form_data()), **files})

    def test_form_is_multipart_and_explains_the_photo_options(self):
        self.c.force_login(self.operator)
        html = self.c.get(self.create_url).content.decode()
        for text in ('enctype="multipart/form-data"', "Upload a photo", "Fetch from a web address", "Pick from the product catalog",
                     "does not bundle vendor photos", 'accept="image/png,image/jpeg,image/webp"', "Linked image URL (legacy)"):
            self.assertIn(text, html)

    def test_operator_uploads_a_photo_and_it_is_stored_and_audited(self):
        with mock.patch("network.jobs.check_devices"):
            r = self.post(self.operator, image_file=upload(IMG))
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        d = Device.objects.get(name="edge-sw-01")
        self.assertEqual(d.image.sha256, hashlib.sha256(IMG).hexdigest())
        self.assertEqual((d.image.source, d.image.uploaded_by_id), ("upload", self.operator.pk))
        self.assertEqual(d.product_image_url, "")
        ev = AuditEvent.objects.get(action="create", object_type="network.device")
        self.assertEqual(ev.actor_name, "operator")

    def test_viewer_cannot_upload(self):
        r = self.post(self.viewer, image_file=upload(IMG))
        self.assertIn(r.status_code, (302, 403))
        self.assertFalse(Device.objects.exists())
        self.assertFalse(DeviceImage.objects.exists())

    def test_operator_on_another_site_cannot_upload_to_this_sites_device(self):
        d = dev("mum-sw-01", site="Mumbai")
        Membership.objects.create(user=self.operator, site=self.dr, role="operator")
        r = self.post(self.operator, form_data(name="mum-sw-01"), url=reverse("network:edit", args=[d.pk]), image_file=upload(IMG))
        self.assertEqual(r.status_code, 404)             # outside the operator's sites the device does not exist for them
        self.assertFalse(DeviceImage.objects.exists())
        # a device on a site they may see but only view
        Membership.objects.filter(user=self.operator).delete()
        Membership.objects.create(user=self.operator, site=self.dr, role="operator")
        Membership.objects.create(user=self.operator, site=self.mum, role="viewer")
        r = self.post(User.objects.get(pk=self.operator.pk), form_data(name="mum-sw-01"), url=reverse("network:edit", args=[d.pk]),
                      image_file=upload(IMG))
        self.assertEqual(r.status_code, 403)
        self.assertFalse(DeviceImage.objects.exists())

    def test_operator_cannot_create_a_device_with_a_photo_on_a_site_they_do_not_manage(self):
        Membership.objects.create(user=self.operator, site=self.dr, role="operator")
        r = self.post(self.operator, form_data(site="Mumbai"), image_file=upload(IMG))
        self.assertEqual(r.status_code, 200)
        self.assertIn("cannot manage devices on this site", r.content.decode())
        self.assertFalse(Device.objects.exists())
        self.assertFalse(DeviceImage.objects.exists(), "the upload must not be stored before the permission check passes")

    def test_operator_on_this_site_can(self):
        Membership.objects.create(user=self.operator, site=self.mum, role="operator")
        with mock.patch("network.jobs.check_devices"):
            r = self.post(self.operator, image_file=upload(IMG))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(Device.objects.get().image_id)

    def test_csrf_is_enforced(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.operator)
        r = c.post(self.create_url, {**form_data(), "image_file": upload(IMG)})
        self.assertEqual(r.status_code, 403)
        self.assertFalse(Device.objects.exists())

    def test_bad_uploads_are_refused_with_a_message_and_nothing_is_saved(self):
        cases = ((SVG, "x.svg", "image/svg+xml", "SVG and HTML"), (SVG, "x.png", "image/png", "SVG and HTML"),
                 (png(tail=b"<script>alert(1)</script>"), "x.png", "image/png", "extra data"),
                 (jpeg(), "x.png", "image/png", "contents are image/jpeg"), (png(5000, 5000), "x.png", "image/png", "limit is 4096"),
                 (b"GIF89a" + b"\x00" * 30, "x.gif", "image/gif", "GIF"), (b"", "x.png", "image/png", "empty"),
                 (png(tail=b"\x00" * (3 * 1024 * 1024)), "big.png", "image/png", "limit is 2 MB"))
        for data, name, ctype, text in cases:
            r = self.post(self.operator, image_file=upload(data, name, ctype))
            self.assertEqual(r.status_code, 200, name)
            self.assertIn(text, r.content.decode(), (name, text))
        self.assertFalse(Device.objects.exists())
        self.assertFalse(DeviceImage.objects.exists())

    def test_only_one_way_to_set_the_photo(self):
        r = self.post(self.operator, form_data(image_url="https://cdn.example.com/a.png"), image_file=upload(IMG))
        self.assertIn("Use one way to set the photo", r.content.decode())
        self.assertFalse(Device.objects.exists())

    def test_fetch_from_url_stores_the_bytes_once(self):
        with FakeNet({"cdn.example.com": [PUBLIC]}, {("cdn.example.com", 443): FakeResponse(200, OK_HEADERS, IMG)}) as net, \
                mock.patch("network.jobs.check_devices"), override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[]):
            r = self.post(self.operator, form_data(image_url="https://cdn.example.com/p/a.png?sig=abc"))
        self.assertEqual(r.status_code, 302)
        d = Device.objects.get()
        self.assertEqual((d.image.source, d.image.source_url, d.product_image_url), ("fetched", "https://cdn.example.com/p/a.png", ""))
        self.assertEqual(len(net.opened), 1)
        # showing the device never touches the network again
        with mock.patch.object(checks, "fetch_bytes", side_effect=AssertionError("hotlinking")):
            self.c.get(d.get_absolute_url())
            self.c.get(reverse("network:all"))

    def test_fetch_failures_keep_the_form_open(self):
        with FakeNet({"cdn.example.com": ["10.0.0.4"]}, {}), override_settings(PROBE_ALLOWED_PRIVATE_CIDRS=[]):
            r = self.post(self.operator, form_data(image_url="https://cdn.example.com/a.png"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Blocked by probe policy", r.content.decode())
        r = self.post(self.operator, form_data(image_url="http://cdn.example.com/a.png"))
        self.assertIn("Only https://", r.content.decode())
        self.assertFalse(Device.objects.exists())

    def test_catalog_pick_auto_attach_and_replacement_release(self):
        cat, _ = images.store_image(png(12, 12), source="upload")
        row = NetworkProductImage.objects.create(tenant_key=T, vendor="Arista", model_key="DCS-7050", image=cat)
        with mock.patch("network.jobs.check_devices"):
            self.post(self.operator, form_data(vendor="Arista", model="DCS-7050TX"))                  # no choice: the match is attached
            self.assertEqual(Device.objects.get().image_id, cat.pk)
            other = Device.objects.create(tenant_key=T, name="plain", mgmt_ip="192.0.2.77", vendor="Zed")
            self.post(self.operator, form_data(name="plain", mgmt_ip="192.0.2.77", vendor="Zed", catalog_row=row.pk),
                      url=reverse("network:edit", args=[other.pk]))
            other.refresh_from_db()
            self.assertEqual(other.image_id, cat.pk)
            # an explicit upload replaces it; the catalog photo stays (the catalog row still uses it)
            self.post(self.operator, form_data(name="plain", mgmt_ip="192.0.2.77", vendor="Zed"), url=reverse("network:edit", args=[other.pk]),
                      image_file=upload(IMG))
            other.refresh_from_db()
            self.assertEqual(other.image.sha256, hashlib.sha256(IMG).hexdigest())
            self.assertTrue(DeviceImage.objects.filter(pk=cat.pk).exists())
            # remove: the now unused upload is deleted, an existing photo is never swapped behind the user's back
            self.post(self.operator, form_data(name="plain", mgmt_ip="192.0.2.77", vendor="Zed", clear_image="on"),
                      url=reverse("network:edit", args=[other.pk]))
            other.refresh_from_db()
            self.assertIsNone(other.image_id)
            self.assertFalse(DeviceImage.objects.filter(sha256=hashlib.sha256(IMG).hexdigest()).exists())
            self.post(self.operator, form_data(name="plain", mgmt_ip="192.0.2.77", vendor="Arista", model="DCS-7050"),
                      url=reverse("network:edit", args=[other.pk]))
            other.refresh_from_db()
            self.assertEqual(other.image_id, cat.pk)          # vendor changed and no photo: the catalog match is attached

    def test_legacy_catalog_row_still_fills_the_link_fields(self):
        NetworkProductImage.objects.create(tenant_key=T, vendor="Old", product_image_url="https://cdn.example/old.webp")
        with mock.patch("network.jobs.check_devices"):
            self.post(self.operator, form_data(vendor="Old"))
        d = Device.objects.get()
        self.assertEqual((d.product_image_url, d.image_id), ("https://cdn.example/old.webp", None))


# ------------------------------------------------------------------------------------------ catalog management
@override_settings(LINKEDEYE_TENANT=T)
class CatalogPageTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.c = Client()
        self.c.force_login(self.admin)

    def test_only_admins(self):
        for user in (self.viewer, self.operator):
            self.c.force_login(user)
            for name in ("network:catalog", "network:catalog_create", "network:catalog_export"):
                r = self.c.get(reverse(name))
                self.assertIn(r.status_code, (302, 403), (user.username, name))
            r = self.c.post(reverse("network:catalog_import"), {"csv_text": "vendor,image_url\nA,https://x/a.png"})
            self.assertIn(r.status_code, (302, 403))
        self.assertFalse(NetworkProductImage.objects.exists())
        # an admin of one site is not enough for a tenant-wide catalog
        site = Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        scoped = User.objects.create_user("siteadmin", password="pw", is_staff=True)
        Membership.objects.create(user=scoped, site=site, role="admin")
        self.c.force_login(scoped)
        self.assertEqual(self.c.get(reverse("network:catalog")).status_code, 403)

    def test_empty_page_says_no_photos_are_bundled(self):
        html = self.c.get(reverse("network:catalog")).content.decode()
        self.assertIn("LinkedEye does not bundle vendor photos; add yours here.", html)
        self.assertIn("The catalog is empty", html)
        self.assertEqual(NetworkProductImage.objects.count(), 0, "no seed data")

    def test_create_with_upload_edit_duplicate_and_delete(self):
        r = self.c.post(reverse("network:catalog_create"), {"vendor": "Arista", "device_type": "switch", "model_key": "DCS-7050",
                                                           "image_file": upload(IMG)})
        self.assertEqual(r.status_code, 302)
        row = NetworkProductImage.objects.get()
        self.assertEqual((row.vendor, row.image.source, row.product_image_url), ("Arista", "upload", ""))
        r = self.c.post(reverse("network:catalog_create"), {"vendor": "arista", "device_type": "switch", "model_key": "dcs-7050",
                                                           "image_file": upload(png(2, 2))})
        self.assertIn("already has a row", r.content.decode())
        r = self.c.post(reverse("network:catalog_create"), {"vendor": "Arista", "model_key": "x"})
        self.assertIn("Add a photo", r.content.decode())
        r = self.c.post(reverse("network:catalog_create"), {"vendor": "Arista", "model_key": "svg", "image_file": upload(SVG, "a.svg", "image/svg+xml")})
        self.assertIn("SVG and HTML", r.content.decode())
        # edit keeps the photo, replacing it releases the old bytes
        old = row.image
        self.c.post(reverse("network:catalog_edit", args=[row.pk]), {"vendor": "Arista", "device_type": "switch", "model_key": "DCS-7050X",
                                                                      "notes": "core"})
        row.refresh_from_db()
        self.assertEqual((row.model_key, row.image_id), ("DCS-7050X", old.pk))
        self.c.post(reverse("network:catalog_edit", args=[row.pk]), {"vendor": "Arista", "device_type": "switch", "model_key": "DCS-7050X",
                                                                      "image_file": upload(jpeg(3, 3), "b.jpg", "image/jpeg")})
        row.refresh_from_db()
        self.assertEqual(row.image.content_type, "image/jpeg")
        self.assertFalse(DeviceImage.objects.filter(pk=old.pk).exists())
        # delete: a device that uses the photo keeps it
        dev("uses-it", image=row.image)
        self.c.post(reverse("network:catalog_delete", args=[row.pk]))
        self.assertFalse(NetworkProductImage.objects.exists())
        self.assertTrue(DeviceImage.objects.filter(pk=row.image_id).exists())
        self.assertTrue(AuditEvent.objects.filter(object_type="network.networkproductimage", action="delete").exists())

    def test_fetch_from_url_on_the_row_form(self):
        with FakeNet({"cdn.example.com": [PUBLIC]}, {("cdn.example.com", 443): FakeResponse(200, OK_HEADERS, IMG)}):
            r = self.c.post(reverse("network:catalog_create"), {"vendor": "Cisco", "image_url": "https://cdn.example.com/c.png"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(NetworkProductImage.objects.get().image.source, "fetched")

    def test_store_a_copy_of_a_legacy_row(self):
        row = NetworkProductImage.objects.create(tenant_key=T, vendor="Old", product_image_url="https://cdn.example.com/old.png")
        html = self.c.get(reverse("network:catalog")).content.decode()
        self.assertIn("Link only", html)
        self.assertIn("Store a copy", html)
        with FakeNet({"cdn.example.com": [PUBLIC]}, {("cdn.example.com", 443): FakeResponse(200, OK_HEADERS, IMG)}):
            self.c.post(reverse("network:catalog_store_copy", args=[row.pk]))
        row.refresh_from_db()
        self.assertTrue(row.image_id)
        self.assertEqual(row.product_image_url, "")
        # a failure leaves the row as it was
        row2 = NetworkProductImage.objects.create(tenant_key=T, vendor="Bad", product_image_url="https://cdn.example.com/bad.png")
        with FakeNet({"cdn.example.com": ["10.0.0.1"]}, {}):
            self.c.post(reverse("network:catalog_store_copy", args=[row2.pk]))
        row2.refresh_from_db()
        self.assertEqual((row2.image_id, row2.product_image_url), (None, "https://cdn.example.com/bad.png"))

    def test_export_is_formula_safe_and_round_trips_columns(self):
        img, _ = images.store_image(IMG, source="fetched", source_url="https://cdn.example.com/p.png")
        NetworkProductImage.objects.create(tenant_key=T, vendor="=HYPERLINK(1)", model_key="m1", image=img, product_page_url="https://v.example/m1")
        r = self.c.get(reverse("network:catalog_export"))
        self.assertEqual(r["Content-Type"], "text/csv; charset=utf-8")
        text = r.content.decode()
        self.assertTrue(text.startswith("vendor,device_type,model_key,image_url,product_page_url,notes,image_sha256,image_size"))
        self.assertIn("'=HYPERLINK(1)", text)
        self.assertIn("https://cdn.example.com/p.png", text)
        self.assertIn(img.sha256, text)


CSV_HEAD = "vendor,device_type,model_key,image_url,product_page_url\n"


@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=[])
class CatalogImportTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "r@example.com", "pw")
        self.c = Client()
        self.c.force_login(self.admin)
        self.net = FakeNet({"cdn.example.com": [PUBLIC], "inside.example.com": ["10.0.0.9"]},
                           {("cdn.example.com", 443): lambda: FakeResponse(200, OK_HEADERS, IMG),
                            ("inside.example.com", 443): FakeResponse(200, OK_HEADERS, IMG)})

    def run_import(self, text, dry=False):
        data = {"csv_text": text}
        if dry:
            data["dry_run"] = "on"
        with self.net:
            return self.c.post(reverse("network:catalog_import"), data)

    def rows(self, r):
        return {x.line: x for x in r.context["report"]}

    def test_parse_rejects_unusable_files(self):
        for text, expect in (("", "empty"), ("vendor,model_key\nA,b\n", "missing: image_url"), (CSV_HEAD, "no rows"),
                             (CSV_HEAD + "A,,,https://x/a.png,\n" * 51, "at most 50")):
            rows, problem = catalog_io.parse_csv(text)
            self.assertEqual(rows, [], text[:20])
            self.assertIn(expect, problem)
        rows, problem = catalog_io.parse_csv("﻿Vendor;Device Type;Model Key;Image URL\nA;switch;m;https://x/a.png\n")
        self.assertEqual((problem, rows[0][1]["model_key"], rows[0][1]["image_url"]), ("", "m", "https://x/a.png"))

    def test_the_row_cap_holds_for_direct_callers_before_any_download(self):
        rows = [(i + 2, {"vendor": f"V{i}", "image_url": "https://cdn.example.com/a.png"}) for i in range(catalog_io.MAX_ROWS + 1)]
        with self.net:
            with self.assertRaisesMessage(ValueError, "at most 50 rows"):
                catalog_io.run_import(rows, self.admin, dry_run=True)
        self.assertEqual(self.net.opened, [], "no download may start for an over-sized import")

    def test_dry_run_checks_everything_and_saves_nothing(self):
        text = (CSV_HEAD + "Arista,switch,DCS-7050,https://cdn.example.com/a.png,https://arista.example/7050\n"
                "Cisco,router,,https://cdn.example.com/b.png,\n"
                "Evil,,,https://inside.example.com/x.png,\n"
                "Bad,sofa,,https://cdn.example.com/c.png,\n"
                ",switch,,https://cdn.example.com/c.png,\n"
                "NoUrl,,m,,\n"
                "Http,,,http://cdn.example.com/c.png,\n"
                "Arista,switch,dcs-7050,https://cdn.example.com/dup.png,\n")
        r = self.run_import(text, dry=True)
        self.assertEqual(r.status_code, 200)
        by_line = self.rows(r)
        self.assertEqual((by_line[2].status, by_line[3].status), ("created", "created"))
        self.assertIn("PNG 8×6", by_line[2].detail)
        self.assertIn("Blocked by probe policy", by_line[4].message)
        self.assertNotIn("10.0.0.9", by_line[4].message, "a report must not reveal what an internal name resolves to")
        self.assertIn("device_type", by_line[5].message)
        self.assertIn("vendor is required", by_line[6].message)
        self.assertIn("image_url is required", by_line[7].message)
        self.assertIn("Only https://", by_line[8].message)
        self.assertIn("duplicate of the row on line 2", by_line[9].message)
        self.assertEqual(r.context["counts"], {"created": 2, "updated": 0, "unchanged": 0, "skipped": 0, "error": 6})
        self.assertEqual((NetworkProductImage.objects.count(), DeviceImage.objects.count()), (0, 0))
        self.assertIn("Dry run", r.content.decode())
        self.assertTrue(AuditEvent.objects.filter(action="import", object_type="network.catalog").exists())

    def test_real_run_creates_updates_and_reports_unchanged(self):
        text = CSV_HEAD + "Arista,switch,DCS-7050,https://cdn.example.com/a.png,https://arista.example/7050\nCisco,,,https://cdn.example.com/b.png,\n"
        r = self.run_import(text)
        self.assertEqual(r.context["counts"]["created"], 2)
        self.assertEqual(NetworkProductImage.objects.count(), 2)
        self.assertEqual(DeviceImage.objects.count(), 1, "identical bytes are stored once")
        row = NetworkProductImage.objects.get(vendor="Arista")
        self.assertEqual((row.image.source, row.image.source_url, row.product_image_url, row.product_page_url),
                         ("fetched", "https://cdn.example.com/a.png", "", "https://arista.example/7050"))
        r = self.run_import(text)                                                       # same file again
        self.assertEqual(r.context["counts"], {"created": 0, "updated": 0, "unchanged": 2, "skipped": 0, "error": 0})
        new = png(9, 9)
        self.net.routes[("cdn.example.com", 443)] = lambda: FakeResponse(200, OK_HEADERS, new)
        r = self.run_import(CSV_HEAD + "arista,switch,dcs-7050,https://cdn.example.com/a2.png,\n")
        self.assertEqual(r.context["counts"]["updated"], 1)
        row.refresh_from_db()
        self.assertEqual((row.image.width, NetworkProductImage.objects.count()), (9, 2))

    def test_time_budget_marks_unfinished_rows_instead_of_hanging(self):
        with mock.patch.object(catalog_io, "wait", return_value=(set(), set())):
            r = self.run_import(CSV_HEAD + "Slow,,,https://cdn.example.com/s.png,\n")
        self.assertEqual(r.context["counts"]["skipped"], 1)
        self.assertEqual(NetworkProductImage.objects.count(), 0)

    def test_oversized_and_bad_images_are_row_errors_not_crashes(self):
        self.net.routes[("cdn.example.com", 443)] = lambda: FakeResponse(200, OK_HEADERS, SVG)
        r = self.run_import(CSV_HEAD + "Arista,,,https://cdn.example.com/a.png,\n")
        self.assertIn("SVG and HTML", self.rows(r)[2].message)
        self.assertEqual(NetworkProductImage.objects.count(), 0)

    def test_upload_of_a_csv_file(self):
        with self.net:
            r = self.c.post(reverse("network:catalog_import"), {"csv_file": upload((CSV_HEAD + "A,,,https://cdn.example.com/a.png,\n").encode(),
                                                                                    "c.csv", "text/csv"), "dry_run": "on"})
        self.assertEqual(r.context["counts"]["created"], 1)
        r = self.c.post(reverse("network:catalog_import"), {"csv_file": upload(b"\xff\xfe\x00", "c.csv", "text/csv")})
        self.assertEqual(r.status_code, 400)


# ------------------------------------------------------------------------------------------ fallback illustrations + card
@override_settings(LINKEDEYE_TENANT=T, PROMETHEUS_URL="")
class CardAndFallbackTests(TestCase):
    TYPES = {"switch": "Switch", "router": "Router", "firewall": "Firewall", "wireless": "Access point",
             "physical_server": "Server", "virtual_machine": "Server", "load_balancer": "Device"}

    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.c = Client()
        self.c.force_login(self.viewer)

    def render_card(self, device, variant="tile"):
        from django.template.loader import render_to_string
        card = live.card_data([device])[device.pk]
        return render_to_string("network/_device_card.html", {"device": device, "card": card, "variant": variant})

    def test_every_device_type_has_a_labelled_illustration_when_it_has_no_photo(self):
        for i, (dtype, label) in enumerate(self.TYPES.items()):
            d = dev(f"art-{i}", device_type=dtype)
            html = self.render_card(d)
            self.assertIn('<svg class="dc-art"', html, dtype)
            self.assertIn(f'aria-label="{label} illustration"', html, dtype)
            self.assertIn(">Illustration</span>", html, dtype)
            self.assertIn("is-art", html)
            self.assertNotIn("<img", html)

    def test_ups_and_storage_are_recognised_and_every_kind_draws(self):
        self.assertEqual(art.art_kind(dev("u1", device_type="physical_server", role="Rack UPS")), "ups")
        self.assertEqual(art.art_kind(dev("u2", device_type="physical_server", model="APC Smart-UPS 3000")), "ups")
        self.assertEqual(art.art_kind(dev("s1", device_type="physical_server", role="NAS storage")), "storage")
        self.assertEqual(art.art_kind(dev("s2", device_type="physical_server", tags="netapp")), "storage")
        self.assertEqual(art.art_kind(dev("p1", device_type="physical_server", role="Database server")), "server")
        for kind in art.KINDS:
            svg = art.illustration(kind)
            self.assertTrue(svg.startswith("<svg") and svg.endswith("</svg>"), kind)
            self.assertNotIn("<script", svg)
            self.assertNotIn("http", svg)
        self.assertEqual(len(art.KINDS), 8)

    def test_a_stored_photo_replaces_the_illustration_and_has_no_label(self):
        image, _ = images.store_image(IMG, source="upload")
        d = dev("photo-01", image=image, vendor="Arista", model="DCS-7050")
        html = self.render_card(Device.objects.select_related("image").get(pk=d.pk), "hero")
        self.assertIn(f'src="/network/images/{image.sha256}.png"', html)
        self.assertIn('alt="Arista DCS-7050"', html)
        self.assertNotIn("dc-art", html)
        self.assertNotIn("Illustration", html)
        self.assertIn("has-photo", html)
        self.assertNotIn("http", html.split("<img")[1].split(">")[0])        # never a third-party address

    def test_legacy_link_is_still_shown_but_without_a_referrer(self):
        d = dev("legacy-01", product_image_url="https://cdn.example/old.webp")
        html = self.render_card(d)
        self.assertIn('src="https://cdn.example/old.webp"', html)
        self.assertIn('referrerpolicy="no-referrer"', html)

    def test_no_data_is_never_shown_as_zero(self):
        d = dev("nodata-01")
        card = live.card_data([d])[d.pk]
        self.assertEqual(card["figures"], {"cpu": None, "memory": None, "temp": None, "in": None, "out": None, "uptime": None})
        for key in ("uptime", "cpu", "memory", "temp", "in", "out"):
            self.assertEqual(card["values"][key], "No data", key)
            self.assertEqual(card["tones"][key], "mute", key)
        html = self.render_card(d)
        self.assertEqual(html.count(">No data</b>"), 6)
        self.assertNotIn(">0%<", html)
        self.assertNotIn(">0 Mbps<", html)
        self.assertNotIn(">0 °C<", html)

    def test_real_values_and_a_genuine_zero_are_shown(self):
        now = timezone.now()
        d = dev("real-01", cpu_pct=0.0, memory_pct=63.4, temperature_c=41.0, uptime_seconds=86400 * 3, metrics_updated_at=now, status="up")
        Interface.objects.create(device=d, if_index=1, name="Gi1", if_type="Ethernet", in_mbps=12.5, out_mbps=0.0, oper_status="up")
        v = live.card_data([d])[d.pk]["values"]
        self.assertEqual((v["cpu"], v["memory"], v["temp"], v["uptime"]), ("0.0%", "63%", "41 °C", "3 days"))
        self.assertEqual((v["in"], v["out"]), ("12.5 Mbps", "0 Mbps"))        # an interface that really reports 0

    def test_stale_and_down_device_figures_are_no_data(self):
        old = timezone.now() - timezone.timedelta(hours=1)
        d = dev("stale-01", cpu_pct=55.0, memory_pct=40.0, temperature_c=40.0, uptime_seconds=1000, metrics_updated_at=old, status="up")
        Interface.objects.create(device=d, if_index=1, name="Gi1", if_type="Ethernet", in_mbps=9.0, out_mbps=9.0, updated_at=old)
        v = live.card_data([d])[d.pk]["values"]
        self.assertEqual({k: v[k] for k in ("cpu", "memory", "temp", "uptime", "in", "out")}, {k: "No data" for k in ("cpu", "memory", "temp", "uptime", "in", "out")})
        d.metrics_updated_at, d.status = timezone.now(), "down"
        self.assertEqual(live.card_data([d])[d.pk]["values"]["uptime"], "No data")

    def test_logical_interfaces_are_not_double_counted(self):
        d = dev("lag-01", metrics_updated_at=timezone.now())
        Interface.objects.create(device=d, if_index=1, name="Gi1", if_type="Ethernet", in_mbps=100.0, out_mbps=50.0)
        Interface.objects.create(device=d, if_index=2, name="Gi2", if_type="Ethernet", in_mbps=100.0, out_mbps=50.0)
        Interface.objects.create(device=d, if_index=3, name="Po1", if_type="LAG", in_mbps=200.0, out_mbps=100.0)
        self.assertEqual(live.card_data([d])[d.pk]["figures"]["in"], 200.0)

    def test_tones_flag_hot_figures(self):
        d = dev("hot-01", cpu_pct=95.0, memory_pct=90.0, temperature_c=80.0, metrics_updated_at=timezone.now())
        t = live.card_data([d])[d.pk]["tones"]
        self.assertEqual((t["cpu"], t["memory"], t["temp"]), ("crit", "warn", "crit"))

    def test_servers_read_node_exporter_in_one_batch(self):
        s1 = dev("srv-01", device_type="physical_server", prometheus_instance="10.0.0.5:9100")
        s2 = dev("srv-02", device_type="virtual_machine", prometheus_instance="10.0.0.6:9100")
        sw = dev("sw-01", prometheus_instance="sw1")
        calls = []

        def fake(promql, at=None):
            calls.append(promql)
            if "node_cpu_seconds_total" in promql:
                return [prom_row(31.0, instance="10.0.0.5:9100"), prom_row(7.5, instance="10.0.0.6:9100")]
            if "MemAvailable" in promql:
                return [prom_row(25.0, instance="10.0.0.5:9100")]
            if "node_network_receive" in promql:
                return [prom_row(40.0, instance="10.0.0.5:9100")]
            if "node_network_transmit" in promql:
                return [prom_row(2.5, instance="10.0.0.5:9100")]
            if "node_hwmon_temp_celsius" in promql:
                return [prom_row(48.0, instance="10.0.0.5:9100")]
            return []
        with override_settings(PROMETHEUS_URL="http://prometheus.test:9090"), mock.patch("core.prometheus.query", side_effect=fake):
            cards = live.card_data([s1, s2, sw])
        a, b, c = (cards[x.pk]["values"] for x in (s1, s2, sw))
        self.assertEqual((a["cpu"], a["memory"], a["in"], a["out"], a["temp"]), ("31%", "25%", "40.0 Mbps", "2.5 Mbps", "48 °C"))
        self.assertEqual((b["cpu"], b["memory"], b["in"], b["temp"]), ("7.5%", "No data", "No data", "No data"))
        self.assertEqual(c["cpu"], "No data")
        self.assertTrue(all("sw1" not in q for q in calls), "switches are not asked for node_exporter series")
        self.assertLessEqual(len(calls), 8, "one batched query per metric for the whole page, not one per device")

    def test_snmp_scalars_are_stored_validated_and_cleared(self):
        d = dev("snmp-01", prometheus_instance="sw1")

        def fake(promql, at=None):
            if "hrProcessorLoad" in promql:
                return [prom_row(37.0, instance="sw1")]
            if "hrStorageUsed" in promql:
                return [prom_row(150.0, instance="sw1")]          # out of range: not a percentage, so no data
            if "entPhySensorValue" in promql:
                return [prom_row(52.5, instance="sw1")]
            if "sysUpTime" in promql:
                return [prom_row(86400.0, instance="sw1")]
            return []
        with override_settings(PROMETHEUS_URL="http://prometheus.test:9090"), mock.patch("core.prometheus.query", side_effect=fake):
            data, errors = metrics.fetch(["sw1"])
            metrics.apply(d, data["sw1"], errors)
        d.refresh_from_db()
        self.assertEqual((d.cpu_pct, d.memory_pct, d.temperature_c, d.uptime_seconds), (37.0, None, 52.5, 86400))
        metrics.apply(d, {"interfaces": {}, "uptime": None, "cpu": None, "memory": None, "temp": None}, [])
        d.refresh_from_db()
        self.assertEqual((d.cpu_pct, d.memory_pct, d.temperature_c, d.uptime_seconds), (None, None, None, None))

    def test_snmp_queries_only_name_series_that_exist_in_the_standard_mibs(self):
        q = metrics.device_queries('instance="x"')
        self.assertEqual(set(q), {"cpu", "memory", "temp"})
        for name in ("hrProcessorLoad", "cpmCPUTotal5minRev", "hrStorageUsed", "ciscoMemoryPoolUsed", "entPhySensorValue"):
            self.assertIn(name, "".join(q.values()))

    # ---- pages
    def test_detail_header_shows_the_card_with_live_attributes(self):
        d = dev("detail-01", vendor="Arista", model="DCS-7050", status="up")
        html = self.c.get(d.get_absolute_url()).content.decode()
        self.assertIn('class="dc dc-hero"', html)
        for attr in ('data-live="card_cpu"', 'data-live="card_memory"', 'data-live="card_in"', 'data-live="card_out"',
                     'data-live="card_temp"', 'data-live="card_uptime"', "device-card.js", "device-card.css"):
            self.assertIn(attr, html)
        self.assertEqual(html.count(">No data</b>"), 6)
        self.assertIn(">Illustration</span>", html)

    def test_list_table_has_thumbnails_and_cards_view_has_tiles(self):
        image, _ = images.store_image(IMG, source="upload")
        dev("list-01", image=image)
        dev("list-02", device_type="firewall")
        html = self.c.get(reverse("network:all")).content.decode()
        self.assertEqual(html.count('class="dc dc-thumb"'), 2)
        self.assertIn(f"/network/images/{image.sha256}.png", html)
        self.assertIn('id="dc-live"', html)
        cards = self.c.get(reverse("network:all") + "?view=cards").content.decode()
        self.assertEqual(cards.count('class="dc dc-tile"'), 2)
        self.assertIn('name="view" value="cards"', cards)
        self.assertNotIn("<thead>", cards)

    def test_cards_live_endpoint_is_scoped_and_bounded(self):
        mum = Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        a, b = dev("a-sw", site="Mumbai"), dev("b-sw", site="DR Site", mgmt_ip="192.0.2.11")
        Membership.objects.create(user=self.viewer, site=dr, role="viewer")
        url = reverse("network:cards_live")
        data = self.c.get(url, {"ids": f"{a.pk},{b.pk},abc,,9999"}).json()
        self.assertEqual(list(data["cards"]), [str(b.pk)])
        card = data["cards"][str(b.pk)]
        self.assertEqual((card["values"]["cpu"], card["figures"]["cpu"]), ("No data", None))
        self.assertEqual(self.c.get(url).json()["cards"], {})
        self.assertEqual(self.c.get(url)["Cache-Control"], "no-store")
        self.assertEqual(Client().get(url).status_code, 302)

    def test_cards_live_ids_from_the_query_string_never_cause_a_500(self):
        d = dev("ids-sw", mgmt_ip="192.0.2.30")
        url = reverse("network:cards_live")
        # "²" and "①" pass str.isdigit() but not int(); a 20-digit id overflows the integer column; 5000 digits is
        # past Python's int-conversion limit. None of these may break the request, and a valid id still answers.
        for junk in ("²", "①", "99999999999999999999", "1" * 5000, "-1", " ", "0x10", "1.5"):
            resp = self.c.get(url, {"ids": f"{d.pk},{junk}"})
            self.assertEqual(resp.status_code, 200, msg=junk[:20])
            self.assertEqual(list(resp.json()["cards"]), [str(d.pk)], msg=junk[:20])
        many = ",".join(str(i) for i in range(1, 100000))[:5000]
        self.assertEqual(self.c.get(url, {"ids": many}).status_code, 200)

    def test_compute_server_rows_carry_the_card(self):
        d = dev("srv-01", device_type="physical_server")
        html = self.c.get(reverse("compute:servers")).content.decode()
        self.assertIn('class="dc dc-row"', html)
        self.assertIn(f'data-card="{d.pk}"', html)
        self.assertIn(">Illustration</span>", html)
        self.assertIn("device-card.js", html)

    def test_overview_tab_names_the_photo_source(self):
        image, _ = images.store_image(IMG, source="upload")
        d = dev("tab-01", image=image)
        self.assertIn("Stored photo (uploaded, 8×6)", self.c.get(d.get_absolute_url()).content.decode())
        plain = dev("tab-02", device_type="router", mgmt_ip="192.0.2.12")
        self.assertIn("No photo: showing a router illustration", self.c.get(plain.get_absolute_url()).content.decode())

    def test_snapshot_carries_the_card_keys(self):
        d = dev("snap-01", cpu_pct=10.0, metrics_updated_at=timezone.now())
        data = self.c.get(reverse("network:live", args=[d.pk])).json()
        self.assertEqual(data["values"]["card_cpu"], "10%")
        self.assertEqual(data["figures"]["cpu"], 10.0)
        self.assertIn("card_cpu", data["tones"])
