"""Optional Pillow path: turn print-colour and oversized datasheet pictures into a clean sRGB baseline JPEG or PNG.

Print-oriented datasheets store their product photos as CMYK JPEGs, as indexed or 16-bit Flate images, behind soft
masks, or larger than the 2 MB / 4096 px photo limits. network.datasheet_pdf cannot read those on its own, so when Pillow is
installed (requirements-images.txt; it is imported lazily and the app and its tests work without it) this module converts them:

* CMYK and YCCK JPEGs (also Adobe-inverted ones), CMYK / indexed / 16-bit Flate images become sRGB with Pillow's standard
  conversion. An embedded ICC profile is deliberately NOT used: that would hand an attacker-controlled profile to LittleCMS,
  which has had memory-safety bugs; the cost is slightly flatter print colours;
* a soft mask is composited onto white;
* a picture over 4096 px or 2 MB is downscaled to fit (aspect ratio kept) instead of skipped;
* the result is re-encoded as a baseline JPEG (quality 88) or, with few colours, a PNG, with no metadata, no ICC profile and
  no EXIF, and then goes through network.imaging.inspect_image again before anything stores it.

Pillow's decoders have a long CVE history and every byte here is attacker-controlled, so the rules are:

* nothing reaches Pillow unless its magic bytes say JPEG or PNG, its structure passes network.imaging.inspect_image (which
  also refuses a picture with data appended, a second image or a damaged header) and its declared size, read from the
  header by us, is inside the budget (40 megapixels, 16384 px per side, 20 MB of file);
* only baseline, extended-sequential and progressive Huffman JPEGs are decoded (no lossless, no arithmetic coding);
* Image.open gets formats=[...] with exactly that one format, so no other decoder or plugin is ever tried; truncated files
  are an error; Image.MAX_IMAGE_PIXELS is a hard number (never None) and DecompressionBombWarning is an error;
* a large JPEG is decoded with draft mode, at 1/2, 1/4 or 1/8 size, so that at most MAX_WORK_BYTES of pixels exist;
* conversions are serialised by a semaphore of two and run on a worker thread that keeps its slot until it really finishes; the
  caller waits at most its deadline and walks away from a stuck native decode (Pillow releases the GIL), so a stuck decoder keeps
  using one of the two slots and never more native work than that; the deadline is also checked between stages;
* a JPEG with more than 32 scans is refused (each scan is a pass over the whole picture), and a progressive or multi-scan JPEG
  is held to 64 MB of DCT coefficients because draft mode does not shrink libjpeg's coefficient buffer;
* every Pillow exception becomes ConversionError("<short reason>"), which the caller reports as "skipped"; nothing is written to
  disk and no plugin is loaded from a path.

Memory (measured, 40 MP CMYK JPEG, the largest input accepted): about 85 MB above the baseline per conversion (113 MB process
peak), progressive or multi-scan JPEGs at their 64 MB coefficient limit about 100 MB; the bytes of the input (up to 20 MB) and
of the PDF come on top. With two slots that is about 250 MB at worst, which is why the budgets are not larger.

Pillow 13.0 is the floor (see requirements-images.txt): earlier releases have published heap overflows in the JPEG decoder
(including the draft path used here) and the JPEG encoder."""
from __future__ import annotations

import io
import logging
import math
import struct
import threading
import time
import warnings
from dataclasses import dataclass

from .imaging import JPEG, MAX_BYTES, MAX_DIMENSION, MAX_JPEG_SEGMENTS, PNG, ImageError, inspect_image, sniff

MIN_VERSION = (13, 0)
MAX_INPUT_PIXELS = 40_000_000           # declared width x height, checked from the header before any decode
MAX_INPUT_SIDE = 16384
MAX_INPUT_BYTES = 20 * 1024 * 1024      # a JPEG stream or an inflated sample buffer
MAX_WORK_BYTES = 48 * 1024 * 1024       # the most pixel memory a decode may need (after draft scaling)
QUALITY = 88
SLOTS = 2
SLOT_WAIT_SECONDS = 4.0
HARD_LIMIT_SECONDS = 30.0               # longest a caller waits for one conversion when it gave no deadline
MAX_SCANS = 32                          # SOS segments in a JPEG (libjpeg's own progressive scripts have 10 to 15)
MAX_COEFFICIENT_BYTES = 64 * 1024 * 1024    # libjpeg keeps every DCT coefficient (2 bytes each) of a multi-scan JPEG in memory
MAX_ATTEMPTS = 6                        # resize-and-encode rounds to get under the byte limit
MAX_PALETTE = 256
FEW_COLOURS = 256

log = logging.getLogger(__name__)
_SLOTS = threading.BoundedSemaphore(SLOTS)
_SOF_OK = {0xC0, 0xC1, 0xC2}            # baseline, extended sequential, progressive (all Huffman, 8 bit)


class ConversionError(Exception):
    """The picture cannot be converted; str(e) is a short reason key that datasheet_pdf shows as "skipped"."""


@dataclass
class Converted:
    content_type: str
    data: bytes
    width: int
    height: int
    cmyk: bool = False                  # the source was print colour
    reduced: bool = False               # it was shrunk to fit the limits


@dataclass
class Mask:
    """An 8-bit soft mask (255 = opaque) of any size; it is resized to the picture."""
    width: int
    height: int
    data: bytes


# ------------------------------------------------------------------------------------------ availability
def _load():
    """The PIL.Image module, or None when Pillow is not installed (or is a broken install)."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001 - ImportError, or a native library that fails to load
        return None
    return Image


def _version(Image):
    try:
        import PIL
        return tuple(int(p) for p in str(PIL.__version__).split(".")[:2])
    except Exception:  # noqa: BLE001
        return (0, 0)


def status():
    """("", "") when the converter can be used, else ("missing", "") or ("old", "<installed version>")."""
    Image = _load()
    if Image is None:
        return "missing", ""
    if _version(Image) < MIN_VERSION:
        import PIL
        return "old", str(PIL.__version__)
    return "", ""


def available():
    return status()[0] == ""


def _setup(Image):
    """Hard limits on the imaging library itself, applied before every use (cheap, and robust against other code changing them
    or resetting the warning filters: filterwarnings replaces an identical entry instead of adding another)."""
    from PIL import ImageFile
    Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS            # a number, never None: Pillow warns above it and fails above twice it
    ImageFile.LOAD_TRUNCATED_IMAGES = False              # a truncated file is an error, not a half-grey picture
    warnings.filterwarnings("error", category=Image.DecompressionBombWarning)


# ------------------------------------------------------------------------------------------ header reading (our own)
def jpeg_header(data):
    """(SOF marker, width, height, components) read from the JPEG segments, or raises ConversionError. Only called on data
    that inspect_image has already walked."""
    n, pos = len(data), 2
    for _ in range(600):
        if pos + 4 > n or data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        length = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if pos + 10 > n:
                break
            _, height, width, comps = struct.unpack(">BHHB", data[pos + 4:pos + 10])
            return marker, width, height, comps
        if marker in (0xDA, 0xD9):
            break
        pos += 2 + length
    raise ConversionError("damaged")


def jpeg_scans(data):
    """Number of SOS (scan) segments, walking the segments like network.imaging does. Only called on data that inspect_image has
    already walked, so every length is in range; the loop is bounded anyway."""
    n, pos, scans = len(data), 2, 0
    for _ in range(MAX_JPEG_SEGMENTS + 1):
        if pos + 2 > n or data[pos] != 0xFF:
            break
        marker = data[pos + 1]
        if marker == 0xFF:
            pos += 1
            continue
        pos += 2
        if marker == 0xD9:
            break
        if marker == 0x00 or marker == 0x01 or 0xD0 <= marker <= 0xD8:
            continue
        if pos + 2 > n:
            break
        pos += struct.unpack(">H", data[pos:pos + 2])[0]
        if marker == 0xDA:
            scans += 1
            while True:                                   # entropy-coded data up to the next real marker
                pos = data.find(b"\xff", pos)
                if pos < 0 or pos + 1 >= n:
                    return scans
                nxt = data[pos + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    pos += 1 if nxt == 0xFF else 2
                    continue
                break
    return scans


def _check_budget(width, height):
    if width < 1 or height < 1:
        raise ConversionError("empty")
    if width > MAX_INPUT_SIDE or height > MAX_INPUT_SIDE or width * height > MAX_INPUT_PIXELS:
        raise ConversionError("too large to read safely")


# ------------------------------------------------------------------------------------------ the guarded section
class _Guard:
    """Checks the caller's wall-clock deadline, and whether the caller has given up, between the stages of one conversion."""

    def __init__(self, stop, cancelled):
        self.stop = stop
        self.cancelled = cancelled

    def tick(self):
        if self.cancelled.is_set() or (self.stop is not None and time.monotonic() > self.stop):
            raise ConversionError("took too long")


def _guarded(fn):
    """Run fn(Image, guard, ...) with Pillow configured, on a worker thread that holds one of the conversion slots, and map every
    failure to ConversionError.

    A native decode cannot be interrupted, but Pillow releases the GIL while it decodes, so the caller waits for the worker with
    a timeout (its deadline, at most HARD_LIMIT_SECONDS) and walks away from a worker that is stuck. The worker keeps its slot
    until it really finishes, so a stuck decoder keeps counting against the two-at-a-time memory bound and, if both slots are
    stuck, further pictures are refused ("busy") instead of piling up more native work."""
    def run(*args, stop=None, **kw):
        Image = _load()
        if Image is None or not available():
            raise ConversionError("unavailable")
        now = time.monotonic()
        wait = SLOT_WAIT_SECONDS if stop is None else max(0.0, min(SLOT_WAIT_SECONDS, stop - now))
        if not _SLOTS.acquire(timeout=wait):
            raise ConversionError("busy")
        if stop is not None and time.monotonic() > stop:
            _SLOTS.release()
            raise ConversionError("took too long")
        cancelled, box = threading.Event(), {}

        def work():
            try:
                try:
                    _setup(Image)
                    guard = _Guard(stop, cancelled)
                    guard.tick()
                    box["value"] = fn(Image, guard, *args, **kw)
                except ConversionError as e:
                    box["error"] = e
                except (Image.DecompressionBombError, Image.DecompressionBombWarning, MemoryError):
                    box["error"] = ConversionError("too large to read safely")
                except BaseException:  # noqa: BLE001 - Pillow raises OSError, ValueError, SyntaxError, struct.error, EOFError ... for bad files
                    box["error"] = ConversionError("damaged")
            finally:
                _SLOTS.release()

        worker = threading.Thread(target=work, name="linkedeye-image", daemon=True)
        try:
            worker.start()
        except RuntimeError:                                  # cannot start a thread: refuse, do not decode on the request thread
            _SLOTS.release()
            raise ConversionError("busy") from None
        limit = HARD_LIMIT_SECONDS if stop is None else min(HARD_LIMIT_SECONDS, max(0.0, stop - time.monotonic()))
        worker.join(limit)
        if worker.is_alive():
            cancelled.set()
            log.warning("an image conversion did not finish within %.1f s; its worker keeps its slot until it does", limit)
            raise ConversionError("took too long")
        if "error" in box:
            raise box["error"]
        return box["value"]
    return run


def _open(Image, data, fmt, expect=None):
    """Image.open restricted to exactly one format, after the magic bytes were checked. Loads the pixels."""
    kind = sniff(data)
    if (kind, fmt) not in ((JPEG, "JPEG"), (PNG, "PNG")):
        raise ConversionError("not a clean JPEG" if fmt == "JPEG" else "damaged")
    im = Image.open(io.BytesIO(data), formats=[fmt])
    if im.format != fmt or getattr(im, "n_frames", 1) != 1:
        raise ConversionError("damaged")
    if im.width * im.height > MAX_INPUT_PIXELS:
        raise ConversionError("too large to read safely")
    if expect and im.size != expect:
        raise ConversionError("damaged")
    return im


def _draft_scale(w, h, comps):
    """The smallest JPEG decode scale (1, 2, 4, 8) whose output fits the side limit and MAX_WORK_BYTES."""
    for s in (1, 2, 4, 8):
        sw, sh = -(-w // s), -(-h // s)
        if max(sw, sh) <= MAX_DIMENSION and sw * sh * comps <= MAX_WORK_BYTES:
            return s
    return 8


# ------------------------------------------------------------------------------------------ shared tail
def _flat(Image, im):
    hist = im.convert("L").histogram()
    return max(hist) / max(1, sum(hist)) >= 0.99


def _encode(Image, im):
    im.info = {}                                    # nothing from the source (ICC profile, EXIF, comments) is carried over
    buf = io.BytesIO()
    if im.getcolors(maxcolors=FEW_COLOURS) is not None:
        im.save(buf, "PNG", optimize=False, compress_level=6)
        return PNG, buf.getvalue()
    im.save(buf, "JPEG", quality=QUALITY, optimize=False, progressive=False, exif=b"", icc_profile=None)
    return JPEG, buf.getvalue()


def _finish(Image, guard, rgb, mask, cmyk):
    """Downscale to the limits, composite a soft mask onto white, encode, and validate the result."""
    w, h = rgb.size
    scale = min(1.0, MAX_DIMENSION / max(w, h))
    mask_im = None
    if mask is not None:
        mask_im = Image.frombytes("L", (mask.width, mask.height), mask.data)
    reduced = scale < 1.0
    for attempt in range(MAX_ATTEMPTS):
        guard.tick()
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        work = rgb if size == (w, h) else rgb.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)
        if mask_im is not None:
            m = mask_im if mask_im.size == size else mask_im.resize(size, Image.Resampling.BILINEAR)
            base = Image.new("RGB", size, (255, 255, 255))
            base.paste(work, (0, 0), m)
            work = base
        if attempt == 0 and _flat(Image, work):
            raise ConversionError("near-uniform")
        ctype, data = _encode(Image, work)
        if len(data) <= MAX_BYTES:
            break
        reduced = True
        scale *= max(0.5, min(0.85, math.sqrt(MAX_BYTES / len(data)) * 0.92))
    else:
        raise ConversionError("larger than 2 MB")
    guard.tick()
    try:
        info = inspect_image(data)                  # never trust Pillow to have produced what we meant to store
    except ImageError:
        raise ConversionError("damaged") from None
    if info.content_type != ctype:
        raise ConversionError("damaged")
    return Converted(ctype, data, info.width, info.height, cmyk=cmyk, reduced=reduced)


# ------------------------------------------------------------------------------------------ JPEG in
@_guarded
def _convert_jpeg(Image, guard, data, decode_inverted, mask, expect):
    from PIL import ImageChops
    try:
        info = inspect_image(data, max_bytes=MAX_INPUT_BYTES, max_dimension=MAX_INPUT_SIDE)
    except ImageError:
        raise ConversionError("not a clean JPEG") from None
    if info.content_type != JPEG:
        raise ConversionError("not a clean JPEG")
    sof, w, h, comps = jpeg_header(data)
    _check_budget(w, h)
    if sof not in _SOF_OK or comps not in (1, 3, 4):
        raise ConversionError("unsupported encoding")
    if expect and expect != (w, h):
        raise ConversionError("damaged")
    scans = jpeg_scans(data)
    if scans < 1 or scans > MAX_SCANS:                  # every extra scan costs a pass over the whole picture
        raise ConversionError("unsupported encoding")
    if (sof == 0xC2 or scans > 1) and w * h * comps * 2 > MAX_COEFFICIENT_BYTES:
        raise ConversionError("too large to read safely")   # draft mode does not shrink libjpeg's coefficient buffer
    guard.tick()
    im = _open(Image, data, "JPEG", expect=(w, h))
    scale = _draft_scale(w, h, comps)
    if scale > 1:
        im.draft(None, (w // scale, h // scale))
    if im.width * im.height * comps > MAX_WORK_BYTES or max(im.size) > MAX_DIMENSION:     # draft must have shrunk it
        raise ConversionError("too large to read safely")
    im.load()
    guard.tick()
    cmyk = im.mode == "CMYK"
    if cmyk:
        # Pillow assumes the Adobe convention (stored value = 255 - ink) and undoes it. A PDF says what its samples mean with
        # /Decode: [1 0 1 0 1 0 1 0] is that same convention (use Pillow's result as it is), the default means the stored
        # value is the ink amount itself (so Pillow's result must be inverted back).
        if not decode_inverted:
            im = ImageChops.invert(im)
    elif im.mode not in ("L", "RGB"):
        raise ConversionError("unsupported colour space")
    rgb = im.convert("RGB")
    del im
    guard.tick()
    return _finish(Image, guard, rgb, mask, cmyk)


def convert_jpeg(data, *, decode_inverted=False, mask=None, expect=None, stop=None):
    """Converted for a JPEG stream (any colour model, up to the input budget). Raises ConversionError."""
    if not isinstance(data, (bytes, bytearray)):
        raise ConversionError("damaged")
    return _convert_jpeg(bytes(data), decode_inverted, mask, expect, stop=stop)


# ------------------------------------------------------------------------------------------ raw samples in
_CHANNELS = {"L": 1, "RGB": 3, "CMYK": 4, "P": 1}


def _palette(Image, base, lookup):
    """768 bytes of RGB palette from a PDF /Indexed lookup in a Gray, RGB or CMYK base space."""
    ch = {"L": 1, "RGB": 3, "CMYK": 4}[base]
    n = min(len(lookup) // ch, MAX_PALETTE)
    lookup = bytes(lookup[:n * ch])
    if n < 1:
        raise ConversionError("damaged")
    if base == "RGB":
        pal = lookup
    elif base == "L":
        pal = b"".join(bytes((v, v, v)) for v in lookup)
    else:
        pal = Image.frombytes("CMYK", (n, 1), lookup).convert("RGB").tobytes()
    return pal.ljust(768, b"\x00")


@_guarded
def _convert_raw(Image, guard, width, height, samples, kind, palette, decode_inverted, mask):
    from PIL import ImageChops
    _check_budget(width, height)
    ch = _CHANNELS[kind]
    if len(samples) != width * height * ch or len(samples) > MAX_WORK_BYTES:
        raise ConversionError("too large to read safely" if len(samples) > MAX_WORK_BYTES else "damaged")
    if mask is not None and (mask.width * mask.height > MAX_INPUT_PIXELS or len(mask.data) != mask.width * mask.height):
        mask = None
    im = Image.frombytes(kind, (width, height), samples)
    if kind == "P":
        im.putpalette(_palette(Image, palette[0], palette[1]), "RGB")
    elif kind == "CMYK" and decode_inverted:
        im = ImageChops.invert(im)
    guard.tick()
    rgb = im.convert("RGB")
    del im
    return _finish(Image, guard, rgb, mask, kind == "CMYK")


def convert_raw(width, height, samples, *, kind, palette=None, decode_inverted=False, mask=None, stop=None):
    """Converted for interleaved 8-bit samples (kind "L", "RGB", "CMYK" with ink amounts, or "P" with palette=(base, lookup)).
    The caller has already limited the size of `samples`; this checks again. Raises ConversionError."""
    if kind not in _CHANNELS:
        raise ConversionError("unsupported colour space")
    return _convert_raw(width, height, bytes(samples), kind, palette, decode_inverted, mask, stop=stop)


@_guarded
def _raw_from_png(Image, guard, png, width, height, channels):
    try:
        info = inspect_image(png, max_bytes=MAX_INPUT_BYTES, max_dimension=MAX_INPUT_SIDE)
    except ImageError:
        raise ConversionError("damaged") from None
    if info.content_type != PNG or (info.width, info.height) != (width, height):
        raise ConversionError("damaged")
    _check_budget(width, height)
    im = _open(Image, png, "PNG", expect=(width, height))
    im.load()
    if im.mode != {1: "L", 3: "RGB", 4: "RGBA"}[channels]:
        raise ConversionError("unsupported encoding")
    return im.tobytes()


def raw_from_png(png, width, height, channels, *, stop=None):
    """The sample bytes of a PNG that datasheet_pdf wrapped around a PDF image whose predictor rows use real PNG filters
    (Pillow does the un-filtering). 4 channels means CMYK carried in an RGBA PNG. Raises ConversionError."""
    return _raw_from_png(png, width, height, channels, stop=stop)
