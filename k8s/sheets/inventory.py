"""Importers for the inventory backbone: discovery metadata, nodes, clients and namespace mapping.

Sheets handled here:

===========================  =====  ====================================================================
Sheet                        Order  Goes to
===========================  =====  ====================================================================
``15_RAW_Discovery``            10  ``ImportRun.metadata`` (commands run, resource counts) — no model
``03_Node_Inventory``           20  ``KubeCluster`` + ``Node`` (+ optional compute mirror)
``02_Client_Master``            30  ``ClientProfile``
``13_Client_Summary``           40  ``ClientProfile`` roll-up columns
``14_Namespace_Mapping``        50  ``Namespace``
===========================  =====  ====================================================================

Sheets 04–12 are registered by their own modules; they run after these, so ``ctx.cluster_for()`` and
``ctx.resolve_namespace()`` already have something to hand back.
"""
from datetime import datetime, timezone as dt_timezone

from django.utils import timezone

from ..crosslink import ensure_compute, link_node
from ..importer import register_sheet
from ..models import (Confidence, Exposure, HEALTH_FROM_WORKBOOK, Health, KubeCluster, MappingStatus, Namespace,
                      Node, NodeRole, ClientProfile)
from ..parsing import (clean, is_unmapped, parse_age, parse_bool, parse_cpu, parse_int, parse_ip, parse_labels,
                       parse_list, parse_pods, parse_quantity, parse_taints, parse_url)

MAX_COUNT = 10 ** 9


def _get(row, *names):
    """First non-empty value among `names` (sheets spell some headers slightly differently)."""
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


def _health(value):
    text = clean(value)
    return HEALTH_FROM_WORKBOOK.get(text.upper(), Health.UNKNOWN), text


# ----------------------------------------------------------------------------- 15_RAW_Discovery
RAW_SHEET = "15_RAW_Discovery"
MAX_RAW_ENTRIES = 500       # the run keeps an audit trail, not a copy of the sheet: bound what is stored
TIMESTAMP_NAMES = {"discovery timestamp", "timestamp"}
CONTEXT_NAMES = {"cluster context", "context", "current_context"}


def _parse_timestamp(text):
    text = clean(text)
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt_timezone.utc)


def import_raw_discovery(ctx, sheet):
    """Keep the discovery audit trail (which read-only commands ran, what they counted) on the run.

    This sheet is evidence about the snapshot, not inventory, so it is stored as run metadata rather
    than as rows: it is what lets someone reproduce the collection (docs/IMPORT.md lists the commands).
    """
    commands, counts, metadata, safety = [], {}, {}, []
    for row in sheet.rows:
        category = clean(_get(row, "Category"))
        name = clean(_get(row, "Name"))
        reference = clean(_get(row, "Command / Reference", "Command", "Reference"))
        details = clean(_get(row, "Details"))
        code = parse_int(_get(row, "Return Code"))
        if not name:
            ctx.count(RAW_SHEET, "skipped")
            continue
        if category.lower() == "command":
            if len(commands) < MAX_RAW_ENTRIES:
                commands.append({"name": name[:120], "command": reference[:300], "return_code": code,
                                 "details": details[:300]})
        elif category.lower() == "resource count":
            number = parse_int(details, minimum=0, maximum=MAX_COUNT)
            if number is None:
                ctx.count(RAW_SHEET, "skipped")
                continue
            if len(counts) < MAX_RAW_ENTRIES:
                counts[name[:120]] = number
        elif category.lower() == "safety":
            if len(safety) < MAX_RAW_ENTRIES:
                safety.append({"name": name[:120], "note": (details or reference)[:300]})
        else:
            if len(metadata) < MAX_RAW_ENTRIES:
                metadata[name[:120]] = (details or reference)[:300]
            if name.lower() in TIMESTAMP_NAMES:
                ctx.discovered_at = _parse_timestamp(details)
            elif name.lower() in CONTEXT_NAMES:
                ctx.cluster_context = details[:200]
    for name, value in metadata.items():
        if name.lower() in CONTEXT_NAMES and not ctx.cluster_context:
            ctx.cluster_context = value[:200]
    failed = [c["name"] for c in commands if c["return_code"] not in (None, 0)]
    if failed:
        ctx.warn(RAW_SHEET, f"{len(failed)} discovery command(s) did not return 0: {', '.join(failed[:8])}")
    ctx.note("discovery", {"commands": commands, "resource_counts": counts, "metadata": metadata,
                            "safety": safety})
    if ctx.discovered_at:
        ctx.note("discovered_at", ctx.discovered_at.isoformat())
    if ctx.cluster_context:
        ctx.note("cluster_context", ctx.cluster_context)
    ctx.touched[RAW_SHEET] = len(sheet.rows)     # read, even though it writes no model rows


register_sheet(RAW_SHEET, 10, import_raw_discovery)


# ----------------------------------------------------------------------------- 03_Node_Inventory
NODE_SHEET = "03_Node_Inventory"


def _role(value):
    text = clean(value)
    lowered = text.lower()
    if "control" in lowered or "master" in lowered:
        return NodeRole.CONTROL_PLANE, text
    if "worker" in lowered or "node" in lowered or "unlabeled" in lowered:
        return NodeRole.WORKER, text
    return NodeRole.UNKNOWN, text


def _exposure(value):
    lowered = clean(value).lower()
    if lowered.startswith("pub"):
        return Exposure.PUBLIC
    if lowered.startswith("priv"):
        return Exposure.PRIVATE
    return Exposure.UNKNOWN


def _versions_label(versions):
    distinct = sorted({v for v in versions if v})
    if not distinct:
        return ""
    if len(distinct) == 1:
        return distinct[0][:40]
    return f"{len(distinct)} versions"[:40]


def import_nodes(ctx, sheet):
    clusters, nodes_by_cluster, versions = {}, {}, {}
    for row in sheet.rows:
        hostname = clean(_get(row, "Hostname", "Node", "Node Hostname"))
        if not hostname:
            ctx.warn(NODE_SHEET, "a row has no hostname and was skipped")
            ctx.count(NODE_SHEET, "skipped")
            continue
        cluster_name = clean(_get(row, "Cluster")) or ""
        cluster = ctx.cluster_for(cluster_name)
        clusters[cluster.pk] = cluster
        version = clean(_get(row, "Kubernetes Version"))
        versions.setdefault(cluster.pk, []).append(version)

        role, role_raw = _role(_get(row, "Role"))
        memory_capacity = _get(row, "Memory Capacity")
        memory_allocatable = _get(row, "Memory Allocatable")
        cpu_capacity = _get(row, "CPU Capacity")
        cpu_allocatable = _get(row, "CPU Allocatable")
        pods = _get(row, "Pod Capacity", "Pods")
        age = _get(row, "Age")
        taints = _get(row, "Taints")
        values = {
            "role": role,
            "role_raw": role_raw[:60],
            "internal_ip": parse_ip(_get(row, "Internal IP", "Private IP")),
            "external_ip": parse_ip(_get(row, "External IP", "Public IP")),
            "exposure": _exposure(_get(row, "Public/Private", "Exposure")),
            "kubernetes_version": version[:40],
            "os": clean(_get(row, "OS"))[:120],
            "kernel": clean(_get(row, "Kernel"))[:120],
            "container_runtime": clean(_get(row, "Container Runtime"))[:120],
            "architecture": clean(_get(row, "Architecture"))[:40],
            "cpu_capacity_millicores": parse_cpu(cpu_capacity),
            "cpu_capacity_raw": clean(cpu_capacity)[:40],
            "cpu_allocatable_millicores": parse_cpu(cpu_allocatable),
            "cpu_allocatable_raw": clean(cpu_allocatable)[:40],
            "memory_capacity_bytes": parse_quantity(memory_capacity),
            "memory_capacity_raw": clean(memory_capacity)[:40],
            "memory_allocatable_bytes": parse_quantity(memory_allocatable),
            "memory_allocatable_raw": clean(memory_allocatable)[:40],
            "pod_capacity": parse_pods(pods),
            "pod_capacity_raw": clean(pods)[:40],
            "ready": parse_bool(_get(row, "Ready")),
            "schedulable": parse_bool(_get(row, "Schedulable")),
            "age_days": parse_age(age),
            "age_raw": clean(age)[:40],
            "labels": parse_labels(_get(row, "Labels")),
            "taints": parse_taints(taints),
            "taints_raw": clean(taints),
        }
        node, _created = ctx.upsert(Node, {"cluster": cluster, "hostname": hostname[:255]}, values,
                                    sheet=NODE_SHEET)
        link_node(node, ctx.tenant)
        nodes_by_cluster.setdefault(cluster.pk, []).append(node)

    for pk, cluster in clusters.items():
        label = _versions_label(versions.get(pk, []))
        if label and cluster.kubernetes_version != label:
            cluster.kubernetes_version = label
            cluster.save(update_fields=["kubernetes_version", "updated_at"])
        if ctx.create_compute:
            ensure_compute(ctx, cluster, nodes_by_cluster.get(pk, []), sheet=NODE_SHEET)

    ctx.track(KubeCluster, NODE_SHEET)
    if clusters:
        ctx.track(Node, NODE_SHEET, cluster_id__in=sorted(clusters))


register_sheet(NODE_SHEET, 20, import_nodes)


# ----------------------------------------------------------------------------- 02_Client_Master
CLIENT_SHEET = "02_Client_Master"


def import_client_master(ctx, sheet):
    unlinked = []
    for row in sheet.rows:
        name = clean(_get(row, "Client Name", "Client"))
        if not name:
            ctx.warn(CLIENT_SHEET, "a row has no client name and was skipped")
            ctx.count(CLIENT_SHEET, "skipped")
            continue
        client = ctx.resolve_client(name)
        if client is None and not is_unmapped(name):
            unlinked.append(name)
        storage = _get(row, "Total Storage", "Storage")
        status, status_raw = _health(_get(row, "Status"))
        values = {
            "client": client,
            "site": ctx.site,
            "workbook_id": clean(_get(row, "Client ID"))[:40],
            "project_names": parse_list(_get(row, "Project Name", "Project Names", "Project")),
            "environments": parse_list(_get(row, "Environment", "Environments")),
            "primary_namespace": clean(_get(row, "Primary Namespace"))[:253],
            "app_count": parse_int(_get(row, "Application Count"), minimum=0, maximum=MAX_COUNT),
            "service_count": parse_int(_get(row, "Service Count"), minimum=0, maximum=MAX_COUNT),
            "ingress_count": parse_int(_get(row, "Ingress Count"), minimum=0, maximum=MAX_COUNT),
            "pv_count": parse_int(_get(row, "PV Count"), minimum=0, maximum=MAX_COUNT),
            "pvc_count": parse_int(_get(row, "PVC Count"), minimum=0, maximum=MAX_COUNT),
            "total_storage_bytes": parse_quantity(storage),
            "total_storage_raw": clean(storage)[:40],
            "primary_url": parse_url(_get(row, "Primary Application URL", "Primary URL")),
            "monitoring_url": parse_url(_get(row, "Monitoring URL")),
            "cicd_url": parse_url(_get(row, "CI/CD URL", "CICD URL")),
            "status": status,
            "status_raw": status_raw[:20],
            "remarks": clean(_get(row, "Remarks")),
            "summary_only": False,
        }
        ctx.upsert(ClientProfile, {"workbook_name": name[:150]}, values, sheet=CLIENT_SHEET)
    if unlinked:
        ctx.warn(CLIENT_SHEET, f"{len(unlinked)} workbook client(s) have no matching LinkedEye client yet — "
                               f"link them on the Kubernetes → Clients page")
    # summary-only rows belong to the roll-up sheet, which retires them itself
    ctx.track(ClientProfile, CLIENT_SHEET, summary_only=False)


register_sheet(CLIENT_SHEET, 30, import_client_master)


# ----------------------------------------------------------------------------- 13_Client_Summary
SUMMARY_SHEET = "13_Client_Summary"


def import_client_summary(ctx, sheet):
    """Fold the per-client-group roll-up into the matching client profile.

    The roll-up groups the estate slightly differently from the client master sheet (it has an
    "others / unmapped" bucket, for instance). A group with no matching profile gets its own row,
    flagged ``summary_only`` so the Clients page can say where it came from.
    """
    existing = {p.workbook_name.strip().lower(): p
                for p in ClientProfile.objects.filter(tenant_key=ctx.tenant)}
    seen = set()
    for row in sheet.rows:
        name = clean(_get(row, "Client Group", "Client", "Client Name"))
        if not name:
            ctx.count(SUMMARY_SHEET, "skipped")
            continue
        storage = _get(row, "Storage", "Total Storage")
        health, health_raw = _health(_get(row, "Overall Health", "Health"))
        values = {
            "summary_namespaces": parse_int(_get(row, "Namespaces"), minimum=0, maximum=MAX_COUNT),
            "summary_applications": parse_int(_get(row, "Applications"), minimum=0, maximum=MAX_COUNT),
            "summary_urls": parse_int(_get(row, "URLs"), minimum=0, maximum=MAX_COUNT),
            "summary_storage_bytes": parse_quantity(storage),
            "summary_storage_raw": clean(storage)[:40],
            "monitoring_tools": parse_list(_get(row, "Monitoring")),
            "cicd_tools": parse_list(_get(row, "CI/CD", "CICD")),
            "open_issues": parse_int(_get(row, "Current Issues", "Issues"), minimum=0, maximum=MAX_COUNT),
            "health": health,
            "health_raw": health_raw[:20],
        }
        match = existing.get(name.strip().lower())
        if match is None:
            values.update(summary_only=True, site=ctx.site, client=ctx.resolve_client(name))
            ctx.warn(SUMMARY_SHEET, f"“{name}” is a roll-up group the client master sheet does not list — "
                                    f"it was added as a summary-only row")
        profile, _created = ctx.upsert(ClientProfile, {"workbook_name": name[:150]}, values,
                                       sheet=SUMMARY_SHEET)
        existing.setdefault(name.strip().lower(), profile)
        seen.add(profile.pk)
    if seen:
        gone = (ClientProfile.objects.filter(tenant_key=ctx.tenant, summary_only=True, missing_since__isnull=True)
                .exclude(pk__in=seen).update(missing_since=timezone.now()))
        if gone:
            ctx.count(SUMMARY_SHEET, "missing", gone)


register_sheet(SUMMARY_SHEET, 40, import_client_summary)


# ----------------------------------------------------------------------------- 14_Namespace_Mapping
NAMESPACE_SHEET = "14_Namespace_Mapping"
CONFIDENCE_WORDS = {"high": Confidence.HIGH, "medium": Confidence.MEDIUM, "med": Confidence.MEDIUM,
                    "low": Confidence.LOW}


def _mapping_status(value, client_label):
    text = clean(value).lower()
    if "review" in text or "unmapped" in text:
        return MappingStatus.REVIEW
    if text.startswith("map"):
        return MappingStatus.MAPPED
    if is_unmapped(client_label) or not clean(client_label):
        return MappingStatus.REVIEW
    return MappingStatus.UNKNOWN


def import_namespace_mapping(ctx, sheet):
    cluster = ctx.cluster_for()
    review = 0
    for row in sheet.rows:
        name = clean(_get(row, "Namespace"))
        if not name:
            ctx.count(NAMESPACE_SHEET, "skipped")
            continue
        label = clean(_get(row, "Client"))
        unmapped = is_unmapped(label)
        status = _mapping_status(_get(row, "Status", "Mapping Status"), label)
        if status == MappingStatus.REVIEW:
            review += 1
        values = {
            "site": cluster.site,
            "client": None if unmapped else ctx.resolve_client(label),
            "client_label": "" if unmapped else label[:150],
            "environment": clean(_get(row, "Environment"))[:40],
            "confidence": CONFIDENCE_WORDS.get(clean(_get(row, "Confidence")).lower(), Confidence.UNKNOWN),
            "evidence": clean(_get(row, "Evidence"))[:300],
            "mapping_status": status,
        }
        namespace, _created = ctx.upsert(Namespace, {"cluster": cluster, "name": name[:253]}, values,
                                         sheet=NAMESPACE_SHEET)
        ctx.remember_namespace(namespace)
    if review:
        ctx.warn(NAMESPACE_SHEET, f"{review} namespace(s) are marked review required — their client is a "
                                  f"guess, not a fact")
    ctx.track(Namespace, NAMESPACE_SHEET, cluster_id=cluster.pk)


register_sheet(NAMESPACE_SHEET, 50, import_namespace_mapping)
