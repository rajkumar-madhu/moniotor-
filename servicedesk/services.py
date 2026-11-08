"""Service desk operations. Views, the event listener and other modules all go through these functions so
every change writes a timeline entry and emits an event the same way.

Cross-module interface (docs/MODULES.md):
    open_incident(title, description="", priority="P3", site="", source="", fingerprint="", ci_name="") -> Ticket
    resolve_by_fingerprint(fingerprint, note="")
"""
import logging

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import sla
from . import workflow as wf

log = logging.getLogger("linkedeye.servicedesk")


class WorkflowError(ValueError):
    """A transition or change that is not allowed in the ticket's current state."""


def _name(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    return user.get_full_name() or user.get_username()


def _note(ticket, kind, body, user=None, author_name="", old="", new=""):
    from .models import TimelineEntry
    return TimelineEntry.objects.create(ticket=ticket, kind=kind, body=body, old_value=str(old)[:100], new_value=str(new)[:100],
                                        author=user if user is not None and getattr(user, "is_authenticated", False) else None,
                                        author_name=_name(user) or author_name or "system")


def _emit(ticket, title, severity="info", message="", kind="state_change", **data):
    try:
        from core.events import emit
        emit(source="servicedesk", title=f"{ticket.number} {title}"[:200], severity=severity, message=message or ticket.title,
             kind=kind, site=ticket.site, obj=ticket, fingerprint=f"servicedesk:{ticket.pk}",
             number=ticket.number, ticket_kind=ticket.kind, status=ticket.status, priority=ticket.priority, **data)
    except Exception:  # an event failure must never undo the ticket change
        log.exception("could not emit event for %s", ticket.number)


def _created_severity(ticket):
    if ticket.kind == "incident":
        return {"P1": "critical", "P2": "warning"}.get(ticket.priority, "info")
    return "info"


def _normalise_source(source):
    """Map a free-text source (e.g. a module name) to (source, origin)."""
    keys = {k for k, _ in wf.SOURCES}
    source = (source or "").strip()
    if not source:
        return "manual", ""
    if source in keys:
        return source, ""
    return "alert", source[:40]


def can_on(user, perm, site):
    """`perm` on a ticket's site (site_scope). Global tickets (site None) need the permission tenant-wide, so a
    role held on one site never counts for another site or for every site."""
    from core.access import can_on as _can_on
    return _can_on(user, perm, site)


# ---------------------------------------------------------------- creation
def _create_ticket_rows(user=None, **fields):
    """Create a ticket and its opening timeline entries (no event)."""
    from .models import Ticket
    kind = fields.get("kind") or "incident"
    fields.setdefault("status", wf.INITIAL[kind])
    fields.setdefault("tenant_key", settings.LINKEDEYE_TENANT)
    if user is not None and getattr(user, "is_authenticated", False):
        fields.setdefault("reporter", user)
        fields.setdefault("reporter_name", _name(user))
    with transaction.atomic():
        t = Ticket(**fields)
        t.save()
        via = {"alert": " from an alert", "automation": " by automation", "catalog": " from the service catalog"}.get(t.source, "")
        _note(t, "system", f"{t.get_kind_display()} opened{via}.", user, author_name=t.reporter_name or t.origin,
              new=t.status)
        if t.assignee_id:
            _note(t, "assignment", f"Assigned to {_name(t.assignee)}.", user, author_name=t.reporter_name, new=t.assignee.get_username())
    return t


def _emit_opened(t):
    _emit(t, f"opened: {t.title}", severity=_created_severity(t), message=t.description or t.title, kind="ticket_opened")


def create_ticket(user=None, **fields):
    """Create a ticket (any kind), its 'opened' timeline entry and an event."""
    t = _create_ticket_rows(user, **fields)
    _emit_opened(t)
    return t


def _lock_fingerprint(fingerprint):
    """Inside transaction.atomic(): lock the IncidentKey row of this fingerprint, creating it on first use, so
    open_incident calls for one fingerprint run one at a time (the second sees the first's incident).

    The plain existence check comes first so the common case takes the row lock directly (on MySQL a locking read of
    a missing key takes a gap lock, and two of those plus two inserts deadlock)."""
    from .models import IncidentKey
    keys = IncidentKey.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, fingerprint=fingerprint)
    if not keys.exists():
        try:
            with transaction.atomic():
                IncidentKey.objects.create(tenant_key=settings.LINKEDEYE_TENANT, fingerprint=fingerprint)
        except IntegrityError:
            pass  # a concurrent caller created it; the locking read below waits for its transaction
    list(keys.select_for_update())


def _realert(existing, title, description, priority, src, origin):
    """A duplicate alert for an open incident: timeline note, escalate priority, reopen from Mitigated. Returns the
    list of what changed (no event)."""
    _note(existing, "system", f"Duplicate alert received{' from ' + (origin or src) if (origin or src) else ''}: {title}"
          + (f"\n{description}" if description else ""), author_name=origin or "alert")
    changed = []
    if priority < existing.priority:   # "P1" < "P2": escalate, never downgrade
        old = existing.priority
        existing.priority = priority
        sla.apply_due_dates(existing)
        cleared = sla.clear_stale_breaches(existing)   # a higher priority is not always a stricter target
        existing.save(update_fields=["priority", "response_due", "resolution_due", "response_breached",
                                     "resolution_breached", "updated_at"])
        _note(existing, "priority", f"Priority raised from {old} to {priority} by a new alert.", author_name=origin or "alert",
              old=old, new=priority)
        _note_cleared(existing, cleared, author_name=origin or "alert")
        changed.append("priority")
    if existing.status == "mitigated":   # the problem came back
        existing.status = "investigating"
        existing.save(update_fields=["status", "updated_at"])
        _note(existing, "status", "Alert fired again: back to Investigating.", author_name=origin or "alert",
              old="mitigated", new="investigating")
        changed.append("status")
    return changed


def open_incident(title, description="", priority="P3", site="", source="", fingerprint="", ci_name="", **extra):
    """Open an incident, or — when an open incident already has this fingerprint — add a timeline note to it
    instead of creating a duplicate. Returns the (new or existing) Ticket.

    Calls for one fingerprint are serialised on its IncidentKey row, so concurrent alerts cannot both see "no open
    incident" and open two. Events are emitted after the locked block."""
    from .models import Ticket
    priority = priority if priority in wf.PRIORITY_LABELS else "P3"
    src, origin = _normalise_source(source)
    fingerprint = (fingerprint or "")[:200]
    fields = dict(kind="incident", title=(title or "Untitled incident")[:200], description=description or "",
                  priority=priority, site=site or "", source=src, origin=origin, fingerprint=fingerprint,
                  ci_name=(ci_name or "")[:200], reporter_name=extra.pop("reporter_name", "") or origin or src,
                  **{k: v for k, v in extra.items() if k in ("category", "client")})
    if not fingerprint:
        return create_ticket(**fields)
    with transaction.atomic():
        _lock_fingerprint(fingerprint)
        # a locking read: on MySQL (REPEATABLE READ) it sees an incident committed while we waited for the lock
        existing = (Ticket.objects.for_tenant().open().filter(kind="incident", fingerprint=fingerprint)
                    .order_by("-opened_at", "-pk").select_for_update().first())
        if existing:
            changed = _realert(existing, title, description, priority, src, origin)
        else:
            created = _create_ticket_rows(**fields)
    if existing:
        if changed:
            _emit(existing, f"re-alerted: {existing.title}", severity="warning", kind="ticket_updated")
        return existing
    _emit_opened(created)
    return created


def resolve_by_fingerprint(fingerprint, note=""):
    """Resolve every open incident carrying this fingerprint. Returns how many were resolved."""
    from .models import Ticket
    if not fingerprint:
        return 0
    n = 0
    for t in Ticket.objects.for_tenant().open().filter(kind="incident", fingerprint=fingerprint):
        transition(t, "resolved", user=None, note=note or "Resolved automatically: the alert cleared.", actor_name="automation",
                   force=True)
        n += 1
    return n


def request_from_catalog(item, user, details="", site="", client=""):
    return create_ticket(user=user, kind="request", title=f"Request: {item.name}"[:200], description=details or item.description,
                         priority=item.default_priority, category=item.category, catalog_item=item, source="catalog",
                         site=site, client=client)


# ---------------------------------------------------------------- changes
# (field name for update_fields, attribute to compare) of everything transition() may change
TRANSITION_FIELDS = (("status", "status"), ("acknowledged_at", "acknowledged_at"), ("resolved_at", "resolved_at"),
                     ("closed_at", "closed_at"), ("approved_by", "approved_by_id"), ("approved_at", "approved_at"),
                     ("resolution", "resolution"))


def _lock_current_state(ticket):
    """Inside transaction.atomic(): lock the ticket's row and copy what a transition reads or writes from it onto the
    caller's object, so the workflow is judged against the database, not against a copy loaded earlier (two requests, or an
    alert recovery racing an operator). SQLite has no row locks, but its writes are serialised and the re-read still holds."""
    row = type(ticket).objects.select_for_update().get(pk=ticket.pk)
    for name in ("status", "acknowledged_at", "resolved_at", "closed_at", "approved_by_id", "approved_at", "resolution",
                 "planned_start", "planned_end", "reporter_id", "site", "kind"):
        setattr(ticket, name, getattr(row, name))
    ticket.__dict__.pop("approved_by", None)         # drop the cached related object: approved_by_id was just refreshed
    return ticket


def transition(ticket, target, user=None, note="", actor_name="", force=False):
    """Move a ticket to `target` if the workflow allows it from its current status.

    force=True lets automation move an incident to resolved from any open state (resolve_by_fingerprint). The check and the
    write happen under a lock on the ticket row, against the status stored in the database."""
    with transaction.atomic():
        if ticket.pk:
            _lock_current_state(ticket)
        old = ticket.status
        if target == old:
            raise WorkflowError(f"{ticket.number} is already {wf.STATUS_LABELS.get(old, old)}.")
        if not (wf.allowed(ticket.kind, old, target) or (force and target == "resolved" and ticket.is_open and ticket.kind == "incident")):
            raise WorkflowError(f"{ticket.get_kind_display()} {ticket.number} cannot move from "
                                f"{wf.STATUS_LABELS.get(old, old)} to {wf.STATUS_LABELS.get(target, target)}.")
        if ticket.kind == "change" and target == "approved" and old == "submitted" and user is not None:
            if ticket.reporter_id == user.pk and not can_on(user, "admin", ticket.site_scope):
                raise WorkflowError("A change must be approved by someone other than its requester (or an admin).")
        if ticket.kind == "change" and target == "scheduled" and not (ticket.planned_start and ticket.planned_end):
            raise WorkflowError("Set the planned start and end of the change window before scheduling it.")
        if ticket.kind == "change" and target == "scheduled" and ticket.planned_end <= ticket.planned_start:
            raise WorkflowError("The planned end must be after the planned start.")

        now = timezone.now()
        before = {f: getattr(ticket, a) for f, a in TRANSITION_FIELDS}
        reopen = wf.is_reopen(old, target)
        if old == wf.INITIAL[ticket.kind] and ticket.acknowledged_at is None and target not in ("draft",):
            ticket.acknowledged_at = now
        if target in wf.RESOLVING and ticket.resolved_at is None:
            ticket.resolved_at = now
        if target in wf.FINAL:
            ticket.closed_at = now
            if target == "closed" and ticket.resolved_at is None:
                ticket.resolved_at = now
        if reopen:
            ticket.resolved_at = None
            ticket.closed_at = None
        if ticket.kind == "change":
            if target == "approved" and old == "submitted":
                ticket.approved_by = user if user is not None and getattr(user, "is_authenticated", False) else None
                ticket.approved_at = now
            if target in ("draft", "rejected"):
                ticket.approved_by, ticket.approved_at = None, None
        if note and target in wf.RESOLVING:
            ticket.resolution = note
        ticket.status = target
        # only what this transition changed: a full save() would write back stale SLA breach flags (set meanwhile by
        # the servicedesk.sla job) and make sla.check() below flag and announce the same breach a second time
        changed = [f for f, a in TRANSITION_FIELDS if getattr(ticket, a) != before[f]]
        ticket.save(update_fields=changed + ["updated_at"])
        label = "Reopened" if reopen else f"Status changed to {wf.STATUS_LABELS[target]}"
        _note(ticket, "status", label + (f": {note}" if note else "."), user, author_name=actor_name, old=old, new=target)
    # outside the atomic block: check() emits events, and a listener's database error must not poison (and on
    # PostgreSQL abort) the transaction that saved the status change
    sla.check(ticket, now)
    sev = "ok" if target in wf.RESOLVING else "info"
    if reopen and ticket.kind == "incident":
        sev = _created_severity(ticket)
    _emit(ticket, f"{'reopened' if reopen else wf.STATUS_LABELS[target].lower()}: {ticket.title}", severity=sev,
          message=note or ticket.title, kind="ticket_status", previous=old)
    return ticket


def assign(ticket, assignee, user=None):
    old = ticket.assignee
    if (old.pk if old else None) == (assignee.pk if assignee else None):
        return ticket
    ticket.assignee = assignee
    ticket.save(update_fields=["assignee", "updated_at"])
    text = f"Assigned to {_name(assignee)}." if assignee else "Unassigned."
    if old and assignee:
        text = f"Reassigned from {_name(old)} to {_name(assignee)}."
    _note(ticket, "assignment", text, user, old=old.get_username() if old else "", new=assignee.get_username() if assignee else "")
    _emit(ticket, f"assigned to {_name(assignee)}" if assignee else "unassigned", kind="ticket_assigned",
          assignee=assignee.get_username() if assignee else "")
    return ticket


def _note_cleared(ticket, cleared, user=None, author_name=""):
    """Timeline entries for the breach flags sla.clear_stale_breaches dropped after a priority change."""
    for w in cleared:
        due = ticket.response_due if w == "response" else ticket.resolution_due
        _note(ticket, "sla", f"{w.title()} SLA breach cleared: "
              + (f"the new target is {timezone.localtime(due):%d %b %H:%M}." if due else f"no SLA applies to {ticket.priority}."),
              user, author_name=author_name, old=f"{w}_breached")


def set_priority(ticket, priority, user=None):
    if priority not in wf.PRIORITY_LABELS:
        raise WorkflowError("Unknown priority.")
    old = ticket.priority
    if old == priority:
        return ticket
    ticket.priority = priority
    cleared = []
    if ticket.pk:   # the breach flags are written below: start from what the SLA job may have flagged meanwhile
        ticket.refresh_from_db(fields=["response_breached", "resolution_breached"])
    if ticket.kind in wf.SLA_KINDS:
        sla.apply_due_dates(ticket)
        cleared = sla.clear_stale_breaches(ticket)
    ticket.save(update_fields=["priority", "response_due", "resolution_due", "response_breached", "resolution_breached",
                               "updated_at"])
    _note(ticket, "priority", f"Priority changed from {old} ({wf.PRIORITY_LABELS[old]}) to {priority} ({wf.PRIORITY_LABELS[priority]}).",
          user, old=old, new=priority)
    _note_cleared(ticket, cleared, user)
    sla.check(ticket)
    sev = "warning" if ticket.kind == "incident" and priority == "P1" and ticket.is_open else "info"
    _emit(ticket, f"priority {old} → {priority}", severity=sev, kind="ticket_priority", previous=old)
    return ticket


def comment(ticket, body, user=None, author_name=""):
    body = (body or "").strip()
    if not body:
        raise WorkflowError("A comment cannot be empty.")
    return _note(ticket, "comment", body[:10000], user, author_name=author_name)


def link_problem(incident, problem, user=None):
    if incident.kind != "incident" or (problem is not None and problem.kind != "problem"):
        raise WorkflowError("Only incidents can be linked to problems.")
    old = incident.problem
    if (old.pk if old else None) == (problem.pk if problem else None):
        return incident
    incident.problem = problem
    incident.save(update_fields=["problem", "updated_at"])
    if problem:
        _note(incident, "link", f"Linked to problem {problem.number}.", user, new=problem.number)
        _note(problem, "link", f"Incident {incident.number} linked.", user, new=incident.number)
    else:
        _note(incident, "link", f"Unlinked from problem {old.number}.", user, old=old.number)
        _note(old, "link", f"Incident {incident.number} unlinked.", user, old=incident.number)
    return incident


def add_ci(ticket, ci_name, user=None, note=""):
    from .models import TicketCI
    ci_name = (ci_name or "").strip()[:200]
    if not ci_name:
        raise WorkflowError("Enter a configuration item name.")
    link, created = TicketCI.objects.get_or_create(ticket=ticket, ci_name=ci_name, defaults={"note": note[:200]})
    if created:
        _note(ticket, "link", f"Configuration item {ci_name} added.", user, new=ci_name)
    return link


def remove_ci(link, user=None):
    ticket, name = link.ticket, link.ci_name
    link.delete()
    _note(ticket, "link", f"Configuration item {name} removed.", user, old=name)


# Change fields an approval covers: editing any of them after approval needs a fresh approval.
APPROVAL_FIELDS = ("risk", "planned_start", "planned_end", "implementation_plan", "backout_plan")


def approval_snapshot(ticket):
    """The values an approval covers, normalised the way the edit form returns them (text stripped, CRLF line ends
    as LF, times to the minute), so that re-submitting an unchanged plan is not taken for an edit."""
    def norm(v):
        if isinstance(v, str):
            return v.replace("\r\n", "\n").strip()
        return v.replace(second=0, microsecond=0) if v else v
    return {f: norm(getattr(ticket, f)) for f in APPROVAL_FIELDS}


def withdraw_approval(ticket, before, user=None):
    """After the plan, risk or window of an approved/scheduled change was edited (`before`: approval_snapshot taken
    before the edit): back to Submitted, approval cleared, so someone other than the requester must approve what will
    actually be done. Returns True when withdrawn."""
    if ticket.kind != "change" or ticket.status not in ("approved", "scheduled") or approval_snapshot(ticket) == before:
        return False
    old = ticket.status
    ticket.status, ticket.approved_by, ticket.approved_at = "submitted", None, None
    with transaction.atomic():
        ticket.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        _note(ticket, "status", "Approval withdrawn: the change plan, risk or window was edited after approval. "
              "Back to Submitted for re-approval.", user, old=old, new="submitted")
    _emit(ticket, f"approval withdrawn: {ticket.title}", severity="warning", kind="ticket_status", previous=old)
    return True


def record_edit(ticket, changed, user=None):
    """Timeline entry + event after the edit form saved `changed` (list of field labels)."""
    if not changed:
        return
    _note(ticket, "edit", "Updated " + ", ".join(changed) + ".", user)
    _emit(ticket, "updated", kind="ticket_updated", fields=changed)


def find_ci(name):
    """Look a CI up in the CMDB module when it is installed (None otherwise)."""
    if not name:
        return None
    try:
        from django.apps import apps
        if not apps.is_installed("cmdb"):
            return None
        from cmdb.services import find_ci as cmdb_find  # noqa: WPS433 (optional dependency)
        return cmdb_find(name)
    except Exception:
        return None
