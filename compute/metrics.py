"""Live host metrics for physical servers (node_exporter through Prometheus).

A server is matched to a node_exporter target by network.Device.prometheus_instance: either the exact
`instance` label (e.g. "10.0.0.5:9100") or its address part ("10.0.0.5"). The host list comes from
telemetry.hosts (shared 30 s cache with the Infrastructure page) when that app is installed; memory totals for
capacity maths come from one extra query here. Every Prometheus call goes through core.prometheus.
"""
import logging
import math

from django.core.cache import cache
from django.urls import NoReverseMatch, reverse

from core import prometheus
from core.prometheus import PrometheusError

log = logging.getLogger("linkedeye.compute")

BAD_RESPONSE = ("Prometheus returned an unexpected response – is LINKEDEYE_PROMETHEUS_URL the Prometheus server "
                "root (for example http://prometheus:9090)?")
MEM_TOTAL_QUERY = 'sum by (instance) (node_memory_MemTotal_bytes{job=~".*node.*"})'
CACHE_SECONDS = 30


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and not math.isnan(v) and not math.isinf(v)


def query(promql):
    """core.prometheus.query that turns malformed responses into PrometheusError (never a 500)."""
    try:
        rows = prometheus.query(promql)
    except PrometheusError:
        raise
    except (ValueError, TypeError, AttributeError, KeyError, IndexError):
        raise PrometheusError(BAD_RESPONSE) from None
    if not isinstance(rows, list):
        raise PrometheusError(BAD_RESPONSE)
    return [r for r in rows if isinstance(r, dict)]


def _fetch_hosts():
    try:
        from telemetry import hosts as thosts
    except ImportError:  # telemetry app absent: fall back to a minimal query set of our own
        thosts = None
    if thosts is not None:
        try:
            return thosts.fetch_hosts()
        except PrometheusError:
            raise
        except (ValueError, TypeError, AttributeError, KeyError, IndexError):
            raise PrometheusError(BAD_RESPONSE) from None
    out = []
    for r in query('up{job=~".*node.*"}'):
        inst = (r.get("metric") or {}).get("instance")
        if inst:
            out.append({"instance": inst, "up": int(r["value"]) if finite(r.get("value")) else None,
                        "address": inst.rsplit(":", 1)[0] if inst.count(":") == 1 else inst})
    return out


def _mem_totals():
    hit = cache.get("compute:memtotal:v1")
    if hit is not None:
        return hit
    out = {}
    for r in query(MEM_TOTAL_QUERY):
        inst = (r.get("metric") or {}).get("instance")
        if inst and finite(r.get("value")):
            out[inst] = float(r["value"])
    cache.set("compute:memtotal:v1", out, CACHE_SECONDS)
    return out


def host_detail_url(instance):
    try:
        return reverse("telemetry:host_detail", args=[instance])
    except NoReverseMatch:
        return ""


def server_metrics(devices):
    """({device pk: metrics dict}, error message or "", prometheus configured?).

    metrics dict: instance, up, cpu, memory, disk (percent), cores, mem_total_gb, health, url."""
    wanted = [d for d in devices if (d.prometheus_instance or "").strip()]
    if not prometheus.configured():
        return {}, "", False
    if not wanted:
        return {}, "", True
    try:
        hosts = _fetch_hosts()
        try:
            mem = _mem_totals()
        except PrometheusError as e:  # memory totals are optional extras
            log.info("compute: node memory totals unavailable: %s", e)
            mem = {}
    except PrometheusError as e:
        return {}, str(e), True
    by_inst = {h.get("instance"): h for h in hosts if h.get("instance")}
    by_addr = {}
    for h in hosts:
        if h.get("address"):
            by_addr.setdefault(h["address"], h)
    out = {}
    for d in wanted:
        key = d.prometheus_instance.strip()
        h = by_inst.get(key) or by_addr.get(key) or by_addr.get(key.rsplit(":", 1)[0] if key.count(":") == 1 else key)
        if not h:
            continue
        inst = h["instance"]
        total = mem.get(inst)
        out[d.pk] = {
            "instance": inst, "up": h.get("up"),
            "cpu": h.get("cpu") if finite(h.get("cpu")) else None,
            "memory": h.get("memory") if finite(h.get("memory")) else None,
            "disk": h.get("disk") if finite(h.get("disk")) else None,
            "cores": int(h["cores"]) if finite(h.get("cores")) and h["cores"] > 0 else None,
            "mem_total_gb": round(total / 1024 ** 3, 1) if finite(total) and total > 0 else None,
            "health": h.get("health", ""), "url": host_detail_url(inst),
        }
    return out, "", True


def tone(value, warn=80.0, crit=90.0):
    if not finite(value):
        return "mute"
    return "crit" if value >= crit else "warn" if value >= warn else "ok"
