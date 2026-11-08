"""Product photos out of a vendor datasheet PDF, with an optional imaging library for print-colour pictures.

The operator downloads the datasheet from the vendor in their own browser and gives the file to LinkedEye; this module
finds the product pictures in its first pages and returns them as ranked candidates (PNG or JPEG bytes that
network.imaging accepts). The PDF is untrusted input:

* the file is capped (25 MB), only the first 6 pages are looked at, at most 40 image objects are read and form XObjects
  are followed two levels deep, never further;
* every image's declared size is checked before anything is allocated (4096 px per side, 20 MB decoded) and a Flate
  stream is inflated with a hard output limit, so a small file cannot expand into a large one;
* the whole run has a wall-clock budget, checked between objects;
* any exception from the PDF parser means "no candidates" plus a message, never a failed request;
* nothing is written to disk.

Without Pillow (the default) the rules below apply. With the optional Pillow extra (requirements-images.txt, detected at
runtime, see network.imaging_pillow) the pictures that would otherwise be skipped are converted instead: CMYK and YCCK JPEGs,
CMYK / indexed / 16-bit Flate images become sRGB, a soft mask is composited onto white, and a picture over 2 MB or 4096 px is
downscaled; the result is a clean re-encoded baseline JPEG or PNG. The PDF's own declared sizes are checked first and the
converter has its own, larger, budget (40 megapixels).

What is extracted: JPEG streams (DCTDecode) pass through as the original JPEG bytes. FlateDecode images in
DeviceGray / DeviceRGB (also ICC-based and Cal colour spaces with 1 or 3 components) at 8 bits become a PNG that this
module encodes itself with zlib; a soft mask the same size as the picture is merged as an alpha channel when it can be
read without un-filtering. Everything else (JPEG 2000, CCITT, JBIG2, CMYK, indexed, 1-bit images and stencil masks) is
skipped and counted (CMYK, indexed, 16-bit and oversized ones are converted when Pillow is installed). Pictures smaller than 200x120 px, banner/logo shaped ones (wider than 7:1 either way) and
near-uniform ones are skipped. Without Pillow a picture whose bytes would exceed the 2 MB photo limit cannot be shrunk, so it
is skipped and the message says so.

pypdf is a normal dependency (requirements.txt) but is imported lazily: without it extract() returns a message that says how
to install it, and the rest of LinkedEye is unaffected."""
from __future__ import annotations

import hashlib
import io
import math
import re
import struct
import time
import zlib
from collections import Counter
from dataclasses import dataclass, field

from . import imaging_pillow
from .imaging import MAX_BYTES as MAX_IMAGE_BYTES
from .imaging import MAX_DIMENSION, ImageError, inspect_image
from .imaging_pillow import ConversionError

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 6
MAX_IMAGE_OBJECTS = 40
MAX_FORM_DEPTH = 2
MAX_WALK_ENTRIES = 2000           # XObject entries examined per pass over a document (all pages, all nested Forms)
MAX_DECODED_BYTES = 20 * 1024 * 1024
MIN_WIDTH, MIN_HEIGHT = 200, 120
MAX_ASPECT = 7.0                        # a straight-on 1U appliance photo is 5:1 or wider (one real 1U firewall photo measured 5.07:1); header strips are wider
DEADLINE_SECONDS = 12.0
MAX_CANDIDATES = 12
IDEAL_ASPECT = 1.6                      # a product shot of a rack unit or an appliance is wider than tall

MAX_CONVERSIONS = 10                    # pictures per PDF handed to Pillow (each can take a second and tens of MB)
MAX_MASK_PIXELS = 16_000_000

PRINT_COLOUR_HINT = ("These photos are in print (CMYK) colour. Install the optional image library "
                     "(pip install -r requirements-images.txt) and LinkedEye will convert them, or save the photo from the PDF and upload it.")
PRINT_COLOUR_HINT_OLD = ("These photos are in print (CMYK) colour. The installed image library (Pillow {version}) is older than the "
                         "{minimum} that LinkedEye needs: upgrade it with pip install -U -r requirements-images.txt and LinkedEye will "
                         "convert them, or save the photo from the PDF and upload it.")
# reasons a plain skip that Pillow can turn into a candidate
_FIXABLE = {"CMYK JPEG", "CMYK", "indexed colours", "1-bit or high-depth", "larger than 2 MB", "too large to read safely",
            "needs conversion"}

INSTALL_HINT = ("Reading PDF datasheets needs the pypdf package, which is not installed here. Install it with: "
                "pip install \"pypdf>=6.4,<8\" (it is listed in requirements.txt) and restart LinkedEye.")


@dataclass
class PdfCandidate:
    data: bytes
    content_type: str
    width: int
    height: int
    page: int
    origin: str                         # "datasheet page 1"
    sha256: str = ""
    demoted: bool = False               # placed as page decoration (bleeds off the page) or logo / badge sized

    @property
    def byte_size(self):
        return len(self.data)


@dataclass
class PdfResult:
    candidates: list = field(default_factory=list)
    message: str = ""                   # why there are no candidates (empty when there are)
    notes: list = field(default_factory=list)   # things worth telling the operator even when there are candidates
    unavailable: bool = False           # pypdf is not installed

    @property
    def ok(self):
        return bool(self.candidates)


class _Skip(Exception):
    """An image that is not a usable candidate; str(e) is a short reason key shown in the summary."""


# ------------------------------------------------------------------------------------------ PNG encoder
def _chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF)


_COLOR_TYPES = {(1, False): 0, (3, False): 2, (1, True): 4, (3, True): 6}


def png_from_idat(width, height, channels, idat, has_alpha=False):
    """A PNG file around an existing zlib stream of filtered scanlines (8 bits per sample)."""
    ctype = _COLOR_TYPES[(channels, has_alpha)]
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, ctype, 0, 0, 0))
            + _chunk(b"IDAT", idat) + _chunk(b"IEND", b""))


def encode_png(width, height, channels, pixels, level=6):
    """PNG bytes for `pixels`: width*height*channels bytes, rows top to bottom, samples interleaved (gray = 1,
    gray+alpha = 2, RGB = 3, RGBA = 4 channels), 8 bits per sample. Pure zlib, no filtering beyond type 0."""
    if channels not in (1, 2, 3, 4):
        raise ValueError("channels must be 1 to 4")
    row = width * channels
    if width < 1 or height < 1 or len(pixels) != row * height:
        raise ValueError("pixel data does not match the size")
    scan = b"".join(b"\x00" + pixels[y * row:(y + 1) * row] for y in range(height))
    return png_from_idat(width, height, 3 if channels in (3, 4) else 1, zlib.compress(scan, level), has_alpha=channels in (2, 4))


# ------------------------------------------------------------------------------------------ helpers on pypdf objects
def _name(value):
    return str(value) if value is not None else ""


def _num(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _resolve(obj):
    try:
        return obj.get_object() if hasattr(obj, "get_object") else obj
    except Exception:  # noqa: BLE001 - a broken reference is just a missing value
        return None


def _filters(xobj):
    f = _resolve(xobj.get("/Filter"))
    if f is None:
        return []
    if isinstance(f, (list, tuple)) or (hasattr(f, "__iter__") and not isinstance(f, (str, bytes))):
        return [_name(_resolve(x)) for x in f]
    return [_name(f)]


def _parms(xobj):
    p = _resolve(xobj.get("/DecodeParms"))
    if isinstance(p, (list, tuple)):
        p = _resolve(p[0]) if len(p) == 1 else None
    return p if hasattr(p, "get") else {}


def _space(cs):
    """Components of a colour space object: 1 or 3 for Gray / RGB (also Cal and ICC-based), "cmyk", "indexed", or a reason."""
    cs = _resolve(cs)
    if cs is None:
        return None
    if isinstance(cs, (list, tuple)):
        head = _name(_resolve(cs[0])) if cs else ""
        if head == "/ICCBased" and len(cs) > 1:
            prof = _resolve(cs[1])
            n = _num(prof.get("/N") if hasattr(prof, "get") else 0)
            return {1: 1, 3: 3}.get(n, "cmyk" if n == 4 else "colour space")
        if head in ("/CalRGB",):
            return 3
        if head in ("/CalGray",):
            return 1
        if head == "/Indexed":
            return "indexed"
        return "colour space"
    name = _name(cs)
    return {"/DeviceGray": 1, "/DeviceRGB": 3, "/CalGray": 1, "/CalRGB": 3, "/DeviceCMYK": "cmyk",
            "/Indexed": "indexed"}.get(name, "colour space")


def _components(xobj):
    """(components, is_cmyk_or_unsupported_reason). Colour spaces: Device/Cal Gray and RGB and ICC-based with 1 or 3."""
    return _space(xobj.get("/ColorSpace"))


def _default_decode(xobj, comps):
    d = _resolve(xobj.get("/Decode"))
    if d is None:
        return True
    try:
        vals = [float(_resolve(x)) for x in d]
    except (TypeError, ValueError):
        return False
    return vals == [0.0, 1.0] * comps


def _raw_stream(xobj):
    """The still-encoded bytes of a stream object (what is in the file, before any filter)."""
    data = getattr(xobj, "_data", None)
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    raise _Skip("unreadable stream")


def _jpeg_components(data):
    """Number of colour components from the JPEG frame header, or 0."""
    n, pos = len(data), 2
    for _ in range(400):
        if pos + 4 > n or data[pos] != 0xFF:
            return 0
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            return data[pos + 9] if pos + 10 <= n else 0
        if marker == 0xDA or marker == 0xD9:
            return 0
        pos += 2 + struct.unpack(">H", data[pos + 2:pos + 4])[0]
    return 0


# ------------------------------------------------------------------------------------------ decoding one image
def _check_size(width, height, comps=3):
    if width < 1 or height < 1:
        raise _Skip("empty")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise _Skip("too large to read safely")
    if width * height * comps > MAX_DECODED_BYTES:
        raise _Skip("too large to read safely")


def _inflate(raw, expected):
    """Exactly `expected` bytes from a zlib stream, or _Skip. The output is limited before it is built."""
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw, expected + 1)
    except zlib.error:
        raise _Skip("damaged") from None
    if len(out) > expected:
        raise _Skip("expands past its declared size")
    if len(out) < expected:
        raise _Skip("damaged")
    return out, d


def _flate_samples(xobj, width, height, comps, bpc=8):
    """(rows, idat) of a Flate image. `rows` are the inflated scanlines exactly as stored; `idat` is the original zlib
    stream when those rows already carry PNG filter bytes (predictor 10-15), else None. Raises _Skip. `bpc` is 8 or 16."""
    parms = _parms(xobj)
    pred = _num(parms.get("/Predictor", 1), 1) if parms else 1
    per = comps * bpc // 8                  # bytes per pixel
    if pred == 1:
        row = width * per
        rows, _ = _inflate(_raw_stream(xobj), row * height)
        return rows, None, False
    if pred >= 10:
        if (_num(parms.get("/Colors", 1), 1) != comps or _num(parms.get("/Columns", 1), 1) != width
                or _num(parms.get("/BitsPerComponent", 8), 8) != bpc):
            raise _Skip("unsupported encoding")
        row = width * per + 1
        raw = _raw_stream(xobj)
        rows, d = _inflate(raw, row * height)
        if max(rows[::row]) > 4:
            raise _Skip("damaged")
        return rows, (raw if d.eof and not d.unused_data else None), True
    raise _Skip("unsupported encoding")


def _unfiltered(rows, row_len, filtered):
    """Scanline bytes without PNG filter bytes, but only when every row uses filter type 0 (no un-filtering is done)."""
    if not filtered:
        return rows
    step = row_len + 1
    if any(rows[::step]):
        return None
    return b"".join(rows[i + 1:i + step] for i in range(0, len(rows), step))


def _soft_mask(xobj, width, height):
    """Alpha bytes (width*height) of an 8-bit gray Flate soft mask the same size as the picture, else None."""
    sm = _resolve(xobj.get("/SMask"))
    if sm is None or not hasattr(sm, "get") or "/Matte" in sm:
        return None
    try:
        if (_num(sm.get("/Width")), _num(sm.get("/Height"))) != (width, height) or _num(sm.get("/BitsPerComponent", 8), 8) != 8:
            return None
        if _components(sm) != 1 or _filters(sm) != ["/FlateDecode"] or not _default_decode(sm, 1):
            return None
        rows, _idat, filtered = _flate_samples(sm, width, height, 1)
        return _unfiltered(rows, width, filtered)
    except _Skip:
        return None


def _has_smask(xobj):
    return _resolve(xobj.get("/SMask")) is not None


def _decode_image(xobj, notes, stop=None, state=None):
    """(content type, bytes, width, height) for one image XObject, or raises _Skip(reason). With Pillow available a picture the
    plain reader cannot take (CMYK, indexed, 16-bit, soft mask, over the size limits) is converted instead of skipped."""
    width, height = _num(xobj.get("/Width")), _num(xobj.get("/Height"))
    px = state["pillow"] if state is not None else imaging_pillow.available()
    side = imaging_pillow.MAX_INPUT_SIDE if px else MAX_DIMENSION
    if width < 1 or height < 1:
        raise _Skip("empty")
    if width > side or height > side:
        raise _Skip("too large to read safely")
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise _Skip("too small")
    ratio = max(width / height, height / width)
    if ratio > MAX_ASPECT:
        raise _Skip("logo or banner shaped")
    if xobj.get("/ImageMask") in (True, "true"):
        raise _Skip("mask")
    try:
        return _decode_plain(xobj, notes, width, height, px)
    except _Skip as e:
        if not px or str(e) not in _FIXABLE:
            raise
    return _decode_with_pillow(xobj, notes, width, height, stop, state)


def _decode_plain(xobj, notes, width, height, px):
    """The reader that needs no imaging library (the original behaviour)."""
    _check_size(width, height, 0)
    if px and _has_smask(xobj):
        raise _Skip("needs conversion")         # Pillow composites a soft mask onto white
    filters = _filters(xobj)
    if filters == ["/DCTDecode"] or filters == ["/DCT"]:
        data = _raw_stream(xobj)
        if len(data) > MAX_IMAGE_BYTES:
            raise _Skip("larger than 2 MB")
        comps = _components(xobj)
        if comps in ("cmyk", "indexed") or _jpeg_components(data) == 4:
            raise _Skip("CMYK JPEG")
        if len(data) < width * height * 0.006:      # a flat colour compresses to almost nothing
            raise _Skip("near-uniform")
        try:
            info = inspect_image(data)
        except ImageError:
            raise _Skip("not a clean JPEG") from None
        return info.content_type, data, info.width, info.height
    if filters != ["/FlateDecode"] and filters != ["/Fl"]:
        raise _Skip("unsupported encoding")
    if _num(xobj.get("/BitsPerComponent", 8), 8) != 8:
        raise _Skip("1-bit or high-depth")
    comps = _components(xobj)
    if comps == "cmyk":
        raise _Skip("CMYK")
    if comps == "indexed":
        raise _Skip("indexed colours")
    if not isinstance(comps, int):
        raise _Skip("unsupported colour space")
    if not _default_decode(xobj, comps):
        raise _Skip("unsupported encoding")
    _check_size(width, height, comps)
    rows, idat, filtered = _flate_samples(xobj, width, height, comps)
    pixels = _unfiltered(rows, width * comps, filtered)     # None when PNG filters are in use
    # near-uniform check on a sample of the stored bytes (filter bytes are a small, constant share)
    sample = rows[:: max(1, len(rows) // 20000)]
    if Counter(sample).most_common(1)[0][1] / len(sample) >= 0.99:
        raise _Skip("near-uniform")
    alpha = _soft_mask(xobj, width, height) if "/SMask" in xobj else None
    if alpha is not None:
        if pixels is None:
            alpha = None            # the colour rows use PNG filters: merging would need un-filtering
        else:
            merged = bytearray(width * height * (comps + 1))
            for c in range(comps):
                merged[c::comps + 1] = pixels[c::comps]
            merged[comps::comps + 1] = alpha
            data = encode_png(width, height, comps + 1, bytes(merged))
            if len(data) > MAX_IMAGE_BYTES:
                raise _Skip("larger than 2 MB")
            return "image/png", data, width, height
    elif "/SMask" in xobj:
        notes.add("transparency")
    if idat is not None:
        data = png_from_idat(width, height, comps, idat)
    elif filtered:
        data = png_from_idat(width, height, comps, zlib.compress(rows, 6))
    else:
        data = encode_png(width, height, comps, rows)
    if len(data) > MAX_IMAGE_BYTES:
        raise _Skip("larger than 2 MB")
    return "image/png", data, width, height


# ------------------------------------------------------------------------------------------ the optional Pillow path
def _lookup_bytes(obj, need):
    """`need` bytes of an /Indexed lookup table (a string, or a small unfiltered or Flate stream), or _Skip."""
    o = _resolve(obj)
    if isinstance(o, (bytes, bytearray)):
        data = bytes(o)
    elif isinstance(o, str):
        data = getattr(o, "original_bytes", None) or o.encode("latin-1", "ignore")
    elif hasattr(o, "get") and getattr(o, "_data", None) is not None:
        raw, f = _raw_stream(o), _filters(o)
        if not f:
            data = raw
        elif f == ["/FlateDecode"]:
            try:
                data = zlib.decompressobj().decompress(raw, need + 1)
            except zlib.error:
                raise _Skip("damaged") from None
        else:
            raise _Skip("unsupported colour space")
    else:
        raise _Skip("unsupported colour space")
    if len(data) < need:
        raise _Skip("damaged")
    return data[:need]


_KIND = {1: "L", 3: "RGB", "cmyk": "CMYK"}
_KIND_CHANNELS = {"L": 1, "RGB": 3, "CMYK": 4}


def _colour_info(xobj):
    """(kind, palette): kind is "L", "RGB", "CMYK" or "P"; palette is (base kind, lookup bytes) for "P". Raises _Skip."""
    cs = _resolve(xobj.get("/ColorSpace"))
    comps = _space(cs)
    if comps in _KIND:
        return _KIND[comps], None
    if comps == "indexed" and isinstance(cs, (list, tuple)) and len(cs) >= 4:
        base = _KIND.get(_space(cs[1]))
        hival = _num(_resolve(cs[2]), -1)
        if base is None or not 0 <= hival <= 255:
            raise _Skip("unsupported colour space")
        return "P", (base, _lookup_bytes(cs[3], (hival + 1) * _KIND_CHANNELS[base]))
    raise _Skip("unsupported colour space")


def _cmyk_decode(xobj):
    """False for the default /Decode, True for [1 0 1 0 1 0 1 0] (inverted: the Adobe convention), None for anything else."""
    d = _resolve(xobj.get("/Decode"))
    if d is None:
        return False
    try:
        vals = [float(_resolve(x)) for x in d]
    except (TypeError, ValueError):
        return None
    return False if vals == [0.0, 1.0] * 4 else True if vals == [1.0, 0.0] * 4 else None


def _pillow_mask(xobj, stop):
    """The soft mask of a picture as an imaging_pillow.Mask (8-bit gray Flate, any size within budget), else None."""
    sm = _resolve(xobj.get("/SMask"))
    if sm is None or not hasattr(sm, "get") or "/Matte" in sm:
        return None
    try:
        mw, mh = _num(sm.get("/Width")), _num(sm.get("/Height"))
        if (mw < 1 or mh < 1 or mw > imaging_pillow.MAX_INPUT_SIDE or mh > imaging_pillow.MAX_INPUT_SIDE
                or mw * mh > MAX_MASK_PIXELS or _num(sm.get("/BitsPerComponent", 8), 8) != 8):
            return None
        if _components(sm) != 1 or _filters(sm) != ["/FlateDecode"] or not _default_decode(sm, 1):
            return None
        rows, idat, filtered = _flate_samples(sm, mw, mh, 1)
        pixels = _unfiltered(rows, mw, filtered)
        if pixels is None:
            png = png_from_idat(mw, mh, 1, idat or zlib.compress(rows, 6))
            pixels = imaging_pillow.raw_from_png(png, mw, mh, 1, stop=stop)
        return imaging_pillow.Mask(mw, mh, pixels)
    except (_Skip, ConversionError):
        return None


def _decode_with_pillow(xobj, notes, width, height, stop, state):
    if state is not None:
        if state["converted"] >= MAX_CONVERSIONS:
            raise _Skip("conversion limit")
        state["converted"] += 1
    mask = None
    if _has_smask(xobj):
        mask = _pillow_mask(xobj, stop)
        if mask is None:
            notes.add("transparency")
    try:
        out = _pillow_convert(xobj, width, height, mask, stop)
    except ConversionError as e:
        raise _Skip(str(e)) from None
    if state is not None:
        state["cmyk"] += out.cmyk
        state["reduced"] += out.reduced
    return out.content_type, out.data, out.width, out.height


def _pillow_convert(xobj, width, height, mask, stop):
    filters = _filters(xobj)
    if filters == ["/DCTDecode"] or filters == ["/DCT"]:
        data = _raw_stream(xobj)
        if len(data) > imaging_pillow.MAX_INPUT_BYTES:
            raise _Skip("too large to read safely")
        comps = _components(xobj)
        if comps == "indexed":
            raise _Skip("indexed colours")
        if len(data) < width * height * 0.006:
            raise _Skip("near-uniform")
        jc = _jpeg_components(data)
        inverted = False
        if comps == "cmyk" or jc == 4:
            inverted = _cmyk_decode(xobj)
            if inverted is None:
                raise _Skip("unsupported encoding")
        elif not _default_decode(xobj, jc or 3):
            raise _Skip("unsupported encoding")
        return imaging_pillow.convert_jpeg(data, decode_inverted=inverted, mask=mask, expect=(width, height), stop=stop)
    if filters != ["/FlateDecode"] and filters != ["/Fl"]:
        raise _Skip("unsupported encoding")
    bpc = _num(xobj.get("/BitsPerComponent", 8), 8)
    if bpc not in (8, 16):
        raise _Skip("1-bit or high-depth")
    kind, palette = _colour_info(xobj)
    if kind == "P" and bpc != 8:
        raise _Skip("1-bit or high-depth")
    ch = _KIND_CHANNELS.get(kind, 1)
    per = ch * bpc // 8
    if width * height > imaging_pillow.MAX_INPUT_PIXELS or width * height * per > imaging_pillow.MAX_WORK_BYTES:
        raise _Skip("too large to read safely")
    inverted = False
    if kind == "CMYK":
        inverted = _cmyk_decode(xobj)
        if inverted is None:
            raise _Skip("unsupported encoding")
    elif _resolve(xobj.get("/Decode")) is not None and (kind == "P" or not _default_decode(xobj, ch)):
        raise _Skip("unsupported encoding")
    rows, idat, filtered = _flate_samples(xobj, width, height, ch, bpc)
    pixels = _unfiltered(rows, width * per, filtered)
    if pixels is None:                              # real PNG filters: Pillow un-filters a PNG built around the same stream
        if bpc != 8:
            raise _Skip("unsupported encoding")
        png = png_from_idat(width, height, 3 if ch in (3, 4) else 1, idat or zlib.compress(rows, 6), has_alpha=ch == 4)
        pixels = imaging_pillow.raw_from_png(png, width, height, ch, stop=stop)
    del rows
    if bpc == 16:
        pixels = pixels[0::2]                       # big-endian samples: the high byte is the 8-bit value
    return imaging_pillow.convert_raw(width, height, pixels, kind=kind, palette=palette, decode_inverted=inverted,
                                      mask=mask, stop=stop)


# ------------------------------------------------------------------------------------------ where a picture is drawn
MAX_CONTENT_OPS = 60000                 # operators read per document when looking for picture positions
MAX_CONTENT_FORMS = 24
EDGE = 0.02                             # a picture within 2 % of a page edge "touches" it
SMALL_AREA = 0.012                      # under 1.2 % of the page: a logo, a badge, a bullet picture


def _mul(a, b):
    """The PDF matrix product a x b for [a b c d e f] lists (a is applied first)."""
    return [a[0] * b[0] + a[1] * b[2], a[0] * b[1] + a[1] * b[3], a[2] * b[0] + a[3] * b[2], a[2] * b[1] + a[3] * b[3],
            a[4] * b[0] + a[5] * b[2] + b[4], a[4] * b[1] + a[5] * b[3] + b[5]]


def _floats(values, n):
    try:
        out = [float(_resolve(v)) for v in values]
    except (TypeError, ValueError):
        return None
    return out if len(out) == n and all(math.isfinite(x) for x in out) else None


def _placements(reader, page, stop):
    """{image object number: (x0, y0, x1, y1)}: where the page draws each picture, from its content stream (`cm`, `q`, `Q`
    and `Do`, following form XObjects). Only used to tell page decoration and logos from product shots, so any failure just
    means "unknown" for that picture. Bounded by MAX_CONTENT_OPS operators and MAX_CONTENT_FORMS forms."""
    from pypdf.generic import ContentStream

    out, budget = {}, {"ops": MAX_CONTENT_OPS, "forms": MAX_CONTENT_FORMS}

    def run(contents, resources, ctm, depth):
        res = _resolve(resources)
        xobjs = _resolve(res.get("/XObject")) if res is not None and hasattr(res, "get") else None
        if xobjs is None or not hasattr(xobjs, "get"):
            return
        stack = []
        for operands, op in contents.operations:
            budget["ops"] -= 1
            if budget["ops"] < 0 or time.monotonic() > stop:
                return
            if op == b"q":
                if len(stack) < 64:
                    stack.append(ctm)
            elif op == b"Q":
                if stack:
                    ctm = stack.pop()
            elif op == b"cm":
                m = _floats(operands, 6)
                if m:
                    ctm = _mul(m, ctm)
            elif op == b"Do" and operands:
                ref = xobjs.get(operands[0])
                xo = _resolve(ref)
                if xo is None or not hasattr(xo, "get"):
                    continue
                sub = _name(xo.get("/Subtype"))
                if sub == "/Image":
                    ident = getattr(getattr(ref, "indirect_reference", None) or ref, "idnum", None)
                    if ident is None:
                        continue
                    xs = [ctm[4], ctm[4] + ctm[0], ctm[4] + ctm[2], ctm[4] + ctm[0] + ctm[2]]
                    ys = [ctm[5], ctm[5] + ctm[1], ctm[5] + ctm[3], ctm[5] + ctm[1] + ctm[3]]
                    box = (min(xs), min(ys), max(xs), max(ys))
                    old = out.get(ident)
                    if old is None or (box[2] - box[0]) * (box[3] - box[1]) > (old[2] - old[0]) * (old[3] - old[1]):
                        out[ident] = box
                elif sub == "/Form" and depth < MAX_FORM_DEPTH and budget["forms"] > 0:
                    budget["forms"] -= 1
                    m = _floats(_resolve(xo.get("/Matrix")) or [1, 0, 0, 1, 0, 0], 6) or [1, 0, 0, 1, 0, 0]
                    run(ContentStream(xo, reader), xo.get("/Resources"), _mul(m, ctm), depth + 1)

    try:
        run(page.get_contents(), page.get("/Resources"), [1.0, 0.0, 0.0, 1.0, 0.0, 0.0], 0)
    except Exception:  # noqa: BLE001 - hostile or odd content streams: positions stay unknown
        pass
    return out


def _page_size(page):
    try:
        box = page.mediabox
        x0, y0, x1, y1 = float(box.left), float(box.bottom), float(box.right), float(box.top)
    except Exception:  # noqa: BLE001
        return None
    return (x0, y0, x1, y1) if x1 - x0 > 1 and y1 - y0 > 1 else None


def _is_decoration(box, media):
    """True when the picture is drawn as page decoration or at logo size rather than as the product shot: it reaches two
    or more page edges (a header band, a full-bleed background, a corner graphic) or covers under 1.2 % of the page."""
    if not box or not media:
        return False
    px0, py0, px1, py1 = media
    w, h = px1 - px0, py1 - py0
    x0, y0, x1, y1 = box
    edges = (x0 <= px0 + EDGE * w) + (x1 >= px1 - EDGE * w) + (y0 <= py0 + EDGE * h) + (y1 >= py1 - EDGE * h)
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0) / (w * h)
    return edges >= 2 or area < SMALL_AREA


# ------------------------------------------------------------------------------------------ walking the document
class _WalkBudget:
    """One shared allowance for a whole pass over the document's XObject dictionaries: the wall-clock deadline and a count
    of every entry examined, so a fan-out of Forms inside Forms cannot multiply into millions of lookups."""
    def __init__(self, stop=None, entries=MAX_WALK_ENTRIES):
        self.stop, self.entries, self.out = stop, entries, False

    def spend(self):
        """True while the walk may examine one more entry; once it returns False the whole walk is over."""
        if self.out:
            return False
        self.entries -= 1
        if self.entries < 0 or (self.stop is not None and time.monotonic() > self.stop):
            self.out = True
        return not self.out


def _walk_resources(resources, depth, visit, budget=None, seen=None):
    """Call visit(xobject) for every image XObject; follow form XObjects up to MAX_FORM_DEPTH. The shared budget (entries
    examined and the deadline) ends the whole walk, not just the current level; a Form met twice is walked once."""
    budget = budget or _WalkBudget()
    seen = set() if seen is None else seen
    res = _resolve(resources)
    if res is None or not hasattr(res, "get"):
        return
    xobjs = _resolve(res.get("/XObject"))
    if xobjs is None or not hasattr(xobjs, "values"):
        return
    for key in list(xobjs.keys())[:200]:
        if not budget.spend():
            return
        ref = xobjs[key]
        xo = _resolve(ref)
        if xo is None or not hasattr(xo, "get"):
            continue
        sub = _name(xo.get("/Subtype"))
        ident = getattr(getattr(ref, "indirect_reference", None) or ref, "idnum", None)
        if sub == "/Image":
            if visit(xo, ident) is False:
                budget.out = True
                return
        elif sub == "/Form" and depth < MAX_FORM_DEPTH:
            if ident is not None:
                if ident in seen:
                    continue
                seen.add(ident)
            _walk_resources(xo.get("/Resources"), depth + 1, visit, budget, seen)
            if budget.out:
                return


_REASON_TEXT = {
    "too small": "too small", "logo or banner shaped": "logo or banner shaped", "near-uniform": "blank or a flat colour",
    "mask": "a mask", "CMYK JPEG": "CMYK (print colour)", "CMYK": "CMYK (print colour)", "indexed colours": "indexed colours",
    "1-bit or high-depth": "1-bit or high bit depth", "unsupported encoding": "in a format that is not supported",
    "unsupported colour space": "in a colour space that is not supported", "larger than 2 MB": "larger than 2 MB",
    "too large to read safely": "too large to read safely", "damaged": "damaged", "empty": "empty",
    "expands past its declared size": "damaged", "not a clean JPEG": "damaged", "unreadable stream": "unreadable",
    "took too long": "not converted in time", "busy": "not converted (the converter was busy)", "unavailable": "not converted",
    "conversion limit": "not converted (limit reached)", "needs conversion": "not converted",
}


def _summary(skipped):
    merged = Counter()                  # "CMYK" and "CMYK JPEG" read the same to the operator: one count
    for reason, n in skipped.most_common():
        merged[_REASON_TEXT.get(reason, reason)] += n
    parts = [f"{n} {text}" for text, n in merged.most_common()]
    return "Skipped " + ", ".join(parts) + "." if parts else ""


def extract(pdf_bytes, *, deadline_seconds=DEADLINE_SECONDS, max_candidates=MAX_CANDIDATES):
    """Ranked product-photo candidates from PDF bytes. Never raises; see PdfResult."""
    res = PdfResult()
    if not isinstance(pdf_bytes, (bytes, bytearray, memoryview)):
        res.message = "No file was given."
        return res
    pdf_bytes = bytes(pdf_bytes)
    if not pdf_bytes:
        res.message = "The file is empty."
        return res
    if len(pdf_bytes) > MAX_PDF_BYTES:
        res.message = f"The file is {len(pdf_bytes) / 1024 / 1024:.0f} MB; PDFs up to {MAX_PDF_BYTES // 1024 // 1024} MB are read."
        return res
    if b"%PDF-" not in pdf_bytes[:1024]:
        res.message = "This does not look like a PDF file. Choose the datasheet you downloaded from the vendor."
        return res
    try:
        import pypdf
    except ImportError:
        res.unavailable = True
        res.message = INSTALL_HINT
        return res
    stop = time.monotonic() + deadline_seconds
    found, skipped, seen_ids, notes = [], Counter(), set(), set()
    state = {"images": 0, "timeout": False, "pillow": imaging_pillow.available(), "converted": 0, "cmyk": 0, "reduced": 0}
    try:
        try:
            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes), strict=False)
            if reader.is_encrypted:
                try:
                    ok = reader.decrypt("")           # many PDFs are "encrypted" only to restrict printing: empty password
                except Exception:  # noqa: BLE001 - missing crypto library, unsupported scheme
                    res.message = ("This PDF is encrypted in a way LinkedEye cannot open. Open it in your PDF viewer, save an "
                                   "unprotected copy (print to PDF) and upload that one.")
                    return res
                if not ok:
                    res.message = ("This PDF is password protected. Open it in your PDF viewer, save an unprotected copy "
                                   "(print to PDF) and upload that one.")
                    return res
            total_pages = len(reader.pages)
        except Exception:  # noqa: BLE001 - every parser failure is "cannot read this file"
            res.message = "This PDF could not be read: it is damaged or uses a feature LinkedEye does not support."
            return res
        # Where each scanned page draws its pictures. Some generators (dompdf, for one) put every picture of the document in
        # ONE shared resource dictionary, so without this a picture drawn on page 2 would be credited to page 1 and could
        # outrank the real page-1 photo; a picture a page does not draw is left to the page that does.
        placements = {}
        for page_no in range(min(total_pages, MAX_PAGES)):
            if time.monotonic() > stop:
                break
            try:
                placements[page_no] = _placements(reader, reader.pages[page_no], stop)
            except Exception:  # noqa: BLE001 - positions are optional
                placements[page_no] = {}
        # A soft mask (or explicit mask) is part of the picture it belongs to, never a picture of its own, yet some generators
        # list it in the page resources too: offering it would show a white silhouette as a "photo".
        mask_ids = set()

        def note_masks(xo, ident):
            for key in ("/SMask", "/Mask"):
                ref = xo.get(key)
                mid = getattr(ref, "idnum", None)
                if mid is not None:
                    mask_ids.add(mid)
            return len(mask_ids) < 1000
        mask_budget = _WalkBudget(stop)
        for page_no in range(min(total_pages, MAX_PAGES)):
            if mask_budget.out:
                break
            try:
                _walk_resources(reader.pages[page_no].get("/Resources"), 0, note_masks, mask_budget)
            except Exception:  # noqa: BLE001
                pass
        walk_budget = _WalkBudget(stop)
        for page_no in range(min(total_pages, MAX_PAGES)):
            if time.monotonic() > stop:
                state["timeout"] = True
                break
            if state["images"] >= MAX_IMAGE_OBJECTS:
                break
            try:
                page = reader.pages[page_no]
                resources = page.get("/Resources")
            except Exception:  # noqa: BLE001
                skipped["unreadable page"] += 1
                continue

            media = _page_size(page)
            where = placements.get(page_no) or {}
            elsewhere = {i for n, w in placements.items() if n != page_no for i in w} - set(where)

            def visit(xo, ident, page_no=page_no, media=media, where=where, elsewhere=elsewhere):
                if time.monotonic() > stop:
                    state["timeout"] = True
                    return False
                if ident is not None and (ident in elsewhere or ident in mask_ids):
                    return True                     # drawn on another page (shared resources), or only the mask of a picture
                if ident is not None:
                    if ident in seen_ids:
                        return True
                    seen_ids.add(ident)
                if state["images"] >= MAX_IMAGE_OBJECTS:
                    return False
                state["images"] += 1
                try:
                    ctype, data, w, h = _decode_image(xo, notes, stop, state)
                except _Skip as e:
                    skipped[str(e)] += 1
                except Exception:  # noqa: BLE001 - a broken image object must not stop the others
                    skipped["damaged"] += 1
                else:
                    found.append(PdfCandidate(data, ctype, w, h, page_no + 1, f"datasheet page {page_no + 1}",
                                              hashlib.sha256(data).hexdigest(), _is_decoration(where.get(ident), media)))
                return True
            try:
                _walk_resources(resources, 0, visit, walk_budget)
                if walk_budget.out and time.monotonic() > stop:
                    state["timeout"] = True
            except Exception:  # noqa: BLE001
                skipped["unreadable page"] += 1
    except Exception:  # noqa: BLE001 - anything pypdf throws that the guards above missed
        res.message = "This PDF could not be read: it is damaged or uses a feature LinkedEye does not support."
        return res
    unique, seen_hash = [], set()
    for c in found:
        if c.sha256 not in seen_hash:
            seen_hash.add(c.sha256)
            unique.append(c)
    # Product shots first (page order, then size, then closeness to a product-shot aspect); page decoration, header bands,
    # vendor badges and logos after them, because on a datasheet the biggest picture is often a graphic, not the product.
    unique.sort(key=lambda c: (c.demoted, c.page, -(c.width * c.height), abs(math.log((c.width / c.height) / IDEAL_ASPECT))))
    res.candidates = unique[:max_candidates]
    summary = _summary(skipped)
    big = skipped.get("larger than 2 MB", 0)
    cmyk_skipped = skipped.get("CMYK", 0) + skipped.get("CMYK JPEG", 0)
    if state["timeout"]:
        res.notes.append("Stopped early: the PDF took too long to read.")
    if res.candidates:
        if summary:
            res.notes.append(summary)
        if "transparency" in notes:
            res.notes.append("Some pictures had transparency that could not be kept; they may show a solid background.")
        if state["cmyk"]:
            res.notes.append(f"Converted {state['cmyk']} print-colour (CMYK) picture{'s' if state['cmyk'] != 1 else ''} to screen colour.")
        if state["reduced"]:
            res.notes.append(f"Reduced {state['reduced']} large picture{'s' if state['reduced'] != 1 else ''} to fit the photo limits "
                             f"({MAX_IMAGE_BYTES // 1024 // 1024} MB, {MAX_DIMENSION} px).")
        if cmyk_skipped and not state["pillow"]:
            res.notes.append(_print_colour_hint())
    else:
        if total_pages == 0:
            res.message = "The PDF has no pages."
        else:
            res.message = ("No product pictures were found in the first pages of this PDF. " + summary).strip()
            if cmyk_skipped and not state["pillow"]:
                # Print-oriented datasheets store their photos in CMYK; the optional Pillow extra converts them.
                res.message += " " + _print_colour_hint()
            if not state["images"]:
                res.message = ("No pictures were found in the first pages of this PDF. If the product photo is drawn as "
                               "vector art or the PDF is a scan, use a product page address or upload the photo itself.")
    if big:
        what = f"{big} picture{'s are' if big != 1 else ' is'} larger than the {MAX_IMAGE_BYTES // 1024 // 1024} MB photo limit"
        if state["pillow"]:
            res.notes.append(f"{what} and could not be reduced to fit; save a smaller copy of that picture and upload it directly if you want it.")
        else:
            res.notes.append(f"{what} and cannot be shrunk here. Install the optional image library (pip install -r "
                             "requirements-images.txt) and LinkedEye will shrink them, or save a smaller copy of that picture and "
                             "upload it directly.")
    return res


def _print_colour_hint():
    status, version = imaging_pillow.status()
    if status == "old":
        return PRINT_COLOUR_HINT_OLD.format(version=version, minimum=".".join(map(str, imaging_pillow.MIN_VERSION)))
    return PRINT_COLOUR_HINT
