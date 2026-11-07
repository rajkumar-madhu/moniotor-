"""Strict validation of uploaded or downloaded product photos (PNG, JPEG, WebP) without an imaging library.

The bytes are untrusted. Nothing here trusts a file name, a declared content type or a server header: the type is
decided from the magic bytes, the dimensions come from the header the decoder will read, and the whole container is
walked so a file that is a valid image *and* something else (HTML, script or an archive appended after the image
data, a second image, a chunk that overruns the file) is refused as a polyglot. SVG, GIF, BMP, TIFF, HTML and every
other format are refused. Animated WebP is refused.

inspect_image(data) -> ImageInfo or raises ImageError with a message that is safe to show to the user."""
import struct
import zlib
from dataclasses import dataclass

MAX_BYTES = 2 * 1024 * 1024      # stored size limit
MAX_DIMENSION = 4096             # width and height, pixels
MAX_PNG_CHUNKS = 5000
MAX_JPEG_SEGMENTS = 5000

PNG, JPEG, WEBP = "image/png", "image/jpeg", "image/webp"
EXTENSIONS = {PNG: "png", JPEG: "jpg", WEBP: "webp"}
CONTENT_TYPES = {"png": PNG, "jpg": JPEG, "webp": WEBP}
# file-name extensions people use for the same formats
EXT_ALIASES = {"png": PNG, "jpg": JPEG, "jpeg": JPEG, "jfif": JPEG, "webp": WEBP}
# types a server or a browser may legitimately declare for these bytes; anything else is refused
GENERIC_TYPES = {"application/octet-stream", "binary/octet-stream", ""}
DECLARED_TYPES = {PNG: {PNG, "image/x-png"}, JPEG: {JPEG, "image/jpg", "image/pjpeg"}, WEBP: {WEBP}}


class ImageError(ValueError):
    """The bytes are not an acceptable image. str(e) is a sentence for the user."""


@dataclass(frozen=True)
class ImageInfo:
    content_type: str
    width: int
    height: int

    @property
    def ext(self):
        return EXTENSIONS[self.content_type]


def _dims(width, height):
    if width < 1 or height < 1:
        raise ImageError("The image has no pixels (width or height is zero).")
    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ImageError(f"The image is {width}×{height} px; the limit is {MAX_DIMENSION}×{MAX_DIMENSION} px.")


# ------------------------------------------------------------------ PNG
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_PNG_DEPTHS = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}


def _png(data):
    pos, n, chunks, seen_idat = 8, len(data), 0, False
    width = height = None
    while True:
        if pos + 12 > n:
            raise ImageError("The PNG is truncated.")
        length, ctype = struct.unpack(">I4s", data[pos:pos + 8])
        end = pos + 12 + length
        if end > n:
            raise ImageError("The PNG is truncated (a chunk runs past the end of the file).")
        if not ctype.isalpha():
            raise ImageError("The PNG has a damaged chunk header.")
        body = data[pos + 8:pos + 8 + length]
        if zlib.crc32(ctype + body) & 0xFFFFFFFF != struct.unpack(">I", data[end - 4:end])[0]:
            raise ImageError("The PNG is damaged (a chunk checksum does not match).")
        chunks += 1
        if chunks > MAX_PNG_CHUNKS:
            raise ImageError("The PNG has too many chunks.")
        if chunks == 1:
            if ctype != b"IHDR" or length != 13:
                raise ImageError("The PNG does not start with a valid header chunk.")
            width, height, depth, color, comp, filt, interlace = struct.unpack(">IIBBBBB", body)
            if depth not in _PNG_DEPTHS.get(color, ()) or comp != 0 or filt != 0 or interlace not in (0, 1):
                raise ImageError("The PNG header is not valid.")
            _dims(width, height)
        elif ctype == b"IHDR":
            raise ImageError("The PNG has a second header chunk.")
        elif ctype == b"IDAT":
            seen_idat = True
        elif ctype == b"IEND":
            if length != 0 or not seen_idat:
                raise ImageError("The PNG has no image data.")
            if end != n:
                raise ImageError("The file has extra data after the end of the PNG. Files that are an image plus something else are refused.")
            return width, height
        pos = end


# ------------------------------------------------------------------ JPEG
# start-of-frame markers that carry the dimensions (all but DHT C4, JPG C8 and DAC CC)
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_NO_LENGTH = set(range(0xD0, 0xD8)) | {0x01, 0xD8}


def _jpeg(data):
    n, pos, segments = len(data), 2, 0
    width = height = None
    while True:
        # skip fill bytes, then expect a marker
        while pos < n and data[pos] == 0xFF and pos + 1 < n and data[pos + 1] == 0xFF:
            pos += 1
        if pos + 2 > n or data[pos] != 0xFF:
            raise ImageError("The JPEG is damaged (a marker is missing).")
        marker = data[pos + 1]
        pos += 2
        segments += 1
        if segments > MAX_JPEG_SEGMENTS:
            raise ImageError("The JPEG has too many segments.")
        if marker == 0xD9:                                   # EOI
            if width is None:
                raise ImageError("The JPEG has no image data.")
            if data[pos:].strip(b"\x00 \t\r\n"):
                raise ImageError("The file has extra data after the end of the JPEG. Files that are an image plus something else are refused.")
            return width, height
        if marker == 0x00 or marker in _NO_LENGTH:
            if marker == 0xD8:
                raise ImageError("The JPEG contains a second image.")
            continue
        if pos + 2 > n:
            raise ImageError("The JPEG is truncated.")
        length = struct.unpack(">H", data[pos:pos + 2])[0]
        if length < 2 or pos + length > n:
            raise ImageError("The JPEG is truncated (a segment runs past the end of the file).")
        if marker in _SOF:
            if width is not None:
                raise ImageError("The JPEG has more than one frame header.")
            if length < 8:
                raise ImageError("The JPEG frame header is damaged.")
            precision, height, width, comps = struct.unpack(">BHHB", data[pos + 2:pos + 8])
            if precision != 8 or comps not in (1, 3, 4) or length != 8 + 3 * comps:
                raise ImageError("The JPEG uses an unsupported or damaged frame header (8-bit images only).")
            _dims(width, height)
        pos += length
        if marker == 0xDA:                                   # SOS: entropy-coded data up to the next real marker
            if width is None:
                raise ImageError("The JPEG has image data before its frame header.")
            while True:
                pos = data.find(b"\xff", pos)
                if pos < 0 or pos + 1 >= n:
                    raise ImageError("The JPEG is truncated (no end marker).")
                nxt = data[pos + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7 or nxt == 0xFF:
                    pos += 1 if nxt == 0xFF else 2
                    continue
                break


# ------------------------------------------------------------------ WebP
def _webp(data):
    n = len(data)
    if n < 20 or data[8:12] != b"WEBP":
        raise ImageError("The WebP header is damaged.")
    riff = struct.unpack("<I", data[4:8])[0]
    if riff + 8 != n:                                        # the size excludes the "RIFF" tag and the size field itself
        raise ImageError("The WebP size does not match the file (truncated, or data appended after the image).")
    pos, first, size = 12, True, riff + 8
    width = height = None
    while pos < size:
        if pos + 8 > n:
            raise ImageError("The WebP is truncated.")
        tag, length = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + length]
        if len(body) != length:
            raise ImageError("The WebP is truncated (a chunk runs past the end of the file).")
        if first:
            if tag == b"VP8 ":
                if length < 10 or body[3:6] != b"\x9d\x01\x2a":
                    raise ImageError("The WebP (lossy) header is damaged.")
                width, height = struct.unpack("<HH", body[6:10])
                width, height = width & 0x3FFF, height & 0x3FFF
            elif tag == b"VP8L":
                if length < 5 or body[0] != 0x2F:
                    raise ImageError("The WebP (lossless) header is damaged.")
                bits = struct.unpack("<I", body[1:5])[0]
                width, height = (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
            elif tag == b"VP8X":
                if length < 10:
                    raise ImageError("The WebP extended header is damaged.")
                if body[0] & 0x02:
                    raise ImageError("Animated WebP images are not accepted. Use a still image.")
                width = int.from_bytes(body[4:7], "little") + 1
                height = int.from_bytes(body[7:10], "little") + 1
            else:
                raise ImageError("The WebP does not start with an image header.")
            _dims(width, height)
            first = False
        elif tag == b"ANIM" or tag == b"ANMF":
            raise ImageError("Animated WebP images are not accepted. Use a still image.")
        pos += 8 + length + (length & 1)
    if first:
        raise ImageError("The WebP has no image data.")
    return width, height


# ------------------------------------------------------------------ entry point
def sniff(data):
    """Content type from the magic bytes, or None."""
    if data[:8] == _PNG_SIG:
        return PNG
    if data[:3] == b"\xff\xd8\xff":
        return JPEG
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return WEBP
    return None


def _refusal(data):
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<"):                                # <svg, <?xml, <!doctype, <html, <script ...
        return "SVG and HTML files are not accepted: they can carry scripts. Upload a PNG, JPEG or WebP photo."
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF files are not accepted. Upload a PNG, JPEG or WebP photo."
    return "This is not a PNG, JPEG or WebP image."


def inspect_image(data, declared_type=None, filename=None):
    """Validate `data` and return ImageInfo. `declared_type` (a Content-Type) and `filename` are only compared with
    what the bytes are: a mismatch is refused, an agreement proves nothing."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ImageError("No image data.")
    data = bytes(data)
    if not data:
        raise ImageError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ImageError(f"The file is {len(data) / 1024 / 1024:.1f} MB; the limit is {MAX_BYTES // 1024 // 1024} MB.")
    kind = sniff(data)
    if kind is None:
        raise ImageError(_refusal(data))
    if declared_type is not None:
        declared = declared_type.split(";")[0].strip().lower()
        if declared not in GENERIC_TYPES and declared not in DECLARED_TYPES[kind]:
            raise ImageError(f"The file says it is {declared} but its contents are {kind}.")
    if filename and "." in filename:
        ext = filename.rsplit(".", 1)[1].strip().lower()
        if ext in EXT_ALIASES and EXT_ALIASES[ext] != kind:
            raise ImageError(f"The file name ends in .{ext} but its contents are {kind}.")
    width, height = {PNG: _png, JPEG: _jpeg, WEBP: _webp}[kind](data)
    return ImageInfo(kind, width, height)
