"""compute.k8s – every 300 s, cache a kube-state-metrics summary per Kubernetes cluster and emit an event when a
cluster turns degraded (NotReady nodes or deployments with unavailable replicas) or recovers. Events fire on
state changes only. Bounded (MAX_CLUSTERS per pass, Prometheus timeout from settings) and never raises."""
import logging
import time

from django.conf import settings
from django.utils import timezone

from core import prometheus
from core.events import emit
from core.prometheus import PrometheusError

from . import k8s
from .metrics import is_outage
from .models import Cluster, ClusterKind, K8sState

log = logging.getLogger("linkedeye.compute")

MAX_CLUSTERS = 100
MAX_SECONDS = 240       # wall-clock budget of one pass (the job runs every 300 s)


def _describe(summary):
    nodes = summary.get("nodes") or {}
    parts = []
    if nodes.get("not_ready"):
        names = ", ".join(summary.get("not_ready_nodes") or [])[:300]
        parts.append(f"{nodes['not_ready']} of {nodes.get('total', 0)} node(s) NotReady" + (f" ({names})" if names else ""))
    if summary.get("unavailable_count"):
        deps = ", ".join(f"{u['namespace']}/{u['deployment']}" for u in (summary.get("unavailable") or [])[:10])
        parts.append(f"{summary['unavailable_count']} deployment(s) with unavailable replicas" + (f" ({deps})" if deps else ""))
    return "; ".join(parts)


def refresh_cluster(cluster):
    """Collect one cluster; returns its new state. Persists summary / error and emits on state change."""
    now = timezone.now()
    try:
        summary = k8s.fetch_summary(cluster.selector_pairs)
    except k8s.MetricsMissing:
        cluster.k8s_error = "No kube-state-metrics series match the selector."
        cluster.k8s_checked_at = now
        cluster.save(update_fields=["k8s_error", "k8s_checked_at"])
        return cluster.k8s_state
    except PrometheusError as e:
        cluster.k8s_error = str(e)[:300]
        cluster.k8s_checked_at = now
        cluster.save(update_fields=["k8s_error", "k8s_checked_at"])
        return cluster.k8s_state
    previous = cluster.k8s_state
    state = k8s.state_of(summary)
    cluster.k8s_summary, cluster.k8s_state, cluster.k8s_error, cluster.k8s_checked_at = summary, state, "", now
    cluster.save(update_fields=["k8s_summary", "k8s_state", "k8s_error", "k8s_checked_at"])
    fp = f"compute:k8s:{cluster.pk}"
    if state == K8sState.WARNING and previous != K8sState.WARNING:
        emit(source="compute", title=f"Kubernetes cluster {cluster.name} degraded", severity="warning",
             message=_describe(summary), site=cluster.site, obj=cluster, fingerprint=fp,
             not_ready=summary["nodes"]["not_ready"], unavailable=summary["unavailable_count"])
    elif state == K8sState.OK and previous == K8sState.WARNING:
        emit(source="compute", title=f"Kubernetes cluster {cluster.name} recovered", severity="ok",
             message="All nodes Ready and every deployment has its replicas available.", site=cluster.site,
             obj=cluster, fingerprint=fp)
    return state


def collect_k8s():
    if not prometheus.configured():
        return "prometheus not configured"
    qs = (Cluster.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, kind=ClusterKind.KUBERNETES)
          .exclude(prometheus_selector="").order_by("k8s_checked_at", "pk")[:MAX_CLUSTERS])
    done = degraded = failed = 0
    started, stopped = time.monotonic(), ""
    for c in qs:
        if done + failed and time.monotonic() - started > MAX_SECONDS:
            stopped = "time budget spent"
            break
        try:
            state = refresh_cluster(c)
        except Exception:  # one broken cluster must not stop the rest (or the worker)
            log.exception("compute.k8s failed for cluster %s", c.pk)
            failed += 1
            continue
        done += 1
        degraded += state == K8sState.WARNING
        if is_outage(c.k8s_error):  # Prometheus is down: the other clusters would each wait for a timeout too
            stopped = "Prometheus unreachable"
            break
    msg = f"{done} kubernetes cluster(s) collected, {degraded} degraded, {failed} failed"
    return msg + (f"; stopped early ({stopped})" if stopped else "")
