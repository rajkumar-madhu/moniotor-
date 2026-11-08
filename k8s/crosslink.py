"""Linking imported Kubernetes nodes to the inventory the console already holds.

A node discovered in a cluster is usually the same machine as a ``cmdb.ConfigurationItem`` and/or a
``network.Device`` that someone recorded by hand. Matching them means the Kubernetes pages can link
straight to the CI, and the CMDB gains the cluster view, without anyone re-typing anything.

Matching is deliberately conservative — exact name, exact Prometheus instance host, or exact IP address.
A near-miss is left unlinked rather than guessed, and an existing link is never overwritten by a weaker
match. Other apps are reached lazily through ``apps.get_model`` so this module imports even when one of
them is absent.
"""
import logging

from django.apps import apps

log = logging.getLogger("linkedeye.k8s")


def _model(app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:  # pragma: no cover - only when an app is removed from INSTALLED_APPS
        return None


def _instance_host(value):
    """'prod-oms-01:9100' -> 'prod-oms-01'."""
    return (value or "").split(":")[0].strip().lower()


def find_ci(tenant_key, hostname="", ip=""):
    """The configuration item that describes this host, or None."""
    ConfigurationItem = _model("cmdb", "ConfigurationItem")
    if ConfigurationItem is None:
        return None
    qs = ConfigurationItem.objects.filter(tenant_key=tenant_key)
    host = (hostname or "").strip()
    if host:
        found = qs.filter(name__iexact=host).first()
        if found:
            return found
        for candidate in qs.filter(prometheus_instance__istartswith=host)[:20]:
            if _instance_host(candidate.prometheus_instance) == host.lower():
                return candidate
    if ip:
        return qs.filter(ip_address=ip).first()
    return None


def find_device(tenant_key, hostname="", ip=""):
    """The network device that describes this host, or None."""
    Device = _model("network", "Device")
    if Device is None:
        return None
    qs = Device.objects.filter(tenant_key=tenant_key)
    host = (hostname or "").strip()
    if host:
        found = qs.filter(name__iexact=host).first()
        if found:
            return found
    if ip:
        found = qs.filter(mgmt_ip=ip).first()
        if found:
            return found
        for candidate in qs.filter(prometheus_instance__istartswith=ip)[:20]:
            if _instance_host(candidate.prometheus_instance) == ip:
                return candidate
    return None


def link_node(node, tenant_key=None):
    """Point `node` at the CI and device that already describe it. Returns the fields it changed."""
    tenant_key = tenant_key or node.tenant_key
    ip = node.internal_ip or node.external_ip or ""
    changed = []
    if node.ci_id is None:
        ci = find_ci(tenant_key, node.hostname, ip)
        if ci is not None:
            node.ci = ci
            changed.append("ci")
    if node.device_id is None:
        device = find_device(tenant_key, node.hostname, ip)
        if device is not None:
            node.device = device
            changed.append("device")
    if changed:
        node.save(update_fields=changed + ["updated_at"])
    return changed


# ----------------------------------------------------------------------------- compute mirror (opt-in)
def ensure_compute(ctx, cluster, nodes, sheet=""):
    """Mirror a KubeCluster into ``compute.Cluster`` + ``ClusterMember`` rows (opt-in).

    Only runs when the import was asked for it (``--create-compute`` / the upload page's checkbox),
    because it writes into the device inventory: a node with no matching ``network.Device`` gets one
    (type "Physical Server"). Everything is get-or-create keyed on the tenant and the name, so a
    re-import links the same rows again instead of duplicating them.
    """
    Cluster = _model("compute", "Cluster")
    ClusterMember = _model("compute", "ClusterMember")
    Device = _model("network", "Device")
    if Cluster is None or ClusterMember is None or Device is None:
        ctx.warn(sheet, "compute or network is not installed — no compute cluster was created")
        return None
    from compute.models import ClusterKind, MemberRole

    compute_cluster = Cluster.objects.filter(tenant_key=ctx.tenant, name=cluster.name).first()
    if compute_cluster is None:
        compute_cluster = Cluster.objects.create(
            tenant_key=ctx.tenant, name=cluster.name, kind=ClusterKind.KUBERNETES, site=cluster.site,
            description="Created from a Kubernetes inventory import.")
        ctx.count(sheet or "compute", "created")
    elif compute_cluster.kind != ClusterKind.KUBERNETES:
        ctx.warn(sheet, f"a compute cluster named “{cluster.name}” already exists and is not a "
                        f"Kubernetes cluster — it was left alone")
        return None
    if cluster.compute_cluster_id != compute_cluster.pk:
        cluster.compute_cluster = compute_cluster
        cluster.save(update_fields=["compute_cluster", "updated_at"])

    for node in nodes:
        device = node.device
        if device is None:
            device = Device.objects.filter(tenant_key=ctx.tenant, name=node.hostname).first()
        if device is None:
            device = Device.objects.create(
                tenant_key=ctx.tenant, name=node.hostname, device_type="physical_server",
                mgmt_ip=node.internal_ip or node.external_ip or node.hostname, site=cluster.site,
                role="Kubernetes node", description="Created from a Kubernetes inventory import.")
        role = MemberRole.CONTROL_PLANE if node.is_control_plane else MemberRole.WORKER
        member = ClusterMember.objects.filter(tenant_key=ctx.tenant, cluster=compute_cluster,
                                              device=device).first()
        if member is None:
            member = ClusterMember.objects.create(tenant_key=ctx.tenant, cluster=compute_cluster,
                                                  device=device, role=role)
        elif member.role != role:
            member.role = role
            member.save(update_fields=["role", "updated_at"])
        fields = []
        if node.device_id != device.pk:
            node.device = device
            fields.append("device")
        if node.member_id != member.pk:
            node.member = member
            fields.append("member")
        if fields:
            node.save(update_fields=fields + ["updated_at"])
    return compute_cluster
