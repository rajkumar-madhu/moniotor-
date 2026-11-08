"""Readers for the inventory workbook.

One source can be any of:

* an ``.xlsx`` path or file object (openpyxl — imported lazily, so the rest of the app works without it),
* a directory of ``.csv`` files whose base names are the sheet names,
* a ``.zip`` of those CSV files (what the upload page accepts alongside the workbook).

Every reader yields the same thing: ``SheetData(name, columns, rows)`` where each row is a dict of
``column -> text`` already passed through :func:`k8s.parsing.clean`. A broken or unreadable source raises
:class:`SourceError`, which the importer reports instead of letting it escape.
"""
import csv
import hashlib
import io
import os
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .parsing import clean

XLSX_SUFFIXES = (".xlsx", ".xlsm")
CSV_SUFFIX = ".csv"
ZIP_SUFFIX = ".zip"

# Sheets that are never data: the dashboard is a layout sheet and CL_* sheets repeat other sheets per client.
IGNORED_PREFIXES = ("CL_",)
IGNORED_SHEETS = {"01_Executive_Dashboard"}

MAX_ROWS_PER_SHEET = 100_000
MAX_COLUMNS = 80
MAX_CELL_CHARS = 4000
MAX_MEMBER_BYTES = 64 * 1024 * 1024   # per CSV inside a zip, before decompression is trusted
MAX_ZIP_MEMBERS = 200
MAX_TOTAL_BYTES = 100 * 1024 * 1024   # everything an archive may expand to (a zip of CSVs, or an .xlsx package)
MAX_XLSX_MEMBERS = 2000
MAX_SCANNED_ROWS = 2 * MAX_ROWS_PER_SHEET   # rows read from a sheet, blank ones included
MAX_CELLS_PER_SHEET = 2_000_000             # rows x columns one sheet may hold, so memory stays bounded
MAX_CELLS_PER_SOURCE = 5_000_000            # ... and everything one upload may hold
_XML_PROLOG_BYTES = 256 * 1024


class SourceError(Exception):
    """The source cannot be read at all (not a workbook, no sheets, unreadable archive…)."""


@dataclass
class SheetData:
    name: str
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    # True when the reader stopped at MAX_ROWS_PER_SHEET / MAX_SCANNED_ROWS with data still to come: the rows
    # held are NOT the whole sheet, so the importer must not treat them as a complete snapshot.
    truncated: bool = False

    def __len__(self):
        return len(self.rows)


@dataclass
class Source:
    """A readable snapshot: its display name, a content hash and the data sheets it holds."""
    name: str
    sha256: str
    sheets: dict = field(default_factory=dict)
    ignored: list = field(default_factory=list)
    unread: list = field(default_factory=list)   # sheets present but not parsed because nothing imports them

    def get(self, sheet_name):
        return self.sheets.get(sheet_name)


def is_ignored(sheet_name):
    return sheet_name in IGNORED_SHEETS or sheet_name.startswith(IGNORED_PREFIXES)


# ----------------------------------------------------------------------------- helpers
def _headers(raw_header):
    """Column names, de-duplicated, trimmed to MAX_COLUMNS."""
    names, seen = [], {}
    for index, cell in enumerate(raw_header[:MAX_COLUMNS]):
        name = clean(cell) or f"column_{index + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 1
        names.append(name)
    return names


def _row(columns, cells):
    row, any_value = {}, False
    for index, name in enumerate(columns):
        value = clean(cells[index]) if index < len(cells) else ""
        if len(value) > MAX_CELL_CHARS:
            value = value[:MAX_CELL_CHARS]
        row[name] = value
        if value:
            any_value = True
    return row if any_value else None


class _Budget:
    """Cells a whole source may hold (rows x columns summed over its sheets)."""

    def __init__(self, limit=None):
        self.remaining = MAX_CELLS_PER_SOURCE if limit is None else limit

    def spend(self, name, cells):
        self.remaining -= cells
        if self.remaining < 0:
            raise SourceError(f"{name}: the file holds more than {MAX_CELLS_PER_SOURCE:,} cells in total, "
                              f"which is more than a real inventory needs - it was not read.")


def _sheet_from_rows(name, rows_iter, budget=None):
    """Build a SheetData from an iterable of cell lists; the first non-empty row is the header.

    Stops at MAX_ROWS_PER_SHEET data rows / MAX_SCANNED_ROWS scanned rows. When data was still waiting past
    the cut-off the sheet is flagged ``truncated`` so nothing downstream mistakes it for the whole sheet.
    """
    columns, rows, scanned, truncated, spent = None, [], 0, False, 0
    for cells in rows_iter:
        scanned += 1
        if scanned > MAX_SCANNED_ROWS:
            truncated = any(clean(c) for c in list(cells)[:MAX_COLUMNS])
            break
        if columns is None:
            if not any(clean(c) for c in cells):
                continue
            columns = _headers(list(cells))
            continue
        row = _row(columns, list(cells))
        if row is not None:
            if len(rows) >= MAX_ROWS_PER_SHEET:
                truncated = True
                break
            rows.append(row)
            spent += len(columns)
            if spent > MAX_CELLS_PER_SHEET:
                raise SourceError(f"{name}: the sheet holds more than {MAX_CELLS_PER_SHEET:,} cells "
                                  f"(rows x columns), which is more than a real inventory needs - it was not read.")
    if budget is not None:
        budget.spend(name, spent)
    return SheetData(name=name, columns=columns or [], rows=rows, truncated=truncated)


def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_parts(parts):
    """Stable hash of a set of named blobs, so a CSV directory has a reproducible fingerprint."""
    digest = hashlib.sha256()
    for name, blob_digest in sorted(parts):
        digest.update(name.encode("utf-8") + b"\0" + blob_digest.encode("ascii") + b"\0")
    return digest.hexdigest()


# ----------------------------------------------------------------------------- xlsx
def _check_package(handle):
    """Refuse an .xlsx that is not a plain, modest zip package before openpyxl parses a byte of it.

    * it must be a zip with a sane number of parts whose declared sizes add up to a bounded total, so a
      small upload cannot expand into gigabytes (zip bomb);
    * no XML part may carry a DOCTYPE or ENTITY declaration — a genuine workbook never does, and
      refusing them rules out entity-expansion tricks whichever XML parser openpyxl ends up using.
    """
    try:
        with zipfile.ZipFile(handle) as package:
            infos = [i for i in package.infolist() if not i.is_dir()]
            if len(infos) > MAX_XLSX_MEMBERS:
                raise SourceError("The workbook package holds too many parts to be a real workbook.")
            if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
                raise SourceError(f"The workbook expands to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
            for info in infos:
                if not info.filename.lower().endswith((".xml", ".rels")):
                    continue
                with package.open(info) as part:
                    head = part.read(_XML_PROLOG_BYTES).upper()
                if b"<!DOCTYPE" in head or b"<!ENTITY" in head:
                    raise SourceError("The workbook contains an XML DOCTYPE or ENTITY declaration, "
                                      "which a real workbook never has — it was not read.")
    except zipfile.BadZipFile as exc:
        raise SourceError("The file is not a valid .xlsx workbook (it is not a zip package).") from exc
    finally:
        if hasattr(handle, "seek"):
            handle.seek(0)


def _load_openpyxl():
    try:
        import openpyxl  # noqa: PLC0415 - optional dependency, imported only when a workbook is read
    except ImportError as exc:  # pragma: no cover - exercised by the "openpyxl missing" message
        raise SourceError("Reading .xlsx workbooks needs the openpyxl package "
                          "(pip install 'openpyxl>=3.1,<4'). A directory or zip of CSV sheets works "
                          "without it.") from exc
    return openpyxl


def read_xlsx(source, name=None, only=None):
    openpyxl = _load_openpyxl()
    if hasattr(source, "read"):
        blob = source.read()
        sha = _sha256_bytes(blob)
        handle = io.BytesIO(blob)
        display = name or getattr(source, "name", "") or "workbook.xlsx"
    else:
        path = Path(source)
        sha = _sha256_file(path)
        handle = str(path)
        display = name or path.name
    _check_package(handle)
    try:
        book = openpyxl.load_workbook(handle, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many shapes for a broken file
        raise SourceError(f"The file could not be read as an .xlsx workbook ({type(exc).__name__}).") from exc
    sheets, ignored, unread, budget = {}, [], [], _Budget()
    try:
        for sheet_name in book.sheetnames:
            if is_ignored(sheet_name):
                ignored.append(sheet_name)
                continue
            if only is not None and sheet_name not in only:
                unread.append(sheet_name)
                continue
            worksheet = book[sheet_name]
            # bounded on both axes: a sheet that declares A1:XFD1048576 must not be padded to that size
            rows_iter = worksheet.iter_rows(max_row=MAX_SCANNED_ROWS + 1, max_col=MAX_COLUMNS, values_only=True)
            sheets[sheet_name] = _sheet_from_rows(sheet_name, rows_iter, budget)
    finally:
        book.close()
    if not sheets and not unread:
        raise SourceError("The workbook holds no data sheets.")
    return Source(name=display, sha256=sha, sheets=sheets, ignored=ignored, unread=unread)


# ----------------------------------------------------------------------------- csv
def _read_csv_text(text, sheet_name, budget=None):
    try:
        reader = csv.reader(io.StringIO(text, newline=""))
        return _sheet_from_rows(sheet_name, reader, budget)
    except csv.Error as exc:
        raise SourceError(f"{sheet_name}: the CSV file could not be read ({exc}).") from exc


def _decode(blob, sheet_name):
    for encoding in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise SourceError(f"{sheet_name}: the file is not text in UTF-8 or Windows-1252.")


def read_csv_dir(directory, name=None, only=None):
    path = Path(directory)
    files = sorted(p for p in path.iterdir() if p.is_file() and p.suffix.lower() == CSV_SUFFIX)
    if not files:
        raise SourceError(f"No .csv sheet files found in {path.name}.")
    sheets, ignored, unread, parts, budget = {}, [], [], [], _Budget()
    for file_path in files:
        sheet_name = file_path.stem
        if is_ignored(sheet_name):
            ignored.append(sheet_name)
            continue
        blob = file_path.read_bytes()
        parts.append((sheet_name, _sha256_bytes(blob)))
        if only is not None and sheet_name not in only:
            unread.append(sheet_name)
            continue
        sheets[sheet_name] = _read_csv_text(_decode(blob, sheet_name), sheet_name, budget)
    if not sheets and not unread:
        raise SourceError(f"No usable sheet files found in {path.name}.")
    return Source(name=name or path.name, sha256=_sha256_parts(parts), sheets=sheets, ignored=ignored,
                  unread=unread)


def read_zip(source, name=None, only=None):
    if hasattr(source, "read"):
        blob = source.read()
        sha = _sha256_bytes(blob)
        handle = io.BytesIO(blob)
        display = name or getattr(source, "name", "") or "sheets.zip"
    else:
        path = Path(source)
        sha = _sha256_file(path)
        handle = str(path)
        display = name or path.name
    sheets, ignored, unread, budget = {}, [], [], _Budget()
    try:
        with zipfile.ZipFile(handle) as archive:
            members = [i for i in archive.infolist() if not i.is_dir()]
            if len(members) > MAX_ZIP_MEMBERS:
                raise SourceError(f"The archive holds more than {MAX_ZIP_MEMBERS} files.")
            if sum(i.file_size for i in members if i.filename.lower().endswith(CSV_SUFFIX)) > MAX_TOTAL_BYTES:
                raise SourceError(f"The archive expands to more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB.")
            for info in members:
                member_name = os.path.basename(info.filename)
                if not member_name.lower().endswith(CSV_SUFFIX):
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise SourceError(f"{member_name} is larger than "
                                      f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB uncompressed.")
                sheet_name = member_name[: -len(CSV_SUFFIX)]
                if is_ignored(sheet_name):
                    ignored.append(sheet_name)
                    continue
                if only is not None and sheet_name not in only:
                    unread.append(sheet_name)
                    continue
                with archive.open(info) as fh:
                    blob = fh.read(MAX_MEMBER_BYTES + 1)
                if len(blob) > MAX_MEMBER_BYTES:
                    raise SourceError(f"{member_name} is larger than "
                                      f"{MAX_MEMBER_BYTES // (1024 * 1024)} MB uncompressed.")
                sheets[sheet_name] = _read_csv_text(_decode(blob, sheet_name), sheet_name, budget)
    except zipfile.BadZipFile as exc:
        raise SourceError("The file is not a readable .zip archive.") from exc
    if not sheets and not unread:
        raise SourceError("The archive holds no .csv sheet files.")
    return Source(name=display, sha256=sha, sheets=sheets, ignored=ignored, unread=unread)


# ----------------------------------------------------------------------------- entry point
def _single_csv(blob, sheet_name, only):
    if only is not None and sheet_name not in only:
        return {}, [sheet_name]
    return {sheet_name: _read_csv_text(_decode(blob, sheet_name), sheet_name, _Budget())}, []


def open_source(source, name=None, only=None):
    """Read `source` (path, directory, uploaded file or file object) into a :class:`Source`.

    `only`, when given, is the set of sheet names somebody imports: every other sheet is listed in
    ``Source.unread`` but never parsed, so a file full of sheets nobody asked for costs nothing.

    Raises :class:`SourceError` with a message meant for the person who supplied the file.
    """
    if isinstance(source, Source):
        return source
    if hasattr(source, "read"):
        filename = (name or getattr(source, "name", "") or "").lower()
        if filename.endswith(ZIP_SUFFIX):
            return read_zip(source, name=name, only=only)
        if filename.endswith(CSV_SUFFIX):
            blob = source.read()
            sheet_name = Path(name or getattr(source, "name", "sheet.csv")).stem
            sheets, unread = _single_csv(blob, sheet_name, only)
            return Source(name=name or sheet_name, sha256=_sha256_bytes(blob), sheets=sheets, unread=unread)
        return read_xlsx(source, name=name, only=only)
    path = Path(source)
    if path.is_dir():
        return read_csv_dir(path, name=name, only=only)
    if not path.exists():
        raise SourceError(f"{path} does not exist.")
    suffix = path.suffix.lower()
    if suffix in XLSX_SUFFIXES:
        return read_xlsx(path, name=name, only=only)
    if suffix == ZIP_SUFFIX:
        return read_zip(path, name=name, only=only)
    if suffix == CSV_SUFFIX:
        blob = path.read_bytes()
        # the staged upload is saved under a random token: the sheet is named by the file the person chose
        sheet_name = Path(name).stem if name else path.stem
        sheets, unread = _single_csv(blob, sheet_name, only)
        return Source(name=name or path.name, sha256=_sha256_bytes(blob), sheets=sheets, unread=unread)
    raise SourceError(f"{path.name}: expected an .xlsx workbook, a .zip of CSV sheets, or a directory of them.")
