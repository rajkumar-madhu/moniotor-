"""Cell parsing for the Kubernetes inventory workbook.

Every sheet importer goes through these helpers so one rule is applied everywhere:

* ``clean()``  – the discovery collector writes placeholder strings where it found nothing. They mean
  "no value" and must never be stored or shown as data.
* ``parse_quantity()`` / ``parse_cpu()`` – Kubernetes quantities ("74098976Ki", "2.70 Ti", "500m").
* ``parse_age()``, ``parse_labels()``, ``parse_taints()``, ``parse_ports()``, ``parse_list()``.

Nothing in here looks at the database, so it is cheap to unit-test and safe to import anywhere.
"""
import re

# Exact cell values that mean "the collector found nothing here".
PLACEHOLDERS = {
    "NOT DISCOVERED",
    "NOT APPLICABLE",
    "MANUAL INPUT REQUIRED",
    "MULTIPLE / SEE NODE INVENTORY",
    "N/A",
    "NA",
    "-",
    "--",
    "—",
}

# Cell values that mean "a client could not be determined" (a real finding, not a missing value).
UNMAPPED_MARKERS = {"UNMAPPED / REVIEW REQUIRED", "UNMAPPED", "REVIEW REQUIRED", "OTHERS/UNMAPPED"}

TRUE_WORDS = {"TRUE", "YES", "Y", "1", "READY", "ACTIVE", "ON"}
FALSE_WORDS = {"FALSE", "NO", "N", "0", "NOTREADY", "NOT READY", "OFF"}

BINARY_SUFFIXES = {"KI": 1024, "MI": 1024 ** 2, "GI": 1024 ** 3, "TI": 1024 ** 4, "PI": 1024 ** 5, "EI": 1024 ** 6}
DECIMAL_SUFFIXES = {"K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4, "P": 1000 ** 5, "E": 1000 ** 6}
AGE_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}

# Largest values the importers will store: they fit a signed 64-bit column (bytes) and a 32-bit one
# (counts, millicores, days) on every database, so a crafted cell can never overflow an integer column.
MAX_BIGINT = 2 ** 63 - 1
MAX_INT = 2 ** 31 - 1
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Defence in depth for the "names only" rule: if a free-text cell ever carries a credential (a pasted
# "password=…", a bearer token, a JWT, a PEM block), it is masked on the way in, whichever sheet it is on.
_SECRET_PAIR = re.compile(
    r"(?i)(?<![A-Za-z0-9])(--?)?(password|passwd|pwd|passphrase|token|api[ _-]?key|access[ _-]?key|secret[ _-]?key|"
    r"private[ _-]?key|client[ _-]?secret)(?![A-Za-z0-9])(\s*[:=]\s*|(?<=--token)\s+|(?<=--password)\s+)"
    r"(?:bearer\s+|basic\s+)?[^\s,;\"']+")
# a token has at least one digit, which keeps prose such as "basic authentication failed" intact
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+(?=[A-Za-z._~+/=-]*\d)[A-Za-z0-9._~+/=-]{12,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")
_PEM = re.compile(r"-----BEGIN [A-Z ]*(?:PRIVATE KEY|CERTIFICATE|SECRET)[A-Z ]*-----[\s\S]*?(?:-----END [A-Z ]*-----|$)")
REDACTED = "[redacted]"
_SECRET_HINTS = ("pass", "pwd", "token", "key", "secret", "authorization", "bearer", "basic", "eyj", "-----begin")


def redact_secrets(text):
    """Mask anything in `text` that looks like a credential value. Names of Secrets are left alone."""
    lowered = text.lower()
    if not any(hint in lowered for hint in _SECRET_HINTS):
        return text
    text = _PEM.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {REDACTED}", text)
    return _SECRET_PAIR.sub(lambda m: f"{m.group(2)}={REDACTED}", text)


_QUANTITY_RE = re.compile(r"^([+-]?\d{1,30}(?:\.\d{1,30})?)\s*([A-Za-z]{0,2})$")
_AGE_RE = re.compile(r"(\d{1,15}(?:\.\d{1,15})?)\s*([smhdwy])", re.IGNORECASE)
_PORT_RE = re.compile(r"^(\d+)(?:\s*/\s*([A-Za-z]+))?$")


def clean(value):
    """A cell as text, with placeholders normalised to "".

    Numbers keep an integral shape ("13", not "13.0"); ``None`` and placeholder strings become "".
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        value = int(value) if value.is_integer() else value
    if not isinstance(value, str):
        value = str(value)
    # a NUL byte (or another control character) in a cell would be refused by PostgreSQL
    text = _CONTROL_CHARS.sub("", value.replace("\u00a0", " ")).strip()
    if not text:
        return ""
    if text.upper() in PLACEHOLDERS:
        return ""
    return redact_secrets(text)


def is_unmapped(value):
    """True for the workbook's "no client could be determined" markers."""
    return clean(value).upper() in UNMAPPED_MARKERS


def parse_int(value, minimum=None, maximum=None):
    """An integer, or None when the cell is empty / not a number / outside the allowed range."""
    text = clean(value)
    if not text:
        return None
    text = text.replace(",", "")
    try:
        number = int(float(text))
    except (TypeError, ValueError, OverflowError):   # "nan", "inf", "1e999"
        return None
    if minimum is not None and number < minimum:
        return None
    if maximum is None:
        maximum = MAX_INT
    if number > maximum:
        return None
    return number


def parse_bool(value):
    """True / False from the workbook's "True", "Yes", … — None when the cell says nothing."""
    text = clean(value).upper()
    if text in TRUE_WORDS:
        return True
    if text in FALSE_WORDS:
        return False
    return None


def parse_quantity(value):
    """A Kubernetes quantity as **bytes**: "10Gi" -> 10737418240, "2.70 Ti", "74098976Ki", "500m", "0 Gi".

    Returns None for an empty cell or anything that is not a quantity. Binary suffixes (Ki, Mi, …) are
    powers of 1024; bare decimal suffixes (k, M, G, …) are powers of 1000, as kubectl prints them.
    """
    text = clean(value)
    if not text:
        return None
    m = _QUANTITY_RE.match(text)
    if not m:
        return None
    number, suffix = m.group(1), m.group(2)
    try:
        amount = float(number)
    except ValueError:
        return None
    key = suffix.upper()
    if not suffix:
        factor = 1.0
    elif key in BINARY_SUFFIXES:
        factor = float(BINARY_SUFFIXES[key])
    elif suffix == "m":  # milli — only lowercase single "m" is milli; "M" is mega
        factor = 0.001
    elif len(suffix) == 1 and key in DECIMAL_SUFFIXES:
        factor = float(DECIMAL_SUFFIXES[key])
    else:
        return None
    total = amount * factor
    if total < 0 or total > MAX_BIGINT:
        return None
    return int(round(total))


def parse_cpu(value):
    """CPU capacity as **millicores**: "16" -> 16000, "500m" -> 500, "8" -> 8000."""
    text = clean(value)
    if not text:
        return None
    if text.endswith("m") and not text.endswith("Mi"):
        milli = parse_int(text[:-1])
        return milli if milli is not None and milli >= 0 else None
    try:
        cores = float(text)
    except ValueError:
        return None
    if not 0 <= cores * 1000 <= MAX_INT:   # also rejects nan and inf
        return None
    return int(round(cores * 1000))


def parse_pods(value):
    """Pod capacity: "110" -> 110, "1k" -> 1000 (kubectl shortens large capacities)."""
    quantity = parse_quantity(value)
    if quantity is None or quantity > MAX_INT:
        return None
    return int(quantity)


def parse_age(value):
    """kubectl age as **whole days**: "134d" -> 134, "3d4h" -> 3, "12h" -> 0, "2y" -> 730."""
    text = clean(value)
    if not text:
        return None
    if text.startswith("-"):
        return None
    seconds = 0.0
    found = False
    for amount, unit in _AGE_RE.findall(text):
        seconds += float(amount) * AGE_UNITS[unit.lower()]
        found = True
    if not found:
        plain = parse_int(text)
        return plain if plain is not None and plain >= 0 else None
    days = int(seconds // 86400)
    return days if days <= MAX_INT else None


def parse_labels(value, pair_sep=";", kv_sep="="):
    """'a=1; b=2' -> {"a": "1", "b": "2"}. A key with no value keeps "" (as kubectl prints it)."""
    text = clean(value)
    out = {}
    if not text:
        return out
    for chunk in text.split(pair_sep):
        chunk = chunk.strip()
        if not chunk:
            continue
        key, _, val = chunk.partition(kv_sep)
        key = key.strip()
        if key:
            out[key] = val.strip()
    return out


def parse_taints(value):
    """'key=value:Effect; key=:NoSchedule' -> [{"key":…, "value":…, "effect":…}]."""
    text = clean(value)
    out = []
    if not text:
        return out
    for chunk in re.split(r"[;,]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        head, _, effect = chunk.rpartition(":")
        if not head:  # no ":" at all — the whole chunk is the key
            head, effect = chunk, ""
        key, _, val = head.partition("=")
        out.append({"key": key.strip(), "value": val.strip(), "effect": effect.strip()})
    return out


def parse_ports(value):
    """Service / container ports: '80, 443/TCP, http' -> [{"port":80,…}, …].

    Each entry is ``{"port": int|None, "name": str, "protocol": str}``; a named target port (``http``)
    keeps the name and no number, which is what the workbook records for many services.
    """
    text = clean(value)
    out = []
    if not text:
        return out
    for chunk in re.split(r"[;,]", text):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = _PORT_RE.match(chunk)
        if m:
            port = parse_int(m.group(1), minimum=0, maximum=65535)
            if port is None:
                continue
            out.append({"port": port, "name": "", "protocol": (m.group(2) or "").upper()})
        else:
            out.append({"port": None, "name": chunk, "protocol": ""})
    return out


def parse_list(value, sep=","):
    """'a, b, c' -> ["a", "b", "c"] with placeholders dropped and order preserved."""
    text = clean(value)
    if not text:
        return []
    seen, out = set(), []
    for chunk in text.split(sep):
        item = clean(chunk)
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_ip(value):
    """An IPv4 / IPv6 literal, or None (the workbook also writes "None" for headless services)."""
    import ipaddress
    text = clean(value)
    if not text or text.upper() == "NONE":
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def parse_url(value, max_length=500):
    """An http(s) URL, or "" — never a host name on its own and never an over-long string."""
    text = clean(value)
    if not text or len(text) > max_length:
        return ""
    return text if text.lower().startswith(("http://", "https://")) else ""


def fmt_bytes(value):
    """Bytes as a short binary string for display: 1073741824 -> '1 GiB'."""
    if value is None:
        return ""
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(amount) < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(amount)} B"
            return f"{amount:.1f} {unit}".replace(".0 ", " ")
        amount /= 1024
    return f"{amount:.1f} PiB"
