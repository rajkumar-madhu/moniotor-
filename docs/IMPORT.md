# Kubernetes client inventory import

The `k8s` module holds a client's Kubernetes estate as a **read-only `kubectl` discovery** recorded it. The
discovery runs outside LinkedEye, on a machine that already has `kubectl` access, and produces one workbook;
LinkedEye imports that workbook.

```bash
# the whole workbook
manage.py import_k8s_inventory /path/to/inventory.xlsx

# see what would change, write nothing
manage.py import_k8s_inventory /path/to/inventory.xlsx --dry-run

# file the records under one site, and mirror the cluster into Compute → Clusters
manage.py import_k8s_inventory /path/to/inventory.xlsx --site "Mumbai (NSE)" --create-compute
```

Or in the console: **Kubernetes → Imports → Import workbook** (`/kubernetes/import/`). The upload shows the
dry-run diff first and writes nothing until the confirm button is pressed. It is open to **admins on every
site** only, because one workbook describes an estate that spans sites.

A source can be:

* the `.xlsx` workbook (read with `openpyxl`, which is in `requirements.txt` and imported lazily — a CSV
  import needs nothing extra);
* a `.zip` of the sheets exported as CSV, each file named after its sheet;
* a directory of those CSV files (handy in CI and in the tests).

---

## Data handling — read this first

**No part of a client workbook belongs in this repository.** Not a row, a host name, an IP address, a client
name, a URL or a Kubernetes Secret name — not in tests, fixtures, seed commands, documentation or comments.
The tests use synthetic fixtures written for the purpose (`k8s/tests/fixtures.py`: make-believe clients
"Acme" and "Globex", RFC 5737 documentation addresses, `example.com` host names). A real workbook lives
wherever the customer keeps it and is handed to the importer by path or upload.

**Secret names only, never values.** The discovery lists Kubernetes Secrets by name, type and namespace and
never reads, decodes or stores their contents (the workbook's own raw-discovery sheet records this as a
safety note, which the import keeps and the import detail page shows). The `k8s` models have no field that
could hold a secret value, and `k8s/tests/test_models.py` asserts that none is ever added.

**Placeholders mean "no value".** The collector writes a fixed set of strings where it found nothing:

| Cell | Meaning |
|---|---|
| `NOT DISCOVERED` | the collector could not read this |
| `NOT APPLICABLE` | the resource has no such property |
| `MANUAL INPUT REQUIRED` | a human has to fill this in; the discovery cannot |
| `MULTIPLE / SEE NODE INVENTORY` | the value differs per node; the node sheet has it |

`k8s.parsing.clean()` turns each of them into an empty value, centrally, for every sheet. They are never
stored and never shown: a page prints a dash instead, so an empty cell never reads as a fact.

`UNMAPPED / REVIEW REQUIRED` is **not** a placeholder — it is a real finding, "no client could be
determined". It is kept as such: the namespace gets no client and is flagged **Review required** on the
Namespaces page, so a guess is never presented as a mapping.

---

## What an import does

* **Idempotent.** Importing the same workbook twice changes nothing: rows are matched on their natural key
  within the tenant (cluster name; cluster + host name; cluster + namespace; workbook client name) and only
  changed fields are written.
* **Versioned.** Every imported row carries `first_seen`, `last_seen` and `missing_since`. A row the newest
  snapshot no longer contains gets `missing_since` and is shown as "no longer present"; if it comes back,
  that stamp is cleared. Nothing is ever deleted by an import.
* **Scoped.** A source that holds only some sheets only speaks for those sheets — the rows owned by a sheet
  that is absent are left untouched rather than retired.
* **Damaged sheets retire nothing.** A sheet that failed, or whose rows could not be read at all (its key
  column was renamed or removed), is not taken to mean "everything is gone": its rows are left as they were,
  and namespaces that only other sheets mention are kept while any sheet of the run was unreadable.
* **Namespaces need every contributing sheet.** A namespace can be created by any of the workload, service,
  volume, ingress, certificate, secret, monitoring, tool and issue sheets as well as the mapping sheet. A
  source that lacks one of those sheets leaves the namespaces nothing mentions any more as they were (with a
  warning) instead of retiring namespaces the absent sheets still use.
* **Size limits fail closed.** A sheet is read up to 100,000 data rows (200,000 scanned rows) and 2,000,000
  cells; one upload may hold 5,000,000 cells. A sheet that is longer than the row limit is **not imported at
  all** - the run reports an error naming the sheet and its records are left untouched - because importing
  only its first rows would make the rest look "no longer present". A source over a cell limit is refused
  as unreadable. Only the sheets the importer knows are parsed; others are listed as unknown without being read.
* **Large files.** The upload page runs the import inside the web request, so a very large workbook can hit the
  web worker's time limit. Use `manage.py import_k8s_inventory` for those. A run still "running" an hour after
  it started (the process was killed) is marked failed when the import pages are next opened.
* **Per sheet.** Each sheet runs in its own transaction, so one broken sheet is reported and rolled back
  while the others still import. The run ends as "failed" if anything was reported.
* **Audited.** The upload writes an `AuditEvent` and emits a `core.events` event; every run is kept as an
  `ImportRun` with its source name, SHA-256, per-sheet counts, warnings and errors.
* **Dry run.** `--dry-run` (and the upload's first step) runs the whole import inside one transaction and
  rolls it back, so the numbers are real and the database is untouched — including the `ImportRun` row,
  which is why dry runs do not appear in the import history.

### Cross-links

* A node is linked to a `cmdb.ConfigurationItem` when the CI's **name** equals the host name, its
  **`prometheus_instance`** host part equals the host name, or its **`ip_address`** equals the node's
  internal IP; to a `network.Device` on name or `mgmt_ip`. Matching is exact — a near miss is left unlinked
  rather than guessed, and an existing link is never overwritten.
* `--create-compute` (or the upload's checkbox) additionally mirrors the cluster into
  `compute.Cluster` (kind *Kubernetes*) with a `ClusterMember` per node, creating a `network.Device` of type
  *Physical Server* for any node that is not in the device inventory yet. It is opt-in precisely because it
  writes into the device inventory. Everything is get-or-create, so a re-import links the same rows again.
* Workbook clients are matched to `core.Client` by code or name, ignoring case, spaces and punctuation. A
  client is **never created** by an import; an unmatched row stays unlinked and can be linked by hand on
  **Kubernetes → Clients**.

---

## Workbook format

Row 1 of each sheet holds the column names; one record per row after that. Column names are matched by
name, so extra columns are ignored and a missing column reads as empty. Two sheet groups are skipped on
purpose: `01_Executive_Dashboard` (a layout sheet) and every `CL_*` sheet (per-client repeats of data that
is already in the numbered sheets).

| Sheet | Order | Imported into | Status |
|---|---|---|---|
| `02_Client_Master` | 30 | `ClientProfile` | yes |
| `03_Node_Inventory` | 20 | `KubeCluster`, `Node` | yes |
| `04_Application_Inventory` | 60+ | workloads | page pending |
| `05_Service_Inventory` | 60+ | services | page pending |
| `06_Ingress_URLs` | 60+ | ingresses | page pending |
| `07_PV_PVC_Storage` | 60+ | volumes | page pending |
| `08_Credentials_References` | 60+ | Secret **names** and namespaces | page pending |
| `09_Certificate_Inventory` | 60+ | certificates | page pending |
| `10_Monitoring` | 60+ | monitoring endpoints | page pending |
| `11_CICD_Platform_Tools` | 60+ | platform tools | page pending |
| `12_Issues_Risks` | 60+ | issues and risks | page pending |
| `13_Client_Summary` | 40 | `ClientProfile` roll-up columns | yes |
| `14_Namespace_Mapping` | 50 | `Namespace` | yes |
| `15_RAW_Discovery` | 10 | `ImportRun.metadata` (no model) | yes |

A sheet with no importer yet is read, counted and skipped; the import page lists it as such.

### `02_Client_Master` → `ClientProfile`

`Client ID`, `Client Name`, `Project Name` (comma-separated), `Environment` (comma-separated),
`Primary Namespace`, `Application Count`, `Service Count`, `Ingress Count`, `PV Count`, `PVC Count`,
`Total Storage` (a quantity, parsed to bytes with the raw string kept), `Primary Application URL`,
`Monitoring URL`, `CI/CD URL`, `Status` (GREEN / AMBER / RED), `Remarks`. `Node Hostname`, `Private IP`,
`Public IP`, `Kubernetes Version` and `Owner` are per-node or human-entered values the collector fills with
placeholders; node facts come from `03_Node_Inventory`.

### `03_Node_Inventory` → `KubeCluster` + `Node`

`Cluster`, `Hostname`, `Role`, `Internal IP`, `External IP`, `Public/Private`, `Kubernetes Version`, `OS`,
`Kernel`, `Container Runtime`, `Architecture`, `CPU Capacity`, `CPU Allocatable`, `Memory Capacity`,
`Memory Allocatable`, `Pod Capacity`, `Ready`, `Schedulable`, `Age`, `Labels`, `Taints`.

Parsed centrally by `k8s.parsing`, with the raw string kept beside every parsed number:

| Column | Parsed as | Examples |
|---|---|---|
| `CPU Capacity` / `CPU Allocatable` | millicores | `16` → 16000, `500m` → 500 |
| `Memory Capacity` / `Memory Allocatable` | bytes | `74098976Ki`, `2.70 Ti`, `150.00 Gi`, `0 Gi` |
| `Pod Capacity` | count | `110`, `1k` → 1000 |
| `Age` | whole days | `134d` → 134, `3d4h` → 3, `12h` → 0 |
| `Labels` | dict | `a=1; b=2; node-role.kubernetes.io/control-plane=` |
| `Taints` | list | `node-role.kubernetes.io/control-plane=:NoSchedule` |
| `Ready` / `Schedulable` | true / false / unknown | `True`, `Yes`, `No` |
| `Role` | control plane / worker / unknown | `Control Plane`, `Worker/Unlabeled` |

Binary suffixes (`Ki`, `Mi`, `Gi`, `Ti`, …) are powers of 1024; bare decimal suffixes (`k`, `M`, `G`, …) are
powers of 1000, as `kubectl` prints them. A node has no site of its own: it belongs to its cluster's site.

### `13_Client_Summary` → `ClientProfile` roll-up

`Client Group`, `Namespaces`, `Applications`, `URLs`, `Storage`, `Monitoring` (comma-separated tools),
`CI/CD` (comma-separated tools), `Current Issues`, `Overall Health`. The groups are matched to
`02_Client_Master` rows by name, ignoring case. A group the master sheet does not list (an
"others / unmapped" bucket, for instance) gets its own profile row flagged `summary_only`, so the Clients
page can say where it came from. `Nodes` and `Authentication` are placeholder columns and are not stored.

### `14_Namespace_Mapping` → `Namespace`

`Namespace`, `Client`, `Environment`, `Confidence` (High / Medium / Low), `Evidence`, `Status`
(Mapped / REVIEW REQUIRED). The confidence and the evidence are stored and shown, because the mapping is
evidence-based: a low-confidence row is a guess and the page says so.

### `15_RAW_Discovery` → `ImportRun.metadata`

`Category`, `Name`, `Command / Reference`, `Return Code`, `Details`, with four categories:

* `Metadata` — the discovery timestamp and the `kubectl` context (both land on the `KubeCluster` row);
* `Command` — one row per command the collector ran, with its exit code;
* `Resource Count` — what the cluster held at discovery time, per resource kind;
* `Safety` — the collector's own note that Secret values were not fetched, decoded or stored.

This is evidence about the snapshot rather than inventory, so it is kept as run metadata and shown on the
import detail page. A command with a non-zero exit code raises a warning on the run.

---

## Producing the workbook

The collector runs **read-only** commands only: `kubectl config`, `kubectl version`, `kubectl cluster-info`,
`kubectl api-resources` and `kubectl get` — no `apply`, `patch`, `delete`, `exec`, `logs` or `port-forward`,
and no reading of Secret contents. `15_RAW_Discovery` of any workbook is the authoritative list of what was
run for that snapshot; the commands below are the set it normally contains.

```bash
# context and cluster
kubectl config current-context
kubectl version -o json
kubectl cluster-info
kubectl api-resources -o wide

# nodes and namespaces
kubectl get nodes -o wide
kubectl get nodes --show-labels
kubectl get nodes -o json
kubectl get namespaces
kubectl get namespaces -o json

# storage and ingress classes
kubectl get storageclass            # and -o json
kubectl get ingressclass            # and -o json
kubectl get pv                      # and -o json
kubectl get pvc -A                  # and -o json

# services and ingresses
kubectl get svc -A -o wide          # and -o json
kubectl get ingress -A -o wide      # and -o json

# workloads
kubectl get deployments -A -o wide  # and -o json
kubectl get statefulsets -A -o wide # and -o json
kubectl get daemonsets -A -o wide   # and -o json
kubectl get replicasets -A -o json
kubectl get pods -A -o wide         # and -o json
kubectl get hpa -A                  # and -o json
kubectl get jobs -A                 # and -o json
kubectl get cronjobs -A             # and -o json

# references (NAMES AND TYPES ONLY — never the contents)
kubectl get configmap -A
kubectl get secrets -A
kubectl get serviceaccount -A       # and -o json
kubectl get networkpolicy -A        # and -o json

# certificates (cert-manager)
kubectl get certificates -A         # and -o json
kubectl get certificaterequests -A  # and -o json
kubectl get clusterissuer           # and -o json
kubectl get issuer -A               # and -o json

# events and the collector's own host
kubectl get events -A --sort-by=.lastTimestamp
hostname; hostname -f; hostname -I; ip addr; ip route
```

`kubectl get secrets -A` prints names, types and ages — never values. Never add `-o yaml`, `-o json` or
`-o jsonpath` to a Secret query, and never `kubectl get secret <name> -o ...`: that returns the base64 data,
which must not reach the workbook, the repository or this module.

---

## Adding a sheet importer

One module per group of sheets under `k8s/sheets/`, listed in `k8s/sheets/__init__.py`:

```python
from ..importer import register_sheet
from ..models import ...
from ..parsing import clean, parse_int, parse_ports, parse_quantity

SHEET = "05_Service_Inventory"


def import_services(ctx, sheet):
    namespaces = set()
    for row in sheet.rows:
        name = clean(row.get("Service"))
        if not name:
            ctx.count(SHEET, "skipped")
            continue
        namespace = ctx.resolve_namespace(row.get("Namespace"), client_label=row.get("Client"), sheet=SHEET)
        namespaces.add(namespace.pk)
        ctx.upsert(KubeService, {"namespace": namespace, "name": name}, {
            "service_type": clean(row.get("Service Type")),
            "ports": parse_ports(row.get("Service Port")),
        }, sheet=SHEET)
    ctx.track(KubeService, SHEET, namespace_id__in=sorted(namespaces))


register_sheet(SHEET, 60, import_services)
```

Rules for a sheet importer:

* register at **order 60 or above** — the backbone sheets (clusters, clients, namespaces) run first, so
  `ctx.cluster_for()` and `ctx.resolve_namespace()` already have something to hand back;
* read cells through `k8s.parsing` so placeholders and quantities are handled the same way everywhere;
* write rows through `ctx.upsert(...)`, never `Model.objects.create(...)`, so a re-import stays idempotent
  and the counts are right;
* call `ctx.track(Model, SHEET, **scope)` once, with a scope that covers exactly the rows the sheet speaks
  for, so a partial import does not retire anything else;
* never raise for bad data: `ctx.count(SHEET, "skipped")` and `ctx.warn(SHEET, ...)` instead;
* add the page to `k8s/urls.py` and flip its `"enabled"` flag in `k8s/tabs.py`;
* test it with synthetic fixtures (`k8s/tests/fixtures.py`) — never with customer data.
