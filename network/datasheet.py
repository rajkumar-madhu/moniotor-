"""Find product photos from whatever the operator has: a datasheet PDF file, a product page / datasheet address, or the
address of a picture. Returns ranked candidates (the picker shows them; nothing is attached to a device here).

    find_from_upload(bytes)  -> FindResult      (a PDF, or a PNG / JPEG / WebP picture, from the operator's computer)
    find_from_url(url, model) -> FindResult     (ONE https address: page, PDF or picture; routed by what comes back)

Failures are DatasheetError with a sentence for the operator; `blocked` means the site refuses automated downloads and the
datasheet upload is the way forward."""
from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import datasheet_page, datasheet_pdf
from .datasheet_page import BLOCKED_MESSAGE, DatasheetError
from .imaging import ImageError, inspect_image, sniff

__all__ = ["DatasheetError", "FindResult", "Item", "find_from_upload", "find_from_url"]


@dataclass
class Item:
    data: bytes
    origin: str                  # "datasheet page 1", "og:image", "image address"
    source_url: str = ""         # https address it came from (stored without query string); empty for an upload
    width: int = 0
    height: int = 0
    source: str = "datasheet"    # DeviceImage.source the candidate is stored with: "datasheet", "upload" or "fetched"


@dataclass
class FindResult:
    items: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    kind: str = ""               # "pdf" | "page" | "image"
    source: str = ""             # host or file name shown to the operator


def _from_pdf(data, source_url="", name=""):
    res = datasheet_pdf.extract(data)
    if not res.candidates:
        raise DatasheetError(res.message or "No product pictures were found in that PDF.")
    items = [Item(c.data, c.origin, source_url, c.width, c.height) for c in res.candidates]
    return FindResult(items, list(res.notes), "pdf", name or urlsplit(source_url).hostname or "")


def _from_image(data, source_url="", name="", declared=None):
    try:
        info = inspect_image(data, declared_type=declared)
    except ImageError as e:
        raise DatasheetError(str(e)) from None
    item = Item(data, "image address" if source_url else "uploaded picture", source_url, info.width, info.height,
                source="fetched" if source_url else "upload")
    return FindResult([item], [], "image", name or urlsplit(source_url).hostname or "")


def find_from_upload(data, filename=""):
    """The operator's own file: a datasheet PDF (the main route) or a picture."""
    data = bytes(data)
    # By content, never by name or declared type. A real picture starts with its magic bytes; a PDF may carry its header
    # anywhere in the first kilobyte, so the picture check goes first and a picture that merely mentions "%PDF-" is a picture.
    if sniff(data) is not None:
        return _from_image(data, name=filename)
    if b"%PDF-" in data[:1024]:
        return _from_pdf(data, name=filename)
    raise DatasheetError("That file is not a PDF or a PNG, JPEG or WebP picture. Choose the datasheet PDF you downloaded from the vendor.")


def find_from_url(url, model=""):
    doc = datasheet_page.fetch_document(url)
    if doc.kind == "pdf":
        return _from_pdf(doc.body, source_url=doc.url)
    if doc.kind == "image":
        return _from_image(doc.body, source_url=doc.url, declared=doc.content_type or None)
    text = doc.body.decode("utf-8", errors="replace")
    found = datasheet_page.parse_page(text, doc.url, model)
    if not found:
        if datasheet_page.looks_blocked(text):
            raise DatasheetError(BLOCKED_MESSAGE, blocked=True)
        raise DatasheetError("That page has no usable product pictures (no page image, og:image or product data was found, "
                             "or they were logos and icons). Try the datasheet PDF, or paste the address of the picture itself.")
    got, stats = datasheet_page.download_images(found)
    host = urlsplit(doc.url).hostname or ""
    if not got:
        if stats.refused and stats.refused >= stats.tried - stats.late:
            raise DatasheetError(BLOCKED_MESSAGE, blocked=True)
        raise DatasheetError(f"Found {stats.tried} picture address{'es' if stats.tried != 1 else ''} on the page, but none could be used "
                             f"({_stats_text(stats)}). Download the datasheet PDF yourself and upload it here.")
    # Same score: squarer shots before banner-like crops (a wide strip is rarely the best picture to show on a device card).
    # The sort is stable, so everything else keeps the page ranking.
    got.sort(key=lambda d: (-d.page_image.score, d.width / d.height > datasheet_page.WIDE))
    notes = []
    if model and not any(d.page_image.bonus for d in got):
        notes.append(f"None of these pictures mention {model} by name on the page, so check that they show the right product "
                     "before you choose one.")
    if stats.tried > len(got):
        notes.append(f"{len(got)} of {stats.tried} pictures on the page could be used ({_stats_text(stats)}).")
    items = [Item(d.data, d.page_image.label, d.page_image.url, d.width, d.height, source="fetched") for d in got]
    return FindResult(items, notes, "page", host)


def _stats_text(s):
    parts = []
    if s.refused:
        parts.append(f"{s.refused} refused by the site")
    if s.failed:
        parts.append(f"{s.failed} could not be downloaded")
    if s.late:
        parts.append(f"{s.late} timed out")
    if s.unusable:
        parts.append(f"{s.unusable} too small, banner shaped or not a PNG, JPEG or WebP")
    if s.duplicates:
        parts.append(f"{s.duplicates} duplicates")
    return ", ".join(parts) or "none usable"
