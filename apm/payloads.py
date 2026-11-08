"""Parsing and strict validation of everything a producer sends. Pure functions: no database, no network.

The producer is outside the trust boundary, so this module is deliberately paranoid:

* JSON is parsed strictly (``NaN`` / ``Infinity`` refused, depth and size bounded);
* the toolkit's ``str(dict)`` form is parsed ONLY with ``ast.literal_eval`` after a size check and a bracket-depth scan,
  never with ``eval``;
* every string is stripped of control, bidi-override and surrogate characters and truncated, so what reaches the database
  is plain text (the pages render it escaped, CSV export defuses formulas);
* numbers are range-checked (no huge integers, no NaN);
* a bad row becomes a **warning** and is skipped; a structural problem or a limit breach is a ``PayloadError`` and the
  whole payload is refused (nothing is applied).
"""
import ast
import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone
from django.utils.dateparse import parse_datetime

# ----------------------------------------------------------------------------- hard limits
MAX_BODY = 2 * 1024 * 1024          # request body, bytes
MAX_LITERAL = 1024 * 1024           # the payload_str text, characters
MAX_BROKERS = 200
MAX_SEGMENTS = 40
MAX_ROWS = 2000
MAX_LOGS = 1000
MAX_DEPTH = 12                      # nesting of the parsed payload
MAX_NUMBER = 10 ** 15
MAX_WARNINGS = 200                  # kept; the rest are only counted
FUTURE_SKEW = timedelta(minutes=15)  # a producer clock may run this far ahead; beyond it the receive time is used. A date
                                     # far in the future would otherwise make every later (real) report look "older"

L_CLIENT, L_SEGMENT, L_ID, L_VALUE, L_ERROR = 40, 20, 64, 120, 240
L_NAME, L_HOST, L_REMARKS, L_PROCESS, L_MESSAGE = 120, 120, 200, 80, 1000

TYPES = ("matrix", "table", "logs")
FAILED, WARNING, HEALTHY, UNKNOWN = 0, 1, 2, 3


class PayloadError(ValueError):
    """The payload is refused as a whole. ``http`` is the status the API answers with."""

    def __init__(self, message, code="invalid", http=422):
        super().__init__(message)
        self.code = code
        self.http = http


# ----------------------------------------------------------------------------- text
_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | {0x200E, 0x200F, 0x061C}


def clean_text(value, limit):
    """Plain single-line text: control, surrogate and bidi-override characters become spaces, runs of whitespace collapse,
    the result is truncated to ``limit``. Never raises; non-strings are stringified."""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, float):
        text = repr(value) if math.isfinite(value) else ""
    else:
        text = value if isinstance(value, str) else str(value)
    if len(text) > limit * 4:               # do not scan megabytes to keep 120 characters
        text = text[:limit * 4]
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cs") or ord(ch) in _BIDI or ch in (" ", " "):
            out.append(" ")
        elif cat == "Cf":
            continue                        # zero-width and other invisible format characters (look-alike client codes)
        else:
            out.append(ch)
    return " ".join("".join(out).split())[:limit]


def _number(value, name="number"):
    """An int or finite float within bounds, or a PayloadError. Booleans are not numbers."""
    if isinstance(value, bool):
        raise PayloadError(f"{name} must be a number")
    if isinstance(value, int):
        if abs(value) > MAX_NUMBER:
            raise PayloadError(f"{name} is out of range", "range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value) or abs(value) > MAX_NUMBER:
            raise PayloadError(f"{name} is not a finite number", "range")
        return value
    raise PayloadError(f"{name} must be a number")


# ----------------------------------------------------------------------------- parsing
def _reject_constant(name):
    raise ValueError(f"{name} is not allowed")


def loads_json(raw):
    """Strict JSON: NaN / Infinity refused. Raises PayloadError (400) on anything wrong, including a RecursionError."""
    try:
        text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
        return json.loads(text, parse_constant=_reject_constant)
    except (ValueError, RecursionError, UnicodeDecodeError, MemoryError):
        raise PayloadError("The body is not valid JSON.", "bad_json", 400) from None


def _bracket_depth_ok(text, limit=60):
    """Cheap, quote-aware scan: refuse text that nests brackets deeper than ``limit`` before the parser sees it."""
    depth, quote, escaped = 0, "", False
    for ch in text:
        if quote:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                quote = ""
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "([{":
            depth += 1
            if depth > limit:
                return False
        elif ch in ")]}":
            depth -= 1
    return True


def parse_literal(text):
    """The toolkit stores ``str(dict)``: parse it with ``ast.literal_eval`` ONLY (never eval), after a size check."""
    if not isinstance(text, str):
        raise PayloadError("payload_str must be text", "bad_literal", 400)
    if len(text) > MAX_LITERAL:
        raise PayloadError(f"payload_str is larger than {MAX_LITERAL // 1024} KB", "too_large", 413)
    if not _bracket_depth_ok(text):
        raise PayloadError("payload_str is nested too deeply", "too_deep", 422)
    try:
        return ast.literal_eval(text.strip())
    except (ValueError, SyntaxError, RecursionError, MemoryError, TypeError, OverflowError):
        raise PayloadError("payload_str is not a plain Python/JSON literal.", "bad_literal", 422) from None


def parse_text(text):
    """A stored value (a Redis string): JSON first, then the Python literal form."""
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8", "replace")
    if len(text) > MAX_LITERAL:
        raise PayloadError(f"the value is larger than {MAX_LITERAL // 1024} KB", "too_large", 413)
    try:
        return loads_json(text)
    except PayloadError:
        return parse_literal(text)


def sanitize_tree(obj, depth=0):
    """A deep copy that contains only str, bool, int, finite float, None, list and dict (tuples become lists). Refuses
    anything else (bytes, sets, complex numbers...), huge numbers and excessive nesting. Strings are cleaned of control
    characters and truncated later, per field, by ``clean_text``."""
    if depth > MAX_DEPTH:
        raise PayloadError("The payload is nested too deeply.", "too_deep")
    if obj is None or isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return _number(obj)
    if isinstance(obj, str):
        return obj if len(obj) <= MAX_LITERAL else obj[:MAX_LITERAL]
    if isinstance(obj, (list, tuple)):
        return [sanitize_tree(v, depth + 1) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if not isinstance(k, (str, int)) or isinstance(k, bool):
                raise PayloadError("Object keys must be text.", "bad_key")
            out[str(k)] = sanitize_tree(v, depth + 1)
        return out
    raise PayloadError(f"Unsupported value type {type(obj).__name__}.", "bad_type")


# ----------------------------------------------------------------------------- dates
_DMY = re.compile(r"^\s*(\d{1,2})-(\d{1,2})-(\d{4})[ T](\d{1,2}):(\d{2}):(\d{2})")
_EXECUTED = re.compile(r"^\s*(\d{1,4}-\d{1,2}-\d{1,4}[ T]\d{1,2}:\d{2}:\d{2})")
_DECLARED = re.compile(r"Total\s*:\s*(\d+)\s*;\s*Success\s*:\s*(\d+)\s*;\s*Failure\s*:\s*(\d+)", re.I)


def _aware(dt, tz):
    if dt.tzinfo is None:
        try:
            dt = timezone.make_aware(dt, tz)
        except Exception:                   # noqa: BLE001 - nonexistent / ambiguous local time
            dt = dt.replace(tzinfo=dt_timezone.utc)
    return dt


def parse_dt(value, tz=None):
    """A timezone-aware datetime from ISO 8601, ``YYYY-MM-DD HH:MM:SS[,mmm]``, the toolkit's ``DD-MM-YYYY HH:MM:SS`` or an
    epoch number (seconds or milliseconds); naive times are in ``tz`` (the project time zone). None when unusable."""
    tz = tz or timezone.get_default_timezone()
    dt = None
    try:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value <= 0:
                return None
            secs = value / 1000.0 if value > 1e11 else float(value)
            dt = datetime.fromtimestamp(secs, dt_timezone.utc)
        elif isinstance(value, str):
            text = value.strip()[:64]
            m = _DMY.match(text)
            if m:
                d, mo, y, h, mi, s = (int(x) for x in m.groups())
                dt = _aware(datetime(y, mo, d, h, mi, s), tz)
            else:
                dt = parse_datetime(text)
                if dt is not None:
                    dt = _aware(dt, tz)
    except (ValueError, OverflowError, OSError):
        return None
    if dt is None or not (2000 <= dt.year <= 2100):
        return None
    return dt


def parse_executed(text, tz=None):
    """(datetime or None, key or "", declared (total, success, failure) or None) from the toolkit's executedOn string
    ``'DD-MM-YYYY HH:MM:SS [ Total: N ; Success: S ; Failure: F ]'`` (the process script uses ``YYYY-MM-DD``)."""
    text = clean_text(text, L_NAME + 40)
    if not text:
        return None, "", None
    m = _EXECUTED.match(text)
    stamp = m.group(1) if m else ""
    declared = None
    d = _DECLARED.search(text)
    if d:
        declared = tuple(int(x) for x in d.groups())
    return (parse_dt(stamp, tz) if stamp else None), (stamp or text[:L_NAME]), declared


# ----------------------------------------------------------------------------- results
@dataclass
class CellData:
    client: str
    segment: str
    ctcl_id: str = ""
    value: str = ""
    status: int = UNKNOWN
    error: str = ""
    exit_code: int = None


@dataclass
class Matrix:
    executed_at: datetime
    executed_key: str
    overall: bool
    cells: list
    payload_hash: str
    brokers: int = 0
    warnings: list = field(default_factory=list)
    warnings_total: int = 0


@dataclass
class ProcessData:
    host: str
    name: str
    pid: int = None
    status: int = UNKNOWN
    cpu_pct: float = None
    mem_pct: float = None
    uptime_s: int = None
    last_heartbeat: datetime = None
    last_execution: datetime = None
    next_execution: datetime = None
    response_ms: float = None
    exit_code: int = None
    remarks: str = ""


@dataclass
class Table:
    executed_at: datetime
    executed_key: str
    overall: bool
    rows: list
    payload_hash: str
    warnings: list = field(default_factory=list)
    warnings_total: int = 0


@dataclass
class LogData:
    ts: datetime
    level: str
    process: str
    message: str
    client: str = ""
    segment: str = ""
    fp: str = ""


class _Warn:
    def __init__(self):
        self.items, self.total = [], 0

    def add(self, text):
        self.total += 1
        if len(self.items) < MAX_WARNINGS:
            self.items.append(text)


def _int_or_none(value, lo=-(2 ** 31), hi=2 ** 31 - 1):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?\d{1,12}", value):
            return None
        value = int(value)
    if isinstance(value, float):
        if not math.isfinite(value) or value != int(value):
            return None
        value = int(value)
    if isinstance(value, int) and lo <= value <= hi:
        return value
    return None


def _float_or_none(value, lo=0.0, hi=1e9):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?\d{1,12}(\.\d{1,9})?", value):
            return None
        value = float(value)
    if isinstance(value, (int, float)) and math.isfinite(value) and lo <= value <= hi:
        return float(value)
    return None


def _status_from(raw, is_success=None, text=None):
    """0..3 from the toolkit's status, else isSuccess / a status text; (status, ok) where ok=False means 'a status was
    given but is not usable'."""
    s = _int_or_none(raw, 0, 3) if raw is not None else None
    if s is not None:
        return s, True
    if isinstance(is_success, bool):
        return (HEALTHY if is_success else FAILED), raw is None
    if isinstance(text, str) and text.strip():
        t = text.strip().lower()
        if t in ("running", "up", "ok", "healthy", "success", "online"):
            return HEALTHY, True
        if t in ("not running", "down", "failed", "failure", "stopped", "offline", "error"):
            return FAILED, True
    return UNKNOWN, raw is None


def _digest(parts):
    h = hashlib.sha256()
    for p in sorted(parts):
        h.update(p.encode("utf-8", "replace"))
        h.update(b"\n")
    return h.hexdigest()


def _resolve_times(raw_executed, tz, now, warn):
    executed_at, key, declared = parse_executed(raw_executed, tz)
    if executed_at is None:
        if raw_executed not in (None, ""):
            warn.add("executedOn could not be read; the receive time is used")
        executed_at = now
    elif executed_at > now + FUTURE_SKEW:
        warn.add("executedOn is in the future; the receive time is used")
        executed_at = now
    return executed_at, key, declared


# ----------------------------------------------------------------------------- the matrix
def parse_matrix(payload, *, tz=None, now=None):
    """Validate the toolkit's matrix dict. See the module docstring for the refusal / warning rule."""
    now = now or timezone.now()
    if not isinstance(payload, dict):
        raise PayloadError("The matrix payload must be an object.", "bad_payload")
    kind = payload.get("type")
    if kind not in (None, "matrix"):
        raise PayloadError("The payload says it is not a matrix.", "bad_type")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise PayloadError("The matrix payload needs a 'data' object of brokers.", "bad_payload")
    if len(data) > MAX_BROKERS:
        raise PayloadError(f"A matrix may have at most {MAX_BROKERS} brokers.", "limit_brokers")
    warn = _Warn()
    cells, seen, brokers = [], set(), set()
    for raw_client, row in data.items():
        client = clean_text(raw_client, L_CLIENT)
        if not client:
            warn.add("a broker with an empty name was skipped")
            continue
        if not isinstance(row, dict):
            warn.add(f"broker {client}: the row is not an object; skipped")
            continue
        if len(row) > MAX_SEGMENTS:
            raise PayloadError(f"Broker {client} has more than {MAX_SEGMENTS} segments.", "limit_segments")
        for raw_seg, cell in row.items():
            segment = clean_text(raw_seg, L_SEGMENT)
            if not segment:
                warn.add(f"broker {client}: a segment with an empty name was skipped")
                continue
            if cell is None or cell == "-" or cell == "" or (isinstance(cell, str) and cell.strip() == "-"):
                continue                                   # not configured: nothing to monitor
            if (client, segment) in seen:
                warn.add(f"{client}/{segment}: duplicate cell; the first one is kept")
                continue
            seen.add((client, segment))
            brokers.add(client)
            if isinstance(cell, dict):
                status, ok = _status_from(cell.get("status"), cell.get("isSuccess"))
                if not ok:
                    warn.add(f"{client}/{segment}: status {clean_text(cell.get('status'), 12)!r} is not 0-3; "
                             "treated as unknown")
                ctcl = clean_text(cell.get("ctcl_id", cell.get("adapter_id", "")), L_ID)
                value = clean_text(cell.get("value", ""), L_VALUE)
                error = clean_text(cell.get("error", cell.get("message", cell.get("last_error", ""))), L_ERROR)
                exit_code = _int_or_none(cell.get("exit_code"))
                cells.append(CellData(client, segment, ctcl, value or ctcl, status, error, exit_code))
            else:
                warn.add(f"{client}/{segment}: the cell is not an object; stored as unknown")
                cells.append(CellData(client, segment, "", clean_text(cell, L_VALUE), UNKNOWN))
    executed_at, key, declared = _resolve_times(payload.get("executedOn"), tz, now, warn)
    if declared is not None and declared[0] != len(cells):
        warn.add(f"executedOn declares {declared[0]} adapters but the matrix holds {len(cells)}")
    overall = payload.get("overallStatus")
    if not isinstance(overall, bool):
        overall = not any(c.status == FAILED for c in cells)
    digest = _digest(f"{c.client}|{c.segment}|{c.ctcl_id}|{c.value}|{c.status}|{c.error}|{c.exit_code}" for c in cells)
    if not key:
        key = "h:" + digest[:32]
    return Matrix(executed_at, key, overall, cells, digest, len(brokers), warn.items, warn.total)


# ----------------------------------------------------------------------------- the process table
def _uptime(value):
    n = _int_or_none(value, 0, 10 ** 9)
    if n is not None:
        return n
    if isinstance(value, str):
        m = re.fullmatch(r"\s*(?:(\d+)d\s*)?(\d{1,4}):(\d{2})(?::(\d{2}))?\s*", value)
        if m:
            d, h, mi, s = (int(x) if x else 0 for x in m.groups())
            return d * 86400 + h * 3600 + mi * 60 + s
    return None


def parse_table(payload, *, host="", tz=None, now=None):
    now = now or timezone.now()
    if not isinstance(payload, dict):
        raise PayloadError("The table payload must be an object.", "bad_payload")
    if payload.get("type") not in (None, "table"):
        raise PayloadError("The payload says it is not a table.", "bad_type")
    data = payload.get("data")
    if not isinstance(data, list):
        raise PayloadError("The table payload needs a 'data' list of processes.", "bad_payload")
    if len(data) > MAX_ROWS:
        raise PayloadError(f"A table may have at most {MAX_ROWS} rows.", "limit_rows")
    warn = _Warn()
    default_host = clean_text(host, L_HOST)
    rows, seen = [], {}
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            warn.add(f"row {i + 1}: not an object; skipped")
            continue
        name = clean_text(raw.get("name", raw.get("process")), L_NAME)
        if not name:
            warn.add(f"row {i + 1}: no process name; skipped")
            continue
        row_host = clean_text(raw.get("host", default_host), L_HOST)
        status, ok = _status_from(raw.get("status"), None, raw.get("Status_", raw.get("status_text")))
        if not ok:
            warn.add(f"{name}: status {clean_text(raw.get('status'), 12)!r} is not 0-3; treated as unknown")
        resp = raw.get("response_ms", raw.get("response_time_ms", raw.get("response_time")))
        p = ProcessData(
            host=row_host, name=name, pid=_int_or_none(raw.get("pid"), 0, 2 ** 31 - 1), status=status,
            cpu_pct=_float_or_none(raw.get("cpu_percent", raw.get("cpu_pct")), 0, 100000),
            mem_pct=_float_or_none(raw.get("memory_percent", raw.get("mem_pct")), 0, 100000),
            uptime_s=_uptime(raw.get("uptime_s", raw.get("uptime"))),
            last_heartbeat=parse_dt(raw.get("last_heartbeat"), tz), last_execution=parse_dt(raw.get("last_execution"), tz),
            next_execution=parse_dt(raw.get("next_execution"), tz), response_ms=_float_or_none(resp, 0, 1e9),
            exit_code=_int_or_none(raw.get("exit_code")), remarks=clean_text(raw.get("remarks"), L_REMARKS))
        if (row_host, name) in seen:
            warn.add(f"{name}: listed twice for host {row_host or '-'}; the last one wins")
            rows[seen[(row_host, name)]] = p
            continue
        seen[(row_host, name)] = len(rows)
        rows.append(p)
    executed_at, key, declared = _resolve_times(payload.get("executedOn"), tz, now, warn)
    overall = payload.get("overallStatus")
    if not isinstance(overall, bool):
        overall = not any(r.status == FAILED for r in rows)
    digest = _digest(f"{r.host}|{r.name}|{r.pid}|{r.status}|{r.cpu_pct}|{r.mem_pct}|{r.exit_code}|{r.remarks}" for r in rows)
    if not key:
        key = "h:" + digest[:32]
    return Table(executed_at, key, overall, rows, digest, warn.items, warn.total)


# ----------------------------------------------------------------------------- logs
LEVEL_ALIASES = {"WARN": "WARNING", "CRITICAL": "ERROR", "FATAL": "ERROR", "ERR": "ERROR", "TRACE": "DEBUG",
                 "NOTICE": "INFO", "INFORMATION": "INFO"}


def parse_logs(items, *, tz=None, now=None):
    """Validate a list of log lines. Returns (list of LogData, warnings, warnings_total)."""
    now = now or timezone.now()
    if not isinstance(items, list):
        raise PayloadError("'logs' must be a list.", "bad_payload")
    if len(items) > MAX_LOGS:
        raise PayloadError(f"At most {MAX_LOGS} log lines per request.", "limit_logs")
    warn = _Warn()
    out = []
    for i, raw in enumerate(items):
        if not isinstance(raw, dict):
            warn.add(f"log {i + 1}: not an object; skipped")
            continue
        message = clean_text(raw.get("message"), L_MESSAGE)
        if not message:
            warn.add(f"log {i + 1}: empty message; skipped")
            continue
        ts = parse_dt(raw.get("ts"), tz)
        if ts is None:
            if raw.get("ts") not in (None, ""):
                warn.add(f"log {i + 1}: timestamp could not be read; the receive time is used")
            ts = now
        elif ts > now + FUTURE_SKEW:
            warn.add(f"log {i + 1}: timestamp is in the future; the receive time is used")
            ts = now
        level = clean_text(raw.get("level"), 12).upper() or "INFO"
        level = LEVEL_ALIASES.get(level, level)
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            warn.add(f"log {i + 1}: unknown level {level[:12]!r}; INFO is used")
            level = "INFO"
        process = clean_text(raw.get("process"), L_PROCESS)
        client = clean_text(raw.get("client"), L_CLIENT)
        segment = clean_text(raw.get("segment"), L_SEGMENT)
        fp = hashlib.sha1(f"{ts.isoformat()}|{level}|{process}|{message}".encode("utf-8", "replace")).hexdigest()[:32]
        out.append(LogData(ts, level, process, message, client, segment, fp))
    return out, warn.items, warn.total


# ----------------------------------------------------------------------------- the request envelope
@dataclass
class Request:
    kind: str
    payload: object = None
    host: str = ""
    script: str = ""
    duration_s: float = None
    logs: list = None
    completed: list = field(default_factory=list)


def parse_request(raw):
    """The ingest request body -> Request. ``payload`` is the toolkit's dict as JSON; ``payload_str`` is its ``str(dict)``."""
    body = loads_json(raw)
    if not isinstance(body, dict):
        raise PayloadError("The body must be a JSON object.", "bad_payload", 400)
    kind = body.get("type")
    if kind not in TYPES:
        raise PayloadError("'type' must be one of matrix, table, logs.", "bad_type")
    payload = None
    if kind in ("matrix", "table"):
        has_p, has_s = body.get("payload") is not None, body.get("payload_str") is not None
        if has_p == has_s:
            raise PayloadError("Send exactly one of 'payload' (JSON) or 'payload_str' (the toolkit's str(dict)).",
                               "bad_payload")
        payload = sanitize_tree(parse_literal(body["payload_str"]) if has_s else body["payload"])
    duration = None
    if body.get("duration_s") is not None:
        d = _float_or_none(body.get("duration_s"), 0, 7 * 86400)
        if d is None:
            raise PayloadError("duration_s must be a number of seconds.", "bad_payload")
        duration = d
    logs = body.get("logs")
    if logs is not None:
        if not isinstance(logs, list):
            raise PayloadError("'logs' must be a list.", "bad_payload")
        if len(logs) > MAX_LOGS:
            raise PayloadError(f"At most {MAX_LOGS} log lines per request.", "limit_logs")
        logs = sanitize_tree(logs)
    if kind == "logs" and not logs:
        raise PayloadError("A logs request needs a non-empty 'logs' list.", "bad_payload")
    done = body.get("completed_commands") or []
    if not isinstance(done, list) or len(done) > 200:
        raise PayloadError("'completed_commands' must be a short list of ids.", "bad_payload")
    ids = [i for i in (_int_or_none(x, 1, 2 ** 31 - 1) for x in done) if i is not None]
    return Request(kind, payload, clean_text(body.get("host"), L_HOST), clean_text(body.get("script"), L_HOST),
                   duration, logs, ids)
