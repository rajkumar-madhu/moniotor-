from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core import access
from core.access import can, require, scope_by_site
from domainmonitor.views import SafeCsvWriter
from probes import runner

from . import stats
from .forms import UrlCheckForm
from .models import Status, UrlCheck, UrlResult


GLOBAL_SITES = ("", "All Sites")
RUN_ALL_LIMIT = 50          # checks probed synchronously by one "Run all" request


def _scope(request):
    """Checks of this tenant on the sites the user may see ("All Sites" checks stay visible)."""
    return scope_by_site(UrlCheck.objects.filter(tenant_key=settings.LINKEDEYE_TENANT), request.user)


def can_on(user, perm, site):
    """RUNNING a check: `perm` on its site; an 'All Sites' check may be run by an operator of any site (read-only
    against the target, and operators need it during incidents; same split as Domain Monitor)."""
    return can(user, perm, None if (site or "") in GLOBAL_SITES else site)


def can_change(user, perm, site):
    """CHANGING a check (create, edit, delete): an 'All Sites' check needs `perm` on every site, so a one-site
    operator cannot alter or remove monitoring that other sites rely on."""
    return access.can_on(user, perm, site)


def mark_allowed(user, items):
    """Set c.can_op / c.can_admin so templates show per-row actions only where the user may use them."""
    for c in items:
        c.can_run = can_on(user, "operate", c.site)
        c.can_op = can_change(user, "operate", c.site)      # edit
        c.can_admin = can_change(user, "admin", c.site)     # delete
    return items


def _filtered(request):
    qs = _scope(request)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(url__icontains=q) | Q(tags__icontains=q))
    for param, field in (("site", "site"), ("status", "last_status"), ("type", "check_type")):
        v = request.GET.get(param)
        if v:
            qs = qs.filter(**{field: v})
    return qs


def _per_page(request, default=10):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _change(now, then):
    """Whole-number % change between two stored values, or None when there is nothing to compare with."""
    if now is None or not then:
        return None
    return round(100.0 * (now - then) / then)


def index(request, pk=None):
    scope = _scope(request)
    checks = list(scope.filter(enabled=True))
    counts = {s: sum(1 for c in checks if c.last_status == s) for s in Status.values}
    total = len(checks)
    pct = lambda n: (100.0 * n / total) if total else 0  # noqa: E731

    editing = get_object_or_404(scope, pk=pk) if pk else None
    if request.method == "POST":
        if not can(request.user, "operate") or (editing and not can_change(request.user, "operate", editing.site)):
            messages.error(request, "Only operators can add or change URL checks.")
            return redirect("urlchecker:index")
        form = UrlCheckForm(request.POST, instance=editing)
        if form.is_valid() and not can_change(request.user, "operate", form.cleaned_data["site"]):
            form.add_error("site", "You do not have operator rights on this site (an All Sites check needs them on every site).")
        if form.is_valid():
            obj = form.save(commit=False)
            obj.tenant_key = settings.LINKEDEYE_TENANT
            try:
                with transaction.atomic():
                    obj.save()
            except IntegrityError:  # a concurrent save took the name after validation
                form.add_error("name", "A URL check with this name already exists.")
            else:
                runner.run_url_checks([obj])
                messages.success(request, f"Saved “{obj.name}” and ran its first check.")
                return redirect("urlchecker:index")
    else:
        if editing and not can_change(request.user, "operate", editing.site):
            editing = None
        form = UrlCheckForm(instance=editing)

    page = Paginator(_filtered(request), _per_page(request)).get_page(request.GET.get("page"))
    mark_allowed(request.user, page)
    up24 = stats.uptime_map([c.pk for c in page], 24)
    for c in page:
        c.uptime24 = up24.get(c.pk)
    avg = stats.avg_response(checks)
    avg_prev = stats.avg_response(checks, hours=24, offset_hours=24)
    # Total URLs trend: enabled checks now vs the ones that already existed 7 days ago.
    before = sum(1 for c in checks if c.created_at <= timezone.now() - timedelta(days=7))
    all_events = request.GET.get("events") == "all"
    history = stats.daily_history(checks)
    history["xLabelEvery"] = 1
    history.update(stats.axis_scale(max((sum(s["values"][i] or 0 for s in history["series"]) for i in range(len(history["labels"]))), default=0)))
    ctx = {
        "page": page, "form": form, "editing": editing, "total": total, "counts": counts,
        "pct": {k: pct(v) for k, v in counts.items()}, "avg_response": avg,
        "total_delta": _change(total, before), "avg_delta": _change(avg, avg_prev),
        "uptime7": stats.overall_uptime(checks, 24 * 7),
        "donut": {"segments": [
            {"label": "Online", "value": counts["online"], "color": "#0f7a55"},
            {"label": "Down", "value": counts["down"], "color": "#d92d20"},
            {"label": "Degraded", "value": counts["degraded"], "color": "#e08a00"},
            {"label": "Unknown", "value": counts["unknown"], "color": "#d6cfc1"}],
            "center": {"value": str(total), "label": "Total URLs"}},
        "trend": stats.response_trend(checks), "history": history, "all_events": all_events,
        "events": UrlResult.objects.filter(url_check__in=scope).select_related("url_check")[:50 if all_events else 6],
        "sites": scope.order_by().values_list("site", flat=True).distinct(),
        "statuses": Status.choices, "types": UrlCheck.TYPES,
        "query": request.GET.copy(), "can_operate": can(request.user, "operate"),
    }
    ctx["query"].pop("page", None)
    return render(request, "urlchecker/index.html", ctx)


@require_POST
@require("operate")
def run_now(request, pk=None):
    qs = _scope(request).filter(enabled=True)
    if pk:
        items = [get_object_or_404(qs, pk=pk)]
        if not can_on(request.user, "operate", items[0].site):
            return JsonResponse({"error": f"'operate' permission required on {items[0].site}"}, status=403)
    else:
        # bound the request: probe at most RUN_ALL_LIMIT checks, the ones checked longest ago first; the rest stay due
        # and run on the scheduler
        ordered = qs.order_by(F("last_checked_at").asc(nulls_first=True), "pk")
        allowed = [c for c in ordered if can_on(request.user, "operate", c.site)]
        items, skipped = allowed[:RUN_ALL_LIMIT], len(allowed) - min(len(allowed), RUN_ALL_LIMIT)
    results = runner.run_url_checks(items)
    if pk:
        summary, skipped = ", ".join(f"{c.name}: {s}" for c, s in zip(items, results)), 0
    else:
        summary = f"Checked {len(items)} URL(s)"
        if skipped:
            summary += (f"; {skipped} more were not checked now (at most {RUN_ALL_LIMIT} per run, longest-unchecked "
                        f"first) and will run on the scheduler")
    return JsonResponse({"checked": len(items), "skipped": skipped, "summary": summary})


@require_POST
@require("admin")
def delete(request, pk):
    obj = get_object_or_404(_scope(request), pk=pk)
    if not can_change(request.user, "admin", obj.site):
        messages.error(request, f"You do not have admin rights on {obj.site}.")
        return redirect("urlchecker:index")
    obj.delete()
    messages.success(request, f"Deleted “{obj.name}”.")
    return redirect("urlchecker:index")


def detail(request, pk):
    check = get_object_or_404(_scope(request), pk=pk)
    mark_allowed(request.user, [check])
    results = check.results.all()[:200]
    return render(request, "urlchecker/detail.html", {
        "check": check, "results": results[:50],
        "uptime24": stats.uptime_map([check.pk], 24).get(check.pk),
        "uptime7": stats.uptime_map([check.pk], 24 * 7).get(check.pk),
        "trend": stats.response_trend([check.pk]),
    })


def export_csv(request):
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="url-checks.csv"'
    w = SafeCsvWriter(resp)   # names/messages may start with = + - @ (formula injection when opened in a spreadsheet)
    w.writerow(["name", "url", "type", "site", "status", "http_status", "response_ms", "last_checked", "message"])
    for c in _filtered(request):
        w.writerow([c.name, c.url, c.check_type, c.site, c.last_status, c.last_http_status or "",
                    "" if c.last_response_ms is None else round(c.last_response_ms), c.last_checked_at or "", c.last_error])
    return resp
