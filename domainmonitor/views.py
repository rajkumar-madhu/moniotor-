import json
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from probes import checks, runner

from .forms import DomainForm
from .models import Domain, DomainCheck

HEALTHY_SSL = ("Valid",)


def _scope():
    return Domain.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)


def _uptime(domain_ids, hours):
    since = timezone.now() - timedelta(hours=hours)
    rows = (DomainCheck.objects.filter(domain_id__in=domain_ids, checked_at__gte=since).values("domain_id")
            .annotate(total=Count("id"), up=Count("id", filter=Q(up=True))))
    return {r["domain_id"]: 100.0 * r["up"] / r["total"] for r in rows if r["total"]}


def _query(request):
    q = request.GET.copy()
    q.pop("page", None)
    q.pop("selected", None)
    return q


# ------------------------------------------------------------------ overview (monitoring view)
def overview(request):
    scope = _scope().filter(is_active=True)
    domains = list(scope)
    total = len(domains)
    down = [d for d in domains if d.status == "down"]
    expiring = [d for d in domains if d.ssl_status in ("Expiring", "Expired")]
    paused = [d for d in domains if not d.monitor_enabled]
    healthy = [d for d in domains if d.status == "up" and d.dns_status == "Resolved" and d.ssl_status in HEALTHY_SSL]
    ids = [d.pk for d in domains]
    now = timezone.now()

    # response time, 24h, 30-minute buckets
    start = now - timedelta(hours=24)
    acc = defaultdict(list)
    for at, ms in DomainCheck.objects.filter(domain_id__in=ids, checked_at__gte=start, response_ms__isnull=False).values_list("checked_at", "response_ms"):
        acc[int((at - start).total_seconds() // 1800)].append(ms)
    labels = [timezone.localtime(start + timedelta(minutes=30 * i)).strftime("%H:%M") for i in range(48)]
    resp = {"labels": labels, "unit": "ms", "series": [{"name": "Average", "color": "#1565f0", "area": True, "width": 2,
            "values": [round(sum(acc[i]) / len(acc[i]), 1) if acc.get(i) else None for i in range(48)]}]}

    # uptime per day, 7d
    today = timezone.localdate()
    per_day = defaultdict(lambda: [0, 0])
    for at, up in DomainCheck.objects.filter(domain_id__in=ids, checked_at__date__gte=today - timedelta(days=6)).values_list("checked_at", "up"):
        p = per_day[timezone.localtime(at).date()]
        p[0] += 1
        p[1] += up
    days = [today - timedelta(days=6 - i) for i in range(7)]
    up_series = [round(100 * per_day[d][1] / per_day[d][0], 2) if per_day[d][0] else None for d in days]
    known = [v for v in up_series if v is not None]
    uptime_chart = {"labels": [d.strftime("%b %d") for d in days], "unit": "%", "yMin": min([95.0] + known) // 1, "yMax": 100,
                    "series": [{"name": "Uptime", "color": "#16a34a", "area": True, "areaOpacity": 0.25, "values": up_series, "width": 2}]}

    qs = scope
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(hostname__icontains=q)
    for param, field in (("status", "status"), ("ssl", "ssl_status"), ("group", "group")):
        if request.GET.get(param):
            qs = qs.filter(**{field: request.GET[param]})
    page = Paginator(qs, 10).get_page(request.GET.get("page"))
    up7 = _uptime([d.pk for d in page], 24 * 7)
    for d in page:
        d.uptime7 = up7.get(d.pk)
    resp_vals = [d.response_ms for d in domains if d.response_ms is not None]
    overall = _uptime(ids, 24 * 7)
    pct = lambda n: 100.0 * n / total if total else 0  # noqa: E731
    return render(request, "domainmonitor/overview.html", {
        "tab": "overview", "page": page, "query": _query(request), "total": total,
        "n": {"healthy": len(healthy), "down": len(down), "expiring": len(expiring), "paused": len(paused)},
        "p": {"healthy": pct(len(healthy)), "down": pct(len(down)), "expiring": pct(len(expiring)), "paused": pct(len(paused))},
        "avg_ms": sum(resp_vals) / len(resp_vals) if resp_vals else None,
        "uptime": sum(overall.values()) / len(overall) if overall else None,
        "donut": {"segments": [{"label": "Healthy", "value": len(healthy), "color": "#16a34a"},
                               {"label": "Down", "value": len(down), "color": "#ef4444"},
                               {"label": "SSL Expiring", "value": len(expiring), "color": "#f59e0b"},
                               {"label": "Paused", "value": len(paused), "color": "#94a3b8"}],
                  "center": {"value": str(total), "label": "Domains"}},
        "resp": resp, "uptime_chart": uptime_chart,
        "groups": scope.exclude(group="").order_by().values_list("group", flat=True).distinct(),
        "ssl_states": ["Valid", "Expiring", "Expired", "Invalid", "Unreachable", "Unknown"],
    })


# ------------------------------------------------------------------ list (configuration view)
def domain_list(request):
    scope = _scope()
    qs = scope
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(hostname__icontains=q) | Q(description__icontains=q))
    if request.GET.get("site"):
        qs = qs.filter(site_key=request.GET["site"])
    if request.GET.get("type"):
        qs = qs.filter(domain_type=request.GET["type"])
    if request.GET.get("active") in ("1", "0"):
        qs = qs.filter(is_active=request.GET["active"] == "1")
    page = Paginator(qs, 12).get_page(request.GET.get("page"))

    selected = None
    if request.GET.get("selected"):
        selected = scope.filter(pk=request.GET["selected"]).first()
    selected = selected or (page.object_list[0] if page.object_list else None)

    editing = None
    if request.GET.get("edit"):
        editing = get_object_or_404(scope, pk=request.GET["edit"])
    if request.method == "POST":
        if not request.user.is_staff:
            messages.error(request, "Only operators can add or change domains.")
            return redirect("domains:list")
        editing = scope.filter(pk=request.POST.get("pk")).first() if request.POST.get("pk") else None
        form = DomainForm(request.POST, instance=editing)
        if form.is_valid():
            obj = form.save(commit=False)
            obj.tenant_key = settings.LINKEDEYE_TENANT
            try:
                obj.save()
            except IntegrityError:
                form.add_error("hostname", "This domain already exists.")
            else:
                if obj.monitor_enabled and obj.is_active:
                    runner.run_domain_checks([obj])
                messages.success(request, f"Saved {obj.hostname}.")
                return redirect(f"{request.path}?selected={obj.pk}")
    else:
        form = DomainForm(instance=editing)

    all_active = scope.filter(is_active=True)
    return render(request, "domainmonitor/list.html", {
        "tab": "list", "page": page, "query": _query(request), "selected": selected, "form": form, "editing": editing,
        "show_form": bool(editing) or request.GET.get("add") == "1" or request.method == "POST",
        "recent": selected.checks.all()[:6] if selected else [],
        "k": {"total": scope.count(), "active": all_active.count(), "inactive": scope.filter(is_active=False).count(),
              "expiring": all_active.filter(ssl_status__in=["Expiring", "Expired"]).count(),
              "dns": all_active.filter(dns_status="Failed").count(),
              "sites": all_active.order_by().values("site_key").distinct().count()},
        "sites": scope.order_by().values_list("site_key", flat=True).distinct(),
        "types": scope.order_by().values_list("domain_type", flat=True).distinct(),
    })


def ssl_certificates(request):
    rows = sorted(_scope().filter(is_active=True), key=lambda d: (d.ssl_expires_at is None, d.ssl_expires_at or timezone.now()))
    return render(request, "domainmonitor/ssl.html", {"tab": "ssl", "rows": rows})


@staff_member_required
@require_POST
def deactivate(request, pk):
    d = get_object_or_404(_scope(), pk=pk)
    d.is_active = not d.is_active
    d.save(update_fields=["is_active", "updated_at"])
    messages.success(request, f"{d.hostname} {'reactivated' if d.is_active else 'deactivated'}.")
    return redirect(f"/domains/list/?selected={d.pk}")


# ------------------------------------------------------------------ quick actions (JSON)
@staff_member_required
@require_POST
def action(request, pk, kind):
    d = get_object_or_404(_scope(), pk=pk)
    if kind == "dns":
        r = checks.dns_check(d.hostname)
        summary = f"{d.hostname} resolves to {', '.join(r.ips)}" if r.ok else r.error
        return JsonResponse({"ok": r.ok, "ips": r.ips, "summary": summary})
    if kind == "ssl":
        r = checks.tls_check(d.hostname)
        if r.expires_at:
            summary = f"{r.status}: expires {timezone.localtime(r.expires_at):%d %b %Y} ({r.days_left} days)\nIssuer: {r.issuer}\nSubject: {r.subject}"
        else:
            summary = f"{r.status}: {r.error}"
        return JsonResponse({"status": r.status, "days_left": r.days_left, "summary": summary})
    if kind == "uptime":
        runner.run_domain_checks([d])
        d.refresh_from_db()
        summary = (f"{d.status.upper()} · HTTP {d.http_status or '–'} · {d.response_ms or 0:.0f} ms · DNS {d.dns_status} · SSL {d.ssl_status}"
                   + (f"\n{d.last_error}" if d.last_error else ""))
        return JsonResponse({"status": d.status, "summary": summary})
    return JsonResponse({"error": "unknown action"}, status=404)


@staff_member_required
@require_POST
def run_all(request):
    items = list(_scope().filter(is_active=True, monitor_enabled=True))
    runner.run_domain_checks(items)
    return JsonResponse({"checked": len(items), "summary": f"Checked {len(items)} domain(s)"})


# ------------------------------------------------------------------ JSON API (contract from the integration patch)
# SECURITY: staff-only until existing tenant RBAC and site membership are mapped.

@staff_member_required
def page(request):
    return redirect("domains:list")


@staff_member_required
@require_http_methods(["GET", "POST"])
def api_domains(request):
    if request.method == "GET":
        tenant = request.GET.get("tenant", "").strip()
        if not tenant:
            return JsonResponse({"error": "tenant query parameter required"}, status=400)
        rows = Domain.objects.filter(tenant_key=tenant)
        return JsonResponse({"results": [{
            "id": x.id, "hostname": x.hostname, "site": x.site_key,
            "type": x.domain_type, "active": x.is_active,
            "dns": x.dns_status, "ssl": x.ssl_status,
            "description": x.description,
            "status": x.status, "ip": x.ip_address, "response_ms": x.response_ms,
            "ssl_expires_at": x.ssl_expires_at.isoformat() if x.ssl_expires_at else None,
            "last_checked_at": x.last_checked_at.isoformat() if x.last_checked_at else None,
        } for x in rows[:500]]})
    try:
        data = json.loads(request.body)
        tenant = str(data["tenant"]).strip()
        site = str(data["site"]).strip()
        hostname = str(data["hostname"]).strip().lower().rstrip(".")
        if not tenant or not site:
            return JsonResponse({"error": "tenant and site are required"}, status=400)
        obj = Domain(tenant_key=tenant, site_key=site, hostname=hostname,
                     domain_type=str(data.get("type", "Production"))[:32],
                     description=str(data.get("description", ""))[:5000])
        obj.full_clean()
        obj.save()
        return JsonResponse({"id": obj.pk, "status": "created"}, status=201)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError, ValidationError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)


@staff_member_required
@require_http_methods(["PATCH", "DELETE"])
def api_detail(request, pk):
    tenant = request.GET.get("tenant", "").strip()
    if not tenant:
        return JsonResponse({"error": "tenant required"}, status=400)
    try:
        obj = Domain.objects.get(pk=pk, tenant_key=tenant)
    except Domain.DoesNotExist:
        return JsonResponse({"error": "Not found"}, status=404)
    if request.method == "DELETE":
        obj.is_active = False  # non-destructive deactivation
        obj.save(update_fields=["is_active", "updated_at"])
        return JsonResponse({"status": "deactivated"})
    try:
        data = json.loads(request.body)
        allowed = {"site": "site_key", "type": "domain_type", "description": "description", "active": "is_active"}
        for key, field in allowed.items():
            if key in data:
                setattr(obj, field, data[key])
        obj.full_clean()
        obj.save()
        return JsonResponse({"status": "updated"})
    except (ValueError, TypeError, ValidationError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
