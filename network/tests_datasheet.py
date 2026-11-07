"""Real device photos from datasheets: PDF extraction, product page extraction, fetch error wording, the candidate store
(TTL, cap, prune job), the picker and apply views (RBAC, CSRF, isolation), the missing-photos page, and how a stored photo is
shown. Every PDF, page and image is built from bytes in this file; no test opens a socket and no vendor file or address is
used (the hosts are example.com names behind a fake network)."""
import functools
import hashlib
import io
import os
import random
import struct
import sys
import threading
import time
import zlib
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import jobs as core_jobs
from core.models import AuditEvent, Membership, Site
from probes import checks

from . import datasheet, datasheet_page, datasheet_pdf, images, imaging, jobs, photos
from .datasheet import DatasheetError
from .imaging import inspect_image
from .models import Device, DeviceImage, NetworkProductImage
from .tests import T, dev, users
from .tests_images import PUBLIC, FakeConn, FakeNet, FakeResponse, form_data, png, upload

try:
    import pypdf
except ImportError:                                           # pragma: no cover - pypdf is a normal dependency
    pypdf = None

needs_pypdf = __import__("unittest").skipUnless(pypdf is not None, "pypdf is not installed")


# ------------------------------------------------------------------------------------------ synthetic images
def sample_jpeg(w, h, comps=3, size=None):
    """A structurally valid baseline JPEG (headers + pseudo scan data, no 0xFF in the scan): not decodable, but exactly what
    network.imaging checks. `size` is the scan length (default: enough that it is not near-uniform)."""
    scan = random.Random(w * 31 + h).randbytes(size or max(64, w * h // 12)).replace(b"\xff", b"\xfe")
    comp = b"".join(bytes([i + 1, 0x11, 0]) for i in range(comps))
    sof = b"\xff\xc0" + struct.pack(">HBHHB", 8 + 3 * comps, 8, h, w, comps) + comp
    sos = b"\xff\xda" + struct.pack(">HB", 6 + 2 * comps, comps) + b"".join(bytes([i + 1, 0]) for i in range(comps)) + b"\x00\x3f\x00"
    return b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + sof + sos + scan + b"\xff\xd9"


@functools.lru_cache(maxsize=32)
def sample_pixels(w, h, ch, kind="photo"):
    """w*h*ch bytes: 'device' is a light background with a darker rack-unit shape, 'photo' is textured, 'flat' is one colour."""
    rnd = random.Random(w * 7 + h)
    out = bytearray()
    for y in range(h):
        for x in range(w):
            if kind == "flat":
                out += bytes([240] * ch)
            elif kind == "device":
                inside = 0.12 * w < x < 0.88 * w and 0.3 * h < y < 0.7 * h
                port = inside and (x // 9) % 2 == 0 and 0.45 * h < y < 0.6 * h
                base = 40 if port else 120 + int((y - 0.3 * h) * 0.4) if inside else 236
                out += bytes([min(255, max(0, base + rnd.randrange(5)))] * ch)
            else:
                out += bytes([(x * 5 + y * 3 + c * 61 + rnd.randrange(8)) % 256 for c in range(ch)])
    return bytes(out)


def decode_png(data):
    """(width, height, channels, pixel bytes) of a PNG made by network.datasheet_pdf (filter type 0 only)."""
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        pos += 12 + n
    w, h, depth, ctype = ihdr[:4]
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    row = w * ch + 1
    assert len(raw) == row * h and not any(raw[::row]), "unexpected PNG filters"
    return w, h, ch, b"".join(raw[i * row + 1:(i + 1) * row] for i in range(h))


# ------------------------------------------------------------------------------------------ a tiny PDF writer
class Doc:
    """Builds a PDF from objects. Object 1 is the catalog and 2 the page tree."""

    def __init__(self):
        self.objs = [b"", b""]
        self.kids = []

    def add(self, body):
        self.objs.append(body if isinstance(body, bytes) else body.encode())
        return len(self.objs)

    def stream(self, entries, data):
        return self.add(f"<< {entries} /Length {len(data)} >>".encode() + b"\nstream\n" + data + b"\nendstream")

    def image(self, w, h, *, kind="jpeg", ch=3, content="photo", entries="", data=None, smask=None, parms="", bpc=8):
        if kind == "jpeg":
            cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}[ch]
            return self.stream(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {cs} /BitsPerComponent 8 "
                               f"/Filter /DCTDecode {entries}", data if data is not None else sample_jpeg(w, h, ch))
        cs = "/DeviceRGB" if ch == 3 else "/DeviceGray"
        sm = f"/SMask {smask} 0 R" if smask else ""
        body = data if data is not None else zlib.compress(sample_pixels(w, h, ch, content), 6)
        return self.stream(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {cs} /BitsPerComponent {bpc} "
                           f"/Filter /FlateDecode {sm} {parms} {entries}", body)

    def form(self, xobjects):
        res = " ".join(f"/X{i} {n} 0 R" for i, n in enumerate(xobjects))
        return self.stream(f"/Type /XObject /Subtype /Form /BBox [0 0 600 800] /Resources << /XObject << {res} >> >>", b"")

    def page(self, xobjects, places=None):
        """places: one (width, height, x, y) in points per picture (default 400 x 300 in the middle of the 612 x 792 page)."""
        res = " ".join(f"/Im{i} {n} 0 R" for i, n in enumerate(xobjects))
        places = places or [(400, 300, 50, 400 - 10 * i) for i in range(len(xobjects))]
        content = "".join(f"q {w} 0 0 {h} {x} {y} cm /Im{i} Do Q\n" for i, (w, h, x, y) in enumerate(places)).encode()
        c = self.stream("", content)
        self.kids.append(self.add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {c} 0 R "
                                  f"/Resources << /XObject << {res} >> >> >>"))

    def build(self):
        self.objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
        self.objs[1] = f"<< /Type /Pages /Count {len(self.kids)} /Kids [{' '.join(f'{k} 0 R' for k in self.kids)}] >>".encode()
        out, offsets = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"), []
        for i, body in enumerate(self.objs, 1):
            offsets.append(len(out))
            out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objs) + 1}\n".encode() + b"0000000000 65535 f \n"
        out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
        out += f"trailer\n<< /Size {len(self.objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


def pdf_of(*pages):
    """pages: lists of dicts for Doc.image (page 1 first); returns the PDF bytes."""
    d = Doc()
    for specs in pages:
        d.page([d.image(**spec) for spec in specs])
    return d.build()


PRODUCT = dict(w=900, h=520, kind="flate", ch=3, content="device")


# ------------------------------------------------------------------------------------------ PNG encoder
class PngEncoderTests(SimpleTestCase):
    def test_round_trip_through_the_image_validator_for_every_channel_count(self):
        for ch in (1, 2, 3, 4):
            pix = sample_pixels(37, 23, ch)
            data = datasheet_pdf.encode_png(37, 23, ch, pix)
            info = inspect_image(data)
            self.assertEqual((info.content_type, info.width, info.height), ("image/png", 37, 23), ch)
            self.assertEqual(decode_png(data), (37, 23, ch, pix), ch)

    def test_rejects_data_that_does_not_match_the_size(self):
        for args in ((10, 10, 3, b"\x00" * 5), (0, 10, 3, b""), (10, 10, 5, b"\x00" * 500)):
            with self.assertRaises(ValueError):
                datasheet_pdf.encode_png(*args)

    def test_a_large_image_stays_valid(self):
        data = datasheet_pdf.encode_png(1024, 768, 3, bytes(1024 * 768 * 3))
        info = inspect_image(data)
        self.assertEqual((info.width, info.height), (1024, 768))


# ------------------------------------------------------------------------------------------ PDF extraction
@needs_pypdf
class PdfExtractTests(SimpleTestCase):
    def test_a_jpeg_passes_through_as_the_original_bytes(self):
        res = datasheet_pdf.extract(pdf_of([dict(w=640, h=420, kind="jpeg")]))
        self.assertEqual(len(res.candidates), 1, res.message)
        c = res.candidates[0]
        self.assertEqual((c.content_type, c.width, c.height, c.page, c.origin), ("image/jpeg", 640, 420, 1, "datasheet page 1"))
        self.assertEqual(c.data, sample_jpeg(640, 420))
        inspect_image(c.data)

    def test_a_flate_rgb_image_becomes_a_png_with_the_same_pixels(self):
        res = datasheet_pdf.extract(pdf_of([PRODUCT]))
        c = res.candidates[0]
        self.assertEqual((c.content_type, c.width, c.height), ("image/png", 900, 520))
        self.assertEqual(inspect_image(c.data).content_type, "image/png")
        self.assertEqual(decode_png(c.data), (900, 520, 3, sample_pixels(900, 520, 3, "device")))

    def test_a_flate_gray_image_becomes_a_gray_png(self):
        res = datasheet_pdf.extract(pdf_of([dict(w=600, h=400, kind="flate", ch=1, content="device")]))
        self.assertEqual(decode_png(res.candidates[0].data), (600, 400, 1, sample_pixels(600, 400, 1, "device")))

    def test_a_png_predictor_stream_is_reused_and_decodes_to_the_same_pixels(self):
        w, h = 400, 260
        pix = sample_pixels(w, h, 3)
        rows = b"".join(b"\x00" + pix[y * w * 3:(y + 1) * w * 3] for y in range(h))
        d = Doc()
        d.page([d.image(w, h, kind="flate", data=zlib.compress(rows), parms=f"/DecodeParms << /Predictor 15 /Colors 3 /Columns {w} /BitsPerComponent 8 >>")])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual(decode_png(res.candidates[0].data), (w, h, 3, pix))

    def test_a_soft_mask_becomes_the_alpha_channel(self):
        d = Doc()
        mask = d.image(300, 200, kind="flate", ch=1, content="device")
        d.page([d.image(300, 200, kind="flate", ch=3, smask=mask)])
        res = datasheet_pdf.extract(d.build())
        w, h, ch, pix = decode_png(res.candidates[0].data)
        self.assertEqual((w, h, ch), (300, 200, 4))
        rgb, alpha = sample_pixels(300, 200, 3), sample_pixels(300, 200, 1, "device")
        self.assertEqual(pix[0::4], rgb[0::3])
        self.assertEqual(pix[3::4], alpha)

    def test_logo_banner_tiny_flat_mask_and_one_bit_images_are_skipped(self):
        res = datasheet_pdf.extract(pdf_of([
            dict(w=1500, h=200, kind="flate", ch=3),                      # 7.5:1, a logo or banner
            dict(w=150, h=100, kind="flate", ch=3),                       # under 200 x 120
            dict(w=400, h=300, kind="flate", ch=3, content="flat"),       # one colour
            dict(w=400, h=300, kind="jpeg", data=sample_jpeg(400, 300, size=20)),       # a flat JPEG compresses to almost nothing
            dict(w=400, h=300, kind="flate", ch=1, bpc=1, entries="/ImageMask true"),     # stencil mask
            dict(w=400, h=300, kind="flate", ch=1, bpc=1),                # 1-bit
            dict(w=400, h=300, kind="jpeg", ch=4),                        # CMYK JPEG
            PRODUCT]))
        self.assertEqual([(c.width, c.height) for c in res.candidates], [(900, 520)])
        text = " ".join(res.notes)
        for word in ("logo or banner shaped", "too small", "flat colour", "a mask", "1-bit", "CMYK"):
            self.assertIn(word, text)

    def test_unsupported_encodings_are_skipped_not_fatal(self):
        d = Doc()
        d.page([d.stream("/Type /XObject /Subtype /Image /Width 500 /Height 400 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /JPXDecode", b"x" * 500),
                d.stream("/Type /XObject /Subtype /Image /Width 500 /Height 400 /ColorSpace /DeviceGray /BitsPerComponent 1 /Filter /CCITTFaxDecode", b"x" * 500),
                d.stream("/Type /XObject /Subtype /Image /Width 500 /Height 400 /ColorSpace [/Indexed /DeviceRGB 1 <000000ffffff>] /BitsPerComponent 8 /Filter /FlateDecode", zlib.compress(bytes(200000))),
                d.image(**PRODUCT)])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual(len(res.candidates), 1)
        self.assertIn("not supported", " ".join(res.notes))

    def test_ranked_by_page_then_area_then_aspect(self):
        res = datasheet_pdf.extract(pdf_of(
            [dict(w=640, h=420, kind="jpeg"), dict(w=1000, h=500, kind="jpeg"), dict(w=1000, h=1000, kind="jpeg")],
            [dict(w=1600, h=1000, kind="jpeg")]))
        self.assertEqual([(c.page, c.width, c.height) for c in res.candidates],
                         [(1, 1000, 1000), (1, 1000, 500), (1, 640, 420), (2, 1600, 1000)])
        self.assertEqual(res.candidates[-1].origin, "datasheet page 2")
        # equal area: the one closer to a product-shot aspect (1.6:1) first
        res = datasheet_pdf.extract(pdf_of([dict(w=800, h=800, kind="jpeg"), dict(w=1000, h=640, kind="jpeg")]))
        self.assertEqual([c.width for c in res.candidates], [1000, 800])

    def test_page_decoration_and_logo_sized_pictures_rank_after_the_product_shot(self):
        """A datasheet's biggest picture is often a header graphic that bleeds off the page, and its badges and logos are small:
        where the page draws a picture decides, not only its pixel size."""
        d = Doc()
        header = d.image(2100, 661, kind="jpeg")                    # bleeds off the top left corner and across the page
        badge = d.image(500, 500, kind="jpeg")                      # drawn about 60 pt wide
        product = d.image(1000, 229, kind="jpeg")
        logo = d.image(581, 170, kind="jpeg")
        d.page([header, badge, product, logo], places=[(612, 192, 0, 600), (60, 60, 500, 700), (480, 110, 66, 300), (90, 26, 40, 40)])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.width, c.height, c.demoted) for c in res.candidates],
                         [(1000, 229, False), (2100, 661, True), (500, 500, True), (581, 170, True)])

    def test_a_decoration_on_page_one_does_not_beat_a_product_shot_on_page_two(self):
        d = Doc()
        d.page([d.image(2000, 700, kind="jpeg")], places=[(612, 214, 0, 578)])
        d.page([d.image(900, 520, kind="jpeg")])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.page, c.demoted) for c in res.candidates], [(2, False), (1, True)])

    def test_the_position_is_found_through_a_form_xobject_and_its_matrix(self):
        d = Doc()
        im = d.image(1400, 500, kind="jpeg")
        form = d.stream("/Type /XObject /Subtype /Form /BBox [0 0 1 1] /Matrix [612 0 0 220 0 572] "
                        f"/Resources << /XObject << /Inner {im} 0 R >> >>", b"q 1 0 0 1 0 0 cm /Inner Do Q")
        prod = d.image(900, 520, kind="jpeg")
        c = d.stream("", b"q /Fm Do Q q 400 0 0 300 50 200 cm /P Do Q")
        d.kids.append(d.add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {c} 0 R "
                            f"/Resources << /XObject << /Fm {form} 0 R /P {prod} 0 R >> >> >>"))
        res = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.width, c.demoted) for c in res.candidates], [(900, False), (1400, True)])

    def test_unreadable_positions_leave_the_old_order_and_never_fail(self):
        d = Doc()
        c = d.stream("", b"q 1 0 0 cm /Im0 Do Q ((( BT")                    # garbage operators
        a, b = d.image(640, 420, kind="jpeg"), d.image(1000, 500, kind="jpeg")
        d.kids.append(d.add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {c} 0 R "
                            f"/Resources << /XObject << /Im0 {a} 0 R /Im1 {b} 0 R >> >> >>"))
        res = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.width, c.demoted) for c in res.candidates], [(1000, False), (640, False)])

    def test_a_datasheet_with_only_cmyk_photos_says_what_to_do(self):
        res = datasheet_pdf.extract(pdf_of([dict(w=1800, h=490, kind="jpeg", ch=4), dict(w=800, h=600, kind="jpeg", ch=4)]))
        self.assertEqual(res.candidates, [])
        self.assertIn("2 CMYK (print colour)", res.message)
        self.assertIn("save or screenshot the product photo", res.message)
        # the same reason counted once even when it arrives under two names
        self.assertEqual(datasheet_pdf._summary(__import__("collections").Counter({"CMYK": 3, "CMYK JPEG": 1})), "Skipped 4 CMYK (print colour).")

    def test_only_the_first_six_pages_are_read(self):
        pages = [[dict(w=300 + i, h=250, kind="jpeg")] for i in range(8)]
        res = datasheet_pdf.extract(pdf_of(*pages))
        self.assertEqual(sorted({c.page for c in res.candidates}), [1, 2, 3, 4, 5, 6])

    def test_at_most_forty_image_objects_are_read_and_twelve_returned(self):
        d = Doc()
        d.page([d.image(300 + i, 250, kind="jpeg") for i in range(45)])
        with mock.patch.object(datasheet_pdf, "_decode_image", wraps=datasheet_pdf._decode_image) as dec:
            res = datasheet_pdf.extract(d.build())
        self.assertEqual(dec.call_count, 40)
        self.assertEqual(len(res.candidates), 12)

    def test_the_same_picture_used_twice_is_one_candidate(self):
        d = Doc()
        n = d.image(**PRODUCT)
        d.page([n]), d.page([n, d.image(**PRODUCT)])
        self.assertEqual(len(datasheet_pdf.extract(d.build()).candidates), 1)

    def test_pictures_inside_forms_are_found_but_forms_are_not_followed_forever(self):
        d = Doc()
        inner = d.form([d.image(**PRODUCT)])
        d.page([d.form([inner])])               # page -> form -> form -> image: two levels deep is allowed
        self.assertEqual(len(datasheet_pdf.extract(d.build()).candidates), 1)
        d = Doc()
        deep = d.form([d.image(**PRODUCT)])
        for _ in range(3):
            deep = d.form([deep])
        d.page([deep])
        self.assertEqual(datasheet_pdf.extract(d.build()).candidates, [])
        # a form that contains itself terminates
        d = Doc()
        loop = d.add("PLACEHOLDER")
        d.objs[loop - 1] = (f"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /L {loop} 0 R >> >> /Length 0 >>"
                            "\nstream\n\nendstream").encode()
        d.page([loop])
        self.assertEqual(datasheet_pdf.extract(d.build()).candidates, [])

    def test_a_huge_declared_size_is_refused_before_anything_is_allocated(self):
        for w, h in ((100000, 100000), (5000, 100), (4097, 4097), (4096, 4096)):
            d = Doc()
            d.page([d.image(w, h, kind="flate", data=zlib.compress(b"\x00" * 100))])
            res = datasheet_pdf.extract(d.build())
            self.assertEqual(res.candidates, [], (w, h))
            self.assertTrue(res.message, (w, h))
        self.assertIn("too large", datasheet_pdf.extract(d.build()).message)

    def test_a_flate_bomb_is_refused_without_inflating_it(self):
        bomb = zlib.compress(bytes(60 * 1024 * 1024), 9)             # ~60 KB that inflates to 60 MB
        self.assertLess(len(bomb), 100_000)
        d = Doc()
        d.page([d.image(400, 300, kind="flate", data=bomb), d.image(**PRODUCT)])
        t = time.monotonic()
        res = datasheet_pdf.extract(d.build())
        self.assertLess(time.monotonic() - t, 3)
        self.assertEqual(len(res.candidates), 1)
        self.assertIn("damaged", " ".join(res.notes))

    def test_inflating_always_passes_an_output_limit(self):
        """The limit is what keeps a small file from becoming a large allocation: every inflate in the extractor must pass
        max_length (the picture's declared size + 1), however big the stream really is."""
        import types
        limits, real = [], zlib.decompressobj

        def spy(*a, **k):
            inner = real(*a, **k)

            class Wrapped:
                def decompress(self, data, max_length=0):
                    limits.append(max_length)
                    return inner.decompress(data, max_length)

                def __getattr__(self, name):
                    return getattr(inner, name)
            return Wrapped()
        fake = types.SimpleNamespace(compress=zlib.compress, crc32=zlib.crc32, error=zlib.error, decompressobj=spy)
        d = Doc()
        d.page([d.image(400, 300, kind="flate", data=zlib.compress(bytes(60 * 1024 * 1024), 9)), d.image(**PRODUCT)])
        with mock.patch.object(datasheet_pdf, "zlib", fake):
            datasheet_pdf.extract(d.build())
        self.assertTrue(limits, "the bomb was never inflated through the limited path")
        self.assertIn(400 * 300 * 3 + 1, limits, "the bomb is inflated only up to its declared size")
        self.assertTrue(all(0 < n <= datasheet_pdf.MAX_DECODED_BYTES + 1 for n in limits), limits)

    def test_a_picture_over_the_photo_limit_is_skipped_and_says_so(self):
        big = dict(w=900, h=800, kind="flate", ch=3, data=zlib.compress(os.urandom(900 * 800 * 3), 1))      # incompressible, ~2.1 MB
        res = datasheet_pdf.extract(pdf_of([big, dict(w=700, h=500, kind="jpeg")]))
        self.assertEqual([(c.width, c.height) for c in res.candidates], [(700, 500)])
        self.assertTrue(any("2 MB" in n and "cannot be shrunk" in n for n in res.notes), res.notes)
        only = datasheet_pdf.extract(pdf_of([big]))
        self.assertEqual(only.candidates, [])
        self.assertIn("larger than 2 MB", only.message)

    def test_bad_input_gives_a_message_never_an_exception(self):
        good = pdf_of([PRODUCT])
        cases = {
            "empty": b"", "not bytes": None, "garbage": os.urandom(5000), "text": b"hello world",
            "header only": b"%PDF-1.7\n", "garbage after header": b"%PDF-1.4\n" + os.urandom(3000),
            "truncated": good[:len(good) // 2], "truncated early": good[:200],
            "no pages": Doc().build(),
        }
        for name, data in cases.items():
            res = datasheet_pdf.extract(data)
            self.assertEqual(res.candidates, [], name)
            self.assertTrue(res.message, name)

    def test_a_pdf_without_pictures_says_so(self):
        d = Doc()
        d.page([])
        self.assertIn("No pictures were found", datasheet_pdf.extract(d.build()).message)

    def test_the_size_cap(self):
        res = datasheet_pdf.extract(b"%PDF-1.4\n" + b"\x00" * datasheet_pdf.MAX_PDF_BYTES)
        self.assertIn("25 MB", res.message)

    def test_an_encrypted_pdf_asks_for_an_unprotected_copy(self):
        w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(pdf_of([PRODUCT]))))
        w.encrypt("secret", algorithm="RC4-128")
        out = io.BytesIO()
        w.write(out)
        res = datasheet_pdf.extract(out.getvalue())
        self.assertEqual(res.candidates, [])
        self.assertIn("password protected", res.message)
        # restricted but openable without a password: read normally
        w = pypdf.PdfWriter(clone_from=pypdf.PdfReader(io.BytesIO(pdf_of([PRODUCT]))))
        w.encrypt("", owner_password="owner", algorithm="RC4-128")
        out = io.BytesIO()
        w.write(out)
        self.assertEqual(len(datasheet_pdf.extract(out.getvalue()).candidates), 1)

    def test_the_wall_clock_budget_stops_the_work(self):
        res = datasheet_pdf.extract(pdf_of([PRODUCT]), deadline_seconds=-1)
        self.assertEqual(res.candidates, [])
        self.assertIn("took too long", " ".join(res.notes))

    def test_any_pypdf_failure_is_a_message(self):
        with mock.patch("pypdf.PdfReader", side_effect=RuntimeError("boom")):
            res = datasheet_pdf.extract(pdf_of([PRODUCT]))
        self.assertIn("could not be read", res.message)
        with mock.patch.object(datasheet_pdf, "_decode_image", side_effect=MemoryError()):
            res = datasheet_pdf.extract(pdf_of([PRODUCT]))
        self.assertEqual(res.candidates, [])

    def test_without_pypdf_the_message_says_how_to_install_it(self):
        with mock.patch.dict(sys.modules, {"pypdf": None}):
            res = datasheet_pdf.extract(pdf_of([PRODUCT]))
        self.assertTrue(res.unavailable)
        self.assertIn("pip install", res.message)
        self.assertIn("requirements.txt", res.message)

    def test_nothing_is_written_to_disk(self):
        with mock.patch("builtins.open", side_effect=AssertionError("file opened")), \
                mock.patch("tempfile.NamedTemporaryFile", side_effect=AssertionError("temp file")), \
                mock.patch("tempfile.TemporaryFile", side_effect=AssertionError("temp file")):
            self.assertEqual(len(datasheet_pdf.extract(pdf_of([PRODUCT])).candidates), 1)


# ------------------------------------------------------------------------------------------ product page extraction
BASE = "https://www.vendor-example.com/products/switches/cat-9500/"


def page(head="", body=""):
    return f"<!doctype html><html><head><title>x</title>{head}</head><body>{body}</body></html>"


def urls(found):
    return [c.url for c in found]


class HtmlExtractTests(SimpleTestCase):
    def test_meta_images_come_first_in_the_documented_order(self):
        html = page('<meta property="og:image" content="https://cdn.example.com/og.jpg">'
                    '<meta property="og:image:secure_url" content="https://cdn.example.com/og-secure.jpg">'
                    '<meta name="twitter:image" content="https://cdn.example.com/tw.jpg">'
                    '<link rel="image_src" href="https://cdn.example.com/link.jpg">',
                    '<img src="https://cdn.example.com/body.jpg" alt="x">')
        found = datasheet_page.parse_page(html, BASE)
        self.assertEqual(urls(found), [f"https://cdn.example.com/{n}.jpg" for n in ("og", "og-secure", "tw", "link", "body")])
        self.assertEqual([c.label for c in found][:2], ["og:image", "og:image"])
        self.assertEqual(found[2].label, "twitter:image")

    def test_json_ld_product_images(self):
        ld = ('{"@context":"https://schema.org","@graph":[{"@type":"WebSite","image":"https://cdn.example.com/site.jpg"},'
              '{"@type":["Product","Thing"],"name":"x","image":["https://cdn.example.com/p1.jpg",'
              '{"@type":"ImageObject","url":"https://cdn.example.com/p2.jpg"}]}]}')
        found = datasheet_page.parse_page(page(f'<script type="application/ld+json">{ld}</script>'), BASE)
        self.assertEqual(urls(found), ["https://cdn.example.com/p1.jpg", "https://cdn.example.com/p2.jpg"])
        self.assertEqual(found[0].label, "product data (JSON-LD)")
        broken = page('<script type="application/ld+json">{"@type": "Product", "image": </script>')
        self.assertEqual(datasheet_page.parse_page(broken, BASE), [])

    def test_srcset_picture_and_lazy_attributes(self):
        html = page(body=(
            '<img srcset="/m/a-400.jpg 400w, /m/a-1200.jpg 1200w, /m/a-800.jpg 800w" src="/m/a-400.jpg" alt="Front">'
            '<picture><source srcset="/m/b-1x.webp 1x, /m/b-2x.webp 2x"><img src="/m/b.jpg" alt="Side"></picture>'
            '<img data-src="/m/lazy.jpg" alt="Lazy">'))
        found = datasheet_page.parse_page(html, BASE)
        got = urls(found)
        self.assertIn("https://www.vendor-example.com/m/a-1200.jpg", got)
        self.assertLess(got.index("https://www.vendor-example.com/m/a-1200.jpg"), got.index("https://www.vendor-example.com/m/a-800.jpg"))
        self.assertIn("https://www.vendor-example.com/m/b-2x.webp", got)
        self.assertIn("https://www.vendor-example.com/m/lazy.jpg", got)

    def test_relative_urls_resolve_against_the_final_address(self):
        html = page(body='<img src="../img/a.jpg" alt="a"><img src="//cdn2.example.com/b.png"><img src="c.png"><img src="/d.png">')
        got = urls(datasheet_page.parse_page(html, BASE))
        self.assertEqual(got, ["https://www.vendor-example.com/products/switches/img/a.jpg", "https://cdn2.example.com/b.png",
                               "https://www.vendor-example.com/products/switches/cat-9500/c.png", "https://www.vendor-example.com/d.png"])

    def test_logos_icons_sprites_pixels_and_unusable_formats_are_dropped(self):
        html = page(
            '<meta property="og:image" content="https://cdn.example.com/brand/site-logo.png">',
            ''.join([
                '<img src="https://cdn.example.com/a/logo.png" alt="Acme">', '<img src="https://cdn.example.com/a/x.png" alt="Company logo">',
                '<img src="https://cdn.example.com/a/y.png" class="header-logo">', '<img src="https://cdn.example.com/icon-24.png">',
                '<img src="https://cdn.example.com/banner_home.jpg">', '<img src="https://cdn.example.com/avatar.jpg">',
                '<img src="https://cdn.example.com/flags/in.png">', '<img src="https://cdn.example.com/sprite.png">',
                '<img src="https://cdn.example.com/t.png" width="1" height="1">', '<img src="https://cdn.example.com/s.png" width="32" height="32">',
                '<img src="https://cdn.example.com/draw.svg">', '<img src="https://cdn.example.com/anim.gif">',
                '<img src="https://cdn.example.com/p.avif">', '<img src="https://cdn.example.com/spacer.png">',
                '<img src="http://cdn.example.com/insecure.jpg">', '<img src="data:image/png;base64,AAAA">',
                '<img src="javascript:alert(1)">', '<img src="https://user:pw@cdn.example.com/cred.jpg">',
                '<img src="https://cdn.example.com/track/1x1.png">',
                '<img src="https://cdn.example.com/products/c9500-front.jpg" alt="Catalyst 9500 front view">']))
        self.assertEqual(urls(datasheet_page.parse_page(html, BASE)), ["https://cdn.example.com/products/c9500-front.jpg"])

    def test_the_model_in_the_alt_file_name_or_caption_ranks_first(self):
        html = page('<meta property="og:image" content="https://cdn.example.com/generic.jpg">', (
            '<img src="https://cdn.example.com/other.jpg" alt="Some other box">'
            '<img src="https://cdn.example.com/c9500-48y4c-front.jpg">'
            '<figure><img src="https://cdn.example.com/rear.jpg"><figcaption>Rear of the C9500-48Y4C</figcaption></figure>'
            '<img src="https://cdn.example.com/z.jpg" alt="Catalyst 9500-48Y4C switch">'))
        got = urls(datasheet_page.parse_page(html, BASE, model="C9500-48Y4C"))
        self.assertEqual(set(got[:3]), {"https://cdn.example.com/c9500-48y4c-front.jpg", "https://cdn.example.com/rear.jpg", "https://cdn.example.com/z.jpg"})
        self.assertEqual(got[3], "https://cdn.example.com/generic.jpg")        # no model: the usual order afterwards
        # only the model number matches: a smaller preference than the whole string, still ahead of nothing
        part = datasheet_page.parse_page(page(body='<img src="https://c.example.com/a.jpg"><img src="https://c.example.com/b.jpg" alt="Catalyst 9500 Series">'),
                                         BASE, model="Catalyst 9500-48Y4C")
        self.assertEqual(urls(part)[0], "https://c.example.com/b.jpg")

    def test_a_page_level_picture_that_names_the_model_beats_gallery_pictures_that_name_it_too(self):
        """The picture the vendor chose to represent the page (og:image) wins a tie, even when it comes late in the markup."""
        html = page('<meta property="og:image" content="https://cdn.example.com/spi/server-r760-hero.jpg">', (
            '<img src="https://cdn.example.com/gallery/a.png" alt="View forward-facing PowerEdge R760 Rack Server">'
            '<img src="https://cdn.example.com/gallery/b.png" alt="View PowerEdge R760 Rack Server">'))
        got = datasheet_page.parse_page(html, BASE, model="PowerEdge R760")
        self.assertEqual(urls(got)[0], "https://cdn.example.com/spi/server-r760-hero.jpg")
        self.assertEqual(got[0].score, 2)
        # a generic og:image that does not name the model gets no boost
        html = page('<meta property="og:image" content="https://cdn.example.com/brand.jpg">',
                    '<img src="https://cdn.example.com/x.png" alt="PowerEdge R760">')
        self.assertEqual(urls(datasheet_page.parse_page(html, BASE, model="PowerEdge R760"))[0], "https://cdn.example.com/x.png")

    def test_a_front_view_outranks_rear_internal_and_diagram_views_of_the_same_model(self):
        html = page(body=(
            '<img src="https://c.example.com/sr650-internal-view.png" alt="Internal view of the SR650 V3">'
            '<img src="https://c.example.com/sr650-block-diagram.png" alt="SR650 V3 system architectural block diagram">'
            '<img src="https://c.example.com/sr650-rear.png" alt="Rear view of the SR650 V3">'
            '<img src="https://c.example.com/sr650-top.png" alt="SR650 V3">'
            '<img src="https://c.example.com/sr650-front-angled.png" alt="SR650 V3 front angled left">'))
        got = urls(datasheet_page.parse_page(html, BASE, model="SR650 V3"))
        self.assertEqual(got[:2], ["https://c.example.com/sr650-front-angled.png", "https://c.example.com/sr650-top.png"])
        self.assertEqual(set(got[2:]), {"https://c.example.com/sr650-internal-view.png", "https://c.example.com/sr650-block-diagram.png",
                                        "https://c.example.com/sr650-rear.png"})
        # a rear view of the right model still beats a front view of nothing in particular
        html = page(body='<img src="https://c.example.com/front-hero.png" alt="Front"><img src="https://c.example.com/r.png" alt="Rear of the SR650 V3">')
        self.assertEqual(urls(datasheet_page.parse_page(html, BASE, model="SR650 V3"))[0], "https://c.example.com/r.png")

    def test_news_tiles_awards_ebook_covers_and_pdf_thumbnails_are_not_candidates(self):
        html = page(body=''.join(f'<img src="{u}" alt="x">' for u in (
            "https://c.example.com/images/news/featured-news-12.jpg", "https://c.example.com/images/awards/gartner-2024.png",
            "https://c.example.com/assets/ebook/eb-guide.pdf.thumb.319.319.png", "https://c.example.com/images/videos/video-case.png",
            "https://c.example.com/images/general/gartner-quadrant.jpg", "https://c.example.com/img/fg-100f-front.jpg")))
        self.assertEqual(urls(datasheet_page.parse_page(html, BASE, model="FG-100F")), ["https://c.example.com/img/fg-100f-front.jpg"])

    def test_json_ld_addresses_are_unescaped_so_they_equal_the_markup_ones(self):
        ld = '{"@type":"Product","image":["https://c.example.com/p.jpg?fmt=jpg&amp;wid=504"]}'
        html = page('<meta property="og:image" content="https://c.example.com/p.jpg?fmt=jpg&amp;wid=504">'
                    f'<script type="application/ld+json">{ld}</script>')
        got = datasheet_page.parse_page(html, BASE)
        self.assertEqual(urls(got), ["https://c.example.com/p.jpg?fmt=jpg&wid=504"])      # one candidate, not two

    def test_the_same_address_twice_and_the_cap_of_twelve(self):
        html = page(body="".join(f'<img src="https://cdn.example.com/i{i % 20}.jpg" alt="p{i}">' for i in range(60)))
        found = datasheet_page.parse_page(html, BASE)
        self.assertEqual(len(found), 12)
        self.assertEqual(len(set(urls(found))), 12)

    def test_hostile_markup_does_not_raise_or_hang(self):
        evil = [
            "<img src=" + "https://x.example.com/" + "a" * 50000 + ">",
            "<div>" * 20000 + '<img src="https://ok.example.com/a.jpg">',
            "<img src='https://ok.example.com/a.jpg' alt='" + "<script>alert(1)</script>" * 100 + "'>",
            "<meta property=og:image content=>" + "<img src>" * 100, "<<<<>>>> <img src=\"\x00https://x.example.com/a.jpg\x00\">",
            "<script type=application/ld+json>" + "[" * 10000 + "</script>", "<script type=application/ld+json>" + '{"a":' * 5000 + "</script>",
            "<img srcset=\"" + ", ".join(f"https://e.example.com/{i}.jpg {i}w" for i in range(5000)) + "\">",
            "<figure>" * 5000 + "<figcaption>" * 5000, "<![CDATA[ <img src=https://x.example.com/c.jpg> ]]>", "<!--" + "<img src=https://x.example.com/d.jpg>" * 10,
            "￾￿<img src=\"https://x.example.com/e.jpg\">",
        ]
        for i, text in enumerate(evil):
            found = datasheet_page.parse_page(text, BASE, model="X")
            self.assertLessEqual(len(found), 12, i)
            for c in found:
                self.assertTrue(c.url.startswith("https://"), i)

    def test_a_huge_page_is_cut_off_and_parsed_quickly(self):
        html = page(body="".join(f'<img src="https://cdn.example.com/p/{i}.jpg" alt="n{i}">' for i in range(100000)))
        self.assertGreater(len(html), 4_000_000)
        t = time.monotonic()
        found = datasheet_page.parse_page(html, BASE)
        self.assertLess(time.monotonic() - t, datasheet_page.PARSE_BUDGET + 3)
        self.assertEqual(len(found), 12)

    def test_challenge_pages_are_recognised(self):
        self.assertTrue(datasheet_page.looks_blocked("<title>Just a moment...</title><div id='cf-challenge'>"))
        self.assertTrue(datasheet_page.looks_blocked("<h1>Access Denied</h1> You don't have permission"))
        self.assertFalse(datasheet_page.looks_blocked("<h1>Catalyst 9500 Series</h1>"))


# ------------------------------------------------------------------------------------------ fetching, routing, error wording
HOST = "www.vendor-example.com"
CDN = "cdn.vendor-example.com"
HTML = {"Content-Type": "text/html; charset=utf-8"}
PNG_H = {"Content-Type": "image/png"}
GOOD = png(320, 200, color=(30, 90, 160))
GOOD2 = png(400, 260, color=(160, 90, 30))
SMALL = png(40, 30)


def net(routes, dns=None):
    return FakeNet(dns or {HOST: [PUBLIC], CDN: [PUBLIC]}, routes)


class PathConn(FakeConn):
    def __init__(self, host, table, log):
        super().__init__(None, log)
        self.host, self.table, self.path = host, table, None

    def request(self, method, path, headers=None):
        super().request(method, path, headers)
        self.path = path.split("?")[0]

    def getresponse(self):
        return self.table.get((self.host, self.path)) or FakeResponse(404, PNG_H, b"")


class PathNet(FakeNet):
    """A fake network that answers by host AND path: {(host, "/path"): FakeResponse}."""

    def __init__(self, table, dns=None):
        super().__init__(dns or {HOST: [PUBLIC], CDN: [PUBLIC]}, {})
        self.table = table

    def _open(self, scheme, host, ip, port, timeout):
        self.opened.append((scheme, host, ip, port))
        return PathConn(host, self.table, self.requests)


def at(host, response):
    return {(host, 443): response}


@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=[], PROBE_TIMEOUT=5)
class FetchWordingTests(SimpleTestCase):
    """The real probes.checks.fetch_bytes runs against a fake network; failures become sentences an operator can act on."""

    def find(self, routes, url=f"https://{HOST}/p", dns=None, model=""):
        with net(routes, dns) as n:
            self.n = n
            return datasheet.find_from_url(url, model)

    def fail(self, routes, **kw):
        with self.assertRaises(DatasheetError) as cm:
            self.find(routes, **kw)
        return cm.exception

    def test_blocked_by_status_says_to_download_the_pdf_yourself(self):
        for status in (403, 429, 401):
            e = self.fail(at(HOST, FakeResponse(status, HTML, b"nope")))
            self.assertTrue(e.blocked, status)
            self.assertEqual(str(e), "This site blocks automated downloads. Download the datasheet PDF yourself and upload it here.")
            self.assertEqual(len(self.n.requests), 1, "it must not retry or try another way in")

    def test_blocked_by_a_challenge_page(self):
        html = b"<html><title>Just a moment...</title><body>Checking your browser before accessing</body></html>"
        e = self.fail(at(HOST, FakeResponse(200, HTML, html)))
        self.assertTrue(e.blocked)
        self.assertIn("blocks automated downloads", str(e))

    def test_not_found_and_server_errors(self):
        self.assertIn("not found", str(self.fail(at(HOST, FakeResponse(404, HTML, b"")))))
        self.assertIn("HTTP 404", str(self.fail(at(HOST, FakeResponse(404, HTML, b"")))))
        e = self.fail(at(HOST, FakeResponse(503, HTML, b"")))
        self.assertFalse(e.blocked)
        self.assertIn("HTTP 503", str(e))

    def test_a_server_error_also_offers_the_upload_in_case_it_is_a_refusal(self):
        e = self.fail(at(HOST, FakeResponse(503, HTML, b"")))
        self.assertIn("refusing automated downloads", str(e))
        self.assertIn("upload it here", str(e))

    def test_a_redirect_loop_is_reported_as_a_refusal_with_the_upload(self):
        e = self.fail(at(HOST, FakeResponse(301, {"Location": f"https://{HOST}/p"})))
        self.assertTrue(e.blocked)
        self.assertIn("kept redirecting", str(e))
        self.assertIn("upload it here", str(e))
        self.assertLessEqual(len(self.n.requests), checks.MAX_REDIRECTS + 1)

    def test_a_large_product_guide_page_is_read(self):
        """Vendor product guides inline the whole guide: 3 to 4 MB of markup is normal and must not be refused."""
        body = page('<meta property="og:image" content="https://cdn.vendor-example.com/og.png">', "<p>" + "x" * (3 * 1024 * 1024) + "</p>").encode()
        table = {(HOST, "/p"): FakeResponse(200, HTML, body), (CDN, "/og.png"): FakeResponse(200, PNG_H, GOOD)}
        with PathNet(table):
            found = datasheet.find_from_url(f"https://{HOST}/p")
        self.assertEqual((found.kind, len(found.items)), ("page", 1))

    def test_pictures_ranked_equal_put_squarer_shots_before_banner_crops(self):
        wide, square = png(1200, 300, color=(30, 30, 30)), png(500, 360, color=(90, 40, 40))
        html = page(body=f'<img src="https://{CDN}/w.png" alt="Switch X1"><img src="https://{CDN}/s.png" alt="Switch X1">').encode()
        table = {(HOST, "/p"): FakeResponse(200, HTML, html), (CDN, "/w.png"): FakeResponse(200, PNG_H, wide),
                 (CDN, "/s.png"): FakeResponse(200, PNG_H, square)}
        with PathNet(table):
            found = datasheet.find_from_url(f"https://{HOST}/p", "Switch X1")
        self.assertEqual([(i.width, i.height) for i in found.items], [(500, 360), (1200, 300)])
        self.assertEqual(found.notes, [])

    def test_a_page_that_never_names_the_model_says_so(self):
        html = page(body=f'<img src="https://{CDN}/s.png" alt="Some tile">').encode()
        def table():
            return {(HOST, "/p"): FakeResponse(200, HTML, html), (CDN, "/s.png"): FakeResponse(200, PNG_H, GOOD)}
        with PathNet(table()):
            found = datasheet.find_from_url(f"https://{HOST}/p", "FortiGate 100F")
        self.assertTrue(any("mention FortiGate 100F by name" in n for n in found.notes), found.notes)
        with PathNet(table()):                                        # no model given: nothing to compare with, no note
            self.assertEqual(datasheet.find_from_url(f"https://{HOST}/p").notes, [])

    def test_timeout(self):
        e = self.fail(at(HOST, FakeResponse(200, HTML, b"<html>", read_error=__import__("socket").timeout())))
        self.assertIn("did not answer in time", str(e))
        self.assertFalse(e.blocked)

    def test_redirect_to_a_private_address_is_refused_and_not_followed(self):
        routes = {(HOST, 443): FakeResponse(302, {"Location": "https://internal.example.com/x"}),
                  ("internal.example.com", 443): FakeResponse(200, HTML, b"<html>secret</html>")}
        e = self.fail(routes, dns={HOST: [PUBLIC], "internal.example.com": ["10.0.0.7"]})
        self.assertIn("Blocked by probe policy", str(e))
        self.assertEqual([o[1] for o in self.n.opened], [HOST])

    def test_not_a_web_page_pdf_or_picture(self):
        e = self.fail(at(HOST, FakeResponse(200, {"Content-Type": "application/zip"}, b"PK\x03\x04")))
        self.assertIn("not a web page, a PDF or a PNG, JPEG or WebP picture", str(e))
        self.assertIn("application/zip", str(e))
        e = self.fail(at(HOST, FakeResponse(200, {"Content-Type": "application/octet-stream"}, b"\x00\x01binary")))
        self.assertIn("not a web page", str(e))

    def test_oversize_page_and_oversize_pdf(self):
        big = b"<html>" + b"x" * (datasheet_page.MAX_HTML_BYTES + 100)
        e = self.fail(at(HOST, FakeResponse(200, HTML, big)))
        self.assertIn("bigger than the 6 MB", str(e))
        e = self.fail(at(HOST, FakeResponse(200, {**HTML, "Content-Length": str(10 * 1024 * 1024)}, b"<html>")))
        self.assertIn("bigger than the 6 MB", str(e))

    def test_a_page_with_no_usable_pictures(self):
        e = self.fail(at(HOST, FakeResponse(200, HTML, page(body='<img src="/logo.png"><p>Hello</p>').encode())))
        self.assertFalse(e.blocked)
        self.assertIn("no usable product pictures", str(e))

    def test_every_picture_refused_is_reported_as_blocked(self):
        html = page('<meta property="og:image" content="https://cdn.vendor-example.com/a.png">', '<img src="https://cdn.vendor-example.com/b.png" alt="b">')
        e = self.fail({(HOST, 443): FakeResponse(200, HTML, html.encode()), (CDN, 443): FakeResponse(403, PNG_H, b"")})
        self.assertTrue(e.blocked)

    def test_pictures_that_cannot_be_used_are_counted_not_hidden(self):
        html = page('<meta property="og:image" content="https://cdn.vendor-example.com/a.png">', '<img src="https://cdn.vendor-example.com/b.png" alt="b">')
        e = self.fail({(HOST, 443): FakeResponse(200, HTML, html.encode()), (CDN, 443): FakeResponse(200, PNG_H, SMALL)})
        self.assertIn("too small", str(e))

    def test_the_page_route_downloads_the_candidates_validates_them_and_ranks_them(self):
        html = page(f'<meta property="og:image" content="https://{CDN}/og.png">', f'<img src="https://{CDN}/second.png" alt="Rear">'
                    f'<img src="https://{CDN}/tiny.png" alt="Tiny"><img src="https://{CDN}/notimg.png" alt="Fake">'
                    f'<img src="https://{CDN}/missing.png" alt="Missing">').encode()
        table = {(HOST, "/p"): FakeResponse(200, HTML, html), (CDN, "/og.png"): FakeResponse(200, PNG_H, GOOD),
                 (CDN, "/second.png"): FakeResponse(200, PNG_H, GOOD2), (CDN, "/tiny.png"): FakeResponse(200, PNG_H, SMALL),
                 (CDN, "/notimg.png"): FakeResponse(200, PNG_H, b"<html>not an image</html>")}
        with PathNet(table) as n:
            found = datasheet.find_from_url(f"https://{HOST}/p")
        self.assertEqual(found.kind, "page")
        self.assertEqual([(i.origin, i.width, i.height) for i in found.items], [("og:image", 320, 200), ("page image", 400, 260)])
        self.assertEqual(found.items[0].source_url, f"https://{CDN}/og.png")
        self.assertTrue(any("2 of 5 pictures" in note for note in found.notes), found.notes)
        # one page request, then one request per picture address, nothing else (no crawling)
        self.assertEqual(sorted(p for _m, p, _h in n.requests), sorted(["/p", "/og.png", "/second.png", "/tiny.png", "/notimg.png", "/missing.png"]))

    def test_the_user_agent_is_honest_and_no_browser_is_imitated(self):
        with net({(HOST, 443): FakeResponse(200, PNG_H, GOOD)}) as n:
            datasheet.find_from_url(f"https://{HOST}/photo.png")
        ua = n.requests[0][2]["User-Agent"]
        self.assertRegex(ua, r"^LinkedEye/\d")
        for word in ("Mozilla", "Chrome", "Safari", "Gecko", "AppleWebKit"):
            self.assertNotIn(word, ua)
        self.assertNotIn("Cookie", n.requests[0][2])
        self.assertNotIn("Referer", n.requests[0][2])

    def test_a_picture_address_is_routed_to_the_image_path(self):
        found = self.find(at(HOST, FakeResponse(200, PNG_H, GOOD)), url=f"https://{HOST}/x/photo.png")
        self.assertEqual((found.kind, found.items[0].origin, found.items[0].width, found.items[0].height), ("image", "image address", 320, 200))

    def test_a_pdf_address_is_routed_to_the_pdf_path(self):
        if pypdf is None:
            self.skipTest("pypdf is not installed")
        pdf = pdf_of([PRODUCT])
        for ctype in ("application/pdf", "application/octet-stream"):
            found = self.find(at(HOST, FakeResponse(200, {"Content-Type": ctype}, pdf)), url=f"https://{HOST}/ds.pdf")
            self.assertEqual((found.kind, found.items[0].origin), ("pdf", "datasheet page 1"), ctype)

    def test_only_https_addresses_are_fetched(self):
        for url in ("http://www.vendor-example.com/p", "ftp://x.example.com/", "javascript:alert(1)", "", "   ", "https://", "https://a b.example.com/",
                    "file:///etc/passwd", "https://" + "a" * 600 + ".example.com/"):
            with net({}) as n, self.assertRaises(DatasheetError, msg=url):
                datasheet.find_from_url(url)
            self.assertEqual(n.opened, [], url)

    def test_candidate_downloads_run_in_parallel_but_never_more_than_four_at_once(self):
        live, peak, lock = [0], [0], threading.Lock()

        def slow(*a, **kw):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.05)
            with lock:
                live[0] -= 1
            return checks.FetchResult(ok=True, status=200, body=GOOD, content_type="image/png", final_url=a[0])
        items = [datasheet_page.PageImage(f"https://{CDN}/{i}.png", "img", order=i) for i in range(12)]
        with mock.patch.object(checks, "fetch_bytes", side_effect=slow):
            got, stats = datasheet_page.download_images(items)
        self.assertEqual(stats.tried, 12)
        self.assertEqual(len(got), 1, "identical bytes are one candidate")
        self.assertEqual(stats.duplicates, 11)
        self.assertGreater(peak[0], 1)
        self.assertLessEqual(peak[0], 4)

    def test_the_overall_download_budget_is_enforced(self):
        def hang(*a, **kw):
            time.sleep(0.6)
            return checks.FetchResult(ok=True, status=200, body=GOOD, content_type="image/png", final_url=a[0])
        items = [datasheet_page.PageImage(f"https://{CDN}/{i}.png", "img", order=i) for i in range(8)]
        t = time.monotonic()
        with mock.patch.object(checks, "fetch_bytes", side_effect=hang):
            got, stats = datasheet_page.download_images(items, budget=0.2)
        self.assertLess(time.monotonic() - t, 1.5)
        self.assertEqual(got, [])
        self.assertEqual(stats.late, 8)


# ------------------------------------------------------------------------------------------ candidate store
def img_bytes(n, w=60, h=40):
    return png(w, h, color=(n % 256, (n * 7) % 256, (n * 13) % 256))


class Item:
    def __init__(self, data, origin="datasheet page 1", source_url=""):
        self.data, self.origin, self.source_url = data, origin, source_url


@override_settings(LINKEDEYE_TENANT=T)
class CandidateStoreTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()

    def test_candidates_belong_to_their_owner_and_are_not_photos(self):
        rows = images.replace_candidates(self.operator, [Item(img_bytes(1)), Item(img_bytes(2), "og:image")])
        self.assertEqual([r.origin for r in rows], ["datasheet page 1", "og:image"])
        for r in rows:
            self.assertEqual((r.is_candidate, r.candidate_owner, r.uploaded_by_id, r.source), (True, self.operator.pk, self.operator.pk, "datasheet"))
        self.assertEqual(images.live_candidates(self.operator).count(), 2)
        self.assertEqual(images.live_candidates(self.admin).count(), 0)
        self.assertFalse(images.visible_to(self.admin, rows[0]))
        self.assertFalse(images.visible_to(self.operator, rows[0]), "a candidate is on no device, so the device photo route never serves it")
        # the real-photo store neither matches nor counts them
        real, created = images.store_image(img_bytes(1), source="upload")
        self.assertTrue(created)
        self.assertNotEqual(real.pk, rows[0].pk)
        self.assertFalse(real.is_candidate)

    def test_a_new_search_replaces_your_previous_candidates(self):
        images.replace_candidates(self.operator, [Item(img_bytes(1)), Item(img_bytes(2))])
        images.replace_candidates(self.operator, [Item(img_bytes(3))])
        self.assertEqual(list(images.live_candidates(self.operator).values_list("width", flat=True)), [60])
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), 1)

    def test_two_people_can_hold_candidates_with_the_same_bytes(self):
        images.replace_candidates(self.operator, [Item(img_bytes(1))])
        images.replace_candidates(self.admin, [Item(img_bytes(1))])
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), 2)
        self.assertEqual(images.live_candidates(self.operator).count(), 1)

    def test_ttl_hides_then_the_prune_job_deletes(self):
        rows = images.replace_candidates(self.operator, [Item(img_bytes(1)), Item(img_bytes(2))])
        DeviceImage.objects.filter(pk=rows[0].pk).update(created_at=timezone.now() - timedelta(minutes=61))
        self.assertEqual(images.live_candidates(self.operator).count(), 1)          # expired = invisible even before the job ran
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), 2)
        self.assertIn("1 expired", jobs.prune_photo_candidates())
        self.assertEqual(list(DeviceImage.objects.filter(is_candidate=True).values_list("pk", flat=True)), [rows[1].pk])
        self.assertIsNone(jobs.prune_photo_candidates())
        # a photo that is in use is never touched by the job, however old
        real, _ = images.store_image(img_bytes(9), source="upload")
        DeviceImage.objects.filter(pk=real.pk).update(created_at=timezone.now() - timedelta(days=30))
        jobs.prune_photo_candidates()
        self.assertTrue(DeviceImage.objects.filter(pk=real.pk).exists())

    def test_the_job_is_registered_every_fifteen_minutes(self):
        job = core_jobs.JOBS["network.photo_candidates"]
        self.assertEqual(job["interval"], 15 * 60)
        self.assertIs(job["func"], jobs.prune_photo_candidates)

    def test_the_per_tenant_cap_drops_the_oldest(self):
        users_ = [User.objects.create_user(f"u{i}", password="pw", is_staff=True) for i in range(7)]
        n = 0
        for u in users_:                       # 7 people x 12 pictures = 84 > 60
            images.replace_candidates(u, [Item(img_bytes(n + i + 100)) for i in range(12)])
            n += 12
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), DeviceImage.MAX_CANDIDATES_PER_TENANT)
        self.assertEqual(images.live_candidates(users_[-1]).count(), 12, "the newest search is complete")
        self.assertLess(images.live_candidates(users_[0]).count(), 12, "the oldest were dropped to make room")

    def test_a_single_search_cannot_exceed_the_cap_either(self):
        rows = images.replace_candidates(self.operator, [Item(img_bytes(i + 500)) for i in range(80)])
        self.assertEqual(len(rows), 60)
        self.assertEqual(DeviceImage.objects.filter(is_candidate=True).count(), 60)

    def test_unacceptable_bytes_are_refused_and_nothing_is_stored_when_all_are_bad(self):
        with self.assertRaises(imaging.ImageError):
            images.replace_candidates(self.operator, [Item(b"<svg onload=alert(1)>"), Item(b"")])
        self.assertFalse(DeviceImage.objects.exists())
        rows = images.replace_candidates(self.operator, [Item(b"<svg/>"), Item(img_bytes(1))])
        self.assertEqual(len(rows), 1)

    def test_promote_makes_a_real_photo_only_for_its_owner_within_the_hour(self):
        rows = images.replace_candidates(self.operator, [Item(img_bytes(1), "datasheet page 2", "https://cdn.example.com/a.png?token=secret")])
        c = rows[0]
        self.assertNotIn("token", c.source_url)
        with self.assertRaises(imaging.ImageError):
            images.promote(c, self.admin)
        DeviceImage.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(minutes=61))
        with self.assertRaises(imaging.ImageError):
            images.promote(c, self.operator)
        DeviceImage.objects.filter(pk=c.pk).update(created_at=timezone.now())
        real = images.promote(c, self.operator)
        self.assertEqual((real.pk, real.is_candidate, real.candidate_owner, real.source, real.origin), (c.pk, False, 0, "datasheet", "datasheet page 2"))
        self.assertEqual(images.live_candidates(self.operator).count(), 0)

    def test_promoting_bytes_that_are_already_a_photo_reuses_that_photo(self):
        existing, _ = images.store_image(img_bytes(1), source="upload")
        c = images.replace_candidates(self.operator, [Item(img_bytes(1))])[0]
        self.assertEqual(images.promote(c, self.operator).pk, existing.pk)
        self.assertFalse(DeviceImage.objects.filter(pk=c.pk).exists())

    def test_another_tenants_candidate_is_invisible(self):
        c = images.replace_candidates(self.operator, [Item(img_bytes(1))], tenant_key="other-tenant")[0]
        self.assertEqual(images.live_candidates(self.operator).count(), 0)
        with self.assertRaises(imaging.ImageError):
            images.promote(c, self.operator)


# ------------------------------------------------------------------------------------------ the picker views
def multipart_pdf(data, name="datasheet.pdf"):
    return SimpleUploadedFile(name, data, content_type="application/pdf")


@override_settings(LINKEDEYE_TENANT=T, PROBE_ALLOWED_PRIVATE_CIDRS=[], PROBE_TIMEOUT=5)
class PickerViewTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.mum = Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        self.dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        self.d1 = dev("mum-sw-01", site="Mumbai", vendor="Cisco", model="Catalyst 9500-48Y4C", mgmt_ip="192.0.2.11")
        self.d2 = dev("mum-sw-02", site="Mumbai", vendor="Cisco", model="Catalyst 9500-48Y4C", mgmt_ip="192.0.2.12")
        self.d3 = dev("dr-sw-01", site="DR Site", vendor="Cisco", model="Catalyst 9500-48Y4C", mgmt_ip="192.0.2.13")
        self.find_url = reverse("network:photo_find")
        self.pdf = pdf_of([PRODUCT], [dict(w=700, h=500, kind="jpeg")]) if pypdf else b""
        self.c = Client()

    def login(self, user):
        self.c.force_login(user)
        return self.c

    def search(self, user, device=None, **data):
        self.login(user)
        return self.c.post(self.find_url, {"device": (device or self.d1).pk, **data})

    def pick(self, user, candidate, device=None, **data):
        self.login(user)
        return self.c.post(reverse("network:photo_apply"), {"device": (device or self.d1).pk, "candidate": candidate.pk, **data})

    # --- finding
    @needs_pypdf
    def test_operator_uploads_a_datasheet_and_sees_ranked_candidates(self):
        r = self.search(self.operator, pdf_file=multipart_pdf(self.pdf))
        self.assertEqual(r.status_code, 302, r.content[:500])
        r = self.c.get(r["Location"])
        html = r.content.decode()
        self.assertEqual(r.status_code, 200)
        cands = list(images.live_candidates(self.operator))
        self.assertEqual([(c.width, c.height, c.origin) for c in cands], [(900, 520, "datasheet page 1"), (700, 500, "datasheet page 2")])
        for c in cands:
            self.assertIn(f'src="{c.candidate_url}"', html)
            self.assertIn(f"{c.width} × {c.height} px", html)
        self.assertIn("datasheet page 1", html)
        self.assertIn("Search for the datasheet", html)
        self.assertIn("https://duckduckgo.com/?q=Cisco%20Catalyst%209500-48Y4C%20datasheet", html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertEqual(Device.objects.get(pk=self.d1.pk).image_id, None, "nothing is attached before a candidate is picked")
        ev = AuditEvent.objects.get(action="search")
        self.assertEqual((ev.actor_name, ev.details["via"], ev.details["candidates"]), ("operator", "pdf", 2))

    @needs_pypdf
    def test_a_datasheet_larger_than_django_keeps_in_memory_still_works(self):
        big = pdf_of([dict(w=900, h=800, kind="jpeg", data=sample_jpeg(900, 800, size=1_500_000))],
                     [dict(w=800, h=700, kind="jpeg", data=sample_jpeg(800, 700, size=1_500_000))])
        self.assertGreater(len(big), 2_700_000)       # above FILE_UPLOAD_MAX_MEMORY_SIZE (2.5 MB): Django would use a temp file
        with mock.patch("tempfile.NamedTemporaryFile", side_effect=AssertionError("written to disk")), \
                mock.patch("django.core.files.uploadedfile.TemporaryUploadedFile.__init__", side_effect=AssertionError("written to disk")):
            r = self.search(self.operator, pdf_file=multipart_pdf(big))
        self.assertEqual(r.status_code, 302, r.content[:300])
        self.assertEqual(images.live_candidates(self.operator).count(), 2)

    def test_an_oversize_upload_is_refused_up_front(self):
        self.login(self.operator)
        r = self.c.generic("POST", self.find_url, b"--x--", "multipart/form-data; boundary=x", CONTENT_LENGTH=str(30 * 1024 * 1024))
        self.assertEqual(r.status_code, 413)

    def test_a_picture_upload_becomes_one_candidate(self):
        r = self.search(self.operator, pdf_file=upload(GOOD, "front.png"))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(list(images.live_candidates(self.operator).values_list("origin", flat=True)), ["uploaded picture"])

    def test_something_that_is_not_a_pdf_or_picture_is_explained(self):
        r = self.search(self.operator, pdf_file=SimpleUploadedFile("x.pdf", b"<html>hi</html>", content_type="application/pdf"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("not a PDF or a PNG, JPEG or WebP", r.content.decode())

    def test_a_blocking_site_says_so_and_offers_the_upload(self):
        self.login(self.operator)
        with net(at(HOST, FakeResponse(403, HTML, b"denied"))):
            r = self.c.post(self.find_url, {"device": self.d1.pk, "source_url": f"https://{HOST}/c9500"})
        self.assertEqual(r.status_code, 400)
        html = r.content.decode()
        self.assertIn("This site blocks automated downloads. Download the datasheet PDF yourself and upload it here.", html)
        self.assertIn('name="pdf_file"', html)
        self.assertIn(f"https://{HOST}/c9500", html, "the address stays in the box")
        self.assertEqual(AuditEvent.objects.get(action="search").result, "error")

    def test_a_product_page_address_gives_candidates_without_a_real_socket(self):
        html = page(f'<meta property="og:image" content="https://{CDN}/og.png">').encode()
        routes = {(HOST, 443): FakeResponse(200, HTML, html), (CDN, 443): FakeResponse(200, PNG_H, GOOD)}
        self.login(self.operator)
        with net(routes), mock.patch("socket.socket", side_effect=AssertionError("real socket")):
            r = self.c.post(self.find_url, {"device": self.d1.pk, "source_url": f"https://{HOST}/c9500"})
        self.assertEqual(r.status_code, 302)
        c = images.live_candidates(self.operator).get()
        self.assertEqual((c.origin, c.source_url), ("og:image", f"https://{CDN}/og.png"))

    def test_both_a_file_and_an_address_is_refused(self):
        r = self.search(self.operator, pdf_file=upload(GOOD, "a.png"), source_url=f"https://{HOST}/p")
        self.assertEqual(r.status_code, 400)
        self.assertIn("one at a time", r.content.decode())

    def test_nothing_chosen_is_explained(self):
        r = self.search(self.operator)
        self.assertEqual(r.status_code, 400)
        self.assertIn("Choose the datasheet PDF", r.content.decode())

    # --- who may
    def test_a_viewer_cannot_find_or_apply(self):
        self.login(self.viewer)
        self.assertEqual(self.c.get(self.find_url, {"device": self.d1.pk}).status_code, 403)
        r = self.c.post(self.find_url, {"device": self.d1.pk, "source_url": f"https://{HOST}/p"})
        self.assertIn(r.status_code, (302, 403))
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        r = self.c.post(reverse("network:photo_apply"), {"device": self.d1.pk, "candidate": c.pk})
        self.assertIn(r.status_code, (302, 403))
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)
        self.assertTrue(DeviceImage.objects.get(pk=c.pk).is_candidate)

    def test_an_operator_on_another_site_cannot(self):
        Membership.objects.create(user=self.operator, site=self.dr, role="operator")
        self.login(User.objects.get(pk=self.operator.pk))
        self.assertEqual(self.c.get(self.find_url, {"device": self.d1.pk}).status_code, 404)
        self.assertEqual(self.c.post(self.find_url, {"device": self.d1.pk, "source_url": f"https://{HOST}/p"}).status_code, 404)
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        self.assertEqual(self.c.post(reverse("network:photo_apply"), {"device": self.d1.pk, "candidate": c.pk}).status_code, 404)
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)
        # viewing a site but only as a viewer there: forbidden rather than hidden
        Membership.objects.create(user=self.operator, site=self.mum, role="viewer")
        self.login(User.objects.get(pk=self.operator.pk))
        self.assertEqual(self.c.get(self.find_url, {"device": self.d1.pk}).status_code, 403)

    def test_an_operator_on_this_site_can_apply(self):
        Membership.objects.create(user=self.operator, site=self.mum, role="operator")
        op = User.objects.get(pk=self.operator.pk)
        c = images.replace_candidates(op, [Item(GOOD, "datasheet page 1")])[0]
        r = self.pick(op, c)
        self.assertEqual(r.status_code, 302)
        d = Device.objects.get(pk=self.d1.pk)
        self.assertEqual((d.image_id, d.image.source, d.image.is_candidate), (c.pk, "datasheet", False))
        self.assertFalse(NetworkProductImage.objects.exists())
        self.assertIsNone(Device.objects.get(pk=self.d2.pk).image_id, "without the catalog box only this device changes")
        ev = AuditEvent.objects.get(action="update", object_type="network.device", object_id=str(self.d1.pk))
        self.assertEqual((ev.details["changed"], ev.details["via"]), (["image"], "datasheet"))
        # and the finished photo is served through the normal route
        self.assertEqual(self.c.get(d.image.url).status_code, 200)

    def test_the_catalog_box_needs_admin_and_changes_nothing_for_an_operator(self):
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        r = self.pick(self.operator, c, catalog_save="on")
        self.assertEqual(r.status_code, 403)
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)
        self.assertTrue(DeviceImage.objects.get(pk=c.pk).is_candidate)
        self.assertFalse(NetworkProductImage.objects.exists())

    def test_admin_saves_it_to_the_catalog_for_every_device_of_the_model(self):
        own, _ = images.store_image(GOOD2, source="upload")
        Device.objects.filter(pk=self.d3.pk).update(image=own)          # already has its own photo: left alone
        c = images.replace_candidates(self.admin, [Item(GOOD)])[0]
        r = self.pick(self.admin, c, catalog_save="on")
        self.assertEqual(r.status_code, 302)
        row = NetworkProductImage.objects.get()
        self.assertEqual((row.vendor, row.model_key, row.device_type, row.image_id), ("Cisco", "Catalyst 9500-48Y4C", "", c.pk))
        self.assertEqual(Device.objects.get(pk=self.d1.pk).image_id, c.pk)
        self.assertEqual(Device.objects.get(pk=self.d2.pk).image_id, c.pk)
        self.assertEqual(Device.objects.get(pk=self.d3.pk).image_id, own.pk)
        self.assertEqual(AuditEvent.objects.filter(action="create", object_type="network.networkproductimage").count(), 1)
        # a future device of that model picks the catalog photo up on its own (the existing form behaviour)
        self.login(self.admin)
        with mock.patch("network.jobs.check_devices"):
            self.c.post(reverse("network:create"), form_data(name="mum-sw-09", vendor="Cisco", model="Catalyst 9500-48Y4C", mgmt_ip="192.0.2.99"))
        self.assertEqual(Device.objects.get(name="mum-sw-09").image_id, c.pk)

    def test_the_catalog_box_without_a_vendor_and_model_is_refused(self):
        d = dev("bare-01", site="Mumbai", mgmt_ip="192.0.2.50")
        c = images.replace_candidates(self.admin, [Item(GOOD)])[0]
        r = self.pick(self.admin, c, device=d, catalog_save="on")
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(Device.objects.get(pk=d.pk).image_id)
        self.assertFalse(NetworkProductImage.objects.exists())

    def test_replacing_a_photo_releases_the_old_one(self):
        old, _ = images.store_image(GOOD2, source="upload")
        Device.objects.filter(pk=self.d1.pk).update(image=old)
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        self.pick(self.operator, c)
        self.assertFalse(DeviceImage.objects.filter(pk=old.pk).exists())

    # --- isolation
    def test_another_users_candidate_cannot_be_applied_or_viewed(self):
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        other = User.objects.create_user("operator2", password="pw", is_staff=True)
        r = self.pick(other, c)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)
        self.assertTrue(DeviceImage.objects.get(pk=c.pk).is_candidate)
        self.assertEqual(self.c.get(c.candidate_url).status_code, 404)
        self.assertEqual(self.login(self.admin).get(c.candidate_url).status_code, 404, "not even an admin sees it")
        self.assertEqual(self.login(self.operator).get(c.candidate_url).status_code, 200)

    def test_another_tenants_candidate_cannot_be_applied_or_viewed(self):
        c = images.replace_candidates(self.operator, [Item(GOOD)], tenant_key="other-tenant")[0]
        self.assertEqual(self.login(self.operator).get(c.candidate_url).status_code, 404)
        self.pick(self.operator, c)
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)

    def test_an_expired_candidate_cannot_be_applied_or_viewed(self):
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        DeviceImage.objects.filter(pk=c.pk).update(created_at=timezone.now() - timedelta(minutes=61))
        self.assertEqual(self.login(self.operator).get(c.candidate_url).status_code, 404)
        r = self.pick(self.operator, c)
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)
        self.assertIn("no longer available", self.c.get(r["Location"]).content.decode())

    def test_a_candidate_is_not_served_by_the_photo_route_and_needs_login(self):
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        self.assertEqual(self.login(self.admin).get(f"/network/images/{c.sha256}.png").status_code, 404)
        self.assertEqual(Client().get(c.candidate_url).status_code, 302)

    def test_candidate_bytes_are_served_with_locked_down_headers(self):
        c = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        r = self.login(self.operator).get(c.candidate_url)
        self.assertEqual((r.status_code, r.content, r["Content-Type"]), (200, GOOD, "image/png"))
        self.assertEqual(r["Cache-Control"], "private, no-store")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["Content-Security-Policy"], "sandbox; default-src 'none'")

    # --- CSRF
    def test_every_picker_post_needs_the_csrf_token(self):
        c = Client(enforce_csrf_checks=True)
        c.force_login(self.operator)
        cand = images.replace_candidates(self.operator, [Item(GOOD)])[0]
        for name, data in (("photo_find", {"device": self.d1.pk, "source_url": f"https://{HOST}/p"}),
                           ("photo_apply", {"device": self.d1.pk, "candidate": cand.pk}),
                           ("photo_discard", {"device": self.d1.pk}),
                           ("photo_apply_catalog", {"all": "1"})):
            self.assertEqual(c.post(reverse(f"network:{name}"), data).status_code, 403, name)
        self.assertIsNone(Device.objects.get(pk=self.d1.pk).image_id)
        self.assertTrue(DeviceImage.objects.get(pk=cand.pk).is_candidate)
        # with the token the same upload works
        html = c.get(self.find_url, {"device": self.d1.pk}).content.decode()
        token = html.split('name="csrfmiddlewaretoken" value="')[1].split('"')[0]
        r = c.post(self.find_url, {"device": self.d1.pk, "csrfmiddlewaretoken": token, "pdf_file": upload(GOOD, "a.png")})
        self.assertEqual(r.status_code, 302)

    def test_discard_removes_only_your_own(self):
        images.replace_candidates(self.operator, [Item(GOOD)])
        images.replace_candidates(self.admin, [Item(GOOD2)])
        self.login(self.operator).post(reverse("network:photo_discard"), {"device": self.d1.pk})
        self.assertEqual(images.live_candidates(self.operator).count(), 0)
        self.assertEqual(images.live_candidates(self.admin).count(), 1)

    def test_a_missing_or_bad_context(self):
        self.login(self.operator)
        self.assertEqual(self.c.get(self.find_url).status_code, 302)                       # nothing chosen: go and choose
        self.assertEqual(self.c.get(self.find_url, {"device": "99999"}).status_code, 404)
        self.assertEqual(self.c.get(self.find_url, {"device": "x"}).status_code, 404)
        self.assertEqual(self.c.get(self.find_url, {"vendor": "Nobody", "model": "Nothing"}).status_code, 404)
        self.assertEqual(self.c.get(self.find_url, {"catalog": "1"}).status_code, 403)        # the catalog is admin only
        self.assertEqual(self.login(self.admin).get(self.find_url, {"catalog": "99999"}).status_code, 404)

    # --- the catalog row flavour and the page
    def test_a_catalog_row_gets_its_photo_from_a_datasheet_and_waiting_devices_follow(self):
        row = NetworkProductImage.objects.create(tenant_key=T, vendor="Cisco", model_key="Catalyst 9500", device_type="")
        c = images.replace_candidates(self.admin, [Item(GOOD)])[0]
        self.login(self.admin)
        self.assertEqual(self.c.get(self.find_url, {"catalog": row.pk}).status_code, 200)
        r = self.c.post(reverse("network:photo_apply"), {"catalog": row.pk, "candidate": c.pk})
        self.assertEqual(r.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(row.image_id, c.pk)
        self.assertEqual(set(Device.objects.exclude(image=None).values_list("pk", flat=True)), {self.d1.pk, self.d2.pk, self.d3.pk})
        # an operator cannot touch a catalog row
        op = User.objects.create_user("op3", password="pw", is_staff=True)
        Membership.objects.create(user=op, site=self.mum, role="operator")
        self.assertEqual(self.login(User.objects.get(pk=op.pk)).get(self.find_url, {"catalog": row.pk}).status_code, 403)

    def test_the_page_shows_the_context_and_the_buttons(self):
        r = self.login(self.operator).get(self.find_url, {"device": self.d1.pk})
        html = r.content.decode()
        self.assertContains(r, "Photo for mum-sw-01")
        self.assertIn("Get the datasheet", html)
        self.assertNotIn("Use this photo for every", html, "no candidates yet, so no picker")
        images.replace_candidates(self.operator, [Item(GOOD)])
        html = self.c.get(self.find_url, {"device": self.d1.pk}).content.decode()
        self.assertIn("applied to this device only", html)                          # operator: no catalog box
        self.assertNotIn('name="catalog_save"', html)
        images.replace_candidates(self.admin, [Item(GOOD)])
        html = self.login(self.admin).get(self.find_url, {"device": self.d1.pk}).content.decode()
        self.assertIn('name="catalog_save"', html)
        self.assertIn("Use this photo for every Cisco Catalyst 9500-48Y4C device", html)


# ------------------------------------------------------------------------------------------ devices missing a photo
@override_settings(LINKEDEYE_TENANT=T)
class MissingPhotosTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.mum = Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        self.dr = Site.objects.create(tenant_key=T, name="DR Site", code="dr")
        self.url = reverse("network:photos")
        self.cat = dict(vendor="Cisco", model="Catalyst 9500-48Y4C")
        for i in (1, 2, 3):
            dev(f"cat-{i}", site="Mumbai", mgmt_ip=f"192.0.2.{i}", **self.cat)
        dev("forti-1", site="Mumbai", mgmt_ip="192.0.2.20", device_type="firewall", vendor="Fortinet", model="FortiGate 100F")
        dev("bare-1", site="Mumbai", mgmt_ip="192.0.2.30", vendor="Acme")                    # no model: cannot be searched
        dev("vm-1", site="Mumbai", mgmt_ip="192.0.2.31", device_type="virtual_machine", vendor="VMware", model="Guest")
        self.photo, _ = images.store_image(GOOD, source="upload")
        dev("done-1", site="Mumbai", mgmt_ip="192.0.2.32", vendor="Juniper", model="MX204", image=self.photo)
        dev("linked-1", site="Mumbai", mgmt_ip="192.0.2.33", vendor="Aruba", model="CX 6300M", product_image_url="https://cdn.example.com/a.png")
        dev("dr-1", site="DR Site", mgmt_ip="192.0.2.40", vendor="Dell", model="PowerEdge R750", device_type="physical_server")
        self.c = Client()

    def groups(self, user=None):
        self.c.force_login(user or self.operator)
        r = self.c.get(self.url)
        return r, r.context["ov"]

    def test_groups_by_vendor_and_model_with_counts_and_kpis(self):
        r, ov = self.groups()
        self.assertEqual(r.status_code, 200)
        self.assertEqual([(g.label, g.count) for g in ov.groups],
                         [("Cisco Catalyst 9500-48Y4C", 3), ("Dell PowerEdge R750", 1), ("Fortinet FortiGate 100F", 1)])
        self.assertEqual((ov.with_photo, ov.illustration_only, ov.models_missing), (2, 6, 3))
        self.assertEqual([d.name for d in ov.unnamed], ["bare-1"])
        self.assertNotIn("vm-1", [d.name for g in ov.groups for d in g.devices] + [d.name for d in ov.unnamed], "virtual machines have no datasheet")
        html = r.content.decode()
        self.assertIn("Cisco</b> Catalyst 9500-48Y4C", html)
        self.assertIn("With a real photo", html)
        self.assertIn("Models still missing", html)
        self.assertIn('class="ph-table"', html, "the table turns into cards on a phone so the action is never scrolled away")
        css = open(os.path.join(os.path.dirname(__file__), "static", "network", "page.css"), encoding="utf-8").read()
        self.assertIn(".pg-network .ph-table tr { padding", css)
        self.assertIn(f"{reverse('network:photo_find')}?vendor=Cisco&amp;model=Catalyst+9500-48Y4C", html)
        self.assertIn("Get photo from datasheet", html)

    def test_scoped_to_the_sites_the_user_can_operate(self):
        Membership.objects.create(user=self.operator, site=self.dr, role="operator")
        Membership.objects.create(user=self.operator, site=self.mum, role="viewer")
        _, ov = self.groups(User.objects.get(pk=self.operator.pk))
        self.assertEqual([g.label for g in ov.groups], ["Dell PowerEdge R750"])

    def test_viewer_cannot_open_the_page(self):
        self.c.force_login(self.viewer)
        self.assertEqual(self.c.get(self.url).status_code, 403)

    def test_catalog_photo_is_offered_per_group_and_applied_in_one_click(self):
        NetworkProductImage.objects.create(tenant_key=T, vendor="Cisco", model_key="Catalyst 9500", device_type="", image=self.photo)
        r, ov = self.groups()
        g = ov.groups[0]
        self.assertEqual((g.label, g.catalog_count, g.catalog_row.model_key), ("Cisco Catalyst 9500-48Y4C", 3, "Catalyst 9500"))
        self.assertEqual(ov.catalog_applicable, 3)
        self.assertIn("Apply catalog photo to 3 devices", r.content.decode())
        r = self.c.post(reverse("network:photo_apply_catalog"), {"vendor": "Cisco", "model": "Catalyst 9500-48Y4C"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Device.objects.filter(vendor="Cisco", image=self.photo).count(), 3)
        self.assertEqual(AuditEvent.objects.filter(action="update", details__via="catalog").count(), 3)
        _, ov = self.groups()
        self.assertEqual([g.label for g in ov.groups], ["Dell PowerEdge R750", "Fortinet FortiGate 100F"])

    def test_apply_all_catalog_photos(self):
        NetworkProductImage.objects.create(tenant_key=T, vendor="Cisco", model_key="", device_type="", image=self.photo)
        NetworkProductImage.objects.create(tenant_key=T, vendor="Fortinet", model_key="FortiGate", device_type="firewall", image=self.photo)
        self.c.force_login(self.operator)
        self.c.post(reverse("network:photo_apply_catalog"), {"all": "1"})
        self.assertEqual(Device.objects.filter(image=self.photo).count(), 1 + 3 + 1)
        # nothing fits the Dell, and a viewer cannot do it at all
        self.assertIsNone(Device.objects.get(name="dr-1").image_id)
        self.c.force_login(self.viewer)
        self.assertIn(self.c.post(reverse("network:photo_apply_catalog"), {"all": "1"}).status_code, (302, 403))

    def test_group_apply_from_a_datasheet_covers_every_device_of_the_model(self):
        cand = images.replace_candidates(self.operator, [Item(GOOD2)])[0]
        self.c.force_login(self.operator)
        r = self.c.post(reverse("network:photo_apply"), {**self.cat, "candidate": cand.pk})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(set(Device.objects.filter(image=cand.pk).values_list("name", flat=True)), {"cat-1", "cat-2", "cat-3"})
        self.assertFalse(NetworkProductImage.objects.exists(), "operators do not write the catalog")

    def test_group_apply_never_touches_other_sites_a_user_cannot_operate(self):
        dev("cat-dr", site="DR Site", mgmt_ip="192.0.2.50", **self.cat)
        Membership.objects.create(user=self.operator, site=self.mum, role="operator")
        Membership.objects.create(user=self.operator, site=self.dr, role="viewer")
        op = User.objects.get(pk=self.operator.pk)
        cand = images.replace_candidates(op, [Item(GOOD2)])[0]
        self.c.force_login(op)
        self.c.post(reverse("network:photo_apply"), {**self.cat, "candidate": cand.pk})
        self.assertIsNone(Device.objects.get(name="cat-dr").image_id)
        self.assertEqual(Device.objects.filter(image=cand.pk).count(), 3)

    def test_links_to_the_page_from_the_list_catalog_and_card(self):
        self.c.force_login(self.admin)
        self.assertIn(self.url, self.c.get(reverse("network:all")).content.decode())
        self.assertIn(self.url, self.c.get(reverse("network:catalog")).content.decode())
        d = Device.objects.get(name="cat-1")
        html = self.c.get(d.get_absolute_url()).content.decode()
        self.assertIn("Add real photo", html)
        self.assertIn(f"{reverse('network:photo_find')}?device={d.pk}", html)
        self.c.force_login(self.viewer)
        self.assertNotIn("Add real photo", self.c.get(d.get_absolute_url()).content.decode())


# ------------------------------------------------------------------------------------------ how a stored photo is shown
@override_settings(LINKEDEYE_TENANT=T)
class PhotoDisplayTests(TestCase):
    def setUp(self):
        self.viewer, self.operator, self.admin = users()
        self.c = Client()
        self.c.force_login(self.operator)

    def card(self, d):
        return self.c.get(d.get_absolute_url()).content.decode()

    def test_a_real_photo_has_no_illustration_label_and_no_add_link(self):
        big, _ = images.store_image(png(640, 400), source="datasheet")
        d = dev("a", image=big)
        html = self.card(d)
        self.assertIn(f'src="{big.url}"', html)
        self.assertNotIn(">Illustration<", html)
        self.assertNotIn("Add real photo", html)
        self.assertNotIn('class="is-small"', html)

    def test_a_small_photo_is_marked_to_stay_at_natural_size(self):
        small, _ = images.store_image(png(280, 180), source="datasheet")
        self.assertTrue(small.is_small)
        self.assertIn('class="is-small"', self.card(dev("b", image=small)))
        edge, _ = images.store_image(png(300, 100), source="datasheet")
        self.assertFalse(edge.is_small)

    def test_an_illustration_is_labelled_and_offers_the_real_photo(self):
        html = self.card(dev("c"))
        self.assertIn(">Illustration<", html)
        self.assertIn("Add real photo", html)

    def test_the_stylesheet_never_stretches_or_crops_a_photo(self):
        css = open(os.path.join(os.path.dirname(__file__), "static", "network", "device-card.css"), encoding="utf-8").read()
        self.assertIn("object-fit: contain", css)
        self.assertIn(".dc-photo img.is-small { width: auto; height: auto; }", css)
        self.assertIn("--dc-photo-bg", css)
