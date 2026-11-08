"""The optional Pillow path for datasheet photos (network/imaging_pillow.py): print-colour (CMYK) JPEGs, CMYK / indexed / 16-bit
Flate images, soft masks, oversized photos, hostile input, and the behaviour of a deployment WITHOUT Pillow.

Every PDF and picture is built from bytes in this file (the pictures with Pillow itself, when it is installed); no vendor file,
address or image is used and no test opens a socket. Tests that need Pillow skip cleanly when it is absent; the tests of the
no-Pillow behaviour run either way because they make the lazy import fail."""
import ast
import io
import os
import struct
import sys
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.models import Site

from . import datasheet_pdf, imaging_pillow
from .imaging import ImageError, inspect_image
from .tests import T, dev, users
from .tests_datasheet import Doc, PRODUCT, needs_pypdf, pdf_of, sample_jpeg, sample_pixels

try:
    from PIL import Image, ImageChops
except ImportError:                                           # pragma: no cover - Pillow is optional
    Image = ImageChops = None

needs_pillow = unittest.skipUnless(Image is not None and imaging_pillow.available(), "Pillow (optional) is not installed")
RED_HINT = "These photos are in print (CMYK) colour. Install the optional image library (pip install -r requirements-images.txt)"


def hide_pillow():
    """Make `from PIL import Image` fail, as on a deployment that never installed the extra."""
    return mock.patch.dict(sys.modules, {"PIL": None, "PIL.Image": None, "PIL.ImageChops": None, "PIL.ImageFile": None})


# ------------------------------------------------------------------------------------------ synthetic pictures (need Pillow)
def noise(w, h, sigma=30):
    return Image.effect_noise((w, h), sigma)


def red_cmyk(w, h):
    """A CMYK picture whose ink is red (little cyan, magenta and yellow almost full, a little black), with enough texture in
    every channel that it has far more than 256 colours, like a photo."""
    c = noise(w, h, 20).point(lambda v: v // 8)
    m = noise(w, h, 20).point(lambda v: 255 - v // 8)
    y = noise(w, h, 20).point(lambda v: 255 - v // 8)
    k = noise(w, h, 20).point(lambda v: v // 4)
    return Image.merge("CMYK", (c, m, y, k))


def jpeg_bytes(im, **kw):
    buf = io.BytesIO()
    im.save(buf, "JPEG", **kw)
    return buf.getvalue()


def adobe_cmyk_jpeg(w=400, h=300):
    """What Photoshop and Pillow write: the stored values are 255 - ink, with an Adobe marker."""
    return jpeg_bytes(red_cmyk(w, h), quality=90)


def plain_cmyk_jpeg(w=400, h=300):
    """CMYK JPEG whose stored values are the ink amounts themselves (what a PDF without /Decode means)."""
    return jpeg_bytes(ImageChops.invert(red_cmyk(w, h)), quality=90)


INVERTED = "/Decode [1 0 1 0 1 0 1 0]"


def markers(data):
    out, pos = [], 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        m = data[pos + 1]
        out.append(m)
        if m == 0xDA:
            break
        pos += 2 + struct.unpack(">H", data[pos + 2:pos + 4])[0]
    return out


def rgb_means(data):
    im = Image.open(io.BytesIO(data)).convert("RGB")
    px = im.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
    return px


def flate(d, w, h, cs, samples, bpc=8, parms="", extra=""):
    return d.stream(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace {cs} /BitsPerComponent {bpc} "
                    f"/Filter /FlateDecode {parms} {extra}", zlib.compress(samples, 6))


def one_image_pdf(make):
    d = Doc()
    d.page([make(d)])
    return d.build()


def tiny_png(w, h):
    """A valid, tiny PNG that only DECLARES a large size (a decompression bomb for a decoder that believes the header)."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(100))) + chunk(b"IEND", b""))


# ------------------------------------------------------------------------------------------ conversions
@needs_pypdf
@needs_pillow
class PillowConversionTests(SimpleTestCase):
    def check_clean(self, c):
        """Whatever came out is a valid, metadata-free image that the strict validator accepts again."""
        info = inspect_image(c.data)
        self.assertEqual((info.content_type, info.width, info.height), (c.content_type, c.width, c.height))
        self.assertLessEqual(len(c.data), 2 * 1024 * 1024)
        self.assertLessEqual(max(c.width, c.height), 4096)
        if c.content_type == "image/jpeg":
            self.assertIn(0xC0, markers(c.data), "baseline JPEG")
            self.assertEqual(set(markers(c.data)) & {0xE1, 0xE2, 0xE3, 0xEE, 0xFE}, set(), "no EXIF, ICC, Adobe or comment segment")
        else:
            im = Image.open(io.BytesIO(c.data))
            self.assertEqual(set(im.info) - {"dpi", "gamma"}, set(), im.info)

    def run_pdf(self, pdf):
        res = datasheet_pdf.extract(pdf)
        self.assertEqual(len(res.candidates), 1, res.message + " " + str(res.notes))
        c = res.candidates[0]
        self.check_clean(c)
        return c, res

    def assertRed(self, c):
        r, g, b = rgb_means(c.data)
        self.assertGreater(r, 150, (r, g, b))
        self.assertLess(g, 90, (r, g, b))
        self.assertLess(b, 90, (r, g, b))

    def test_an_adobe_inverted_cmyk_jpeg_becomes_a_red_rgb_jpeg(self):
        data = adobe_cmyk_jpeg()
        self.assertEqual(Image.open(io.BytesIO(data)).mode, "CMYK")
        c, res = self.run_pdf(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=data, entries=INVERTED)]))
        self.assertEqual((c.content_type, c.width, c.height), ("image/jpeg", 400, 300))
        self.assertRed(c)
        self.assertTrue(any("Converted 1 print-colour (CMYK) picture" in n for n in res.notes), res.notes)
        self.assertEqual(Image.open(io.BytesIO(c.data)).mode, "RGB")

    def test_a_wide_cmyk_rack_unit_photo_is_kept_but_a_header_strip_is_not(self):
        # a straight-on photo of a 1U appliance is about 5:1 (a real one measured 2100 x 414)
        data = adobe_cmyk_jpeg(1050, 207)
        c, _ = self.run_pdf(pdf_of([dict(w=1050, h=207, kind="jpeg", ch=4, data=data, entries=INVERTED)]))
        self.assertEqual((c.width, c.height), (1050, 207))
        self.assertRed(c)
        strip = adobe_cmyk_jpeg(1500, 200)                          # 7.5:1: a header band
        res = datasheet_pdf.extract(pdf_of([dict(w=1500, h=200, kind="jpeg", ch=4, data=strip, entries=INVERTED)]))
        self.assertEqual(res.candidates, [])
        self.assertIn("logo or banner shaped", res.message)

    def test_a_picture_drawn_on_page_two_is_not_credited_to_page_one_by_shared_resources(self):
        # dompdf-style: one resource dictionary for every page. The big diagram is drawn on page 2 only.
        d = Doc()
        photo = d.image(300, 200, kind="flate", ch=3, content="device")
        diagram = d.image(960, 720, kind="flate", ch=3, content="device")
        res = f"/Resources << /XObject << /A {photo} 0 R /B {diagram} 0 R >> >>"
        for name, size in (("A", (300, 200)), ("B", (600, 450))):
            c = d.stream("", f"q {size[0]} 0 0 {size[1]} 50 300 cm /{name} Do Q\n".encode())
            d.kids.append(d.add(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {c} 0 R {res} >>"))
        out = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.page, c.width, c.height) for c in out.candidates], [(1, 300, 200), (2, 960, 720)])

    def test_a_soft_mask_listed_in_the_page_resources_is_not_offered_as_a_picture(self):
        d = Doc()
        mask = d.image(300, 200, kind="flate", ch=1, content="device")
        photo = d.image(300, 200, kind="flate", ch=3, content="device", smask=mask)
        d.page([photo, mask])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.width, c.height) for c in res.candidates], [(300, 200)], res.notes)

    def test_a_cmyk_jpeg_without_a_decode_array_stores_the_ink_itself(self):
        c, _ = self.run_pdf(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=plain_cmyk_jpeg())]))
        self.assertRed(c)

    def test_the_same_ink_gives_the_same_picture_whichever_way_the_jpeg_stores_it(self):
        a, _ = self.run_pdf(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(), entries=INVERTED)]))
        b, _ = self.run_pdf(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=plain_cmyk_jpeg())]))
        for x, y in zip(rgb_means(a.data), rgb_means(b.data)):
            self.assertLess(abs(x - y), 6)

    def test_a_ycck_jpeg_is_decoded_to_rgb(self):
        data = bytearray(adobe_cmyk_jpeg())
        i = data.index(b"Adobe")
        self.assertEqual(data[i + 11], 0)                          # transform 0 = CMYK
        data[i + 11] = 2                                           # 2 = YCCK
        c, _ = self.run_pdf(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=bytes(data), entries=INVERTED)]))
        self.assertEqual(Image.open(io.BytesIO(c.data)).mode, "RGB")

    def test_a_cmyk_flate_image_is_converted(self):
        w, h = 400, 300
        ink = bytes([0, 255, 255, 20]) * (w * h)
        pix = bytearray(ink)
        pix[3::4] = noise(w, h, 20).point(lambda v: v // 4).tobytes()
        pdf = one_image_pdf(lambda d: flate(d, w, h, "/DeviceCMYK", bytes(pix)))
        self.assertRed(self.run_pdf(pdf)[0])
        inverted = bytes(255 - v for v in pix)
        pdf = one_image_pdf(lambda d: flate(d, w, h, "/DeviceCMYK", inverted, extra=INVERTED))
        self.assertRed(self.run_pdf(pdf)[0])

    def test_a_cmyk_flate_image_with_png_filters_is_unfiltered_by_pillow(self):
        w, h = 300, 200
        pix = bytearray([0, 255, 255, 0]) * (w * h)
        pix[3::4] = noise(w, h, 20).point(lambda v: v // 4).tobytes()
        rows = [bytes(pix[y * w * 4:(y + 1) * w * 4]) for y in range(h)]
        filtered = b"".join(b"\x02" + bytes((a - b) & 255 for a, b in zip(rows[y], rows[y - 1])) if y else b"\x00" + rows[0]
                            for y in range(h))
        parms = f"/DecodeParms << /Predictor 15 /Colors 4 /Columns {w} /BitsPerComponent 8 >>"
        d = Doc()
        d.page([d.stream("/Type /XObject /Subtype /Image /Width 300 /Height 200 /ColorSpace /DeviceCMYK /BitsPerComponent 8 "
                         f"/Filter /FlateDecode {parms}", zlib.compress(filtered))])
        self.assertRed(self.run_pdf(d.build())[0])

    def test_an_indexed_image_keeps_its_palette_colours(self):
        w, h = 400, 300
        palette = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        idx = bytes((x // 7 + y // 5) % 4 for y in range(h) for x in range(w))
        lookup = "<" + "".join(f"{r:02x}{g:02x}{b:02x}" for r, g, b in palette) + ">"
        pdf = one_image_pdf(lambda d: flate(d, w, h, f"[/Indexed /DeviceRGB 3 {lookup}]", idx))
        c, _ = self.run_pdf(pdf)
        self.assertEqual(c.content_type, "image/png")              # four colours: a lossless PNG
        out = Image.open(io.BytesIO(c.data)).convert("RGB")
        self.assertEqual(out.getpixel((0, 0)), palette[0])
        self.assertEqual(out.getpixel((7, 0)), palette[1])
        self.assertEqual(out.getpixel((0, 5)), palette[1])
        self.assertEqual(set(out.getdata()), set(palette))

    def test_an_indexed_cmyk_palette_and_a_stream_lookup_work(self):
        w, h = 300, 200
        idx = bytes((x // 9 + y // 7) % 2 for y in range(h) for x in range(w))
        d = Doc()
        look = d.stream("", bytes([0, 255, 255, 0, 255, 255, 0, 0]))          # red, then blue
        d.page([flate(d, w, h, f"[/Indexed /DeviceCMYK 1 {look} 0 R]", idx)])
        c, _ = self.run_pdf(d.build())
        out = Image.open(io.BytesIO(c.data)).convert("RGB")
        self.assertEqual({out.getpixel((0, 0)), out.getpixel((9, 0))}, {(255, 0, 0), (0, 0, 255)})

    def test_a_16_bit_image_is_reduced_to_8_bits(self):
        w, h = 400, 260
        pix = sample_pixels(w, h, 3, "device")
        wide = bytes(x for v in pix for x in (v, 77))                         # big-endian: high byte is the 8-bit value
        c, _ = self.run_pdf(one_image_pdf(lambda d: flate(d, w, h, "/DeviceRGB", wide, bpc=16)))
        out = Image.open(io.BytesIO(c.data)).convert("RGB")
        ref = Image.frombytes("RGB", (w, h), pix)
        diff = sum(ImageChops.difference(out, ref).convert("L").getdata()) / (w * h)
        self.assertLess(diff, 6)

    def test_a_soft_mask_is_composited_onto_white(self):
        w, h = 400, 300
        mask = bytes(0 if x < w // 2 else 255 for y in range(h) for x in range(w))
        d = Doc()
        m = d.image(w, h, kind="flate", ch=1, data=zlib.compress(mask))
        d.page([d.image(w, h, kind="flate", ch=3, content="photo", smask=m)])
        c, res = self.run_pdf(d.build())
        out = Image.open(io.BytesIO(c.data)).convert("RGB")
        left = out.crop((0, 0, 150, 300)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        right = out.crop((250, 0, 400, 300)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))
        self.assertTrue(all(v > 245 for v in left), left)
        self.assertFalse(all(v > 245 for v in right), right)

    def test_a_soft_mask_of_another_size_is_resized(self):
        w, h = 400, 300
        d = Doc()
        m = d.image(100, 75, kind="flate", ch=1, data=zlib.compress(bytes(0 if x < 50 else 255 for y in range(75) for x in range(100))))
        d.page([d.image(w, h, kind="flate", ch=3, content="photo", smask=m)])
        out = Image.open(io.BytesIO(self.run_pdf(d.build())[0].data)).convert("RGB")
        self.assertTrue(all(v > 245 for v in out.crop((0, 0, 150, 300)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))))

    def test_a_jpeg_with_a_soft_mask_is_composited_too(self):
        w, h = 400, 300
        photo = jpeg_bytes(Image.merge("RGB", [noise(w, h), noise(w, h), noise(w, h)]), quality=85)
        d = Doc()
        m = d.image(w, h, kind="flate", ch=1, data=zlib.compress(bytes(0 if y < h // 2 else 255 for y in range(h) for x in range(w))))
        d.page([d.stream(f"/Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceRGB /BitsPerComponent 8 "
                         f"/Filter /DCTDecode /SMask {m} 0 R", photo)])
        out = Image.open(io.BytesIO(self.run_pdf(d.build())[0].data)).convert("RGB")
        self.assertTrue(all(v > 245 for v in out.crop((0, 0, 400, 100)).resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))))

    def test_an_unreadable_soft_mask_keeps_the_picture_and_says_so(self):
        d = Doc()
        m = d.stream("/Type /XObject /Subtype /Image /Width 400 /Height 300 /ColorSpace /DeviceGray /BitsPerComponent 8 /Matte [0]", b"")
        d.page([d.image(400, 300, kind="flate", ch=3, content="photo", smask=m)])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual(len(res.candidates), 1)
        self.assertTrue(any("transparency" in n for n in res.notes), res.notes)

    def test_a_flate_photo_wider_than_4096_px_is_downscaled_keeping_its_aspect(self):
        w, h = 6000, 1500
        row = bytes((x * 255 // w + (x // 3) % 40) % 256 for x in range(w))
        pdf = one_image_pdf(lambda d: flate(d, w, h, "/DeviceGray", row * h))
        self.assertLess(len(pdf), 25 * 1024 * 1024)
        c, res = self.run_pdf(pdf)
        self.assertEqual(c.width, 4096)
        self.assertAlmostEqual(c.width / c.height, 4.0, delta=0.02)
        self.assertTrue(any("Reduced 1 large picture" in n for n in res.notes), res.notes)

    def test_a_flate_photo_over_2_mb_is_re_encoded_instead_of_skipped(self):
        big = dict(w=900, h=800, kind="flate", ch=3, data=zlib.compress(os.urandom(900 * 800 * 3), 1))        # incompressible, ~2.1 MB
        pdf = pdf_of([big])
        c, res = self.run_pdf(pdf)
        self.assertEqual((c.content_type, c.width, c.height), ("image/jpeg", 900, 800))
        self.assertNotIn("cannot be shrunk", " ".join(res.notes))

    def test_an_incompressible_photo_is_downscaled_until_it_fits_2_mb(self):
        w, h = 2400, 1800
        big = dict(w=w, h=h, kind="flate", ch=3, data=zlib.compress(os.urandom(w * h * 3), 0))               # ~13 MB of noise
        c, res = self.run_pdf(pdf_of([big]))
        self.assertLess(c.width, w)
        self.assertAlmostEqual(c.width / c.height, w / h, delta=0.01)
        self.assertTrue(any("Reduced 1 large picture" in n for n in res.notes), res.notes)

    def test_a_jpeg_over_2_mb_and_4096_px_is_decoded_with_draft_and_shrunk(self):
        data = jpeg_bytes(noise(5000, 1500, 60), quality=80)
        self.assertGreater(len(data), 2 * 1024 * 1024)
        from PIL import JpegImagePlugin
        calls = []
        real = JpegImagePlugin.JpegImageFile.draft

        def spy(self, mode, size):
            calls.append(size)
            return real(self, mode, size)
        with mock.patch.object(JpegImagePlugin.JpegImageFile, "draft", spy):
            c, _ = self.run_pdf(pdf_of([dict(w=5000, h=1500, kind="jpeg", ch=1, data=data)]))
        self.assertEqual(calls, [(2500, 750)], "decoded at half size")
        self.assertAlmostEqual(c.width / c.height, 5000 / 1500, delta=0.02)
        self.assertLessEqual(len(c.data), 2 * 1024 * 1024)

    def test_a_clean_in_limit_rgb_jpeg_still_passes_through_untouched(self):
        data = jpeg_bytes(Image.merge("RGB", [noise(400, 300), noise(400, 300), noise(400, 300)]), quality=85)
        with mock.patch.object(imaging_pillow, "convert_jpeg", side_effect=AssertionError("Pillow was not needed")):
            res = datasheet_pdf.extract(pdf_of([dict(w=400, h=300, kind="jpeg", ch=3, data=data)]))
        self.assertEqual(res.candidates[0].data, data)

    def test_metadata_is_stripped_from_the_output(self):
        w, h = 400, 300
        src = red_cmyk(w, h)
        data = jpeg_bytes(src, quality=90, icc_profile=b"hostile-profile" * 20, comment=b"<script>alert(1)</script>")
        self.assertIn(b"<script>", data)
        c, _ = self.run_pdf(pdf_of([dict(w=w, h=h, kind="jpeg", ch=4, data=data, entries=INVERTED)]))
        self.assertNotIn(b"<script>", c.data)
        self.assertNotIn(b"hostile-profile", c.data)
        self.assertNotIn(b"Exif", c.data)

    def test_few_colours_give_a_png_and_many_give_a_jpeg(self):
        w, h = 400, 300
        pix = b"".join(bytes([0, 255, 255, 0]) if (x // 50 + y // 50) % 2 else bytes([255, 0, 255, 0]) for y in range(h) for x in range(w))
        c, _ = self.run_pdf(one_image_pdf(lambda d: flate(d, w, h, "/DeviceCMYK", pix)))
        self.assertEqual(c.content_type, "image/png")
        c, _ = self.run_pdf(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(), entries=INVERTED)]))
        self.assertEqual(c.content_type, "image/jpeg")

    def test_a_flat_converted_picture_is_still_skipped(self):
        pix = bytes([0, 255, 255, 0]) * (400 * 300)
        res = datasheet_pdf.extract(one_image_pdf(lambda d: flate(d, 400, 300, "/DeviceCMYK", pix)))
        self.assertEqual(res.candidates, [])
        self.assertIn("blank or a flat colour", res.message)

    def test_a_cmyk_photo_next_to_a_normal_one_both_survive_and_no_install_hint_is_shown(self):
        res = datasheet_pdf.extract(pdf_of([PRODUCT, dict(w=400, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(), entries=INVERTED)]))
        self.assertEqual(len(res.candidates), 2)
        self.assertNotIn("requirements-images.txt", " ".join(res.notes))

    def test_only_a_few_conversions_are_done_per_pdf(self):
        pdfs = [dict(w=400 + i, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(400 + i), entries=INVERTED) for i in range(3)]
        with mock.patch.object(datasheet_pdf, "MAX_CONVERSIONS", 2):
            res = datasheet_pdf.extract(pdf_of(pdfs))
        self.assertEqual(len(res.candidates), 2)
        self.assertIn("not converted (limit reached)", " ".join(res.notes))


# ------------------------------------------------------------------------------------------ hostile input
@needs_pypdf
@needs_pillow
class PillowHostileInputTests(SimpleTestCase):
    def no_open(self):
        return mock.patch.object(Image, "open", side_effect=AssertionError("Image.open must not be reached"))

    def test_a_tiny_png_declaring_30000_by_30000_never_reaches_pillow(self):
        bomb = tiny_png(30000, 30000)
        self.assertLess(len(bomb), 200)
        with self.no_open(), self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.raw_from_png(bomb, 30000, 30000, 3)
        self.assertEqual(str(cm.exception), "damaged")

    def test_a_jpeg_declaring_more_than_the_pixel_budget_never_reaches_pillow(self):
        for w, h in ((12000, 12000), (16384, 16384), (20000, 100)):
            with self.no_open(), self.assertRaises(imaging_pillow.ConversionError) as cm:
                imaging_pillow.convert_jpeg(sample_jpeg(w, h, 3))
            self.assertIn(str(cm.exception), ("too large to read safely", "not a clean JPEG"), (w, h))

    def test_a_pdf_dictionary_declaring_a_bomb_is_skipped_before_any_work(self):
        d = Doc()
        d.page([d.image(30000, 30000, kind="flate", data=zlib.compress(bytes(100))),
                d.image(12000, 12000, kind="flate", data=zlib.compress(bytes(100))),
                d.image(12000, 12000, kind="jpeg", ch=4, data=sample_jpeg(12000, 12000, 4)),
                d.image(**PRODUCT)])
        with self.no_open():
            res = datasheet_pdf.extract(d.build())
        self.assertEqual(len(res.candidates), 1)
        self.assertIn("too large to read safely", " ".join(res.notes))

    def test_pillows_own_bomb_check_is_a_hard_number_and_a_warning_is_an_error(self):
        real = Image.new("RGB", (60, 60), "red")
        buf = io.BytesIO()
        Image.merge("RGB", [noise(60, 60), noise(60, 60), noise(60, 60)]).save(buf, "PNG")
        png = buf.getvalue()
        from PIL import ImageFile
        for limit in (1000, 2000):                  # 3600 px is above twice the first limit (error) and above the second (warning)
            with mock.patch.object(imaging_pillow, "MAX_INPUT_PIXELS", limit), \
                    mock.patch.object(imaging_pillow, "_check_budget", lambda w, h: None), \
                    self.assertRaises(imaging_pillow.ConversionError) as cm:
                imaging_pillow.raw_from_png(png, 60, 60, 3)
            self.assertEqual(str(cm.exception), "too large to read safely", limit)
        self.assertIsNotNone(Image.MAX_IMAGE_PIXELS)
        self.assertFalse(ImageFile.LOAD_TRUNCATED_IMAGES)
        del real

    def test_only_jpeg_and_png_are_ever_opened_and_only_the_expected_one(self):
        seen = []
        real = Image.open

        def spy(fp, mode="r", formats=None):
            seen.append(formats)
            return real(fp, mode, formats)
        with mock.patch.object(Image, "open", spy):
            imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True)
            buf = io.BytesIO()
            Image.new("L", (40, 30), 7).save(buf, "PNG")
            imaging_pillow.raw_from_png(buf.getvalue(), 40, 30, 1)
        self.assertEqual(seen, [["JPEG"], ["PNG"]])

    def test_bytes_whose_magic_is_not_jpeg_or_png_never_reach_pillow(self):
        buf = io.BytesIO()
        Image.new("RGB", (300, 200), "red").save(buf, "GIF")
        webp = io.BytesIO()
        Image.new("RGB", (300, 200), "red").save(webp, "WEBP")
        for data in (buf.getvalue(), webp.getvalue(), b"%PDF-1.4 hello", b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"", b"\xff\xd8"):
            with self.no_open(), self.assertRaises(imaging_pillow.ConversionError):
                imaging_pillow.convert_jpeg(data)
        with self.no_open(), self.assertRaises(imaging_pillow.ConversionError):
            imaging_pillow.raw_from_png(buf.getvalue(), 300, 200, 3)

    def test_lossless_and_arithmetic_jpegs_are_not_decoded(self):
        data = bytearray(adobe_cmyk_jpeg())
        i = data.index(b"\xff\xc0")
        for sof in (0xC3, 0xC9):                                             # lossless, arithmetic-coded
            data[i + 1] = sof
            with self.no_open(), self.assertRaises(imaging_pillow.ConversionError) as cm:
                imaging_pillow.convert_jpeg(bytes(data))
            self.assertEqual(str(cm.exception), "unsupported encoding")

    def test_a_truncated_jpeg_is_a_skipped_picture_not_an_error(self):
        data = adobe_cmyk_jpeg()
        d = Doc()
        d.page([d.image(400, 300, kind="jpeg", ch=4, data=data[:len(data) // 2], entries=INVERTED)])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual(res.candidates, [])
        self.assertIn("damaged", res.message)

    def test_a_jpeg_with_a_damaged_scan_but_a_valid_structure_never_raises(self):
        data = adobe_cmyk_jpeg()
        sos = data.index(b"\xff\xda")
        body = data[sos + 14:-2].replace(b"\xff", b"\xfe")                    # keep the structure walkable
        cut = data[:sos + 14] + body[: len(body) // 3] + b"\xff\xd9"
        d = Doc()
        d.page([d.image(400, 300, kind="jpeg", ch=4, data=cut, entries=INVERTED)])
        res = datasheet_pdf.extract(d.build())      # libjpeg pads a short scan up to its end marker: a partial picture, or a skip
        for c in res.candidates:
            inspect_image(c.data)
        self.assertTrue(res.candidates or "damaged" in res.message)

    def test_a_jpeg_with_data_appended_is_refused_not_stripped(self):
        data = adobe_cmyk_jpeg() + b"<script>alert(1)</script>"
        with self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.convert_jpeg(data)
        self.assertEqual(str(cm.exception), "not a clean JPEG")

    def test_a_declared_size_that_disagrees_with_the_jpeg_header_is_refused(self):
        d = Doc()
        d.page([d.image(500, 400, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(400, 300), entries=INVERTED)])
        res = datasheet_pdf.extract(d.build())
        self.assertEqual(res.candidates, [])
        self.assertIn("damaged", res.message)

    def test_a_pillow_failure_of_any_kind_becomes_a_skipped_picture(self):
        for exc in (OSError("x"), ValueError("x"), SyntaxError("x"), MemoryError(), RecursionError(), struct.error("x"), EOFError()):
            with mock.patch.object(Image, "open", side_effect=exc):
                res = datasheet_pdf.extract(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(), entries=INVERTED)]))
            self.assertEqual(res.candidates, [], repr(exc))

    def test_the_output_is_validated_again_before_it_is_returned(self):
        real = imaging_pillow._encode

        def with_tail(Image_, im):
            ctype, data = real(Image_, im)
            return ctype, data + b"<script>"
        with mock.patch.object(imaging_pillow, "_encode", with_tail), self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True)
        self.assertEqual(str(cm.exception), "damaged")
        data = adobe_cmyk_jpeg()
        # the first call validates the input, the second is the check of Pillow's output: make that one fail
        first = inspect_image(data, max_bytes=10 ** 7, max_dimension=16384)
        with mock.patch.object(imaging_pillow, "inspect_image", side_effect=[first, ImageError("no")]), \
                self.assertRaises(imaging_pillow.ConversionError):
            imaging_pillow.convert_jpeg(data, decode_inverted=True)

    def test_the_wall_clock_deadline_is_honoured_between_stages(self):
        with self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True, stop=time.monotonic() - 1)
        self.assertIn(str(cm.exception), ("took too long", "busy"))

    def test_nothing_is_written_to_disk(self):
        with mock.patch("tempfile.NamedTemporaryFile", side_effect=AssertionError("written to disk")), \
                mock.patch("tempfile.mkstemp", side_effect=AssertionError("written to disk")), \
                mock.patch("tempfile.TemporaryFile", side_effect=AssertionError("written to disk")):
            c = imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True)
            self.assertEqual(c.content_type, "image/jpeg")
            res = datasheet_pdf.extract(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(), entries=INVERTED)]))
        self.assertEqual(len(res.candidates), 1)

    def test_at_most_two_conversions_run_at_once(self):
        running, peak, lock = [0], [0], threading.Lock()
        real = imaging_pillow._finish

        def slow(*a, **k):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.15)
            try:
                return real(*a, **k)
            finally:
                with lock:
                    running[0] -= 1
        data = adobe_cmyk_jpeg()
        errors = []

        def work():
            try:
                imaging_pillow.convert_jpeg(data, decode_inverted=True)
            except Exception as e:  # noqa: BLE001
                errors.append(e)
        with mock.patch.object(imaging_pillow, "_finish", slow):
            threads = [threading.Thread(target=work) for _ in range(6)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(peak[0], 2)

    def test_a_busy_converter_makes_the_picture_wait_then_skip(self):
        held = [imaging_pillow._SLOTS.acquire() for _ in range(imaging_pillow.SLOTS)]
        try:
            with mock.patch.object(imaging_pillow, "SLOT_WAIT_SECONDS", 0.05), self.assertRaises(imaging_pillow.ConversionError) as cm:
                imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True)
            self.assertEqual(str(cm.exception), "busy")
        finally:
            for _ in held:
                imaging_pillow._SLOTS.release()
        self.assertEqual(imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True).content_type, "image/jpeg")


# ------------------------------------------------------------------------------------------ without Pillow
def progressive_jpeg(w=400, h=300):
    return jpeg_bytes(red_cmyk(w, h), quality=90, progressive=True)


def with_declared_size(data, w, h):
    """The same JPEG with a different size in its frame header (a lying header)."""
    b = bytearray(data)
    i = max(b.find(b"\xff\xc0"), b.find(b"\xff\xc2"), 0)
    b[i + 5:i + 9] = struct.pack(">HH", h, w)
    return bytes(b)


@needs_pypdf
@needs_pillow
class PillowResourceLimitTests(SimpleTestCase):
    """Time and memory: a stuck native decode, absurd scan counts, libjpeg's coefficient buffer, reset warning filters."""

    def no_open(self):
        return mock.patch.object(Image, "open", side_effect=AssertionError("Image.open must not be reached"))

    def wait_for_slots(self):
        for _ in range(200):
            got = [imaging_pillow._SLOTS.acquire(blocking=False) for _ in range(imaging_pillow.SLOTS)]
            for g in got:
                if g:
                    imaging_pillow._SLOTS.release()
            if all(got):
                return
            time.sleep(0.05)
        self.fail("a conversion slot was never released")

    def test_a_stuck_native_decode_is_abandoned_at_the_deadline_and_keeps_its_slot(self):
        release = threading.Event()
        real = imaging_pillow._finish

        def stuck(*a, **k):
            release.wait(10)
            return real(*a, **k)
        data = adobe_cmyk_jpeg()
        try:
            with mock.patch.object(imaging_pillow, "_finish", stuck):
                started = time.monotonic()
                with self.assertRaises(imaging_pillow.ConversionError) as cm:
                    imaging_pillow.convert_jpeg(data, decode_inverted=True, stop=time.monotonic() + 0.3)
                self.assertEqual(str(cm.exception), "took too long")
                self.assertLess(time.monotonic() - started, 3)       # the caller is not held by the stuck worker
                # a second stuck one fills the other slot; a third is then refused instead of piling up native work
                with self.assertRaises(imaging_pillow.ConversionError):
                    imaging_pillow.convert_jpeg(data, decode_inverted=True, stop=time.monotonic() + 0.3)
                with mock.patch.object(imaging_pillow, "SLOT_WAIT_SECONDS", 0.05), \
                        self.assertRaises(imaging_pillow.ConversionError) as cm:
                    imaging_pillow.convert_jpeg(data, decode_inverted=True)
                self.assertEqual(str(cm.exception), "busy")
        finally:
            release.set()
        self.wait_for_slots()                                       # the abandoned workers finish and free their slots
        self.assertEqual(imaging_pillow.convert_jpeg(data, decode_inverted=True).content_type, "image/jpeg")

    def test_a_stuck_decode_inside_a_pdf_is_a_skipped_picture_and_the_request_finishes(self):
        release = threading.Event()
        real = imaging_pillow._finish
        with mock.patch.object(imaging_pillow, "_finish", lambda *a, **k: (release.wait(10), real(*a, **k))[1]):
            try:
                started = time.monotonic()
                res = datasheet_pdf.extract(pdf_of([dict(w=400, h=300, kind="jpeg", ch=4, data=adobe_cmyk_jpeg(), entries=INVERTED)]),
                                            deadline_seconds=0.4)
            finally:
                release.set()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(res.candidates, [])
        self.wait_for_slots()

    def test_the_worker_does_not_outlive_a_cancelled_call_past_its_next_stage(self):
        seen = []
        real = imaging_pillow._Guard.tick

        def spy(self):
            seen.append(self.cancelled.is_set())
            return real(self)
        with mock.patch.object(imaging_pillow._Guard, "tick", spy):
            imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True)
        self.assertTrue(seen and not any(seen))
        guard = imaging_pillow._Guard(None, threading.Event())
        guard.cancelled.set()
        with self.assertRaises(imaging_pillow.ConversionError):
            guard.tick()

    def test_a_jpeg_with_absurd_scan_counts_is_refused_before_decoding(self):
        data = progressive_jpeg()
        sos = data.index(b"\xff\xda")
        length = struct.unpack(">H", data[sos + 2:sos + 4])[0]
        scan = data[sos:sos + 2 + length] + b"\x00"
        bomb = data[:sos] + scan * 4000 + b"\xff\xd9"
        self.assertEqual(inspect_image(bomb, max_bytes=10 ** 7, max_dimension=16384).width, 400)      # structurally fine
        self.assertEqual(imaging_pillow.jpeg_scans(bomb), 4000)
        with self.no_open(), self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.convert_jpeg(bomb)
        self.assertEqual(str(cm.exception), "unsupported encoding")
        self.assertEqual(imaging_pillow.jpeg_scans(adobe_cmyk_jpeg()), 1)
        self.assertGreater(imaging_pillow.jpeg_scans(data), 1)
        self.assertLessEqual(imaging_pillow.jpeg_scans(data), imaging_pillow.MAX_SCANS)

    def test_a_progressive_jpeg_is_held_to_the_coefficient_budget_not_just_the_pixel_budget(self):
        # 8000 x 5000 is inside the 40 MP pixel budget, but libjpeg would keep 240 MB of DCT coefficients for a progressive RGB JPEG
        lying = with_declared_size(progressive_jpeg(), 8000, 5000)
        with self.no_open(), self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.convert_jpeg(lying)
        self.assertEqual(str(cm.exception), "too large to read safely")
        # the same size as a single-scan baseline JPEG passes this check (draft mode keeps its decode small)
        baseline = with_declared_size(adobe_cmyk_jpeg(), 8000, 5000)
        self.assertEqual(imaging_pillow.jpeg_scans(baseline), 1)
        with mock.patch.object(Image, "open", side_effect=OSError("reached")), self.assertRaises(imaging_pillow.ConversionError) as cm:
            imaging_pillow.convert_jpeg(baseline)
        self.assertEqual(str(cm.exception), "damaged")                 # got as far as Pillow
        # a normal progressive photo is still converted
        c = imaging_pillow.convert_jpeg(progressive_jpeg(), decode_inverted=True)
        self.assertEqual((c.content_type, c.cmyk), ("image/jpeg", True))

    def test_the_worst_accepted_baseline_decode_stays_inside_the_work_budget(self):
        for w, h, comps in ((8000, 5000, 4), (16384, 2400, 4), (8000, 5000, 3), (16384, 2400, 1)):
            s = imaging_pillow._draft_scale(w, h, comps)
            self.assertLessEqual(-(-w // s) * -(-h // s) * comps, imaging_pillow.MAX_WORK_BYTES, (w, h, comps))
            self.assertLessEqual(max(-(-w // s), -(-h // s)), 4096)

    def test_reset_warning_filters_are_restored_so_a_bomb_warning_is_still_an_error(self):
        import warnings
        with warnings.catch_warnings():
            warnings.resetwarnings()
            warnings.simplefilter("ignore")
            imaging_pillow._setup(Image)
            with self.assertRaises(Image.DecompressionBombWarning):
                warnings.warn("bomb", Image.DecompressionBombWarning)
            self.assertIsNotNone(Image.MAX_IMAGE_PIXELS)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            Image.MAX_IMAGE_PIXELS = None                   # something else removed the limit
            imaging_pillow.convert_jpeg(adobe_cmyk_jpeg(), decode_inverted=True)
            self.assertEqual(Image.MAX_IMAGE_PIXELS, imaging_pillow.MAX_INPUT_PIXELS)

    def test_malformed_and_huge_icc_profiles_are_never_parsed_or_carried_over(self):
        im = red_cmyk(300, 200)
        for profile in (b"\x00" * 100, b"acsp" * 50000, os.urandom(200_000)):
            data = jpeg_bytes(im, quality=90, icc_profile=profile)
            self.assertIn(b"ICC_PROFILE", data)                       # the hostile input really carries the profile
            c = imaging_pillow.convert_jpeg(data, decode_inverted=True)
            self.assertNotIn(b"ICC_PROFILE", c.data)
            self.assertNotIn(b"iCCP", c.data)
            with Image.open(io.BytesIO(c.data)) as out:
                self.assertNotIn("icc_profile", out.info)


@needs_pypdf
class WithoutPillowTests(SimpleTestCase):
    """A deployment that never installed the extra: nothing crashes and the message says exactly what to do."""

    def cmyk_pdf(self, extra=()):
        return pdf_of([dict(w=1800, h=490, kind="jpeg", ch=4), dict(w=800, h=600, kind="jpeg", ch=4), *extra])

    def test_the_lazy_import_failing_means_not_available(self):
        with hide_pillow():
            self.assertIsNone(imaging_pillow._load())
            self.assertFalse(imaging_pillow.available())
            self.assertEqual(imaging_pillow.status(), ("missing", ""))
            with self.assertRaises(imaging_pillow.ConversionError) as cm:
                imaging_pillow.convert_jpeg(sample_jpeg(400, 300, 4))
            self.assertEqual(str(cm.exception), "unavailable")

    def test_a_listed_soft_mask_and_shared_resources_are_handled_without_pillow_too(self):
        d = Doc()
        mask = d.image(300, 200, kind="flate", ch=1, content="device")
        photo = d.image(300, 200, kind="flate", ch=3, content="device", smask=mask)
        d.page([photo, mask])
        with hide_pillow():
            res = datasheet_pdf.extract(d.build())
        self.assertEqual([(c.width, c.height) for c in res.candidates], [(300, 200)], res.notes)

    def test_a_cmyk_only_datasheet_gets_the_specific_actionable_message(self):
        with hide_pillow():
            res = datasheet_pdf.extract(self.cmyk_pdf())
        self.assertEqual(res.candidates, [])
        self.assertIn("2 CMYK (print colour)", res.message)
        self.assertIn(RED_HINT + " and LinkedEye will convert them, or save the photo from the PDF and upload it.", res.message)

    def test_a_cmyk_photo_next_to_a_usable_one_adds_the_hint_as_a_note(self):
        with hide_pillow():
            res = datasheet_pdf.extract(self.cmyk_pdf([PRODUCT]))
        self.assertEqual(len(res.candidates), 1)
        self.assertTrue(any(RED_HINT in n for n in res.notes), res.notes)

    def test_oversize_pictures_say_the_library_would_shrink_them(self):
        big = dict(w=900, h=800, kind="flate", ch=3, data=zlib.compress(os.urandom(900 * 800 * 3), 1))
        with hide_pillow():
            res = datasheet_pdf.extract(pdf_of([big, dict(w=700, h=500, kind="jpeg")]))
        self.assertEqual([(c.width, c.height) for c in res.candidates], [(700, 500)])
        self.assertTrue(any("2 MB" in n and "cannot be shrunk" in n and "requirements-images.txt" in n for n in res.notes), res.notes)

    def test_everything_else_is_unchanged_without_pillow(self):
        with hide_pillow():
            res = datasheet_pdf.extract(pdf_of([PRODUCT, dict(w=640, h=420, kind="jpeg")]))
        self.assertEqual(len(res.candidates), 2)

    def test_an_installed_but_too_old_pillow_is_treated_as_missing_and_named(self):
        fake = mock.MagicMock(__version__="12.3.0")
        with mock.patch.dict(sys.modules, {"PIL": fake}):
            self.assertEqual(imaging_pillow.status(), ("old", "12.3.0"))
            self.assertFalse(imaging_pillow.available())
            res = datasheet_pdf.extract(self.cmyk_pdf())
        self.assertIn("Pillow 12.3.0", res.message)
        self.assertIn("older than the 13.0", res.message)
        self.assertIn("requirements-images.txt", res.message)

    def test_importing_the_converter_does_not_import_pillow(self):
        tree = ast.parse(Path(imaging_pillow.__file__).read_text())
        for node in tree.body:                                          # module level only: the imports inside functions are the lazy ones
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            self.assertFalse([n for n in names if n.split(".")[0] == "PIL"], names)

    def test_requirements_images_pins_a_safe_range(self):
        text = (Path(imaging_pillow.__file__).resolve().parent.parent / "requirements-images.txt").read_text()
        lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
        self.assertEqual(lines, ["Pillow>=13.0,<14"])
        self.assertEqual(imaging_pillow.MIN_VERSION, (13, 0))


class PickerPageTests(TestCase):
    def page(self, ready):
        viewer, operator, admin = users()
        Site.objects.create(tenant_key=T, name="Mumbai", code="mum")
        d = dev("mum-sw-01", site="Mumbai", vendor="Cisco", model="Catalyst 9500-48Y4C", mgmt_ip="192.0.2.11")
        self.client.force_login(operator)
        with mock.patch("network.photo_views.imaging_pillow.available", lambda: ready):
            r = self.client.get(reverse("network:photo_find"), {"device": d.pk})
        self.assertEqual(r.status_code, 200)
        return r.content.decode()

    def test_the_page_tells_the_operator_what_the_library_would_do(self):
        html = self.page(False)
        self.assertIn("pip install -r requirements-images.txt", html)
        self.assertIn("save the photo from the PDF and upload it", html)

    def test_with_the_library_the_page_says_print_colour_is_converted(self):
        html = self.page(True)
        self.assertIn("are converted to ordinary screen-colour JPEG or PNG photos", html)
        self.assertNotIn("requirements-images.txt", html)
