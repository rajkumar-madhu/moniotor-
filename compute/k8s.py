"""Kubernetes cluster summaries from kube-state-metrics, read through core.prometheus.

A Cluster of kind "kubernetes" names the series that belong to it with its prometheus_selector
(e.g. cluster="prod-k8s"). The selector is re-built from the validated label pairs and every value is quoted
with promql_string, so stored text can never change the shape of a query.

Metrics used (all from kube-state-metrics):
  kube_node_status_condition{condition="Ready"}       nodes Ready / NotReady
  kube_pod_status_phase                               pods by phase
  kube_namespace_created                              namespaces
  kube_deployment_spec_replicas                       deployments
  kube_deployment_status_replicas_unavailable         deployments with unavailable replicas
"""
import hashlib

from django.core.cache import cache
from django.utils import timezone

from core.prometheus import PrometheusError
from telemetry.fmt import promql_string

from .metrics import finite, is_outage, query

REQUIRED_METRICS = ["kube_node_status_condition", "kube_pod_status_phase", "kube_namespace_created",
                    "kube_deployment_spec_replicas", "kube_deployment_status_replicas_unavailable"]
PHASES = ["Running", "Pending", "Succeeded", "Failed", "Unknown"]
LIST_LIMIT = 50
CACHE_SECONDS = 30
OUTAGE_KEY = "compute:k8s:outage:v1"
OUTAGE_SECONDS = 15     # after Prometheus is found unreachable, the page does not ask again for this long


class MetricsMissing(Exception):
    """Prometheus answered, but no kube-state-metrics series match the cluster's selector."""


def selector(pairs, *extra):
    parts = [f"{k}={promql_string(v)}" for k, v in pairs] + list(extra)
    return ",".join(parts)


def queries(pairs):
    sel = selector(pairs)
    s = ("," + sel) if sel else ""
    return {
        "present_nodes": f"count(kube_node_status_condition{{{sel}}})",
        "present_pods": f"count(kube_pod_status_phase{{{sel}}})",
        "nodes": f'sum by (status) (kube_node_status_condition{{condition="Ready"{s}}} == 1)',
        "not_ready": f'topk({LIST_LIMIT}, kube_node_status_condition{{condition="Ready",status!="true"{s}}} == 1)',
        "pods": f"sum by (phase) (kube_pod_status_phase{{{sel}}} == 1)",
        "namespaces": f"count(kube_namespace_created{{{sel}}})",
        "deployments": f"count(kube_deployment_spec_replicas{{{sel}}})",
        "unavailable": f"topk({LIST_LIMIT}, kube_deployment_status_replicas_unavailable{{{sel}}} > 0)",
        "unavailable_count": f"count(kube_deployment_status_replicas_unavailable{{{sel}}} > 0)",
    }


def _scalar(rows):
    for r in rows or ():
        if finite(r.get("value")):
            return int(round(r["value"]))
    return 0


def fetch_summary(pairs):
    """Live summary dict for one cluster. Raises PrometheusError or MetricsMissing."""
    if not pairs:
        raise MetricsMissing("no selector")
    q = queries(pairs)
    if not query(q["present_nodes"]) and not query(q["present_pods"]):
        raise MetricsMissing("no series")
    nodes = {"ready": 0, "not_ready": 0}
    for r in query(q["nodes"]):
        st = (r.get("metric") or {}).get("status", "")
        n = int(round(r["value"])) if finite(r.get("value")) else 0
        if st == "true":
            nodes["ready"] += n
        else:
            nodes["not_ready"] += n
    nodes["total"] = nodes["ready"] + nodes["not_ready"]
    not_ready = sorted({(r.get("metric") or {}).get("node", "") for r in query(q["not_ready"])} - {""})
    pods = {p: 0 for p in PHASES}
    for r in query(q["pods"]):
        ph = (r.get("metric") or {}).get("phase", "Unknown")
        n = int(round(r["value"])) if finite(r.get("value")) else 0
        pods[ph if ph in pods else "Unknown"] += n
    unavailable = []
    for r in query(q["unavailable"]):
        m = r.get("metric") or {}
        if finite(r.get("value")):
            unavailable.append({"namespace": m.get("namespace", ""), "deployment": m.get("deployment", ""),
                                "replicas": int(round(r["value"]))})
    unavailable.sort(key=lambda u: (-u["replicas"], u["namespace"], u["deployment"]))
    return {
        "nodes": nodes, "not_ready_nodes": not_ready[:LIST_LIMIT],
        "pods": pods, "pods_total": sum(pods.values()),
        "namespaces": _scalar(query(q["namespaces"])),
        "deployments": _scalar(query(q["deployments"])),
        "unavailable": unavailable[:LIST_LIMIT],
        "unavailable_count": _scalar(query(q["unavailable_count"])),
        "collected_at": timezone.now().isoformat(),
    }


def state_of(summary):
    """"warning" when nodes are NotReady or deployments have unavailable replicas, else "ok"."""
    if not summary:
        return "unknown"
    if (summary.get("nodes") or {}).get("not_ready") or summary.get("unavailable_count"):
        return "warning"
    return "ok"


def live_summary(cluster, use_cache=True):
    """(summary or None, problem) for the Kubernetes page. problem is one of
    "" | "no_selector" | "missing" | an error message from Prometheus."""
    pairs = cluster.selector_pairs
    if not pairs:
        return None, "no_selector"
    key = "compute:k8s:v1:" + hashlib.sha1(f"{cluster.tenant_key}|{cluster.pk}|{pairs}".encode()).hexdigest()
    if use_cache:
        hit = cache.get(key)
        if hit is not None:
            return hit
    if use_cache:
        down = cache.get(OUTAGE_KEY)
        if down:  # one cluster already found Prometheus unreachable: do not wait for a timeout per cluster
            return None, down
    try:
        res = (fetch_summary(pairs), "")
    except MetricsMissing:
        res = (None, "missing")
    except PrometheusError as e:
        if is_outage(e):
            cache.set(OUTAGE_KEY, str(e), OUTAGE_SECONDS)
        return None, str(e)  # the error itself is not cached: the next refresh retries once the outage window passed
    cache.set(key, res, CACHE_SECONDS)
    return res
