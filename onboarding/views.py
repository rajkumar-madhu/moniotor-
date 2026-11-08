"""The onboarding wizard: a server-rendered, resumable walk from a site to live metrics.

Every mutating step is POST + CSRF + audited and needs the `admin` role on the run's site. Every
queryset is tenant- and site-scoped. Nothing is probed or installed without the operator pressing
the button on that step.

The four pieces of real work (verify, enable, logs, activate) are hooks in `steps.py`. Until a
hook is filled in it raises `StepPending`, which is recorded as a "pending" step-log line so the
whole wizard still walks end to end.
"""
import importlib.util
import logging
import time

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from administration import services as administration_services
from core import secrets
from core.access import require
from core.audit import audit
from core.events import emit

from . import sd, services, steps
from .forms import CredentialForm, RotateForm, ScopeForm, TargetsForm
from .models import STEP_SLUGS, STEP_TITLES, CredentialKind, OnboardingRun, RunKind, RunStatus, TargetState

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
    """Call a steps.py hook, timing it. Returns (result, pending_msg, duration_ms).

    A hook that is not wired up yet yields (None, message, ms) and a "pending" step-log line. An
    unexpected exception is recorded by exception type only: a hook promises never to put a secret
    in a message, and this keeps even a buggy one from writing a value into the log or a page.
    The caller writes the success line once it knows the verdict.
    """
    host = target.hostname if target is not None else "run"
    start = time.monotonic()

    def ms():
        return round((time.monotonic() - start) * 1000, 1)

    try:
        result = fn(target, **kwargs) if target is not None else fn(**kwargs)
    except steps.StepPending as exc:
        services.log_step(run, step, "pending", f"{host}: {exc}", target=target, duration_ms=ms())
        return None, str(exc), ms()
    except Exception as exc:                      # noqa: BLE001 - deliberate: never echo the message
        log.error("onboarding %s hook failed for %s: %s", step, host, type(exc).__name__)
        services.log_step(run, step, "error", f"{host}: {type(exc).__name__} (see the server log)",
                          target=target, duration_ms=ms())
        return None, f"{step} failed for {host}: {type(exc).__name__}", ms()
    return result or {}, "", ms()


def _record(run, step, target, state, error, ms):
    """The one success/failure line per hook call."""
    services.log_step(run, step, "error" if error else "ok",
                      f"{target.hostname}: {state}" + (f" - {error}" if error else ""),
                      target=target, duration_ms=ms)


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
        "n_sd": services.enabled_sd_targets(user=request.user).count(),
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
        # the Add buttons on Sites, Network devices and Infrastructure arrive here with ?kind= and ?site=
        initial = {}
        if request.GET.get("kind") in RunKind.values:
            initial["kind"] = request.GET["kind"]
        if request.GET.get("site") in services.onboardable_sites(request.user):
            initial["site"] = request.GET["site"]
        form = ScopeForm(user=request.user, initial=initial)
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
def _render_step(request, run, name, form=None, **extra):
    """Render one step page. GET and every re-render after a failed POST go through here."""
    can_manage = services.can_onboard(request.user, run.site)
    ctx = _step_context(request, run, name)
    if form is not None:
        ctx["form"] = form
    ctx.update({"run": run, "step": name, "step_title": STEP_TITLES[name], "rail": run.rail(name),
                "can_manage": can_manage, "summary": services.run_summary(run),
                "next_step": services.next_step(name), "prev_step": services.previous_step(name),
                "targets": list(run.targets.select_related("credential")),
                "recent_log": list(run.step_logs.all()[:8])})
    ctx.update(extra)
    return render(request, f"onboarding/step_{name}.html", ctx)


@require("view")
@require_http_methods(["GET", "POST"])
@sensitive_post_parameters()
@sensitive_variables()
def step(request, pk, name):
    """GET shows a step and changes nothing. POST is CSRF-protected, admin-for-the-site only and audited.

    The POST carries secrets, so neither the debug page nor an error report may show its parameters
    or any local variable of the code that runs under this view (both decorators above)."""
    if name not in STEP_SLUGS:
        raise Http404("Unknown onboarding step")
    run = _run(request, pk)
    if request.method == "POST":
        _need_admin(request, run.site)
        with secrets.acting_as(request):          # a credential read by a hook is audited as this operator
            return _step_post(request, run, name)
    return _render_step(request, run, name)


def _step_context(request, run, name):
    ctx = {"vault_ok": secrets.available(), "vault_reason": secrets.unavailable_reason()}
    if name == "scope":
        ctx["form"] = ScopeForm(instance=run, user=request.user, initial={"site": run.site})
    elif name == "targets":
        ctx["form"] = TargetsForm(initial={"target_type": services.default_target_type(run.kind)})
    elif name == "credentials":
        ctx["form"] = CredentialForm(initial={"name": f"{run.site or 'run'}-{run.kind}"})
        # who stored what (labels, user names) is for the site's admins; a viewer sees none of it
        ctx["credentials"] = (list(services.credentials_for(request.user).filter(site=run.site))
                              if services.can_onboard(request.user, run.site) else [])
        ctx["rotate_form"] = RotateForm()
    elif name == "enable":
        ctx["ssh_available"] = importlib.util.find_spec("paramiko") is not None
        ctx["install_hint"] = ("Nothing is installed unless you tick the box: LinkedEye then uses the "
                               "stored credential over SSH with a fixed command allow-list.")
    elif name == "confirm":
        ctx["sd_configured"] = sd.configured()
        ctx["scrape_config"] = sd.scrape_config(request)
        ctx["poll_url"] = reverse("onboarding:poll", args=[run.pk])
    elif name == "done":
        ctx["links"] = [_target_links(t) for t in run.targets.all()]
        ctx["module_links"] = _module_links(run, request.user)
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


def _module_links(run, user=None):
    site = run.site
    enc = site.replace(" ", "%20")
    items = [("Fleet / Infrastructure", _url("telemetry:infrastructure") + (f"?site={enc}" if site else ""), "gauge"),
             ("Alerts", _url("telemetry:alertmanager") + (f"?site={enc}" if site else ""), "bell"),
             ("Configuration items", _url("cmdb:items") + (f"?site={enc}" if site else ""), "database"),
             ("Exporters", _url("observe:exporters") + (f"?site={enc}" if site else ""), "exporters"),
             ("Logs & events", _url("observe:logs") + (f"?site={enc}" if site else ""), "logs")]
    # the client page is part of the administration console: only an admin on every site may open it
    if run.client_id and user is not None and administration_services.is_global_admin(user):
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
    run.refresh_from_db()
    target_step = services.next_step(name) if action in ("next", "continue") else name
    services.enter_step(run, target_step)
    return redirect("onboarding:step", pk=run.pk, name=target_step)


def _post_scope(request, run, action):
    if action in ("next", "continue"):
        return None
    old_site = run.site                      # before the form assigns the new value to the instance
    form = ScopeForm(request.POST, instance=run, user=request.user)
    if not form.is_valid():
        run.site = old_site
        return _render_step(request, run, "scope", form=form)
    _need_admin(request, form.cleaned_data["site"])
    run = form.save(commit=False)
    run.tenant_key = services.tenant()
    run.client = run.client or services.site_client(run.site)
    run.save()
    if old_site != run.site:
        services.log_step(run, "scope", "ok", f"Site changed from “{old_site or '—'}” to “{run.site}”")
        # a credential belongs to one site: it must not follow a run into another one
        moved = run.targets.exclude(credential__isnull=True).exclude(credential__site=run.site)
        n = moved.update(credential=None)
        if n:
            services.log_step(run, "scope", "warn", f"{n} credential attachment(s) removed: the site changed")
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
            return redirect("onboarding:step", pk=run.pk, name="targets")
        return None
    form = TargetsForm(request.POST, request.FILES)
    if not form.is_valid():
        return _render_step(request, run, "targets", form=form)
    text = form.cleaned_data.get("hosts") or ""
    try:
        csv_text = services.csv_upload_text(form.cleaned_data.get("csv_file"))
    except ValueError as exc:
        messages.error(request, str(exc))
        return None
    default_type = form.cleaned_data.get("target_type") or services.default_target_type(run.kind)
    rows, bad = [], []
    for blob in (text, csv_text):
        if blob.strip():
            parsed, failed = services.parse_target_list(blob, default_type, form.cleaned_data.get("port"))
            rows.extend(parsed)
            bad.extend(failed)
    if not rows:
        detail = f" Could not read: {', '.join(bad[:3])}" if bad else ""
        messages.error(request, "No usable host was found in what you pasted." + detail)
        return None
    created, updated, skipped = services.add_targets(run, rows, request=request)
    messages.success(request, f"{created} target(s) added, {updated} updated, {skipped} already as given.")
    if bad:
        messages.warning(request, f"{len(bad)} line(s) could not be read: {', '.join(bad[:5])}")
    return None


def _run_credential(request, run):
    """A vault row of this run's site that the user may see; 404 otherwise."""
    return get_object_or_404(services.credentials_for(request.user).filter(site=run.site),
                             pk=request.POST.get("credential"))


def _post_credentials(request, run, action):
    if action == "detach":
        target = get_object_or_404(run.targets, pk=request.POST.get("target"))
        services.clear_credential(run, target, request=request)
        messages.success(request, f"Credential detached from {target.hostname}.")
        return None
    if action == "attach":
        credential = _run_credential(request, run)
        ids = request.POST.getlist("targets")
        targets = list(run.targets.filter(pk__in=ids)) if ids else None
        n = services.attach_credential(run, credential, targets, request=request)
        messages.success(request, f"Credential attached to {n} target(s).")
        return None
    if action == "delete":
        credential = _run_credential(request, run)
        name = credential.name
        audit(request, "credential.delete", credential, site=credential.site, kind=credential.kind)
        credential.delete()
        services.log_step(run, "credentials", "info", f"Credential “{name}” deleted from the vault")
        messages.success(request, f"“{name}” deleted from the vault.")
        return None
    if action == "rotate":
        credential = _run_credential(request, run)
        if credential.kind == CredentialKind.SNMP_V3:
            # a v3 login is one encrypted JSON blob (protocols and both passwords): a bare string would corrupt it
            messages.error(request, "An SNMP v3 login is replaced as a whole: store a new one and attach it.")
            return None
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
        return _render_step(request, run, "credentials", form=form)
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
    if action != "verify":
        return None
    targets = list(run.targets.all())
    if not targets:
        messages.error(request, "Add targets first.")
        return None
    services.enter_step(run, "verify")
    pending = done = bad = 0
    for target in targets:
        result, msg, ms = _hook(run, "verify", steps.verify_target, target)
        if msg:
            pending += 1
            continue
        state, error, facts = services.hook_outcome(
            result, target.state, TargetState.UNREACHABLE,
            default_state=services.verify_default_state(services.scrub(result)))
        services.set_target_state(target, state, error=error, facts=facts)
        _record(run, "verify", target, target.get_state_display(), error, ms)
        done += 1
        bad += state in (TargetState.UNREACHABLE, TargetState.FAILED)
    if pending:
        messages.warning(request, f"Reachability checks are not wired up yet ({pending} target(s) "
                                  "left as they were). The step is recorded in the run log.")
    if done:
        (messages.warning if bad else messages.success)(
            request, f"{done} target(s) verified" + (f", {bad} with a problem." if bad else "."))
    audit(request, "onboarding.verify", run, site=run.site, targets=len(targets), verified=done)
    return None


def _post_enable(request, run, action):
    if action != "enable":
        return None
    options = {"install_over_ssh": request.POST.get("install_over_ssh") == "1",
               "snmp_module": (request.POST.get("snmp_module") or "").strip()[:60]}
    services.enter_step(run, "enable")
    pending = done = 0
    for target in run.targets.all():
        result, msg, ms = _hook(run, "enable", steps.enable_collection, target, options=options)
        if msg:
            pending += 1
            continue
        state, error, facts = services.hook_outcome(result, target.state, TargetState.FAILED)
        services.set_target_state(target, state, error=error, facts=facts)
        _record(run, "enable", target, target.get_state_display(), error, ms)
        done += 1
    if pending:
        messages.warning(request, f"Collection set-up is not wired up yet ({pending} target(s)). "
                                  "The run log records the attempt.")
    if done:
        messages.success(request, f"Collection set-up ran for {done} target(s).")
    audit(request, "onboarding.enable", run, site=run.site, install_over_ssh=options["install_over_ssh"])
    return None


def _redact_logs(cfg):
    """(stored, shown): the shipper config safe to keep in the database, and the full text to show
    once. The log-ingest token is replaced by a placeholder in what is stored."""
    cfg = dict(cfg or {})
    shown = str(cfg.get("config") or "")
    secret_values = {str(cfg.get("token") or "")}
    try:
        from administration import integration_config as ic
        secret_values.add(str(ic.log_ingest_token() or ""))
    except Exception:                      # noqa: BLE001
        pass
    stored = shown
    try:
        from .logs import _q
    except Exception:                      # noqa: BLE001
        _q = None
    for value in sorted(secret_values, key=len, reverse=True):
        if not value:
            continue
        # the config quotes the token (backslash, quote and dollar are escaped), so redact that form too
        forms = {value}
        if _q is not None:
            forms.add(_q(value)[1:-1])
        for form in sorted(forms, key=len, reverse=True):
            stored = stored.replace(form, "<LOG_INGEST_TOKEN>")
    keep = services.scrub({k: v for k, v in cfg.items() if k not in ("config",)})
    keep["config"] = stored
    return keep, shown


def _post_logs(request, run, action):
    if action != "logs":
        return None
    services.enter_step(run, "logs")
    pending = done = 0
    fresh = {}
    tenant_wide = services.can_manage_discovery(request.user)
    for target in run.targets.all():
        cfg, msg, ms = _hook(run, "logs", steps.logs_config, target)
        if msg:
            pending += 1
            continue
        stored, shown = _redact_logs(cfg)
        if not tenant_wide:
            shown = stored["config"]  # the ingest token is shared by every site: only an all-sites admin sees it
        services.set_target_state(target, target.state, error=target.last_error, facts={"logs": stored})
        fresh[target.pk] = shown
        _record(run, "logs", target, "shipper configuration ready", "", ms)
        done += 1
    if pending:
        messages.warning(request, f"Log shipper configuration is not wired up yet ({pending} target(s)).")
    audit(request, "onboarding.logs", run, site=run.site, generated=done)
    if done:
        if tenant_wide:
            messages.success(request, f"Log shipper configuration ready for {done} target(s). "
                                      "The ingest token is shown on this page only; copy it now.")
        else:
            messages.warning(request, f"Log shipper configuration ready for {done} target(s), with a placeholder "
                                      "where the ingest token goes: that token is shared by every site, so ask an "
                                      "administrator of all sites for it.")
        # rendered in this response and never stored: a reload shows the placeholder
        resp = _render_step(request, run, "logs", fresh_logs=fresh, token_shown=tenant_wide)
        resp["Cache-Control"] = "no-store"
        return resp
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
    if action not in ("activate", "check"):
        return None
    services.enter_step(run, "confirm")
    if action == "activate":
        pending = done = 0
        for target in list(run.targets.all()):
            result, msg, ms = _hook(run, "confirm", steps.activate, target)
            if msg:
                pending += 1
                continue
            target.refresh_from_db()
            state, error, facts = services.hook_outcome(
                result, target.state, TargetState.FAILED,
                default_state=target.state if target.state == TargetState.LIVE else TargetState.SCRAPING)
            services.set_target_state(target, state, error=error, facts={"activation": facts})
            _record(run, "confirm", target, target.get_state_display(), error, ms)
            done += 1
        if pending:
            messages.warning(request, f"Activation is not wired up yet ({pending} target(s)); "
                                      "the targets stay in the run and keep their state.")
        if done:
            messages.success(request, f"{done} target(s) registered for scraping.")
            emit("onboarding", f"Onboarding activated {done} target(s)", severity="info", site=run.site,
                 kind="onboarding", obj=run)
        audit(request, "onboarding.activate", run, site=run.site, activated=done)
    live = services.refresh_liveness(list(run.targets.select_related("run")), run)
    if live:
        messages.success(request, f"{live} target(s) report up == 1: first samples have landed.")
    elif action == "check":
        messages.info(request, "No new target reports up == 1 yet. Prometheus picks new targets up on its "
                               "next discovery cycle.")
    run.refresh_from_db()
    if run.status == RunStatus.COMPLETE:
        return redirect("onboarding:step", pk=run.pk, name="done")
    return None


@require("admin")
@require_POST
def poll(request, pk):
    """The confirm page's poll: check Prometheus now and return each target's state as JSON."""
    run = _run(request, pk)
    _need_admin(request, run.site)
    before = set(run.targets.filter(state=TargetState.LIVE).values_list("pk", flat=True))
    services.refresh_liveness(list(run.targets.select_related("run")), run)
    run.refresh_from_db()
    after = set(run.targets.filter(state=TargetState.LIVE).values_list("pk", flat=True))
    if after - before:
        audit(request, "onboarding.poll", run, site=run.site, newly_live=len(after - before))
    return JsonResponse(services.status_payload(run))


def _post_done(request, run, action):
    if action == "finish":
        services.enter_step(run, "done")
        live = run.targets.filter(state=TargetState.LIVE).count()
        total = run.targets.count()
        services.complete_run(run, f"Run completed by the operator: {live} of {total} target(s) live")
        audit(request, "onboarding.run.complete", run, site=run.site, targets=total, live=live)
        emit("onboarding", f"Onboarding run #{run.pk} complete", severity="info", site=run.site,
             kind="onboarding", obj=run)
        if live < total:
            messages.warning(request, f"Run completed with {total - live} target(s) not live yet; "
                                      "they stay in service discovery and go live when Prometheus reports them up.")
        else:
            messages.success(request, "Onboarding complete. Everything for these hosts now shows in LinkedEye.")
        return redirect("onboarding:step", pk=run.pk, name="done")
    return None


# ----------------------------------------------------------------------------- targets
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
    back = request.POST.get("step")
    return redirect("onboarding:step", pk=run.pk, name=back if back in STEP_SLUGS else run.step)


# ----------------------------------------------------------------------------- service discovery
def _need_discovery_admin(request):
    if not services.can_manage_discovery(request.user):
        raise PermissionDenied("Service discovery serves every site, so it needs the admin role on every site.")


def _sd_context(request, fresh_token=None):
    row = sd.token_row()
    return {
        "token_row": row, "fresh_token": fresh_token,
        "scrape_config": sd.scrape_config(request, fresh_token),
        "sd_url": sd.sd_url(request),
        "fetches": sd.recent_fetches(),
        "targets": list(services.enabled_sd_targets()[:200]),
        "n_targets": services.enabled_sd_targets().count(),
        "vault_ok": secrets.available(), "vault_reason": secrets.unavailable_reason(),
        "rate_limit": sd.RATE_LIMIT, "rate_window": sd.RATE_WINDOW,
        "payload_sample": sd.targets_payload()[:3],
    }


@require("admin")
@require_GET
def sd_page(request):
    _need_discovery_admin(request)
    return render(request, "onboarding/sd.html", _sd_context(request))


@require("admin")
@require_POST
@sensitive_variables()
def sd_token(request):
    """Generate or rotate the bearer token. The plaintext is rendered in this one response (not kept
    in the session, the database or a log) so the operator can paste the scrape_config."""
    _need_discovery_admin(request)
    try:
        _row, plaintext = sd.issue_token(request.user, request=request)
    except (secrets.VaultUnavailable, ValueError) as exc:
        messages.error(request, str(exc))
        return redirect("onboarding:sd_page")
    messages.success(request, "A new service-discovery token was generated. Copy it now: it is shown once.")
    resp = render(request, "onboarding/sd.html", _sd_context(request, fresh_token=plaintext))
    resp["Cache-Control"] = "no-store"
    return resp


@csrf_exempt
@sensitive_variables()
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
            sd.record_throttled(request)
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
        resp = JsonResponse(payload, safe=False)
        resp["Cache-Control"] = "no-store"
        return resp
    except Exception as exc:                          # noqa: BLE001 - a bad request must never 500
        log.error("service discovery failed: %s", type(exc).__name__)
        return JsonResponse({"error": "Service discovery is temporarily unavailable"}, status=503)
