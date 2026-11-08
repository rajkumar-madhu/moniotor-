"""The Kubernetes inventory import engine.

Three pieces:

* a **registry** — ``register_sheet(name, order, fn)``. Each sheet of the workbook has one importer
  callable, registered from its own module under ``k8s/sheets/``, so a new sheet is added without
  touching this file or any other sheet.
* a **context** (:class:`ImportContext`) — everything a sheet importer needs: the ``ImportRun``, the site,
  the shared resolvers (``resolve_client``, ``resolve_namespace``, ``cluster_for``), ``upsert()`` (which
  makes re-imports idempotent and keeps the created/updated counts) and ``warn()`` / ``error()``.
* :func:`run_import` — opens the source, calls the registered sheets in order, each in its own
  transaction, records counts, warnings and errors on the ``ImportRun``, then marks rows that the new
  snapshot no longer contains.

``dry_run=True`` reports exactly the same numbers and writes nothing: the whole run happens inside one
transaction that is rolled back before returning.

Data rule: the workbook describes a real customer estate. Nothing from it belongs in this repository —
no row, host name, address, client name, URL or Secret name. See docs/IMPORT.md.
"""
import logging
from dataclasses import dataclass, field

from django.apps import apps
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

from .models import (GLOBAL_SITE, Confidence, ImportRun, ImportStatus, ImportedModel, KubeCluster, MappingStatus,
                     Namespace)
from .parsing import clean, is_unmapped
from .readers import SourceError, open_source

log = logging.getLogger("linkedeye.k8s")

MAX_MESSAGES = 200          # warnings / errors kept per run (the rest are counted)
DEFAULT_CLUSTER_NAME = "kubernetes"


def tenant():
    return settings.LINKEDEYE_TENANT


def _max_length(model, field):
    """The column width of `model.field`, so a key taken from a cell is clipped instead of refused by PostgreSQL."""
    return model._meta.get_field(field).max_length


# ----------------------------------------------------------------------------- registry
_SHEETS = {}
_NAMESPACE_SHEETS = set()   # sheets whose rows can mention a namespace the mapping sheet does not list


def register_sheet(name, order, fn, namespaces=False):
    """Register the importer for one sheet. Called at import time from ``k8s/sheets/*.py``.

    `order` decides when the sheet runs: clusters and clients first (low numbers), rows that reference
    them afterwards. Registering the same sheet twice replaces the earlier callable, so a module reload
    never creates duplicates. `namespaces=True` declares that the sheet attaches its rows to namespaces through
    ``ctx.resolve_namespace()``, so a namespace is retired only when every such sheet was part of the run.
    """
    if not callable(fn):
        raise TypeError(f"sheet importer for {name!r} is not callable")
    _SHEETS[name] = (int(order), fn)
    if namespaces:
        _NAMESPACE_SHEETS.add(name)
    else:
        _NAMESPACE_SHEETS.discard(name)
    return fn


def registered_sheets():
    """[(name, fn)] in run order."""
    return [(name, fn) for name, (_order, fn) in sorted(_SHEETS.items(), key=lambda kv: (kv[1][0], kv[0]))]


def sheet_names():
    return [name for name, _fn in registered_sheets()]


def load_sheets():
    """Import the sheet modules so they register themselves (idempotent)."""
    import importlib
    importlib.import_module("k8s.sheets")


# ----------------------------------------------------------------------------- result
@dataclass
class ImportResult:
    """What an import did — safe to return from a dry run, where the ImportRun row no longer exists."""
    source_name: str = ""
    source_sha256: str = ""
    site: str = GLOBAL_SITE
    dry_run: bool = False
    status: str = ImportStatus.RUNNING
    counts: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    ignored_sheets: list = field(default_factory=list)
    unknown_sheets: list = field(default_factory=list)
    missing_sheets: list = field(default_factory=list)
    run_id: int = None
    started_at: object = None
    finished_at: object = None

    @property
    def ok(self):
        return self.status == ImportStatus.OK

    @property
    def total_rows(self):
        return sum(int(c.get("rows") or 0) for c in self.counts.values())

    @property
    def total_created(self):
        return sum(int(c.get("created") or 0) for c in self.counts.values())

    @property
    def total_updated(self):
        return sum(int(c.get("updated") or 0) for c in self.counts.values())

    @property
    def total_missing(self):
        return sum(int(c.get("missing") or 0) for c in self.counts.values())

    @property
    def rows(self):
        """[(sheet, counts)] in sheet-name order, for the dry-run diff table."""
        return sorted(self.counts.items())


class _Rollback(Exception):
    """Raised to undo a dry run once its numbers have been collected."""

    def __init__(self, result):
        super().__init__("dry run")
        self.result = result


# ----------------------------------------------------------------------------- context
class ImportContext:
    """Shared state and helpers handed to every sheet importer."""

    def __init__(self, run, source, site=GLOBAL_SITE, dry_run=False, create_compute=False):
        self.run = run
        self.source = source
        self.site = site or GLOBAL_SITE
        self.dry_run = bool(dry_run)
        self.create_compute = bool(create_compute)
        self.tenant = run.tenant_key
        self.stamp = run.started_at
        self.counts = {}
        self.warnings = []
        self.errors = []
        self.metadata = {}
        self.dropped_messages = 0
        self.seen = {}
        self.scopes = {}
        self.failed_sheets = set()
        self.touched = {}       # sheet -> rows it actually wrote or confirmed (see sweep)
        # filled by the discovery-metadata sheet (15) and used as defaults for the cluster row
        self.cluster_context = ""
        self.discovered_at = None
        self._clients = None
        self._namespaces = {}
        self._clusters = {}
        self._compute_clusters = {}
        self._indexes = {}      # (model label, key fields) -> {key tuple: row}; see _index()
        self._to_stamp = {}     # model label -> pks whose last_seen stamp is written in one batch; see flush()

    # ---------------------------------------------------------------- messages & counts
    def _entry(self, sheet):
        return self.counts.setdefault(sheet, {"rows": 0, "created": 0, "updated": 0, "skipped": 0, "missing": 0})

    def count(self, sheet, key, amount=1):
        entry = self._entry(sheet)
        entry[key] = int(entry.get(key) or 0) + amount

    def warn(self, sheet, message):
        if len(self.warnings) < MAX_MESSAGES:
            self.warnings.append(f"{sheet}: {message}" if sheet else str(message))
        else:
            self.dropped_messages += 1

    def error(self, sheet, message):
        if len(self.errors) < MAX_MESSAGES:
            self.errors.append(f"{sheet}: {message}" if sheet else str(message))
        else:
            self.dropped_messages += 1

    def note(self, key, value):
        self.metadata[key] = value

    # ---------------------------------------------------------------- upsert
    @staticmethod
    def _fk_names(model):
        return {f.name for f in model._meta.get_fields() if getattr(f, "many_to_one", False)}

    @classmethod
    def _normalise(cls, model, values):
        """Turn ``client=<obj or None>`` into ``client_id=<pk or None>`` so comparisons never re-query."""
        fks = cls._fk_names(model)
        out = {}
        for key, value in values.items():
            if key in fks:
                out[f"{key}_id"] = getattr(value, "pk", value) if value is not None else None
            else:
                out[key] = value
        return out

    def _index(self, model, key_fields):
        """{key tuple: row} of every tenant row of `model`, loaded once per run and key shape.

        This replaces a SELECT per row. Rows this run creates are added by :meth:`upsert`; a rolled-back sheet
        drops the index (:meth:`forget_caches`) because rows it created may no longer exist.
        """
        slot = (model._meta.label_lower, key_fields)
        index = self._indexes.get(slot)
        if index is None:
            index = {}
            for obj in model.objects.filter(tenant_key=self.tenant).iterator(chunk_size=2000):
                index.setdefault(tuple(getattr(obj, f) for f in key_fields), obj)
            self._indexes[slot] = index
        return slot, index

    def _stamp_seen(self, model):
        """Write ``import_run`` / ``last_seen`` / ``missing_since`` for every unchanged row seen, in a few UPDATEs."""
        label = model._meta.label_lower
        pending = self._to_stamp.pop(label, None)
        if not pending:
            return
        pks = sorted(pending)
        for start in range(0, len(pks), 500):
            model.objects.filter(pk__in=pks[start:start + 500]).update(
                import_run_id=self.run.pk, last_seen=self.stamp, missing_since=None)

    def flush(self):
        """Write the batched ``last_seen`` stamps. Called at the end of every sheet and before the sweep."""
        for label in list(self._to_stamp):
            self._stamp_seen(apps.get_model(label))

    def upsert(self, model, keys, defaults=None, sheet=None):
        """Create or update one row, keyed by `keys` within the tenant. Returns ``(obj, created)``.

        This is what makes a re-import idempotent: an existing row is updated in place (only changed
        fields are written) and stamped with ``last_seen``; ``missing_since`` is cleared if the row had
        gone away and came back. Existing rows come from a per-run index (no SELECT per row), and an
        unchanged row's stamp is batched into one UPDATE per few hundred rows at the end of the sheet.
        """
        label = model._meta.label_lower
        lookup = self._normalise(model, dict(keys))
        lookup.setdefault("tenant_key", self.tenant)
        values = self._normalise(model, dict(defaults or {}))
        versioned = issubclass(model, ImportedModel)
        key_fields = tuple(sorted(f for f in lookup if f != "tenant_key"))
        _slot, index = self._index(model, key_fields)
        key = tuple(lookup[f] for f in key_fields)
        obj = index.get(key)
        if obj is None:
            fields = {**lookup, **values}
            if versioned:
                fields.update(import_run_id=self.run.pk, first_seen=self.stamp, last_seen=self.stamp,
                              missing_since=None)
            obj = model.objects.create(**fields)
            index[key] = obj
            if sheet:
                self.count(sheet, "created")
            created = True
        else:
            changed = [k for k, v in values.items() if getattr(obj, k) != v]
            for k in changed:
                setattr(obj, k, values[k])
            update_fields = list(changed)
            stamp_only = False
            if versioned:
                if obj.first_seen is None:
                    obj.first_seen = self.stamp
                    update_fields.append("first_seen")
                # the in-memory row carries the new stamp; the database learns it with the row's other changes
                # or, for an unchanged row, with the batched UPDATE in flush()
                stamp_only = not update_fields
                obj.import_run_id = self.run.pk
                obj.last_seen = self.stamp
                obj.missing_since = None
                update_fields += ["import_run", "last_seen", "missing_since"]
            if stamp_only:
                self._to_stamp.setdefault(label, set()).add(obj.pk)
            elif update_fields:
                if any(f.name == "updated_at" for f in model._meta.get_fields()):
                    update_fields.append("updated_at")
                obj.save(update_fields=sorted(set(update_fields)))
            if sheet and changed:
                self.count(sheet, "updated")
            created = False
        self.seen.setdefault(label, set()).add(obj.pk)
        if sheet:
            self.touched[sheet] = self.touched.get(sheet, 0) + 1
        return obj, created

    def track(self, model, sheet, **scope):
        """Declare which rows of `model` this run owns, so ones it did not see are marked missing.

        A model no sheet tracks is never swept, which is what makes a partial import (a single CSV
        sheet) safe: it must not claim that every row of every other sheet has disappeared. Pass a
        narrowing `scope` (for example ``cluster_id__in=[…]``) when the sheet only speaks for part of
        the table.
        """
        self.scopes[model._meta.label_lower] = (sheet, scope)

    def unreadable_sheets(self):
        """Sheets of this run that failed, or that had rows of which none could be read."""
        return set(self.failed_sheets) | {name for name, c in self.counts.items()
                                           if c.get("rows") and not self.touched.get(name)}

    # rows that any sheet may contribute (a namespace seen on the workload, ingress… sheets): they are
    # retired only when every sheet of the run was readable
    SHARED_MODELS = {"k8s.namespace"}

    def absent_namespace_sheets(self):
        """Registered sheets that mention namespaces but are not part of this source (sorted)."""
        present = set(self.source.sheets) | set(getattr(self.source, "unread", ()))
        return sorted(n for n in _NAMESPACE_SHEETS if n in _SHEETS and n not in present)

    def sweep(self):
        """Stamp ``missing_since`` on tracked rows this run did not see; returns the total."""
        now = timezone.now()
        total = 0
        for label, (sheet, scope) in self.scopes.items():
            if sheet in self.failed_sheets:
                continue    # a sheet that blew up half way has not said what is gone: keep its rows as they were
            model = apps.get_model(label)
            seen = self.seen.get(label, set())
            if label in self.SHARED_MODELS and self.unreadable_sheets() - {sheet}:
                self.warn(sheet, "another sheet could not be read, so namespaces nothing mentions any more "
                                 "were not retired")
                continue
            if label in self.SHARED_MODELS and self.absent_namespace_sheets():
                # the rows of an absent sheet were not touched, so what they mention is not "gone"
                self.warn(sheet, "this source does not hold every sheet that mentions namespaces "
                                 f"({', '.join(self.absent_namespace_sheets())}), so namespaces nothing "
                                 "mentions any more were not retired")
                continue
            if not self.touched.get(sheet) and (self.counts.get(sheet) or {}).get("rows"):
                # the sheet had rows but none could be read (a key column renamed or removed): that is a damaged
                # sheet, not an empty estate, so nothing is retired
                self.warn(sheet, "no row could be read, so existing records were left untouched")
                continue
            qs = model.objects.filter(tenant_key=self.tenant, missing_since__isnull=True, **scope)
            if seen:
                qs = qs.exclude(pk__in=seen)
            gone = qs.update(missing_since=now)
            if gone:
                self.count(sheet, "missing", gone)
                total += gone
        return total

    # ---------------------------------------------------------------- resolvers
    def resolve_site(self, value=""):
        """The site a row belongs to: an explicit value, else the run's site."""
        return clean(value) or self.site or GLOBAL_SITE

    def _client_index(self):
        if self._clients is None:
            Client = apps.get_model("core", "Client")
            self._clients = {}
            for client in Client.objects.filter(tenant_key=self.tenant):
                self._clients[self._client_key(client.name)] = client
                if client.code:
                    self._clients.setdefault(self._client_key(client.code), client)
        return self._clients

    @staticmethod
    def _client_key(value):
        return slugify(clean(value) or "").replace("-", "")

    def resolve_client(self, name):
        """The ``core.Client`` this workbook client string names, or None.

        Matching is by code or name, ignoring case, spaces and punctuation. An unmapped marker
        ("UNMAPPED / REVIEW REQUIRED") never matches anything — it is the absence of a client, and a
        client is never created here: linking is offered on the Clients page instead.
        """
        text = clean(name)
        if not text or is_unmapped(text):
            return None
        key = self._client_key(text)
        return self._client_index().get(key) if key else None

    def cluster_for(self, name="", **defaults):
        """The ``KubeCluster`` called `name` (the workbook's single "kubernetes" cluster by default)."""
        cluster_name = (clean(name) or DEFAULT_CLUSTER_NAME)[:_max_length(KubeCluster, "name")]
        cached = self._clusters.get(cluster_name)
        if cached is not None:
            return cached
        values = {"site": self.resolve_site(defaults.pop("site", ""))}
        if self.cluster_context:
            values["context"] = self.cluster_context
        if self.discovered_at:
            values["discovered_at"] = self.discovered_at
        values.update(defaults)
        cluster, _created = self.upsert(KubeCluster, {"name": cluster_name}, values)
        self._clusters[cluster_name] = cluster
        return cluster

    def forget_caches(self):
        """Drop cached rows after a sheet's transaction was rolled back: they may no longer exist."""
        self._clusters.clear()
        self._namespaces.clear()
        self._indexes.clear()
        self._to_stamp.clear()

    def remember_namespace(self, namespace):
        """Cache a Namespace a sheet importer just wrote, so later sheets reuse it without a query."""
        if namespace is not None and namespace.cluster_id:
            self._namespaces[(namespace.cluster_id, namespace.name)] = namespace
        return namespace

    def resolve_namespace(self, name, cluster=None, client_label="", environment="", sheet=None):
        """The ``Namespace`` row for `name`, created as "not mapped" when a sheet mentions one that the
        namespace-mapping sheet did not list. Other sheets' importers use this to attach their rows."""
        ns_name = clean(name)[:_max_length(Namespace, "name")]
        if not ns_name:
            return None
        cluster = cluster or self.cluster_for()
        key = (cluster.pk, ns_name)
        cached = self._namespaces.get(key)
        if cached is not None:
            return cached
        existing = Namespace.objects.filter(tenant_key=self.tenant, cluster=cluster, name=ns_name).first()
        if existing is None:
            label = clean(client_label)
            values = {
                "site": cluster.site,
                "client_label": "" if is_unmapped(label) else label,
                "client": self.resolve_client(label),
                "environment": clean(environment),
                "confidence": Confidence.UNKNOWN,
                "mapping_status": MappingStatus.REVIEW if is_unmapped(label) or not label else MappingStatus.UNKNOWN,
                "evidence": "Seen on another sheet; not listed in the namespace mapping sheet.",
            }
            existing, _created = self.upsert(Namespace, {"cluster": cluster, "name": ns_name}, values, sheet=sheet)
            if sheet:
                self.warn(sheet, f"namespace “{ns_name}” is not in the namespace mapping sheet")
        else:
            self.seen.setdefault(Namespace._meta.label_lower, set()).add(existing.pk)
            if existing.missing_since is not None:
                # a stub that was retired because nothing mentioned it, and is mentioned again
                existing.missing_since = None
                existing.last_seen = self.stamp
                existing.save(update_fields=["missing_since", "last_seen", "updated_at"])
        self._namespaces[key] = existing
        return existing


# ----------------------------------------------------------------------------- the run
def _finish(ctx, result, status):
    result.status = status
    result.counts = ctx.counts
    result.warnings = list(ctx.warnings)
    result.errors = list(ctx.errors)
    result.metadata = ctx.metadata
    if ctx.dropped_messages:
        result.warnings.append(f"{ctx.dropped_messages} further message(s) were not kept.")
    result.finished_at = timezone.now()
    run = ctx.run
    run.status = status
    run.counts = ctx.counts
    run.warnings = result.warnings
    run.errors = result.errors
    run.metadata = ctx.metadata
    run.finished_at = result.finished_at
    run.save(update_fields=["status", "counts", "warnings", "errors", "metadata", "finished_at", "updated_at"])
    return result


def _execute(source_arg, user, dry_run, site, create_compute, source_name):
    load_sheets()
    now = timezone.now()
    try:
        source = open_source(source_arg, name=source_name, only=set(sheet_names()))
    except SourceError as exc:
        run = ImportRun.objects.create(
            tenant_key=tenant(), source_name=source_name or str(source_arg)[:255], started_at=now,
            finished_at=timezone.now(), status=ImportStatus.FAILED, uploaded_by=user if _is_user(user) else None,
            errors=[str(exc)], site=site or GLOBAL_SITE)
        return ImportResult(source_name=run.source_name, site=run.site, dry_run=dry_run,
                            status=ImportStatus.FAILED, errors=[str(exc)], run_id=run.pk,
                            started_at=run.started_at, finished_at=run.finished_at)

    run = ImportRun.objects.create(
        tenant_key=tenant(), source_name=source.name[:255], source_sha256=source.sha256, started_at=now,
        status=ImportStatus.RUNNING, uploaded_by=user if _is_user(user) else None, site=site or GLOBAL_SITE,
        notes="Dry run — nothing was written." if dry_run else "")
    ctx = ImportContext(run, source, site=site, dry_run=dry_run, create_compute=create_compute)
    result = ImportResult(source_name=run.source_name, source_sha256=run.source_sha256, site=run.site,
                          dry_run=dry_run, run_id=run.pk, started_at=run.started_at,
                          ignored_sheets=list(source.ignored))

    handled = sheet_names()
    present = set(source.sheets) | set(source.unread)
    result.unknown_sheets = sorted(n for n in present if n not in handled)
    result.missing_sheets = [n for n in handled if n not in present]
    for name in result.missing_sheets:
        ctx.warn("", f"sheet {name} is not in this source — its records were left untouched")

    failed = False
    for name, fn in registered_sheets():
        sheet = source.get(name)
        if sheet is None:
            continue
        ctx.count(name, "rows", len(sheet.rows))
        if getattr(sheet, "truncated", False):
            # only the first rows were read: importing them would present part of the sheet as all of it, and
            # the sweep would then retire every row past the cut-off. Nothing is written or retired for it.
            ctx.error(name, f"the sheet is longer than the import limit ({len(sheet.rows):,} rows read), so it "
                            f"was not imported and its records were left untouched - split it or raise the limit")
            ctx.failed_sheets.add(name)
            failed = True
            continue
        try:
            with transaction.atomic():
                fn(ctx, sheet)
                ctx.flush()
        except Exception as exc:  # one broken sheet must not lose the others
            log.exception("k8s import: sheet %s failed", name)
            ctx.error(name, f"{type(exc).__name__}: {exc}"[:400])
            ctx.failed_sheets.add(name)
            ctx.forget_caches()
            failed = True
    try:
        with transaction.atomic():
            ctx.flush()
            ctx.sweep()
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("k8s import: sweep failed")
        ctx.error("", f"marking missing rows failed: {type(exc).__name__}: {exc}"[:400])
        failed = True

    return _finish(ctx, result, ImportStatus.FAILED if failed or ctx.errors else ImportStatus.OK)


def _is_user(user):
    return user is not None and getattr(user, "pk", None) and getattr(user, "is_authenticated", False)


def run_import(source, user=None, dry_run=False, site=GLOBAL_SITE, create_compute=False, source_name=None):
    """Import `source` (a path, directory, zip, uploaded file or file object).

    Returns an :class:`ImportResult`. With ``dry_run=True`` the whole run is rolled back, so the result
    describes what *would* change and the database is untouched — including the ``ImportRun`` row.
    """
    if not dry_run:
        return _execute(source, user, False, site, create_compute, source_name)
    try:
        with transaction.atomic():
            result = _execute(source, user, True, site, create_compute, source_name)
            result.run_id = None
            raise _Rollback(result)
    except _Rollback as rollback:
        return rollback.result
