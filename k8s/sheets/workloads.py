"""Importers for the application, service and storage sheets.

===========================  =====  ====================================================================
Sheet                        Order  Goes to
===========================  =====  ====================================================================
``04_Application_Inventory``    60  ``Workload``     one row per container of one pod
``05_Service_Inventory``        70  ``KubeService``  one row per service port
``07_PV_PVC_Storage``           80  ``Volume``       one row per PVC / PV pair
===========================  =====  ====================================================================

They run after the node, client and namespace sheets (orders 20–50), so a workload can be linked to the
``Node`` it runs on and its namespace row already exists. A node the sheet names but the node inventory
does not list is left unlinked (the host name is kept as text) rather than guessed.

Placeholder cells ("NOT DISCOVERED", "NOT APPLICABLE", …) are normalised to empty by ``parsing.clean`` and
never stored as data. A replica count the discovery did not record is NULL, never 0.
"""
import re

from ..importer import register_sheet
from ..models import HEALTH_FROM_WORKBOOK, Health, Node
from ..models_workload import KubeService, PodStatus, Risk, ServiceType, Volume, Workload, WorkloadType
from ..parsing import (clean, is_unmapped, parse_age, parse_ip, parse_int, parse_labels, parse_list, parse_ports,
                       parse_quantity)

MAX_PORT = 65535
MAX_REPLICAS = 10 ** 6

APP_SHEET = "04_Application_Inventory"
SERVICE_SHEET = "05_Service_Inventory"
VOLUME_SHEET = "07_PV_PVC_Storage"


def _get(row, *names):
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


# ----------------------------------------------------------------------------- image references
_DIGEST_RE = re.compile(r"@(sha\d+:[0-9A-Fa-f]{16,})$")


def parse_image(reference):
    """Split a container image reference into ``(registry, tag, digest, latest)``.

    ``registry.example.com/team/app:1.2`` -> ("registry.example.com", "1.2", "", False)
    ``redis``                             -> ("docker.io", "", "", True)   (no tag means ``latest``)
    ``example.com:5000/app@sha256:…``     -> ("example.com:5000", "", "sha256:…", False)

    The first path component is a registry host only when it looks like one (it has a dot or a port, or is
    ``localhost``); otherwise the image comes from Docker Hub. ``latest`` is True for the explicit tag and for
    an image with neither a tag nor a digest, which resolves to ``latest`` at pull time.
    """
    text = clean(reference)
    if not text:
        return "", "", "", False
    digest = ""
    m = _DIGEST_RE.search(text)
    if m:
        digest = m.group(1)
        text = text[:m.start()]
    first, slash, rest = text.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        registry, path = first, rest
    else:
        registry, path = "docker.io", text
    tag = ""
    last = path.rsplit("/", 1)[-1]
    if ":" in last:
        tag = last.rsplit(":", 1)[1]
    latest = tag.lower() == "latest" or (not tag and not digest)
    return registry.lower(), tag, digest, latest


# ----------------------------------------------------------------------------- vocabulary
def _health(value):
    text = clean(value)
    lowered = text.lower()
    if lowered in ("healthy", "green", "ok"):
        return Health.OK, text
    if lowered in ("warning", "amber", "degraded"):
        return Health.WARN, text
    if lowered in ("critical", "red", "unhealthy", "failed"):
        return Health.CRIT, text
    return HEALTH_FROM_WORKBOOK.get(text.upper(), Health.UNKNOWN), text


def _workload_type(value):
    text = clean(value)
    for choice in (WorkloadType.DEPLOYMENT, WorkloadType.DAEMONSET, WorkloadType.STATEFULSET):
        if text.lower() == choice.value.lower():
            return choice.value, text
    return WorkloadType.OTHER.value, text


def _pod_status(value):
    text = clean(value)
    key = re.sub(r"[^a-z]", "", text.lower())
    mapping = {"running": PodStatus.RUNNING, "pending": PodStatus.PENDING, "failed": PodStatus.FAILED,
               "succeeded": PodStatus.SUCCEEDED, "nopod": PodStatus.NO_POD}
    return mapping.get(key, PodStatus.UNKNOWN).value, text


def _service_type(value):
    text = clean(value)
    for choice in (ServiceType.CLUSTER_IP, ServiceType.NODE_PORT, ServiceType.LOAD_BALANCER,
                   ServiceType.EXTERNAL_NAME):
        if text.lower() == choice.value.lower():
            return choice.value, text
    return ServiceType.OTHER.value, text


def _risk(value):
    text = clean(value)
    mapping = {"low": Risk.LOW, "medium": Risk.MEDIUM, "med": Risk.MEDIUM, "high": Risk.HIGH,
               "critical": Risk.HIGH}
    return mapping.get(text.lower(), Risk.UNKNOWN).value, text


def _urls(value):
    """Every http(s) URL in a cell (several are written comma-separated), in order, without duplicates."""
    out = []
    for part in re.split(r"[,\s;]+", clean(value)):
        if part.lower().startswith(("http://", "https://")) and len(part) <= 500 and part not in out:
            out.append(part)
    return out


def _status_word(value):
    """'bound' / 'BOUND' -> 'Bound' (statuses are compared case-insensitively but shown in one style)."""
    text = clean(value)
    return text[:1].upper() + text[1:].lower() if text else ""


class _Scope:
    """What every row of these sheets shares: the cluster, the node lookup, the client of a row."""

    def __init__(self, ctx, sheet):
        self.ctx = ctx
        self.sheet = sheet
        self.cluster = ctx.cluster_for()
        self.nodes = {n.hostname.lower(): n for n in Node.objects.filter(tenant_key=ctx.tenant, cluster=self.cluster)}
        self.unknown_nodes = set()

    def namespace(self, name, client_label, environment):
        return self.ctx.resolve_namespace(name, cluster=self.cluster, client_label=client_label,
                                          environment=environment, sheet=self.sheet)

    def client(self, label):
        """(core.Client or None, label as recorded). An unmapped marker is no client at all."""
        text = clean(label)
        if not text or is_unmapped(text):
            return None, ""
        return self.ctx.resolve_client(text), text[:150]

    def node(self, hostname):
        name = clean(hostname)
        if not name:
            return None, ""
        node = self.nodes.get(name.lower())
        if node is None:
            self.unknown_nodes.add(name)
        return node, name[:255]


# ----------------------------------------------------------------------------- 04_Application_Inventory
def import_applications(ctx, sheet):
    scope = _Scope(ctx, APP_SHEET)
    for row in sheet.rows:
        ns_name = clean(_get(row, "Namespace"))
        workload_name = clean(_get(row, "Workload Name", "Workload"))
        if not ns_name or not workload_name:
            ctx.count(APP_SHEET, "skipped")
            ctx.warn(APP_SHEET, "a row has no namespace or workload name and was skipped")
            continue
        client, client_label = scope.client(_get(row, "Client"))
        environment = clean(_get(row, "Environment"))[:40]
        namespace = scope.namespace(ns_name, client_label, environment)
        node, node_name = scope.node(_get(row, "Node"))
        wtype, wtype_raw = _workload_type(_get(row, "Workload Type", "Kind"))
        health, health_raw = _health(_get(row, "Health"))
        pod_status, pod_status_raw = _pod_status(_get(row, "Pod Status"))
        image = clean(_get(row, "Image"))
        registry, tag, digest, latest = parse_image(image)
        if not image:
            tag = clean(_get(row, "Image Tag"))[:128]
        urls = _urls(_get(row, "URL"))
        container = clean(_get(row, "Container"))[:253]
        pod = clean(_get(row, "Pod"))[:253]
        values = {
            "cluster": scope.cluster,
            "client": client,
            "client_label": client_label,
            "environment": environment,
            "project": clean(_get(row, "Project"))[:253],
            "application": clean(_get(row, "Application"))[:253],
            "workload_type": wtype,
            "type_raw": wtype_raw[:40],
            "desired_replicas": parse_int(_get(row, "Desired Replicas"), minimum=0, maximum=MAX_REPLICAS),
            "ready_replicas": parse_int(_get(row, "Ready Replicas"), minimum=0, maximum=MAX_REPLICAS),
            "health": health,
            "health_raw": health_raw[:20],
            "node": node,
            "node_name": node_name,
            "image": image[:600],
            "image_registry": registry[:253],
            "image_tag": tag[:128],
            "image_digest": digest[:100],
            "image_latest": bool(image) and latest,
            "ports": parse_ports(_get(row, "Port", "Ports")),
            "url": urls[0] if urls else "",
            "urls": urls,
            "storage_attached": parse_list(_get(row, "Storage Attached", "Storage")),
            "pod_status": pod_status,
            "pod_status_raw": pod_status_raw[:20],
            "pod_ip": parse_ip(_get(row, "Pod IP")),
        }
        ctx.upsert(Workload, {"namespace": namespace, "workload_name": workload_name[:253],
                              "container": container, "pod_name": pod}, values, sheet=APP_SHEET)
    if scope.unknown_nodes:
        ctx.warn(APP_SHEET, f"{len(scope.unknown_nodes)} node name(s) are not in the node inventory — those "
                            f"workloads keep the host name as text and are not linked to a node")
    ctx.track(Workload, APP_SHEET, cluster_id=scope.cluster.pk)


register_sheet(APP_SHEET, 60, import_applications)


# ----------------------------------------------------------------------------- 05_Service_Inventory
def import_services(ctx, sheet):
    scope = _Scope(ctx, SERVICE_SHEET)
    for row in sheet.rows:
        ns_name = clean(_get(row, "Namespace"))
        name = clean(_get(row, "Service", "Service Name", "Name"))
        if not ns_name or not name:
            ctx.count(SERVICE_SHEET, "skipped")
            ctx.warn(SERVICE_SHEET, "a row has no namespace or service name and was skipped")
            continue
        client, client_label = scope.client(_get(row, "Client"))
        environment = clean(_get(row, "Environment"))[:40]
        namespace = scope.namespace(ns_name, client_label, environment)
        stype, stype_raw = _service_type(_get(row, "Service Type", "Type"))
        raw_cluster_ip = str(row.get("Cluster IP") or "").strip()
        port = parse_int(_get(row, "Service Port", "Port"), minimum=0, maximum=MAX_PORT)
        protocol = clean(_get(row, "Protocol")).upper()[:10]
        values = {
            "cluster": scope.cluster,
            "client": client,
            "client_label": client_label,
            "environment": environment,
            "application": clean(_get(row, "Application"))[:253],
            "service_type": stype,
            "type_raw": stype_raw[:40],
            "cluster_ip": parse_ip(raw_cluster_ip),
            "headless": raw_cluster_ip.lower() == "none",
            "external_ip": clean(_get(row, "External IP"))[:253],
            "loadbalancer_ip": clean(_get(row, "LoadBalancer IP", "Load Balancer IP"))[:253],
            "node_port": parse_int(_get(row, "NodePort", "Node Port"), minimum=0, maximum=MAX_PORT),
            "target_port": clean(_get(row, "Target Port"))[:63],
            "selector": parse_labels(_get(row, "Selector")),
            "status": clean(_get(row, "Status"))[:40],
        }
        ctx.upsert(KubeService, {"namespace": namespace, "name": name[:253], "port": port,
                                 "protocol": protocol}, values, sheet=SERVICE_SHEET)
    ctx.track(KubeService, SERVICE_SHEET, cluster_id=scope.cluster.pk)


register_sheet(SERVICE_SHEET, 70, import_services)


# ----------------------------------------------------------------------------- 07_PV_PVC_Storage
def import_volumes(ctx, sheet):
    scope = _Scope(ctx, VOLUME_SHEET)
    for row in sheet.rows:
        pvc = clean(_get(row, "PVC", "PVC Name"))[:253]
        pv = clean(_get(row, "PV", "PV Name"))[:253]
        if not pvc and not pv:
            ctx.count(VOLUME_SHEET, "skipped")
            ctx.warn(VOLUME_SHEET, "a row has neither a PVC nor a PV name and was skipped")
            continue
        client, client_label = scope.client(_get(row, "Client"))
        environment = clean(_get(row, "Environment"))[:40]
        ns_name = clean(_get(row, "Namespace"))
        # An unclaimed PV has no namespace: the row stays, with no namespace, rather than being dropped.
        namespace = scope.namespace(ns_name, client_label, environment) if ns_name else None
        capacity = _get(row, "Capacity")
        requested = _get(row, "Requested", "Requested Storage")
        age = _get(row, "Age")
        risk, risk_raw = _risk(_get(row, "Risk"))
        values = {
            "client": client,
            "client_label": client_label,
            "environment": environment,
            "application": clean(_get(row, "Application"))[:253],
            "pvc_status": _status_word(_get(row, "PVC Status"))[:20],
            "pv_status": _status_word(_get(row, "PV Status"))[:20],
            "capacity_bytes": parse_quantity(capacity),
            "capacity_raw": clean(capacity)[:40],
            "requested_bytes": parse_quantity(requested),
            "requested_raw": clean(requested)[:40],
            "access_mode": clean(_get(row, "Access Mode"))[:40],
            "storage_class": clean(_get(row, "Storage Class"))[:253],
            "reclaim_policy": clean(_get(row, "Reclaim Policy"))[:40],
            "volume_mode": clean(_get(row, "Volume Mode"))[:40],
            "provisioner": clean(_get(row, "Provisioner"))[:253],
            "workload_ref": clean(_get(row, "Workload")),
            "pod": clean(_get(row, "Pod"))[:253],
            "mount_path": clean(_get(row, "Mount Path"))[:500],
            "age_days": parse_age(age),
            "age_raw": clean(age)[:40],
            "risk": risk,
            "risk_raw": risk_raw[:20],
            "remarks": clean(_get(row, "Remarks"))[:300],
        }
        ctx.upsert(Volume, {"cluster": scope.cluster, "namespace": namespace, "pvc": pvc, "pv": pv}, values,
                   sheet=VOLUME_SHEET)
    ctx.track(Volume, VOLUME_SHEET, cluster_id=scope.cluster.pk)


register_sheet(VOLUME_SHEET, 80, import_volumes)
