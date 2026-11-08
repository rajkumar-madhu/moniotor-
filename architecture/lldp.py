"""Link suggestions from LLDP neighbour data that snmp_exporter exposes in Prometheus.

Reads (bulk instant queries, never one per device): lldpRemSysName, lldpRemPortId, lldpRemPortDesc, lldpLocPortDesc and
ifName. A neighbour is matched to a device LinkedEye already knows by system name, name or management address
(case-insensitive, short or fully qualified); anything else is only counted. Nothing is created here: the page lists
the suggestions and a user accepts the ones they trust (origin "lldp"). Every string that came from the network is
treated as hostile: control characters are removed, lengths are limited, and the templates escape the rest.
"""
import hashlib
import re
from dataclasses import dataclass, field

from core import prometheus

from .models import Link
from .ports import port_key

MAX_TEXT = 60
MAX_SUGGESTIONS = 500
METRICS = {"sysname": "lldpRemSysName", "portid": "lldpRemPortId", "portdesc": "lldpRemPortDesc",
           "locdesc": "lldpLocPortDesc", "ifname": "ifName"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PORT_SUFFIX = re.compile(r":\d{1,5}$")


@dataclass(frozen=True)
class Suggestion:
    a_id: int
    a_name: str
    a_port: str
    b_id: int
    b_name: str
    b_port: str
    token: str


@dataclass
class Result:
    suggestions: list = field(default_factory=list)
    unknown: int = 0           # neighbours that are not (or not uniquely) a known device
    known: int = 0             # suggestions already recorded as links
    rows: int = 0              # LLDP neighbour rows read
    error: str = ""            # why nothing could be read ("" = fine)


def clean_text(value, limit=MAX_TEXT):
    """Network-supplied text: control characters become spaces, whitespace is collapsed, length is limited."""
    text = " ".join(_CONTROL.sub(" ", str(value or "")).split())
    return text[:limit]


def _host(value):
    value = clean_text(value, 120).lower()
    return _PORT_SUFFIX.sub("", value) if value.count(":") == 1 else value


def _keys(device):
    """Spellings under which a device can be named in LLDP data or in a Prometheus instance label."""
    out = set()
    for v in (device.name, device.prometheus_instance, device.mgmt_ip):
        v = _host(v)
        if v:
            out.add(v)
            if not re.fullmatch(r"[\d.]+|[0-9a-f:]+", v):
                out.add(v.split(".")[0])
    return out


def _index(devices):
    idx = {}
    for d in devices:
        for k in _keys(d):
            idx.setdefault(k, set()).add(d.pk)
    return idx


def _lookup(idx, text):
    """The one device a text names, or None when it names none or several."""
    host = _host(text)
    for key in (host, host.split(".")[0] if host and not re.fullmatch(r"[\d.]+", host) else host):
        ids = idx.get(key)
        if ids and len(ids) == 1:
            return next(iter(ids))
    return None


def _by(series, label_names, value_label):
    """{(instance, local port num, index): text} from one metric's series (the text lives in a label)."""
    out = {}
    for s in series:
        m = s.get("metric", {})
        key = (m.get("instance", ""), m.get("lldpRemLocalPortNum", ""), m.get("lldpRemIndex", ""))
        text = clean_text(m.get(value_label, ""))
        if text:
            out[key] = text
    return out


def parse(series, devices, known=frozenset()):
    """Turn the five metrics' series (dict keyed like METRICS) into a Result. Pure: no queries, no Prometheus. `known`
    holds (a id, a port key, b id, b port key) of the links that already exist."""
    res = Result()
    names = _by(series.get("sysname", []), (), "lldpRemSysName")
    pids = _by(series.get("portid", []), (), "lldpRemPortId")
    pdesc = _by(series.get("portdesc", []), (), "lldpRemPortDesc")
    loc = {(s["metric"].get("instance", ""), s["metric"].get("lldpLocPortNum", "")): clean_text(s["metric"].get("lldpLocPortDesc", ""))
           for s in series.get("locdesc", []) if s.get("metric", {}).get("lldpLocPortNum")}
    ifn = {(s["metric"].get("instance", ""), s["metric"].get("ifIndex", "")): clean_text(s["metric"].get("ifName", ""))
           for s in series.get("ifname", []) if s.get("metric", {}).get("ifIndex")}
    idx = _index(devices)
    by_id = {d.pk: d for d in devices}
    seen, out = set(), []
    for key, remote in names.items():
        instance, num, _i = key
        local_id = _lookup(idx, instance)
        remote_id = _lookup(idx, remote)
        if local_id is None and remote_id is None:
            continue                  # between devices this user cannot see (or nobody we know): not counted, not shown
        res.rows += 1
        if local_id is None or remote_id is None or local_id == remote_id:
            res.unknown += 1
            continue
        local_port = loc.get((instance, num)) or ifn.get((instance, num)) or ""
        remote_port = pdesc.get(key) or pids.get(key) or ""
        a, ap, b, bp = Link.order_ends(local_id, local_port, remote_id, remote_port)
        ident = (a, port_key(ap), b, port_key(bp))
        if ident in seen:
            continue                      # the other end reported the same cable
        seen.add(ident)
        if ident in known:
            res.known += 1
            continue
        token = hashlib.sha1(f"{a}|{ident[1]}|{b}|{ident[3]}".encode()).hexdigest()[:16]
        out.append(Suggestion(a, by_id[a].name, ap, b, by_id[b].name, bp, token))
    out.sort(key=lambda s: (s.a_name.lower(), s.a_port, s.b_name.lower(), s.b_port))
    res.suggestions = out[:MAX_SUGGESTIONS]
    return res


def find(devices):
    """Query Prometheus and return a Result. Never raises: an error is reported in Result.error."""
    if not prometheus.configured():
        return Result(error="Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL), so there is no LLDP data to read.")
    series = {}
    try:
        series["sysname"] = prometheus.query(METRICS["sysname"])
    except prometheus.PrometheusError as exc:
        return Result(error=f"Prometheus could not be read: {str(exc)[:160]}")
    except Exception:
        return Result(error="Prometheus could not be read.")
    for key in ("portid", "portdesc", "locdesc", "ifname"):
        try:
            series[key] = prometheus.query(METRICS[key])
        except Exception:         # optional pieces: fewer ports named, not a failure
            series[key] = []
    if not series["sysname"]:
        return Result(error="Prometheus has no lldpRemSysName series. Enable the lldp module in your snmp_exporter "
                            "configuration and make sure the devices answer LLDP-MIB.")
    devices = list(devices)
    ids = [d.pk for d in devices]
    known = {(l.a_device_id, port_key(l.a_port), l.b_device_id, port_key(l.b_port))
             for l in Link.objects.filter(a_device_id__in=ids, b_device_id__in=ids)}
    return parse(series, devices, known)
