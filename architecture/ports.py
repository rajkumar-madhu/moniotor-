"""Port-name helpers shared by the link forms, the CSV import, the interface join and the LLDP reader. Pure Python."""
import re

MAX_PORT = 60
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_SPACE = re.compile(r"\s+")

# long and short spellings of the same interface prefix; only matched at the start and only before a digit
_CANON = [
    (re.compile(r"^(hundredgige|hundredgigabitethernet|hu)(?=\d)"), "hu"),
    (re.compile(r"^(fortygigabitethernet|fortygige|fo)(?=\d)"), "fo"),
    (re.compile(r"^(twentyfivegige|twe)(?=\d)"), "twe"),
    (re.compile(r"^(tengigabitethernet|tengige|te)(?=\d)"), "te"),
    (re.compile(r"^(gigabitethernet|gi)(?=\d)"), "gi"),
    (re.compile(r"^(fastethernet|fa)(?=\d)"), "fa"),
    (re.compile(r"^(ethernet|eth|et)(?=\d)"), "eth"),
    (re.compile(r"^(port-channel|portchannel|po)(?=\d)"), "po"),
    (re.compile(r"^(management|mgmt)(?=\d)"), "mgmt"),
]


def clean_port(value, limit=MAX_PORT):
    """Port text for storage: control characters removed, whitespace collapsed, at most `limit` characters.
    Raises ValueError when it is longer than `limit` (callers turn that into a form or row error)."""
    text = _SPACE.sub(" ", _CONTROL.sub(" ", str(value or ""))).strip()
    if len(text) > limit:
        raise ValueError(f"Port names are {limit} characters at most.")
    return text


def port_key(name):
    """A spelling-independent key: "Ethernet1/49", "Eth1/49" and "eth 1/49" are the same port."""
    s = _SPACE.sub("", str(name or "").lower())
    for pattern, short in _CANON:
        m = pattern.match(s)
        if m:
            return short + s[m.end():]
    return s
