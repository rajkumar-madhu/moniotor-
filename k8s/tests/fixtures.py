"""Synthetic inventory sheets for the k8s tests.

**No customer data lives in this repository.** Everything below is make-believe: the clients are "Acme" and
"Globex", addresses come from the RFC 5737 documentation range (203.0.113.0/24, 198.51.100.0/24) and the
private range 10.0.0.0/8, and host names are under example.com. The real workbook is never copied here —
not a row, not a host name, not a URL, not a Secret name.
"""
import csv
from pathlib import Path

# ------------------------------------------------------------------ 02_Client_Master
CLIENT_MASTER = [
    ["Client ID", "Client Name", "Project Name", "Environment", "Primary Namespace", "Node Hostname",
     "Private IP", "Public IP", "Kubernetes Version", "Application Count", "Service Count", "Ingress Count",
     "PV Count", "PVC Count", "Total Storage", "Primary Application URL", "Monitoring URL", "CI/CD URL",
     "Status", "Owner", "Remarks"],
    ["CL-001", "Acme", "acme-prod, acme-uat", "PROD, UAT", "acme-prod", "NOT DISCOVERED", "NOT DISCOVERED",
     "NOT DISCOVERED", "MULTIPLE / SEE NODE INVENTORY", "12", "9", "3", "4", "4", "140.00 Gi",
     "https://app.example.com", "https://grafana.example.com", "https://ci.example.com", "GREEN",
     "MANUAL INPUT REQUIRED", "Evidence-based mapping"],
    ["CL-002", "Globex", "globex-prod", "NOT DISCOVERED", "globex-prod", "NOT DISCOVERED", "NOT DISCOVERED",
     "NOT DISCOVERED", "MULTIPLE / SEE NODE INVENTORY", "3", "2", "0", "0", "0", "0 Gi", "NOT DISCOVERED",
     "NOT DISCOVERED", "NOT DISCOVERED", "AMBER", "MANUAL INPUT REQUIRED", "Evidence-based mapping"],
]

# ------------------------------------------------------------------ 03_Node_Inventory
NODE_INVENTORY = [
    ["Cluster", "Hostname", "Role", "Internal IP", "External IP", "Public/Private", "Kubernetes Version",
     "OS", "Kernel", "Container Runtime", "Architecture", "CPU Capacity", "CPU Allocatable",
     "Memory Capacity", "Memory Allocatable", "Pod Capacity", "Ready", "Schedulable", "Age", "Labels",
     "Taints"],
    ["kubernetes", "cp-1.example.com", "Control Plane", "10.0.0.11", "NOT DISCOVERED", "Private", "v1.30.1",
     "Ubuntu 24.04 LTS", "6.8.0-generic", "containerd://2.0.0", "amd64", "8", "8", "32866108Ki",
     "32763708Ki", "110", "True", "Yes", "134d",
     "kubernetes.io/arch=amd64; node-role.kubernetes.io/control-plane=; tier=platform",
     "node-role.kubernetes.io/control-plane=:NoSchedule"],
    ["kubernetes", "worker-1.example.com", "Worker/Unlabeled", "10.0.0.12", "203.0.113.10", "Public",
     "v1.30.1", "Ubuntu 24.04 LTS", "6.8.0-generic", "containerd://2.0.0", "amd64", "16", "16",
     "74098976Ki", "73996576Ki", "1k", "True", "Yes", "3d4h",
     "kubernetes.io/arch=amd64; tier=apps", "NOT APPLICABLE"],
    ["kubernetes", "worker-2.example.com", "Worker/Unlabeled", "10.0.0.13", "NOT DISCOVERED", "Private",
     "v1.30.1", "Ubuntu 22.04 LTS", "5.15.0-generic", "containerd://1.7.0", "amd64", "24", "24", "2.70 Ti",
     "2.70 Ti", "110", "False", "No", "18d", "kubernetes.io/arch=amd64", "NOT APPLICABLE"],
]

# ------------------------------------------------------------------ 13_Client_Summary
CLIENT_SUMMARY = [
    ["Client Group", "Nodes", "Namespaces", "Applications", "URLs", "Storage", "Monitoring", "CI/CD",
     "Authentication", "Current Issues", "Overall Health"],
    ["Acme", "NOT DISCOVERED", "2", "12", "5", "140.00 Gi", "Grafana, Prometheus", "Argocd, Vault",
     "See credential references / MANUAL INPUT REQUIRED", "2", "AMBER"],
    ["Others/Unmapped", "NOT DISCOVERED", "1", "4", "0", "2.70 Ti", "NOT DISCOVERED", "NOT DISCOVERED",
     "See credential references / MANUAL INPUT REQUIRED", "7", "RED"],
]

# ------------------------------------------------------------------ 14_Namespace_Mapping
NAMESPACE_MAPPING = [
    ["Namespace", "Client", "Environment", "Confidence", "Evidence", "Status"],
    ["acme-prod", "Acme", "PROD", "High", "Namespace/label naming match", "Mapped"],
    ["acme-uat", "Acme", "UAT", "High", "Namespace/label naming match", "Mapped"],
    ["globex-prod", "Globex", "NOT DISCOVERED", "Medium", "Namespace/label naming match", "Mapped"],
    ["sandbox", "UNMAPPED / REVIEW REQUIRED", "NOT DISCOVERED", "Low", "No confident known-client pattern",
     "REVIEW REQUIRED"],
]

# ------------------------------------------------------------------ 15_RAW_Discovery
RAW_DISCOVERY = [
    ["Category", "Name", "Command / Reference", "Return Code", "Details"],
    ["Metadata", "Discovery Timestamp", "Local collector", "0", "2026-01-02T03:04:05+00:00"],
    ["Metadata", "Cluster Context", "kubectl config current-context", "0", "admin@example-cluster"],
    ["Command", "nodes_wide", "kubectl get nodes -o wide", "0", "Completed"],
    ["Command", "namespaces_json", "kubectl get namespaces -o json", "0", "Completed"],
    ["Command", "secrets_safe", "kubectl get secrets -A", "1", "Not permitted"],
    ["Resource Count", "nodes", "kubectl get nodes -o json (sanitized)", "0", "3"],
    ["Resource Count", "namespaces", "kubectl get namespaces -o json (sanitized)", "0", "4"],
    ["Safety", "Secrets", "kubectl get secrets -A (names/types only)", "0",
     "Secret values were not fetched, decoded, or stored"],
]

SHEETS = {
    "02_Client_Master": CLIENT_MASTER,
    "03_Node_Inventory": NODE_INVENTORY,
    "13_Client_Summary": CLIENT_SUMMARY,
    "14_Namespace_Mapping": NAMESPACE_MAPPING,
    "15_RAW_Discovery": RAW_DISCOVERY,
}

# Sheets the workbook carries that this module ignores on purpose.
IGNORED_SHEETS = {
    "01_Executive_Dashboard": [["Kubernetes Client Infrastructure"], ["Generated"]],
    "CL_Acme": [["Section", "Detail"], ["Namespaces", "acme-prod"]],
}


def write_csv_dir(directory, sheets=None, extra=None):
    """Write `sheets` (default: all of them) as CSV files named after the sheets. Returns the directory."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, rows in (sheets if sheets is not None else SHEETS).items():
        with open(directory / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    for name, rows in (extra or {}).items():
        with open(directory / f"{name}.csv", "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    return directory


def write_xlsx(path, sheets=None):
    """Write the same sheets as a real .xlsx workbook with openpyxl. Returns the path."""
    import openpyxl
    book = openpyxl.Workbook()
    book.remove(book.active)
    for name, rows in {**IGNORED_SHEETS, **(sheets if sheets is not None else SHEETS)}.items():
        sheet = book.create_sheet(title=name)
        for row in rows:
            sheet.append(row)
    book.save(str(path))
    return path


def sheets_without(*names):
    return {k: v for k, v in SHEETS.items() if k not in names}


def nodes_without(hostname):
    """The node sheet with one host removed, to test the "no longer present" handling."""
    rows = [NODE_INVENTORY[0]] + [r for r in NODE_INVENTORY[1:] if r[1] != hostname]
    return {**SHEETS, "03_Node_Inventory": rows}
