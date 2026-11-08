"""The onboarding wizard: a server-rendered, resumable walk from a site to live metrics.

Every mutating step is POST + CSRF + audited and needs the `admin` role on the run's site. Every
queryset is tenant- and site-scoped. Nothing is probed or installed without the operator pressing
the button on that step.

The four pieces of real work (verify, enable, logs, activate) are hooks in `steps.py`. Until a
hook is filled in it raises `StepPending`, which is recorded as a "pending" step-log line so the
whole wizard still walks end to end.
"""
import logging
import time

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from core import secrets
from core.access import can, require
from core.audit import audit
from core.events import emit

from . import sd, services, steps
from .forms import CredentialForm, RotateForm, ScopeForm, TargetsForm
from .models import (STEP_SLUGS, STEP_TITLES, Credential, OnboardingRun, OnboardingTarget, RunStatus,
                     TargetState, TargetType)

log = logging.getLogger("linkedeye.onboarding")
EXPORT_LIMIT = 5000


# ----------------------------------------------------------------------------- helpers
def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def paginate(request, qs, default=20):
    return Paginator(qs, _per_page(request, default)).get_page(request.GET.get("page"))


def _query(request):
    q = request.GET.copy()
    q.pop("page", None)
    return q


def csv_response(filename, header, rows):
    import csv as _csv
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    writer = _csv.writer(resp)
    writer.writerow(header)
    for row in rows:
        writer.writerow([services.csv_safe(v) for v in row])
    return resp


def _run(request, pk):
    """The run, scoped to the sites the user may see."""
    return get_object_or_404(services.runs_for(request.user), pk=pk)


def _need_admin(request, site):
    if not services.can_onboard(request.user, site):
        raise PermissionDenied(f"You need the admin role on “{site or 'All Sites'}” to onboard into it.")


def _url(name, *args):
    try:
        return reverse(name, args=args)
    except NoReverseMatch:
        return ""


def _hook(run, step, fn, target=None, **kwargs):
    """Call a steps.py hook, timing it and writing one step-log line. Returns (result, pending_msg).

    A hook that is not wired up yet yields (None, message). An unexpected exception is recorded by
    exception type only: a hook promises never to put a secret in a message, and this keeps even a
    buggy one from writing a value into the log.
    """
    host = target.hostname if target is not None else "run"
    start = time.monotonic()
    try:
        result = fn(target, **kwargs) if target is not None else fn(**kwargs)
    except steps.StepPending as exc:
        services.log_step(run, step, "pending", f"{host}: {exc}", target=target,
                          duration_ms=round((time.monotonic() - start) * 1000, 1))
        return None, str(exc)
    except Exception as exc:                      # noqa: BLE001 - deliberate: never echo the message
        log.exception("onboarding %s hook failed for %s", step, host)
        services.log_step(run, step, "error", f"{host}: {type(exc).__name__} (see the server log)",
                          target=target, duration_ms=round((time.monotonic() - start) * 1000, 1))
        return None, f"{step} failed for {host}: {type(exc).__name__}"
    services.log_step(run, step, "ok", f"{host}: done", target=target,
                      duration_ms=round((time.monotonic() - start) * 1000, 1))
    return result or {}, ""


# ----------------------------------------------------------------------------- list / create
@require("view")
@require_GET
def index(request):
    qs = services.runs_for(request.user)
    status = request.GET.get("status") or ""
    kind = request.GET.get("kind") or ""
    site = request.GET.get("site") or ""
    q = (request.GET.get("q") or "").strip()
    if status in dict(RunStatus.choices):
        qs = qs.filter(status=status)
    if kind:
        qs = qs.filter(kind=kind)
    if site:
        qs = qs.filter(site=site)
    if q:
        qs = qs.filter(targets__hostname__icontains=q).distinct()
    page = paginate(request, qs)
    rows = list(page.object_list)
    for run in rows:
        run.summary = services.run_summary(run)
        run.can_manage = services.can_onboard(request.user, run.site)
    all_runs = services.runs_for(request.user)
    ctx = {
        "page": page, "rows": rows, "query": _query(request), "pager_noun": "runs",
        "statuses": RunStatus.choices, "kinds": OnboardingRun._meta.get_field("kind").choices,
        "status": status, "kind": kind, "site": site, "q": q,
        "sites": services.onboardable_sites(request.user),
        "n_total": all_runs.count(),
        "n_open": all_runs.exclude(status__in=[RunStatus.COMPLETE, RunStatus.FAILED]).count(),
        "n_complete": all_runs.filter(status=RunStatus.COMPLETE).count(),
        "n_failed": all_runs.filter(status=RunStatus.FAILED).count(),
        "n_live": services.targets_for(request.user).filter(state=TargetState.LIVE).count(),
        "n_sd": services.enabled_sd_targets().count(),
        "can_start": bool(services.onboardable_sites(request.user)),
        "vault_ok": secrets.available(), "vault_reason": secrets.unavailable_reason(),
        "sd_configured": sd.configured(),
    }
    return render(request, "onboarding/index.html", ctx)


@require("view")
@require_GET
def runs_export(request):
    rows = []
    for run in services.runs_for(request.user)[:EXPORT_LIMIT]:
        s = services.run_summary(run)
        rows.append([run.pk, run.created_at.isoformat(), run.kind, run.status, run.site,
                     run.client.name if run.client_id else "", run.environment, run.step,
                     s["total"], s["live"], s["failed"],
                     run.created_by.get_username() if run.created_by_id else ""])
    return csv_response("onboarding-runs.csv",
                        ["id", "created_at", "kind", "status", "site", "client", "env", "step",
                         "targets", "live", "failed", "created_by"], rows)


@require("admin")
@require_http_methods(["GET", "POST"])
def new(request):
    if request.method == "POST":
        form = ScopeForm(request.POST, user=request.user)
        if form.is_valid():
            _need_admin(request, form.cleaned_data["site"])
            run = services.create_run(request.user, form.cleaned_data["kind"], form.cleaned_data["site"],
                                      client=form.cleaned_data.get("client"),
                                      environment=form.cleaned_data.get("environment") or "prod",
                                      notes=form.cleaned_data.get("notes") or "", request=request)
            messages.success(request, f"Onboarding run #{run.pk} started for {run.site}.")
            return redirect("onboarding:step", pk=run.pk, name="targets")
    else:
        form = ScopeForm(user=request.user)
    return render(request, "onboarding/new.html", {
        "form": form, "vault_ok": secrets.available(), "vault_reason": secrets.unavailable_reason(),
        "sites": services.onboardable_sites(request.user),
    })


@require("view")
@require_GET
def run_detail(request, pk):
    run = _run(request, pk)
    return redirect("onboarding:step", pk=run.pk, name=run.step or "scope")


@require("admin")
@require_POST
def run_delete(request, pk):
    run = _run(request, pk)
    _need_admin(request, run.site)
    audit(request, "onboarding.run.delete", run, site=run.site, targets=run.targets.count())
    run.delete()
    messages.success(request, f"Onboarding run #{pk} deleted.")
    return redirect("onboarding:index")


@require("view")
@require_GET
def run_log(request, pk):
    run = _run(request, pk)
    page = paginate(request, run.step_logs.select_related("target"), 50)
    return render(request, "onboarding/log.html", {
        "run": run, "page": page, "rows": page.object_list, "query": _query(request),
        "pager_noun": "entries", "rail": run.rail(run.step), "summary": services.run_summary(run),
        "can_manage": services.can_onboard(request.user, run.site),
    })


# ----------------------------------------------------------------------------- the wizard
@require("view")
@require_http_methods(["GET", "POST"])
def step(request, pk, name):
    if name not in STEP_SLUGS:
        raise Http404("Unknown onboarding step")
    run = _run(request, pk)
    can_manage = services.can_onboard(request.user, run.site)
    if request.method == "POST":
        _need_admin(request, run.site)
        return _step_post(request, run, name)
    services.enter_step(run, name) if can_manage else None
    ctx = _step_context(request, run, name)
    ctx.update({"run": run, "step": name, "step_title": STEP_TITLES[name], "rail": run.rail(name),
                "can_manage": can_manage, "summary": services.run_summary(run),
                "next_step": services.next_step(name), "prev_step": services.previous_step(name),
                "targets": list(run.targets.select_related("credential")),
                "recent_log": list(run.step_logs.all()[:8])})
    return render(request, f"onboarding/step_{name}.html", ctx)


def _step_context(request, run, name):
    ctx = {"vault_ok": secrets.available(), "vault_reason": secrets.unavailable_reason()}
    if name == "scope":
        ctx["form"] = ScopeForm(instance=run, user=request.user, initial={"site": run.site})
    elif name == "targets":
        ctx["form"] = TargetsForm(initial={"target_type": services.default_target_type(run.kind)})
    elif name == "credentials":
        ctx["form"] = CredentialForm(initial={"name": f"{run.site or 'run'}-{run.kind}"})
        ctx["credentials"] = list(services.credentials_for(request.user).filter(site=run.site))
        ctx["rotate_form"] = RotateForm()
    elif name == "enable":
        ctx["install_hint"] = ("Nothing is installed unless you tick the box: LinkedEye then uses the "
                               "stored credential over SSH with a fixed command allow-list.")
    elif name == "logs":
        ctx["log_configs"] = {t.pk: (t.facts or {}).get("logs") for t in run.targets.all()}
    elif name == "confirm":
        ctx["sd_configured"] = sd.configured()
        ctx["scrape_config"] = sd.scrape_config(request)
    elif name == "done":
        ctx["links"] = [_target_links(t) for t in run.targets.all()]
        ctx["module_links"] = _module_links(run)
    return ctx


def _target_links(target):
    """Where this host already shows, with no further action."""
    instance = target.instance
    out = {"target": target, "items": []}
    add = out["items"].append
    if instance:
        add(("Full metrics page", _url("telemetry:host_metrics", instance), "gauge"))
        add(("Host overview", _url("telemetry:host_detail", instance), "server"))
    add(("Metrics explorer", (_url("telemetry:explorer") + f"?q=up%7Binstance%3D%22{instance}%22%7D")
         if instance else _url("telemetry:explorer"), "analytics"))
    add(("Logs", (_url("observe:logs") + f"?q={target.hostname}"), "logs"))
    if target.ci_id:
        add(("CMDB record", _url("cmdb:item_detail", target.ci_id), "database"))
    if target.device_id:
        add(("Network device", _url("network:detail", target.device_id) or _url("network:all"), "network"))
    if target.exporter_id:
        add(("Exporter", _url("observe:exporter_detail", target.exporter_id), "exporters"))
    return out


def _module_links(run):
    site = run.site
    enc = site.replace(" ", "%20")
    items = [("Fleet / Infrastructure", _url("telemetry:infrastructure") + (f"?site={enc}" if site else ""), "gauge"),
             ("Alerts", _url("telemetry:alertmanager") + (f"?site={enc}" if site else ""), "bell"),
             ("Configuration items", _url("cmdb:items") + (f"?site={enc}" if site else ""), "database"),
             ("Exporters", _url("observe:exporters") + (f"?site={enc}" if site else ""), "exporters"),
             ("Logs & events", _url("observe:logs") + (f"?site={enc}" if site else ""), "logs")]
    if run.client_id:
        items.append(("Client roll-up", _url("administration:client_edit", run.client_id), "users"))
    return [(label, href, icon) for label, href, icon in items if href]


# ----------------------------------------------------------------------------- POST handlers
def _step_post(request, run, name):
    action = request.POST.get("action") or ""
    handler = {
        "scope": _post_scope, "targets": _post_targets, "credentials": _post_credentials,
        "verify": _post_verify, "enable": _post_enable, "logs": _post_logs,
        "confirm": _post_confirm, "done": _post_done,
    }[name]
    result = handler(request, run, action)
    if result is not None:
        return result
    target_step = services.next_step(name) if action in ("next", "continue") else name
    services.enter_step(run, target_step)
    return redirect("onboarding:step", pk=run.pk, name=target_step)


def _post_scope(request, run, action):
    if action in ("next", "continue"):
        return None
    form = ScopeForm(request.POST, instance=run, user=request.user)
    if not form.is_valid():
        ctx = _step_context(request, run, "scope")
        ctx.update({"run": run, "step": "scope", "step_title": STEP_TITLES["scope"], "form": form,
                    "rail": run.rail("scope"), "can_manage": True, "summary": services.run_summary(run),
                    "next_step": services.next_step("scope"), "prev_step": "scope",
                    "targets": list(run.targets.all()), "recent_log": list(run.step_logs.all()[:8])})
        return render(request, "onboarding/step_scope.html", ctx)
    _need_admin(request, form.cleaned_data["site"])
    old_site = run.site
    run = form.save(commit=False)
    run.tenant_key = services.tenant()
    run.save()
    if old_site != run.site:
        services.log_step(run, "scope", "ok", f"Site changed from “{old_site or '—'}” to “{run.site}”")
    services.log_step(run, "scope", "ok", f"Scope saved: {run.get_kind_display()} · {run.site} · {run.environment}")
    audit(request, "onboarding.run.scope", run, site=run.site, kind=run.kind, environment=run.environment)
    messages.success(request, "Scope saved.")
    services.enter_step(run, "targets")
    return redirect("onboarding:step", pk=run.pk, name="targets")


def _post_targets(request, run, action):
    if action == "remove":
        target = get_object_or_404(run.targets, pk=request.POST.get("target"))
        services.remove_target(run, target, request=request)
        messages.success(request, "Target removed.")
        return None
    if action in ("next", "continue"):
        if not run.targets.exists():
            messages.error(request, "Add at least one target before continuing.")
            services.enter_step(run, "targets")
            return redirect("onboarding:step", pk=run.pk, name="targets")
        return None
    form = TargetsForm(request.POST, request.FILES)
    if not form.is_valid():
        ctx = _step_context(request, run, "targets")
        ctx.update({"run": run, "step": "targets", "step_title": STEP_TITLES["targets"], "form": form,
                    "rail": run.rail("targets"), "can_manage": True, "summary": services.run_summary(run),
                    "next_step": "credentials", "prev_step": "scope",
                    "targets": list(run.targets.all()), "recent_log": list(run.step_logs.all()[:8])})
        return render(request, "onboarding/step_targets.html", ctx)
    text = form.cleaned_data.get("hosts") or ""
    csv_text = services.csv_upload_text(form.cleaned_data.get("csv_file"))
    default_type = form.cleaned_data.get("target_type") or services.default_target_type(run.kind)
    rows, bad = [], []
    for blob in (text, csv_text):
        if blob.strip():
            parsed, failed = services.parse_target_list(blob, default_type, form.default_port())
            rows.extend(parsed)
            bad.extend(failed)
    if not rows:
        messages.error(request, "No usable host was found in what you pasted.")
        return None
    created, updated, skipped = services.add_targets(run, rows, request=request)
    messages.success(request, f"{created} target(s) added, {updated} updated, {skipped} already as given.")
    if bad:
        messages.warning(request, f"{len(bad)} line(s) could not be read: {', '.join(bad[:5])}")
    return None


def _post_credentials(request, run, action):
    if action == "detach":
        target = get_object_or_404(run.targets, pk=request.POST.get("target"))
        services.clear_credential(run, target, request=request)
        messages.success(request, f"Credential detached from {target.hostname}.")
        return None
    if action == "attach":
        credential = get_object_or_404(services.credentials_for(request.user), pk=request.POST.get("credential"))
        _need_admin(request, credential.site or run.site)
        ids = request.POST.getlist("targets")
        targets = list(run.targets.filter(pk__in=ids)) if ids else None
        n = services.attach_credential(run, credential, targets, request=request)
        messages.success(request, f"Credential attached to {n} target(s).")
        return None
    if action == "rotate":
        credential = get_object_or_404(services.credentials_for(request.user), pk=request.POST.get("credential"))
        _need_admin(request, credential.site or run.site)
        form = RotateForm(request.POST)
        if not form.is_valid():
            messages.error(request, "; ".join(e for errs in form.errors.values() for e in errs))
            return None
        try:
            secrets.store(credential.kind, credential.username, form.cleaned_data["secret_value"],
                          credential.site, request.user, request=request, credential=credential)
        except (secrets.VaultUnavailable, ValueError) as exc:
            messages.error(request, str(exc))
            return None
        services.log_step(run, "credentials", "ok", f"Secret rotated for “{credential.name}”")
        messages.success(request, f"Secret replaced for “{credential.name}”. The old value is gone.")
        return None
    if action in ("next", "continue"):
        return None
    form = CredentialForm(request.POST)
    if not form.is_valid():
        ctx = _step_context(request, run, "credentials")
        ctx.update({"run": run, "step": "credentials", "step_title": STEP_TITLES["credentials"], "form": form,
                    "rail": run.rail("credentials"), "can_manage": True,
                    "summary": services.run_summary(run), "next_step": "verify", "prev_step": "targets",
                    "targets": list(run.targets.select_related("credential")),
                    "recent_log": list(run.step_logs.all()[:8])})
        return render(request, "onboarding/step_credentials.html", ctx)
    try:
        credential = secrets.store(
            form.cleaned_data["kind"], form.cleaned_data.get("username") or "",
            form.cleaned_data["secret_value"], run.site, request.user,
            name=form.cleaned_data["name"], notes=form.cleaned_data.get("notes") or "",
            request=request, client=run.client)
    except (secrets.VaultUnavailable, ValueError) as exc:
        # the reason, never the value
        messages.error(request, str(exc))
        services.log_step(run, "credentials", "error", "Credential was NOT stored: the vault is unavailable")
        return None
    services.log_step(run, "credentials", "ok",
                      f"{credential.get_kind_display()} “{credential.name}” stored (encrypted)")
    if (form.cleaned_data.get("apply_to") or "all") == "all":
        services.attach_credential(run, credential, request=request)
    messages.success(request, f"“{credential.name}” stored in the vault. It is never shown again.")
    return None


def _post_verify(request, run, action):
    if action in ("next", "continue"):
        return None
    if action != "verify":
        return None
    targets = list(run.targets.all())
    if not targets:
        messages.error(request, "Add targets first.")
        return None
    services.enter_step(run, "verify")
    pending = done = 0
    for target in targets:
        facts, msg = _hook(run, "verify", steps.verify_target, target)
        if msg:
            pending += 1
            continue
        done += 1
        state = facts.get("state") or (TargetState.EXPORTER_FOUND if facts.get("exporter")
                                       else TargetState.REACHABLE)
        services.set_target_state(target, state, error=facts.get("error", ""), facts=facts)
    if pending:
        messages.warning(request, f"Reachability checks are not wired up yet ({pending} target(s) "
                                  "left as they were). The step is recorded in the run log.")
    if done:
        messages.success(request, f"{done} target(s) verified.")
    audit(request, "onboarding.verify", run, site=run.site, targets=len(targets), verified=done)
    return None


def _post_enable(request, run, action):
    if action in ("next", "continue") or action != "enable":
        return None
    options = {"install_over_ssh": request.POST.get("install_over_ssh") == "1",
               "snmp_module": (request.POST.get("snmp_module") or "").strip()[:60]}
    services.enter_step(run, "enable")
    pending = done = 0
    for target in run.targets.all():
        facts, msg = _hook(run, "enable", steps.enable_collection, target, options=options)
        if msg:
            pending += 1
            continue
        done += 1
        services.set_target_state(target, facts.get("state") or TargetState.INSTALLED,
                                  error=facts.get("error", ""), facts=facts)
    if pending:
        messages.warning(request, f"Collection set-up is not wired up yet ({pending} target(s)). "
                                  "The run log records the attempt.")
    if done:
        messages.success(request, f"Collection enabled for {done} target(s).")
    audit(request, "onboarding.enable", run, site=run.site, install_over_ssh=options["install_over_ssh"])
    return None


def _post_logs(request, run, action):
    if action in ("next", "continue") or action != "logs":
        return None
    pending = done = 0
    for target in run.targets.all():
        cfg, msg = _hook(run, "logs", steps.logs_config, target)
        if msg:
            pending += 1
            continue
        done += 1
        services.set_target_state(target, target.state, facts={"logs": cfg})
    if pending:
        messages.warning(request, f"Log shipper configuration is not wired up yet ({pending} target(s)).")
    if done:
        messages.success(request, f"Log shipper configuration ready for {done} target(s).")
    audit(request, "onboarding.logs", run, site=run.site)
    return None


def _post_confirm(request, run, action):
    if action in ("next", "continue"):
        return None
    if action == "fail":
        services.fail_run(run, request.POST.get("reason", "")[:200] or "Marked failed by the operator")
        audit(request, "onboarding.run.fail", run, site=run.site)
        messages.warning(request, "Run marked failed. You can reopen it from the run log.")
        return redirect("onboarding:step", pk=run.pk, name="confirm")
    if action == "reopen":
        services.reopen_run(run)
        audit(request, "onboarding.run.reopen", run, site=run.site)
        messages.success(request, "Run reopened.")
        return redirect("onboarding:step", pk=run.pk, name=run.step)
    if action != "activate":
        return None
    services.enter_step(run, "confirm")
    pending = done = 0
    for target in run.targets.all():
        result, msg = _hook(run, "confirm", steps.activate, target)
        if msg:
            pending += 1
            continue
        done += 1
        target.refresh_from_db()
        if target.state not in (TargetState.LIVE, TargetState.SCRAPING):
            services.set_target_state(target, TargetState.SCRAPING, facts={"activation": result})
    if pending:
        messages.warning(request, f"Activation is not wired up yet ({pending} target(s)); "
                                  "the targets stay in the run and keep their state.")
    if done:
        messages.success(request, f"{done} target(s) registered and activated.")
        emit("onboarding", f"Onboarding activated {done} target(s)", severity="info", site=run.site,
             kind="onboarding", obj=run)
    audit(request, "onboarding.activate", run, site=run.site, activated=done)
    return None


def _post_done(request, run, action):
    if action == "finish":
        services.enter_step(run, "done")
        services.log_step(run, "done", "ok", "Run completed")
        audit(request, "onboarding.run.complete", run, site=run.site,
              targets=run.targets.count(), live=run.targets.filter(state=TargetState.LIVE).count())
        emit("onboarding", f"Onboarding run #{run.pk} complete", severity="info", site=run.site,
             kind="onboarding", obj=run)
        messages.success(request, "Onboarding complete. Everything for these hosts now shows in LinkedEye.")
        return redirect("onboarding:step", pk=run.pk, name="done")
    return None


# ----------------------------------------------------------------------------- targets / credentials
@require("admin")
@require_POST
def target_toggle(request, pk, target_pk):
    run = _run(request, pk)
    _need_admin(request, run.site)
    target = get_object_or_404(run.targets, pk=target_pk)
    target.enabled = not target.enabled
    target.save(update_fields=["enabled", "updated_at"])
    services.log_step(run, run.step, "info",
                      f"{target.hostname} {'joined' if target.enabled else 'left'} service discovery",
                      target=target)
    audit(request, "onboarding.target.toggle", run, site=run.site, hostname=target.hostname,
          enabled=target.enabled)
    return redirect("onboarding:step", pk=run.pk, name=request.POST.get("step") or run.step)


# ----------------------------------------------------------------------------- service discovery
@require("admin")
@require_GET
def sd_page(request):
    if not can(request.user, "admin"):
        raise PermissionDenied
    token = request.session.pop("onboarding_sd_token", None)
    row = sd.token_row()
    ctx = {
        "token_row": row, "fresh_token": token,
        "scrape_config": sd.scrape_config(request, token),
        "sd_url": sd.sd_url(request),
        "fetches": sd.recent_fetches(),
        "targets": list(services.enabled_sd_targets()[:200]),
        "n_targets": services.enabled_sd_targets().count(),
        "vault_ok": secrets.available(), "vault_reason": secrets.unavailable_reason(),
        "rate_limit": sd.RATE_LIMIT, "rate_window": sd.RATE_WINDOW,
        "payload_sample": sd.targets_payload()[:3],
    }
    return render(request, "onboarding/sd.html", ctx)


@require("admin")
@require_POST
def sd_token(request):
    if not can(request.user, "admin"):
        raise PermissionDenied
    try:
        _row, plaintext = sd.issue_token(request.user, request=request)
    except (secrets.VaultUnavailable, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("onboarding:sd_page")
    # handed to the next page render once, then dropped; it is never read back from the vault
    request.session["onboarding_sd_token"] = plaintext
    messages.success(request, "A new service-discovery token was generated. Copy it now: it is shown once.")
    return redirect("onboarding:sd_page")


@csrf_exempt
def sd_targets(request):
    """GET /api/sd/targets.json - Prometheus http_sd, bearer token from the vault.

    Never 500s on a bad token: every failure path returns a small JSON body with a 4xx status.
    """
    if request.method not in ("GET", "HEAD"):
        resp = JsonResponse({"error": "Use GET"}, status=405)
        resp["Allow"] = "GET, HEAD"
        return resp
    try:
        if sd.rate_limited(request):
            sd.record_fetch(request, 0, "throttled")
            resp = JsonResponse({"error": "Too many requests"}, status=429)
            resp["Retry-After"] = str(sd.RATE_WINDOW)
            return resp
        try:
            credential = sd.authenticate(request)
        except (secrets.VaultUnavailable, secrets.VaultError):
            sd.record_fetch(request, 0, "denied")
            return JsonResponse({"error": "Service discovery is unavailable: the credential vault is "
                                          "not configured on the server."}, status=503)
        if credential is None:
            sd.record_fetch(request, 0, "denied")
            resp = JsonResponse({"error": "Invalid or missing bearer token"}, status=401)
            resp["WWW-Authenticate"] = 'Bearer realm="linkedeye-sd"'
            return resp
        payload = sd.targets_payload()
        sd.record_fetch(request, len(payload), "ok")
        return JsonResponse(payload, safe=False)
    except Exception:                                 # noqa: BLE001 - a bad request must never 500
        log.exception("service discovery failed")
        return JsonResponse({"error": "Service discovery is temporarily unavailable"}, status=503)
