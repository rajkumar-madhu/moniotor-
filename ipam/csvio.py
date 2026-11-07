"""CSV export (formula-safe) and the address CSV import."""
import csv
import io

from django.db import transaction
from django.http import HttpResponse

from . import dns, services
from .forms import address_error, clean_vrf
from .models import AddressStatus, IPAddress, Source

IMPORT_COLUMNS = ["address", "vrf", "status", "hostname", "description", "device", "ci"]
MAX_ROWS = 5000
MAX_ERRORS = 50


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def csv_safe(value):
    """Neutralise spreadsheet formulas (CSV injection): text starting with = + - @ or a control character is
    prefixed with a quote; plain numbers stay numbers."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in "=+-@\t\r" and not _is_number(s):
        return "'" + s
    return s


def response(filename, header, rows):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return resp


# ------------------------------------------------------------------ import
class ImportResult:
    def __init__(self):
        self.errors = []      # (line, message)
        self.created = []
        self.updated = []
        self.rows = 0

    @property
    def ok(self):
        return not self.errors


def _decode(upload):
    raw = upload.read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def import_addresses(upload, user, update_existing=False):
    """Validate every row first; import only when the whole file is valid (all or nothing)."""
    res = ImportResult()
    text = _decode(upload)
    if text is None:
        res.errors.append((0, "The file is not UTF-8 text."))
        return res
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        res.errors.append((0, "The file is empty."))
        return res
    header = {(h or "").strip().lower(): h for h in reader.fieldnames}
    if "address" not in header:
        res.errors.append((1, "The header row needs an “address” column (optional: " + ", ".join(IMPORT_COLUMNS[1:]) + ")."))
        return res
    unknown = [h for h in header if h and h not in IMPORT_COLUMNS]
    if unknown:
        res.errors.append((1, f"Unknown column(s): {', '.join(sorted(unknown))[:200]}."))
        return res

    t = services.tenant()
    devices = services.devices_for(user)
    cis = services.cis_for(user)
    device_by_name = {d.name.lower(): d for d in devices} if devices is not None else {}
    ci_by_name = {c.name.lower(): c for c in cis} if cis is not None else {}
    statuses = set(AddressStatus.values)
    seen = set()
    plan = []

    def err(line, msg):
        if len(res.errors) < MAX_ERRORS:
            res.errors.append((line, msg))

    for i, raw in enumerate(reader, start=2):
        row = {k: (raw.get(orig) or "").strip() for k, orig in header.items() if k}
        if not any(row.values()):
            continue
        res.rows += 1
        if res.rows > MAX_ROWS:
            err(i, f"More than {MAX_ROWS:,} rows: split the file.")
            break
        try:
            ip = services.parse_address(row.get("address"))
        except ValueError as e:
            err(i, str(e))
            continue
        try:
            vrf = clean_vrf(row.get("vrf"))
        except Exception as e:  # forms.ValidationError
            err(i, "; ".join(getattr(e, "messages", [str(e)])))
            continue
        if (vrf, str(ip)) in seen:
            err(i, f"{ip} appears more than once in the file.")
            continue
        seen.add((vrf, str(ip)))
        status = (row.get("status") or "").lower()
        if status and status not in statuses:
            err(i, f"Unknown status “{status[:30]}” (use {', '.join(sorted(statuses))}).")
            continue
        hostname = row.get("hostname") or ""
        if hostname:
            try:
                hostname = dns.hostname(hostname, "host name")
            except dns.DNSError as e:
                err(i, str(e))
                continue
        description = row.get("description") or ""
        if len(description) > 300:
            err(i, "The description is longer than 300 characters.")
            continue
        device = ci = None
        if row.get("device"):
            device = device_by_name.get(row["device"].lower())
            if device is None:
                err(i, f"No network device named “{row['device'][:60]}” that you can see.")
                continue
        if row.get("ci"):
            ci = ci_by_name.get(row["ci"].lower())
            if ci is None:
                err(i, f"No configuration item named “{row['ci'][:60]}” that you can see.")
                continue
        existing = IPAddress.objects.filter(tenant_key=t, vrf=vrf, address=str(ip)).first()
        if existing is not None and not update_existing:
            err(i, f"{ip} already exists (tick “Update addresses that already exist” to change it).")
            continue
        if existing is not None and not services.addresses_for(user).filter(pk=existing.pk).exists():
            err(i, f"{ip} already exists on a site you cannot see.")
            continue
        found = address_error(user, str(ip), vrf, exclude_pk=existing.pk if existing else None, tenant_key=t)
        if isinstance(found, str):
            err(i, found)
            continue
        if not services.can_manage(user, "operate", found.site) or (
                existing is not None and not services.can_manage(user, "operate", existing.site)):
            err(i, f"You cannot change addresses on “{found.site}”.")
            continue
        plan.append((existing, {"address": str(ip), "vrf": vrf, "status": status, "hostname": hostname,
                                "description": description, "device": device, "ci": ci, "subnet": found}))
    if not res.rows and not res.errors:
        res.errors.append((0, "The file has no data rows."))
    if res.errors:
        return res
    with transaction.atomic():
        for existing, d in plan:
            if existing is None:
                a = IPAddress(tenant_key=t, address=d["address"], vrf=d["vrf"], subnet=d["subnet"], site=d["subnet"].site,
                              status=d["status"] or AddressStatus.ACTIVE, hostname=d["hostname"],
                              description=d["description"], device=d["device"], ci=d["ci"], source=Source.IMPORT)
                a.save()
                res.created.append(a)
            else:
                existing.subnet, existing.site = d["subnet"], d["subnet"].site
                for f in ("status", "hostname", "description"):
                    if d[f]:
                        setattr(existing, f, d[f])
                if d["device"] is not None:
                    existing.device = d["device"]
                if d["ci"] is not None:
                    existing.ci = d["ci"]
                existing.save()
                res.updated.append(existing)
    return res
