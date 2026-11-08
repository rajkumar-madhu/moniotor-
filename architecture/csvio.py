"""CSV export (formula-safe) and the link import: strict validation, a row-level error report, a dry-run preview and an
all-or-nothing commit. Columns: a_device, a_port, b_device, b_port, medium, speed_mbps, kind, note."""
import csv
import io

from django.db import IntegrityError, transaction
from django.http import HttpResponse

from . import services
from .forms import CONTROL_RE, duplicate_of
from .models import Link, Medium, Origin
from .ports import MAX_PORT, clean_port

COLUMNS = ["a_device", "a_port", "b_device", "b_port", "medium", "speed_mbps", "kind", "note"]
REQUIRED = ["a_device", "b_device"]
MAX_ROWS = 5000
MAX_ERRORS = 50
MAX_BYTES = 2 * 1024 * 1024
FORMULA = "=+-@\t\r"


def _is_number(s):
    try:
        float(s)
        return True
    except ValueError:
        return False


def csv_safe(value):
    """Neutralise spreadsheet formulas: text that starts with = + - @ or a tab / CR is prefixed with a quote."""
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in FORMULA and not _is_number(s):
        return "'" + s
    return s


def unquote(value):
    """Undo csv_safe on a re-imported export: a leading ' in front of a formula character is not part of the text."""
    return value[1:] if len(value) > 1 and value[0] == "'" and value[1] in FORMULA else value


def export_response(links, filename="architecture-links.csv"):
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    w = csv.writer(resp)
    w.writerow(COLUMNS)
    for l in links:
        w.writerow([csv_safe(v) for v in (l.a_device.name, l.a_port, l.b_device.name, l.b_port, l.medium,
                                          "" if l.speed_mbps is None else l.speed_mbps, l.kind, l.note)])
    return resp


class Plan:
    """The outcome of validating a file: per-row actions (create / update / unchanged) and errors."""

    def __init__(self):
        self.errors = []     # (line, message)
        self.rows = []       # dicts: line, action, a, b, a_port, b_port, fields, existing
        self.count = 0

    @property
    def ok(self):
        return not self.errors

    def tally(self, action):
        return sum(1 for r in self.rows if r["action"] == action)

    @property
    def create_count(self):
        return self.tally("create")

    @property
    def update_count(self):
        return self.tally("update")

    @property
    def same_count(self):
        return self.tally("unchanged")


def decode(raw):
    if len(raw) > MAX_BYTES:
        return None, f"The file is larger than {MAX_BYTES // 1024 // 1024} MB."
    try:
        return raw.decode("utf-8-sig"), ""
    except UnicodeDecodeError:
        return None, "The file is not UTF-8 text."


def _rows(reader, err):
    """(line number, row) pairs; a malformed file (a cell over the csv module's size limit, say) ends the read with an error."""
    lines = enumerate(reader, start=2)
    while True:
        try:
            yield next(lines)
        except StopIteration:
            return
        except csv.Error as exc:
            err(0, f"The file is not valid CSV ({str(exc)[:80]}).")
            return


def validate(text, user, request=None, update_existing=False):
    """Check every row against the devices the user may manage. Nothing is written."""
    plan = Plan()
    text = text.lstrip("\ufeff")

    def err(line, msg):
        if len(plan.errors) < MAX_ERRORS:
            plan.errors.append((line, msg))

    reader = csv.DictReader(io.StringIO(text, newline=""))
    try:
        fieldnames = reader.fieldnames
    except csv.Error as exc:
        err(0, f"The file is not valid CSV ({str(exc)[:80]}).")
        return plan
    if not fieldnames:
        err(0, "The file is empty.")
        return plan
    header = {(h or "").strip().lower(): h for h in fieldnames}
    missing = [c for c in REQUIRED if c not in header]
    if missing:
        err(1, f"The header row needs the column(s): {', '.join(missing)} (optional: "
               f"{', '.join(c for c in COLUMNS if c not in REQUIRED)}).")
        return plan
    unknown = sorted(h for h in header if h and h not in COLUMNS)
    if unknown:
        err(1, f"Unknown column(s): {', '.join(unknown)[:200]}.")
        return plan
    tenant = services.tenant()
    exact, folded = {}, {}
    for d in services.device_queryset(user, request):
        exact[d.name] = d
        folded.setdefault(d.name.lower(), []).append(d)

    def find(name):
        """The exact spelling wins; otherwise a case-insensitive match, but only when it names one device."""
        if name in exact:
            return exact[name]
        found = folded.get(name.lower(), [])
        return found[0] if len(found) == 1 else None

    mediums = set(Medium.values)
    seen = set()
    for line, raw in _rows(reader, err):
        if None in raw:     # more cells than headers
            err(line, "The row has more cells than the header.")
            continue
        row = {k: unquote((raw.get(orig) or "").strip()) for k, orig in header.items() if k}
        if not any(row.values()):
            continue
        plan.count += 1
        if plan.count > MAX_ROWS:
            err(line, f"More than {MAX_ROWS:,} rows: split the file.")
            break
        if any(CONTROL_RE.search(v) for v in row.values()):
            err(line, "The row contains control characters.")
            continue
        names = []
        for col in REQUIRED:
            name = row.get(col, "")
            if not name:
                err(line, f"{col} is empty.")
                break
            dev = find(name)
            if dev is None:
                if name.lower() in folded:
                    err(line, f"“{name[:60]}” matches more than one device: write the name exactly as it is spelled.")
                else:
                    err(line, f"No device named “{name[:60]}” that you can see.")
                break
            names.append(dev)
        if len(names) != 2:
            continue
        a, b = names
        if a.pk == b.pk:
            err(line, f"{a.name} cannot be linked to itself.")
            continue
        if not (services.can_manage_device(user, a) and services.can_manage_device(user, b)):
            err(line, f"You cannot manage links between {a.name} and {b.name}.")
            continue
        try:
            ports = [clean_port(row.get("a_port")), clean_port(row.get("b_port"))]
        except ValueError:
            err(line, f"Port names are {MAX_PORT} characters at most.")
            continue
        medium = (row.get("medium") or Medium.FIBRE).lower()
        if medium not in mediums:
            err(line, f"Unknown medium “{medium[:20]}” (use {', '.join(sorted(mediums))}).")
            continue
        speed = None
        if row.get("speed_mbps"):
            if not (row["speed_mbps"].isascii() and row["speed_mbps"].isdigit()) or not 1 <= int(row["speed_mbps"]) <= 10_000_000:
                err(line, "speed_mbps must be a whole number of Mbps between 1 and 10,000,000.")
                continue
            speed = int(row["speed_mbps"])
        kind = " ".join(row.get("kind", "").split())
        if len(kind) > 30:
            err(line, "kind is 30 characters at most.")
            continue
        note = " ".join(row.get("note", "").split())
        if len(note) > 300:
            err(line, "note is 300 characters at most.")
            continue
        ea, ap, eb, bp = Link.order_ends(a.pk, ports[0], b.pk, ports[1])
        ident = (ea, ap.lower(), eb, bp.lower())
        if ident in seen:
            err(line, f"{a.name} {ports[0]} ↔ {b.name} {ports[1]} appears more than once in the file.")
            continue
        seen.add(ident)
        existing = duplicate_of(a.pk, ports[0], b.pk, ports[1], tenant)
        fields = {"medium": medium, "speed_mbps": speed, "kind": kind, "note": note}
        if existing is None:
            action = "create"
        elif not update_existing:
            err(line, "This link already exists (tick “Update links that already exist” to change it).")
            continue
        else:
            same = (existing.medium, existing.speed_mbps, existing.kind, existing.note) == (medium, speed, kind, note)
            action = "unchanged" if same else "update"
        plan.rows.append({"line": line, "action": action, "a": a, "b": b, "a_port": ports[0], "b_port": ports[1],
                          "fields": fields, "existing": existing})
    if not plan.count and not plan.errors:
        err(0, "The file has no data rows.")
    return plan


def commit(plan):
    """Write a valid plan in one transaction. Returns (created, updated) lists of Link."""
    created, updated = [], []
    tenant = services.tenant()
    with transaction.atomic():
        for r in plan["rows"] if isinstance(plan, dict) else plan.rows:
            if r["action"] == "create":
                link = Link(tenant_key=tenant, a_device=r["a"], b_device=r["b"], a_port=r["a_port"], b_port=r["b_port"],
                            origin=Origin.IMPORT, **r["fields"])
                try:
                    with transaction.atomic():
                        link.save()
                except IntegrityError:
                    continue         # created by someone else since the preview: not an error
                created.append(link)
            elif r["action"] == "update":
                link = r["existing"]
                for k, v in r["fields"].items():
                    setattr(link, k, v)
                link.save()
                updated.append(link)
    return created, updated
