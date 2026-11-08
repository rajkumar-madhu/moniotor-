"""Product catalog CSV import and export.

Import columns (header row required, any order, extra columns ignored):
    vendor, device_type, model_key, image_url, product_page_url
Every row's image_url is downloaded once through the safe fetcher (network.images.download: https only, public
addresses only, size and time limited, bytes validated) and the bytes are stored; the URL is not kept as a link.
A dry run does the same downloads and validation but writes nothing."""
import csv
import hashlib
import io
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction

from . import images
from .imaging import ImageError
from .models import DeviceType, NetworkProductImage

COLUMNS = ["vendor", "device_type", "model_key", "image_url", "product_page_url"]
EXPORT_COLUMNS = COLUMNS + ["notes", "image_sha256", "image_size"]
MAX_ROWS = 50                 # rows per import (every row is a download)
MAX_CSV_BYTES = 256 * 1024
TIME_BUDGET = 40              # seconds for all downloads; the web worker times out at 60
WORKERS = 4
TYPE_BY_LABEL = {label.lower(): value for value, label in DeviceType.choices}
CSV_FORMULA_CHARS = ("=", "+", "-", "@", "\t", "\r")
_page_validator = URLValidator(schemes=["http", "https"])


@dataclass
class RowResult:
    line: int
    vendor: str = ""
    device_type: str = ""
    model_key: str = ""
    status: str = "error"          # created | updated | unchanged | error | skipped
    message: str = ""
    detail: str = ""               # e.g. "PNG 800×600, 53 KB"

    @property
    def tone(self):
        return {"created": "ok", "updated": "ok", "unchanged": "mute", "skipped": "warn"}.get(self.status, "crit")


def csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.startswith(CSV_FORMULA_CHARS):
        return "'" + value
    return value


def parse_csv(text):
    """(rows, problem): rows is a list of (line number, {column: value}); problem is a sentence when the file cannot be used."""
    if len(text.encode("utf-8", "ignore")) > MAX_CSV_BYTES:
        return [], f"The CSV is larger than {MAX_CSV_BYTES // 1024} KB."
    text = text.lstrip("﻿")
    try:
        sample = text[:2048]
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample.strip() else csv.excel
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = [h.strip().lower().replace(" ", "_") for h in next(reader)]
    except StopIteration:
        return [], "The CSV is empty."
    except csv.Error as e:
        return [], f"The CSV could not be read: {e}"
    missing = [c for c in ("vendor", "image_url") if c not in header]
    if missing:
        return [], "The header row must have the columns " + ", ".join(COLUMNS) + f" (missing: {', '.join(missing)})."
    rows = []
    try:
        for record in reader:
            if not any(cell.strip() for cell in record):
                continue
            rows.append((reader.line_num, {h: (record[i].strip() if i < len(record) else "") for i, h in enumerate(header)}))
    except csv.Error as e:
        return [], f"The CSV could not be read: {e}"
    if not rows:
        return [], "The CSV has a header but no rows."
    if len(rows) > MAX_ROWS:
        return [], f"The CSV has {len(rows)} rows; import at most {MAX_ROWS} at a time (each row downloads a photo)."
    return rows, ""


def _normalise(line, raw):
    """(RowResult skeleton, clean values) – the result already carries the error when a value is unusable."""
    res = RowResult(line=line, vendor=raw.get("vendor", "")[:100], model_key=raw.get("model_key", "")[:120])
    vendor, model_key = raw.get("vendor", "").strip(), raw.get("model_key", "").strip()
    dtype_raw = raw.get("device_type", "").strip()
    dtype = dtype_raw if dtype_raw in DeviceType.values else TYPE_BY_LABEL.get(dtype_raw.lower(), "")
    res.device_type = dtype
    page = raw.get("product_page_url", "").strip()
    if not vendor or len(vendor) > 100:
        res.message = "vendor is required (100 characters at most)."
    elif len(model_key) > 120:
        res.message = "model_key is longer than 120 characters."
    elif dtype_raw and not dtype:
        res.message = f"device_type “{dtype_raw[:30]}” is not one of: " + ", ".join(DeviceType.values) + " (or empty)."
    elif not raw.get("image_url", "").strip():
        res.message = "image_url is required: every catalog row needs a photo."
    elif page:
        try:
            if len(page) > 500:
                raise ValidationError("long")
            _page_validator(page)
        except ValidationError:
            res.message = "product_page_url is not a valid http(s) address."
    return res, {"vendor": vendor, "device_type": dtype, "model_key": model_key, "image_url": raw.get("image_url", "").strip(),
                 "product_page_url": page}


def _find(tenant_key, v):
    return NetworkProductImage.objects.filter(tenant_key=tenant_key, vendor__iexact=v["vendor"], device_type=v["device_type"],
                                              model_key__iexact=v["model_key"]).select_related("image").first()


def _describe(info, size):
    return f"{info.ext.upper()} {info.width}×{info.height}, {size / 1024:.0f} KB"


def run_import(rows, user, dry_run=True, tenant_key=None):
    """Process parsed rows; returns a list of RowResult in file order. With dry_run nothing is written (the photos are still
    downloaded and validated, so the report is what a real run would do)."""
    t = tenant_key or settings.LINKEDEYE_TENANT
    if len(rows) > MAX_ROWS:      # parse_csv enforces this too; every row is a download, so a direct caller gets the same cap
        raise ValueError(f"at most {MAX_ROWS} rows can be imported at a time")
    results, todo, seen = [], [], {}
    for line, raw in rows:
        res, v = _normalise(line, raw)
        results.append(res)
        if res.message:
            continue
        key = (v["vendor"].lower(), v["device_type"], v["model_key"].lower())
        if key in seen:
            res.message = f"duplicate of the row on line {seen[key]}; only one photo per vendor, type and model."
            continue
        seen[key] = line
        todo.append((res, v))

    # one download per distinct address, in parallel, inside the time budget
    urls = sorted({v["image_url"] for _r, v in todo})
    fetched = {}
    if urls:
        pool = ThreadPoolExecutor(max_workers=WORKERS)
        futures = {pool.submit(images.download, u): u for u in urls}
        done, _pending = wait(futures, timeout=TIME_BUDGET)
        pool.shutdown(wait=False, cancel_futures=True)
        for fut, u in futures.items():
            if fut in done:
                try:
                    fetched[u] = fut.result()
                except ImageError as e:
                    fetched[u] = e
                except Exception as e:  # noqa: BLE001 - never let one bad row take the report down
                    fetched[u] = ImageError(f"unexpected error: {type(e).__name__}")
    for res, v in todo:
        got = fetched.get(v["image_url"])
        if got is None:
            res.status, res.message = "skipped", f"not downloaded within the {TIME_BUDGET} s time budget; import this row again."
            continue
        if isinstance(got, ImageError):
            res.message = str(got)
            continue
        data, info = got
        res.detail = _describe(info, len(data))
        digest = hashlib.sha256(data).hexdigest()
        existing = _find(t, v)
        same_image = bool(existing and existing.image_id and existing.image.sha256 == digest)
        same_page = bool(existing and ((existing.product_page_url or "") == v["product_page_url"] or not v["product_page_url"]))
        if existing and same_image and same_page:
            res.status, res.message = "unchanged", "already in the catalog with this photo."
            continue
        if dry_run:
            res.status = "updated" if existing else "created"
            res.message = ("would replace the photo of an existing row." if existing else "would add a new row.")
            continue
        try:
            with transaction.atomic():
                image, _created = images.store_image(data, source="fetched", source_url=v["image_url"], user=user, tenant_key=t)
                if existing:
                    old = existing.image
                    existing.image, existing.product_image_url = image, ""
                    if v["product_page_url"]:
                        existing.product_page_url = v["product_page_url"]
                    existing.save()
                    if old is not None and old.pk != image.pk:
                        images.release(old)
                    res.status, res.message = "updated", "photo replaced."
                else:
                    NetworkProductImage.objects.create(tenant_key=t, vendor=v["vendor"], device_type=v["device_type"],
                                                       model_key=v["model_key"], image=image,
                                                       product_page_url=v["product_page_url"])
                    res.status, res.message = "created", "added."
        except ImageError as e:
            res.status, res.message = "error", str(e)
    return results


def summary(results):
    counts = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0, "error": 0}
    for r in results:
        counts[r.status] += 1
    return counts


def export_rows(tenant_key=None):
    t = tenant_key or settings.LINKEDEYE_TENANT
    for r in NetworkProductImage.objects.filter(tenant_key=t).select_related("image").defer("image__data"):
        yield [r.vendor, r.device_type, r.model_key, (r.image.source_url if r.image_id else "") or r.product_image_url,
               r.product_page_url, r.notes, r.image.sha256 if r.image_id else "", r.image.byte_size if r.image_id else ""]
