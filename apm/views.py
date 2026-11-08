"""APM pages: the dashboard (rendered client-side from one JSON snapshot and patched from /apm/live/), the adapter detail
panel endpoints, CSV / evidence downloads, the retry and RCA actions, and the Sources admin.

Scoping: every queryset goes through ``services.sources_for`` (tenant + ``scope_by_site`` on the source's site) or
``scope_by_site(..., field="source__site")`` for rows that belong to a source, so another site's adapters, processes and
logs are never readable by id (404, not 403)."""
import logging
import re
from datetime import timedelta

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from core import secrets as vault
from core.access import can, can_on, is_global_site, require, scope_by_site
from core.audit import audit

from . import redis_reader, services, snapshot
from .forms import SourceForm
from .models import AdapterCell, AdapterRun, Source

log = logging.getLogger("linkedeye.apm")

STATUS_FILTERS = ("success", "failure", "warning", "nodata")


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _cells_for(user):
    return scope_by_site(AdapterCell.objects.filter(tenant_key=services.tenant()).select_related("source"), user,
                         field="source__site")


def _cell_or_404(request, pk):
    return get_object_or_404(_cells_for(request.user), pk=pk)


def _can_operate(user, source):
    return source.enabled and can_on(user, "operate", source.site)


def _servicedesk():
    return apps.is_installed("servicedesk")


def _can_rca(user, source):
    return _servicedesk() and can_on(user, "operate", source.site)


def _need_admin(request, source):
    if not can_on(request.user, "admin", source.site):
        raise PermissionDenied(f"You need the admin role on “{source.site}” to manage this source.")


def _base_url(request):
    return request.build_absolute_uri("/").rstrip("/")


def setup_snippet(request, token=None):
    """The copy-paste environment block for the producer's host. The token appears only when it was just issued."""
    return (f"# on the host that runs adapterStatus.py / processStatus.py (cron)\n"
            f"export LINKEDEYE_APM_URL={_base_url(request)}\n"
            f"export LINKEDEYE_APM_TOKEN={token or '<the token shown when the source was created>'}\n")


# ----------------------------------------------------------------------------- the dashboard
def _choose_source(request, sources):
    pk = _int(request.GET.get("source"))
    for s in sources:
        if s.pk == pk:
            return s
    live = [s for s in sources if s.last_data_at and s.enabled]
    if live:
        return max(live, key=lambda s: s.last_data_at)
    return sources[0] if sources else None


@require("view")
@require_GET
def index(request):
    sources = list(services.sources_for(request.user).order_by("name"))
    source = _choose_source(request, sources)
    ctx = {"sources": sources, "source": source, "can_admin": can(request.user, "admin"),
           "setup": setup_snippet(request), "base_url": _base_url(request)}
    if source is not None:
        g = request.GET
        logs_n = _int(g.get("logs"), 100)
        snap = snapshot.build_snapshot(source, logs_n=logs_n if logs_n in snapshot.LOG_CHOICES else 100)
        sel = _int(g.get("sel"))
        if sel and not AdapterCell.objects.filter(pk=sel, source=source).exists():
            sel = None
        exchange = g.get("exchange", "")
        status = g.get("status", "")
        ctx.update({
            "snap": snap,
            "config": {
                "source": source.pk,
                "urls": {"live": reverse("apm:live"), "cell": reverse("apm:cell", args=[0]),
                         "retry": reverse("apm:cell_retry", args=[0]), "rca": reverse("apm:cell_rca", args=[0]),
                         "evidence": reverse("apm:cell_evidence", args=[0]), "export": reverse("apm:export"),
                         "page": reverse("apm:index")},
                "can_retry": _can_operate(request.user, source), "can_rca": _can_rca(request.user, source),
                "state": {"exchange": exchange if exchange in snap["segments"] else "",
                          "client": g.get("client", "")[:40], "status": status if status in STATUS_FILTERS else "",
                          "q": g.get("q", "")[:60], "sel": sel, "logs": snap["logs_n"]},
                "log_choices": list(snapshot.LOG_CHOICES),
            },
            "can_retry": _can_operate(request.user, source),
        })
    resp = render(request, "apm/index.html", ctx)
    return resp


@require("view")
@require_GET
def live(request):
    sources = services.sources_for(request.user)
    source = get_object_or_404(sources, pk=_int(request.GET.get("source"), 0))
    logs_n = _int(request.GET.get("logs"), 100)
    logs_n = logs_n if logs_n in snapshot.LOG_CHOICES else 100
    now = timezone.now()
    rev = snapshot.revision(source, logs_n, now)
    if request.GET.get("rev") == rev:
        body = snapshot.unchanged_stub(source, rev, now)        # nothing but the clock changed: no data is read
    else:
        body = snapshot.build_snapshot(source, logs_n=logs_n, now=now)
    resp = JsonResponse(body)
    resp["Cache-Control"] = "no-store"
    return resp


# ----------------------------------------------------------------------------- one adapter
@require("view")
@require_GET
def cell_json(request, pk):
    cell = _cell_or_404(request, pk)
    data = snapshot.cell_detail(cell, can_retry=_can_operate(request.user, cell.source),
                                can_rca=_can_rca(request.user, cell.source))
    resp = JsonResponse(data)
    resp["Cache-Control"] = "no-store"
    return resp


@require("view")
@require_GET
def cell_evidence(request, pk):
    cell = _cell_or_404(request, pk)
    data = snapshot.cell_detail(cell)
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    name = slugify(f"apm-evidence-{cell.client}-{cell.segment}-{stamp}") or "apm-evidence"
    resp = HttpResponse(data["evidence"], content_type="text/plain; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{name}.txt"'
    resp["Cache-Control"] = "no-store"
    return resp


@require("view")
@require_GET
def export(request):
    sources = services.sources_for(request.user)
    source = get_object_or_404(sources, pk=_int(request.GET.get("source"), 0))
    g = request.GET
    qs = AdapterCell.objects.filter(source=source)
    one = _int(g.get("cell"))
    if one:
        qs = qs.filter(pk=one)
    else:
        qs = snapshot.filter_cells(qs, exchange=g.get("exchange", "")[:20], client=g.get("client", "")[:40],
                                   status=g.get("status", ""), q=g.get("q", "")[:60])
    run = AdapterRun.objects.filter(source=source, kind="matrix").order_by("-executed_at").first()
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{slugify("apm-" + source.name) or "apm"}-adapters.csv"'
    snapshot.write_cells_csv(resp, list(qs.order_by("client", "segment")[:20001]), run.executed_at if run else None)
    return resp


@require("operate")
@require_POST
def cell_retry(request, pk):
    cell = _cell_or_404(request, pk)
    if not _can_operate(request.user, cell.source):
        return JsonResponse({"error": "You need the operator role on this site (and the source must be enabled)."}, status=403)
    req, created = services.request_retry(cell, request.user, note=request.POST.get("note", ""))
    if created:
        audit(request, "apm.retry.request", req, source=cell.source.name, client=cell.client, segment=cell.segment)
    return JsonResponse({"ok": True, "created": created, "retry": snapshot.retry_info(cell)})


@require("view")
@require_POST
def cell_rca(request, pk):
    cell = _cell_or_404(request, pk)
    detail = snapshot.cell_detail(cell)
    draft = snapshot.rca_draft(detail)
    out = {"ok": True, "created": False, "draft": draft}
    reason = ""
    if not _servicedesk():
        reason = "Service Desk is not installed, so the RCA is a draft to copy."
    elif not can_on(request.user, "operate", cell.source.site):
        reason = "You cannot create problems for this site, so the RCA is a draft to copy."
    if not reason:
        try:
            problem, existing = _open_problem(request.user, cell, detail, draft)
        except Exception:                           # noqa: BLE001 - never lose the draft because the ticket failed
            log.exception("apm could not create the RCA problem")
            problem, existing = None, False
            reason = "The problem could not be created; use the draft."
        if problem is not None:
            out.update(created=not existing, existing=existing, number=problem.number, url=problem.get_absolute_url(),
                       title=problem.title)
            audit(request, "apm.rca.existing" if existing else "apm.rca.create", problem, source=cell.source.name,
                  client=cell.client, segment=cell.segment, number=problem.number)
    if reason:
        out["reason"] = reason
        audit(request, "apm.rca.draft", cell, source=cell.source.name, reason=reason)
    return JsonResponse(out)


def _open_problem(user, cell, detail, draft):
    """(Ticket, already_existed). Lazy cross-app call into servicedesk; one open problem per adapter."""
    from servicedesk import services as sd
    from servicedesk.models import Ticket
    fp = f"apm:rca:{cell.source_id}:{cell.client}:{cell.segment}"
    found = Ticket.objects.for_tenant().open().filter(kind="problem", fingerprint=fp).first()
    if found is not None:
        return found, True
    site = "" if is_global_site(cell.source.site) else cell.source.site
    title = f"APM: {cell.client} / {cell.segment} adapter failure ({detail['ctcl_id'] or 'no id'})"[:200]
    ticket = sd.create_ticket(user=user, kind="problem", title=title, description=draft, priority="P3", site=site,
                              source="manual", origin="apm", fingerprint=fp, category="APM",
                              ci_name=f"{cell.client}/{cell.segment}"[:200])
    return ticket, False


# ----------------------------------------------------------------------------- sources admin
def _source_or_404(request, pk):
    return get_object_or_404(services.sources_for(request.user), pk=pk)


def _redis_state(src):
    if not src.redis_poll:
        return ("off", "Off")
    if src.redis_last_error:
        return ("crit", "Error")
    if src.redis_last_polled_at:
        return ("ok", "Reading")
    return ("mute", "Waiting")


@require("admin")
@require_GET
def sources(request):
    rows = []
    for s in services.sources_for(request.user).order_by("name"):
        latest = [t for t in (s.last_matrix_at, s.last_table_at) if t]
        stale = bool(latest) and services.is_stale(s, max(latest))
        rows.append({"s": s, "stale": stale, "silent": bool(s.silent_since), "redis": _redis_state(s),
                     "age": services.age_seconds(max(latest)) if latest else None,
                     "can_manage": can_on(request.user, "admin", s.site)})
    return render(request, "apm/sources.html", {"rows": rows, "redis_ok": redis_reader.available()})


def _store_password(request, form, src):
    """Create / rotate / remove the vault row holding the Redis password. Fails closed (raises) when the vault is unusable."""
    Credential = apps.get_model("onboarding", "Credential")
    row = Credential.objects.filter(pk=src.redis_credential_id, tenant_key=src.tenant_key).first() if src.redis_credential_id else None
    pw = form.cleaned_data.get("redis_password")
    if pw:
        row = vault.store("bearer", form.cleaned_data.get("redis_username") or "redis", pw, src.site, request.user,
                          name=f"APM Redis password: {src.name}"[:120],
                          notes="Used only by APM to read the toolkit's Redis keys. Not an API token.", request=request,
                          credential=row)
        if src.redis_credential_id != row.pk:
            src.redis_credential_id = row.pk
            src.save(update_fields=["redis_credential_id"])
        return "stored"
    if form.cleaned_data.get("clear_password") and row is not None:
        row.delete()
        src.redis_credential_id = None
        src.save(update_fields=["redis_credential_id"])
        audit(request, "apm.source.password_clear", src)
        return "cleared"
    if row is not None and row.site != src.site:
        Credential.objects.filter(pk=row.pk).update(site=src.site)
    return ""


def _reveal(request, src, token, created):
    resp = render(request, "apm/source_token.html", {
        "source": src, "token": token, "created": created, "setup": setup_snippet(request, token),
        "base_url": _base_url(request)})
    resp["Cache-Control"] = "no-store"
    return resp


@require("admin")
@require_http_methods(["GET", "POST"])
def source_new(request):
    form = SourceForm(request.POST or None, user=request.user, initial={"site": "All Sites", "expected_interval_s": 300})
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                src = form.save(commit=False)
                src.tenant_key = services.tenant()
                src.created_by = request.user
                token = services.issue_token(src)
                src.save()
                _store_password(request, form, src)
        except (vault.VaultUnavailable, ValueError) as exc:
            form.add_error("redis_password", str(exc))
        else:
            audit(request, "apm.source.create", src, site=src.site, redis_poll=src.redis_poll)
            messages.success(request, f"Source “{src.name}” created. Copy the token now: it is shown once.")
            return _reveal(request, src, token, created=True)
    return render(request, "apm/source_form.html", {"form": form, "source": None, "redis_ok": redis_reader.available()})


@require("admin")
@require_http_methods(["GET", "POST"])
def source_edit(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    form = SourceForm(request.POST or None, instance=src, user=request.user)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                changed = [f for f in form.changed_data if f not in ("redis_password", "clear_password")]
                src = form.save()
                state = _store_password(request, form, src)
        except (vault.VaultUnavailable, ValueError) as exc:
            form.add_error("redis_password", str(exc))
        else:
            audit(request, "apm.source.update", src, changed=changed, password=state or "unchanged")
            messages.success(request, f"Source “{src.name}” saved.")
            return redirect("apm:source_edit", pk=src.pk)
    runs = list(AdapterRun.objects.filter(source=src).order_by("-received_at")[:10])
    return render(request, "apm/source_form.html", {
        "form": form, "source": src, "runs": runs, "redis_ok": redis_reader.available(), "setup": setup_snippet(request),
        "redis_state": _redis_state(src), "stale_after": services.stale_after(src),
        "keys": redis_reader.key_names(src)})


@require("admin")
@require_POST
def source_rotate(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    token = services.issue_token(src)
    src.save(update_fields=["token_prefix", "token_salt", "token_hash", "token_rotated_at", "updated_at"])
    audit(request, "apm.source.rotate_token", src, prefix=src.token_prefix)
    messages.success(request, "A new token was generated; the old one no longer works. Copy it now: it is shown once.")
    return _reveal(request, src, token, created=False)


@require("admin")
@require_POST
def source_toggle(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    src.enabled = not src.enabled
    src.save(update_fields=["enabled", "updated_at"])
    audit(request, "apm.source.update", src, enabled=src.enabled)
    messages.success(request, f"“{src.name}” is now {'enabled' if src.enabled else 'disabled: it will not accept data'}.")
    return redirect("apm:sources")


@require("admin")
@require_POST
def source_delete(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    name, cred = src.name, src.redis_credential_id
    with transaction.atomic():
        if cred:
            apps.get_model("onboarding", "Credential").objects.filter(pk=cred, tenant_key=src.tenant_key).delete()
        audit(request, "apm.source.delete", src, site=src.site)
        src.delete()
    messages.success(request, f"Source “{name}” and its data were deleted.")
    return redirect("apm:sources")


@require("admin")
@require_POST
def source_test_redis(request, pk):
    src = _source_or_404(request, pk)
    _need_admin(request, src)
    ok, message = redis_reader.test_source(src)
    audit(request, "apm.source.redis_test", src, result="ok" if ok else "failed", message=message[:200])
    (messages.success if ok else messages.error)(request, f"Redis test: {message}")
    return redirect("apm:source_edit", pk=src.pk)
