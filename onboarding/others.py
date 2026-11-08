"""Verify and enable for the target types that have no installer of their own: Kubernetes nodes, URL
services, a Prometheus server and (for enable) network devices.

* Kubernetes nodes and URL services are probed like any exporter (DNS, TCP, GET /metrics through
  probes.checks); nothing is installed on them, the page says what to deploy instead;
* a Prometheus target is checked with its own API (``prometheus_step.verify_prometheus``);
* a network device is checked through the snmp_exporter (``devices``).
"""
from .models import TargetState, TargetType

K8S_COMMANDS = """# Run with a kubeconfig that may create workloads in the cluster.
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
# one node-exporter pod per node (host metrics on :9100)
helm install node-exporter prometheus-community/prometheus-node-exporter -n monitoring --create-namespace
# cluster object metrics (deployments, pods, nodes) on :8080
helm install kube-state-metrics prometheus-community/kube-state-metrics -n monitoring
"""


def verify(target):
    """Kubernetes node / URL service: the generic exporter probe."""
    from . import servers
    return servers.verify(target)


def enable(target, options=None):
    from . import devices, prometheus_step, servers
    if target.target_type == TargetType.DEVICE:
        return devices.enable_device(target, options)
    if target.target_type == TargetType.PROMETHEUS:
        result = prometheus_step.verify_prometheus(target)
        if result.get("state") == TargetState.EXPORTER_FOUND:
            result["note"] = "Prometheus answers its API; there is nothing to install."
        return result
    result = servers.verify(target)                 # also records every probe attempt in the step log
    answering = result.get("state") == TargetState.EXPORTER_FOUND
    if target.target_type == TargetType.K8S_NODE:
        if answering:
            result["note"] = (f"{result.get('exporter') or 'An exporter'} already answers on :{target.port}. Point "
                              "kube-state-metrics at the cluster too if it is not deployed yet (commands below).")
        else:
            result["note"] = ("No exporter answers on this node yet. Deploy node-exporter and kube-state-metrics "
                              "with the commands below, then press Enable collection again.")
        result["install_command"] = K8S_COMMANDS
    else:
        result["note"] = ("This service answers /metrics." if answering else
                          "The service does not expose Prometheus metrics at /metrics yet. Nothing can be installed "
                          "for it from here: enable the metrics endpoint in the service itself.")
    return result
