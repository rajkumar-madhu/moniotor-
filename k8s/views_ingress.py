"""Edge exposure pages: ingresses, certificates and Secret-name references.

/kubernetes/ingresses/      ingress rules grouped by host name, with their paths; filters; CSV
/kubernetes/certificates/   certificates sorted by days left, with an expiry tone; CSV
/kubernetes/secrets/        Secret NAMES referenced by workloads, by namespace and application; CSV

Every queryset is tenant- and site-scoped; the one mutation (creating / linking URL checks and domains
from the ingress list) is POST + CSRF + the operate role on each record's site + audit. Values never
appear here: the Secret page lists names only, and says so.
"""
from collections import OrderedDict
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count, F, Q
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.access import require, scope_by_site
from core.audit import audit
from telemetry.views.common import csv_response

from . import services
from . import views as base
from .models import Exposure
from .models_ingress import (CRITICAL_DAYS, WARNING_DAYS, CertStatus, Certificate, Ingress, SecretRef,
                             status_filter, under_days_filter)
from .sheets.ingress import link_ingresses

CSV_LIMIT = 10000


# ----------------------------------------------------------------------------- querysets
def _tenant():
    return settings.LINKEDEYE_TENANT


def ingresses_for(user):
    qs = scope_by_site(Ingress.objects.filter(tenant_key=_tenant()), user)
    return qs.select_related("namespace", "namespace__client", "certificate", "url_check", "domain")


def certificates_for(user):
    qs = scope_by_site(Certificate.objects.filter(tenant_key=_tenant()), user)
    return qs.select_related("namespace", "namespace__client")


def secrets_for(user):
    qs = scope_by_site(SecretRef.objects.filter(tenant_key=_tenant()), user)
    return qs.select_related("namespace", "namespace__client")


def client_labels(qs):
    """Distinct client names (as the workbook recorded them) behind a queryset, for the filter menus."""
    return sorted({c for c in qs.order_by().values_list("namespace__client_label", flat=True) if c}, key=str.lower)


def _site_param(user, params):
    """The ?site= filter, ignored when it names a site the user cannot see."""
    site = (params.get("site") or "").strip()
    if not site:
        return ""
    names = services.visible_site_names(user)
    return site if names is None or site in names else ""


def _common(qs, params, user):
    """Filters every page shares: site, client and namespace."""
    site = _site_param(user, params)
    if site:
        qs = qs.filter(site=site)
    client = (params.get("client") or "").strip()
    if client:
        qs = qs.filter(namespace__client_label__iexact=client)
    namespace = (params.get("namespace") or "").strip()
    if namespace:
        qs = qs.filter(namespace__name=namespace)
    return qs


def _yn(value):
    return "" if value is None else ("yes" if value else "no")


def _dt(value):
    return value.isoformat() if value else ""


# ----------------------------------------------------------------------------- permissions
def _may_link(user, site):
    """Creating or linking checks / domains on a record's site needs the operator role there (an
    "All Sites" record needs it on every site)."""
    return services.can_manage(user, "operate", site)


# ----------------------------------------------------------------------------- ingresses
INGRESS_FILTERS = ("q", "client", "class", "ip_type", "tls", "environment", "state", "namespace", "site")


def filter_ingresses(user, params):
    """The ingress rules a set of query parameters selects (a GET query string or the link form's POST)."""
    qs = _common(ingresses_for(user), params, user)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(hostname__icontains=q) | Q(name__icontains=q) | Q(application__icontains=q)
                       | Q(backend_service__icontains=q) | Q(path__icontains=q) | Q(tls_secret__icontains=q)
                       | Q(namespace__name__icontains=q))
    ingress_class = (params.get("class") or "").strip()
    if ingress_class:
        qs = qs.filter(ingress_class=ingress_class)
    ip_type = params.get("ip_type") or ""
    if ip_type in Exposure.values:
        qs = qs.filter(ip_type=ip_type)
    tls = params.get("tls") or ""
    if tls == "yes":
        qs = qs.filter(tls=True)
    elif tls == "no":
        qs = qs.filter(tls=False)
    environment = (params.get("environment") or "").strip()
    if environment:
        qs = qs.filter(environment__iexact=environment)
    state = params.get("state") or ""
    if state == "missing_address":
        qs = qs.filter(status__icontains="missing")
    elif state == "no_certificate":
        qs = qs.filter(tls=True, certificate__isnull=True)
    elif state == "no_check":
        qs = qs.filter(url_check__isnull=True)
    elif state == "gone":
        qs = qs.filter(missing_since__isnull=False)
    return qs


def _ingress_queryset(request):
    return filter_ingresses(request.user, request.GET)


def ingress_totals(qs):
    agg = qs.aggregate(rules=Count("pk"), tls=Count("pk", filter=Q(tls=True)),
                       public=Count("pk", filter=Q(ip_type=Exposure.PUBLIC)),
                       missing_address=Count("pk", filter=Q(status__icontains="missing")),
                       checked=Count("pk", filter=Q(url_check__isnull=False)),
                       gone=Count("pk", filter=Q(missing_since__isnull=False)))
    agg["hosts"] = qs.exclude(hostname="").order_by().values("hostname").distinct().count()
    agg["tls_pct"] = round(100.0 * agg["tls"] / agg["rules"]) if agg["rules"] else None
    return agg


def _group(rows):
    """Rows (already ordered by host) -> one dict per host name with its paths."""
    groups = OrderedDict()
    for row in rows:
        g = groups.setdefault(row.hostname, {
            "hostname": row.hostname, "rows": [], "namespaces": [], "classes": [], "ips": [],
            "tls": False, "ip_type": row.ip_type, "certificate": None, "url_check": None, "domain": None,
            "url": "", "client": "", "missing_address": False, "gone": True, "all_tls": True})
        g["rows"].append(row)
        if row.namespace.name not in g["namespaces"]:
            g["namespaces"].append(row.namespace.name)
        if row.ingress_class and row.ingress_class not in g["classes"]:
            g["classes"].append(row.ingress_class)
        if row.ingress_ip and str(row.ingress_ip) not in g["ips"]:
            g["ips"].append(str(row.ingress_ip))
        g["tls"] = g["tls"] or row.tls
        g["all_tls"] = g["all_tls"] and row.tls
        g["certificate"] = g["certificate"] or row.certificate
        g["url_check"] = g["url_check"] or row.url_check
        g["domain"] = g["domain"] or row.domain
        g["url"] = g["url"] or row.url or (row.check_url if row.hostname else "")
        g["client"] = g["client"] or row.namespace.client_display
        g["missing_address"] = g["missing_address"] or row.missing_address
        g["gone"] = g["gone"] and row.is_missing
        if g["ip_type"] == Exposure.UNKNOWN:
            g["ip_type"] = row.ip_type
    return list(groups.values())


@require("view")
def ingresses(request):
    user = request.user
    qs = _ingress_queryset(request)
    everything = ingresses_for(user)
    hosts = list(qs.order_by("hostname").values_list("hostname", flat=True).distinct())
    page = base.paginate(request, hosts)
    rows = qs.filter(hostname__in=list(page.object_list)).order_by("hostname", "path", "name", "backend_service")
    groups = _group(rows)
    ctx = base._base(request, "ingresses",
                     page=page, groups=groups, total=qs.count(), totals=ingress_totals(qs),
                     classes=sorted({c for c in everything.order_by().values_list("ingress_class", flat=True) if c},
                                    key=str.lower),
                     ip_types=Exposure.choices,
                     clients=client_labels(everything),
                     environments=sorted({e for e in everything.order_by().values_list("environment", flat=True) if e},
                                         key=str.lower),
                     can_link=base.can(user, "operate"),
                     filtered=any(request.GET.get(k) for k in INGRESS_FILTERS if k != "site"),
                     filter_items=[(k, request.GET[k]) for k in INGRESS_FILTERS if request.GET.get(k)])
    return render(request, "k8s/ingresses.html", ctx)


INGRESS_CSV = ["Client", "Environment", "Cluster", "Namespace", "Application", "Ingress", "Class", "Host", "URL",
               "Ingress IP", "IP type", "Path", "Backend service", "Backend port", "TLS", "TLS secret",
               "Certificate", "Certificate expiry", "Status", "URL check", "Domain", "Site", "Last seen",
               "Missing since"]


def _ingress_row(i):
    cert = i.certificate
    return [i.namespace.client_display, i.environment, i.namespace.cluster.name, i.namespace.name, i.application,
            i.name, i.ingress_class, i.hostname, i.url, i.ingress_ip or "", i.get_ip_type_display(), i.path,
            i.backend_service, i.backend_port, "yes" if i.tls else "no", i.tls_secret,
            cert.name if cert else i.certificate_name, _dt(cert.expiry) if cert else "", i.status,
            i.url_check.name if i.url_check_id else "", i.domain.hostname if i.domain_id else "", i.site,
            _dt(i.last_seen), _dt(i.missing_since)]


@require("view")
def ingresses_export(request):
    qs = _ingress_queryset(request).select_related("namespace__cluster").order_by("hostname", "path", "name")
    return csv_response("kubernetes-ingresses.csv", INGRESS_CSV, (_ingress_row(i) for i in qs[:CSV_LIMIT]))


@require_POST
@require("operate")
def ingresses_link(request):
    """Create or link URL checks and/or domains for the ingresses in view. Opt-in per box; disabled / unmonitored."""
    want_urls = request.POST.get("urlchecks") == "1"
    want_domains = request.POST.get("domains") == "1"
    back = reverse("k8s:ingresses")
    if not (want_urls or want_domains):
        messages.error(request, "Choose what to link: URL checks, domains, or both.")
        return redirect(back)
    qs = filter_ingresses(request.user, request.POST)
    user = request.user
    result = link_ingresses(_tenant(), qs, urlchecks=want_urls, domains=want_domains,
                            allowed=lambda site: _may_link(user, site))
    audit(request, "link", None, object_type="k8s.ingress", **result,
          urlchecks=want_urls, domains=want_domains)
    parts = []
    if want_urls:
        parts.append(f"{result['urlchecks_created']} URL check{'s' if result['urlchecks_created'] != 1 else ''} "
                     f"created (disabled), {result['urlchecks_linked']} ingress link{'s' if result['urlchecks_linked'] != 1 else ''} set")
    if want_domains:
        parts.append(f"{result['domains_created']} domain{'s' if result['domains_created'] != 1 else ''} created "
                     f"(monitoring off), {result['domains_linked']} ingress link{'s' if result['domains_linked'] != 1 else ''} set")
    text = "; ".join(parts) + "."
    if result["forbidden"]:
        messages.warning(request, f"{text} {result['forbidden']} ingress rule(s) on sites where you lack the "
                                  f"operator role were left alone.")
    else:
        messages.success(request, text)
    kept = urlencode({k: request.POST[k] for k in INGRESS_FILTERS if request.POST.get(k)})
    return redirect(back + (f"?{kept}" if kept else ""))


# ----------------------------------------------------------------------------- certificates
CERT_SORTS = {"expiry": [F("expiry").asc(nulls_last=True), "name"],
              "-expiry": [F("expiry").desc(nulls_last=True), "name"],
              "name": ["name"], "domain": ["domain", "name"], "issuer": ["issuer", "name"]}


def filter_certificates(user, params, now=None):
    now = now or timezone.now()
    qs = _common(certificates_for(user), params, user)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(domain__icontains=q) | Q(tls_secret__icontains=q)
                       | Q(issuer__icontains=q) | Q(namespace__name__icontains=q))
    status = params.get("status") or ""
    if status in CertStatus.values:
        qs = qs.filter(status_filter(status, now))
    elif status == "under30":
        qs = qs.filter(under_days_filter(WARNING_DAYS, now))
    issuer = (params.get("issuer") or "").strip()
    if issuer:
        qs = qs.filter(issuer=issuer)
    ready = params.get("ready") or ""
    if ready == "yes":
        qs = qs.filter(ready=True)
    elif ready == "no":
        qs = qs.filter(ready=False)
    order = CERT_SORTS.get(params.get("sort") or "expiry", CERT_SORTS["expiry"])
    return qs.order_by(*order)


def _certificate_queryset(request):
    return filter_certificates(request.user, request.GET)


def certificate_totals(qs, now=None):
    now = now or timezone.now()
    agg = qs.aggregate(total=Count("pk"),
                       expired=Count("pk", filter=status_filter(CertStatus.EXPIRED, now)),
                       under30=Count("pk", filter=under_days_filter(WARNING_DAYS, now)),
                       critical=Count("pk", filter=status_filter(CertStatus.CRITICAL, now)),
                       not_ready=Count("pk", filter=Q(ready=False)),
                       unknown=Count("pk", filter=status_filter(CertStatus.UNKNOWN, now)))
    agg["next"] = qs.filter(expiry__gt=now).order_by("expiry").first()
    return agg


@require("view")
def certificates(request):
    user = request.user
    qs = _certificate_queryset(request)
    everything = certificates_for(user)
    page = base.paginate(request, qs.annotate(ingress_count=Count("ingresses")))
    now = timezone.now()
    from domainmonitor.models import Domain
    domains = {d.hostname.lower(): d for d in Domain.objects.filter(
        tenant_key=_tenant(), hostname__in=[c.domain for c in page.object_list if c.domain])}
    rows = []
    for cert in page.object_list:
        status = cert.status(now)
        days = cert.days_remaining(now)
        rows.append({"cert": cert, "status": status, "status_label": CertStatus(status).label, "days": days,
                     "tone": cert.tone(now), "pct": cert.lifetime_left_pct(now),
                     "domain_obj": domains.get((cert.domain or "").lower())})
    ctx = base._base(request, "certificates",
                     page=page, rows=rows, total=qs.count(), totals=certificate_totals(qs, now),
                     statuses=[(s.value, s.label) for s in CertStatus],
                     issuers=sorted({i for i in everything.order_by().values_list("issuer", flat=True) if i},
                                    key=str.lower),
                     clients=client_labels(everything),
                     warning_days=WARNING_DAYS, critical_days=CRITICAL_DAYS,
                     filtered=any(request.GET.get(k) for k in ("q", "client", "status", "issuer", "ready",
                                                              "namespace")))
    return render(request, "k8s/certificates.html", ctx)


CERT_CSV = ["Client", "Namespace", "Domain", "Certificate", "TLS secret", "Issuer", "Ready", "Valid from",
            "Expiry", "Days remaining", "Status", "Workbook risk (at discovery)", "Ingresses", "Site", "Remarks",
            "Last seen", "Missing since"]


@require("view")
def certificates_export(request):
    now = timezone.now()
    qs = _certificate_queryset(request).annotate(ingress_count=Count("ingresses"))

    def row(c):
        days = c.days_remaining(now)
        return [c.namespace.client_display, c.namespace.name, c.domain, c.name, c.tls_secret, c.issuer,
                _yn(c.ready), _dt(c.valid_from), _dt(c.expiry), "" if days is None else days,
                CertStatus(c.status(now)).label, c.risk, c.ingress_count, c.site, c.remarks, _dt(c.last_seen),
                _dt(c.missing_since)]
    return csv_response("kubernetes-certificates.csv", CERT_CSV, (row(c) for c in qs[:CSV_LIMIT]))


# ----------------------------------------------------------------------------- secret references
def filter_secrets(user, params):
    qs = _common(secrets_for(user), params, user)
    q = (params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(secret_name__icontains=q) | Q(application__icontains=q) | Q(referenced_by__icontains=q)
                       | Q(namespace__name__icontains=q) | Q(remarks__icontains=q))
    application = (params.get("application") or "").strip()
    if application:
        qs = qs.filter(application=application)
    environment = (params.get("environment") or "").strip()
    if environment:
        qs = qs.filter(environment__iexact=environment)
    return qs.order_by("namespace__name", "application", "secret_name", "referenced_by")


def _secret_queryset(request):
    return filter_secrets(request.user, request.GET)


@require("view")
def secrets(request):
    user = request.user
    qs = _secret_queryset(request)
    everything = secrets_for(user)
    page = base.paginate(request, qs)
    totals = qs.aggregate(references=Count("pk"), secrets=Count("secret_name", distinct=True),
                          namespaces=Count("namespace", distinct=True),
                          applications=Count("application", distinct=True, filter=~Q(application="")))
    by_namespace = list(qs.order_by().values("namespace__name").annotate(
        refs=Count("pk"), names=Count("secret_name", distinct=True)).order_by("-refs", "namespace__name")[:12])
    ctx = base._base(request, "secrets",
                     page=page, items=page.object_list, total=qs.count(), totals=totals,
                     by_namespace=by_namespace,
                     namespaces=sorted(set(everything.order_by().values_list("namespace__name", flat=True)),
                                       key=str.lower),
                     clients=client_labels(everything),
                     environments=sorted({e for e in everything.order_by().values_list("environment", flat=True) if e},
                                         key=str.lower),
                     filtered=any(request.GET.get(k) for k in ("q", "client", "namespace", "application",
                                                              "environment")))
    return render(request, "k8s/secrets.html", ctx)


SECRET_CSV = ["Client", "Environment", "Namespace", "Application", "Secret name", "Secret namespace",
              "Referenced by", "Remarks", "Site", "Last seen", "Missing since"]


@require("view")
def secrets_export(request):
    qs = _secret_queryset(request)
    return csv_response("kubernetes-secret-references.csv", SECRET_CSV, (
        [s.namespace.client_display, s.environment, s.namespace.name, s.application, s.secret_name,
         s.secret_namespace, s.referenced_by, s.remarks, s.site, _dt(s.last_seen), _dt(s.missing_since)]
        for s in qs[:CSV_LIMIT]))


# ----------------------------------------------------------------------------- overview contribution
def edge_summary(user, site="", now=None):
    """Numbers for the overview: ingress rules, TLS coverage, certificates expiring within 30 days."""
    now = now or timezone.now()
    ing = ingresses_for(user)
    certs = certificates_for(user)
    if site:
        ing, certs = ing.filter(site=site), certs.filter(site=site)
    totals = ingress_totals(ing)
    ctotals = certificate_totals(certs, now)
    return {"ingress": totals, "certs": ctotals, "has_data": bool(totals["rules"] or ctotals["total"])}


# ----------------------------------------------------------------------------- urls
urlpatterns = [
    path("kubernetes/ingresses/", ingresses, name="ingresses"),
    path("kubernetes/ingresses/export.csv", ingresses_export, name="ingresses_export"),
    path("kubernetes/ingresses/link/", ingresses_link, name="ingresses_link"),
    path("kubernetes/certificates/", certificates, name="certificates"),
    path("kubernetes/certificates/export.csv", certificates_export, name="certificates_export"),
    path("kubernetes/secrets/", secrets, name="secrets"),
    path("kubernetes/secrets/export.csv", secrets_export, name="secrets_export"),
]
