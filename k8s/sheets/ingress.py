"""Importers for the edge sheets: ingress URLs, certificates and Secret-name references.

===========================  =====  ====================================================================
Sheet                        Order  Goes to
===========================  =====  ====================================================================
``06_Ingress_URLs``             60  ``Ingress`` (+ optional URL check / domain links)
``09_Certificate_Inventory``    70  ``Certificate`` (+ links ingresses that use it)
``08_Credentials_References``   80  ``SecretRef`` — Secret NAMES only
===========================  =====  ====================================================================

Data rules
----------
* The workbook describes a real customer estate; nothing from it belongs in this repository.
* ``08_Credentials_References`` lists Secret **names**. This importer reads a name, a namespace, the
  application and the referencing workload — it never reads a value, and ignores every other column
  (user name, vault path, password location…). A column whose header suggests it holds a value is
  reported and skipped, and a "name" that does not look like a Kubernetes object name is rejected.
* Placeholder cells ("NOT DISCOVERED" …) become empty strings (``parsing.clean``).
* ``Days Remaining`` of the certificate sheet is a snapshot of the discovery day and is not stored; the
  pages compute it from the expiry.

Cross-links (opt-in)
--------------------
Each distinct https ingress URL can create or link a ``urlchecker.UrlCheck`` (same site, **disabled** so
nothing is probed until an operator enables it) and each host's registrable domain can link to a
``domainmonitor.Domain`` (created with monitoring off). Existing records are linked, never duplicated.
The flags are ``link_urlchecks`` / ``link_domains``; set them for one import with::

    with crosslink_options(urlchecks=True, domains=True):
        run_import(path)

or later, from the Ingresses page. Without the flags an import creates no URL check and no domain.
"""
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone as dt_timezone

from django.db import IntegrityError, transaction

from ..importer import register_sheet
from ..models import Exposure
from ..models_ingress import Certificate, Ingress, SecretRef
from ..parsing import clean, parse_bool, parse_ip, parse_url

INGRESS_SHEET = "06_Ingress_URLs"
CERT_SHEET = "09_Certificate_Inventory"
SECRET_SHEET = "08_Credentials_References"


def _get(row, *names):
    for name in names:
        value = row.get(name)
        if value:
            return value
    return ""


# ----------------------------------------------------------------------------- options
_OPTIONS = ContextVar("k8s_ingress_crosslink", default=None)


@contextmanager
def crosslink_options(urlchecks=False, domains=False):
    """Switch the cross-links on for the imports run inside the ``with`` block (thread/context local)."""
    token = _OPTIONS.set({"urlchecks": bool(urlchecks), "domains": bool(domains)})
    try:
        yield
    finally:
        _OPTIONS.reset(token)


def _option(ctx, name):
    """True when the import was asked to cross-link: an attribute on the context wins, then the block."""
    explicit = getattr(ctx, f"link_{name}", None)
    if explicit is not None:
        return bool(explicit)
    return bool((_OPTIONS.get() or {}).get(name))


# ----------------------------------------------------------------------------- helpers
def _ip_type(value):
    lowered = clean(value).lower()
    if lowered.startswith("pub"):
        return Exposure.PUBLIC
    if lowered.startswith("priv"):
        return Exposure.PRIVATE
    return Exposure.UNKNOWN


def parse_timestamp(value):
    """An ISO-8601 (or "YYYY-MM-DD hh:mm:ss") cell as an aware UTC datetime; None when unreadable."""
    text = clean(value)
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%d-%m-%Y %H:%M:%S", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                stamp = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=dt_timezone.utc)


# Headers that would carry a secret VALUE if a workbook ever had them: never read, reported if present.
_VALUE_HEADER = re.compile(
    r"^(value|data|token|password|passwd|pwd|api[ _-]?key|private[ _-]?key|secret|secret[ _-]?(value|data|text)|"
    r"credential[ _-]?value|base64|plain[ _-]?text|decoded)$", re.IGNORECASE)
# What a Kubernetes object name looks like (DNS-1123 subdomain, with a little tolerance for case and ':').
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,252}$")


def looks_like_name(text):
    return bool(_NAME_OK.match(text or ""))


# ----------------------------------------------------------------------------- certificates <-> ingresses
def relink_certificates(tenant):
    """Point each ingress at the certificate that serves it (same namespace; matched by certificate name,
    else by TLS secret). Returns how many links changed. Never touches an ingress with no candidate."""
    certs = list(Certificate.objects.filter(tenant_key=tenant, missing_since__isnull=True))
    by_name, by_secret = {}, {}
    for cert in certs:
        by_name.setdefault((cert.namespace_id, cert.name), cert)
        if cert.tls_secret:
            by_secret.setdefault((cert.namespace_id, cert.tls_secret), cert)
    if not certs:
        return 0
    changed = []
    ingresses = Ingress.objects.filter(tenant_key=tenant, namespace_id__in={c.namespace_id for c in certs})
    for ingress in ingresses:
        match = None
        if ingress.certificate_name:
            match = by_name.get((ingress.namespace_id, ingress.certificate_name))
        if match is None and ingress.tls_secret:
            match = by_secret.get((ingress.namespace_id, ingress.tls_secret))
        if match is not None and ingress.certificate_id != match.pk:
            ingress.certificate = match
            changed.append(ingress)
    if changed:
        Ingress.objects.bulk_update(changed, ["certificate"])
    return len(changed)


# ----------------------------------------------------------------------------- registrable domain
# Second-level labels that sit under a country code ("example.co.uk"). Not the full public-suffix list:
# a host whose suffix is not recognised is treated as <label>.<tld>, which is right for the usual case.
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "edu", "ac", "or", "ne", "nic", "res", "mil", "sch", "ltd", "plc"}


def registrable_domain(hostname):
    """'api.team.example.com' -> 'example.com'; 'x.example.co.uk' -> 'example.co.uk'; '' for an IP address,
    a single label or a wildcard-only name."""
    host = (hostname or "").strip().lower().rstrip(".")
    if host.startswith("*."):
        host = host[2:]
    if not host or "*" in host or re.fullmatch(r"[\d.]+", host) or ":" in host:
        return ""
    labels = host.split(".")
    if len(labels) < 2 or not all(labels):
        return ""
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


# ----------------------------------------------------------------------------- cross-links
def _unique_name(base, taken):
    base = base[:120]
    if base not in taken:
        return base
    for n in range(2, 1000):
        suffix = f" (kubernetes {n})" if n > 2 else " (kubernetes)"
        candidate = base[:120 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    return base[:100] + f" ({len(taken)})"


def link_ingresses(tenant, ingresses, urlchecks=False, domains=False, allowed=None):
    """Create or link URL checks and domains for `ingresses`. Idempotent; returns a counter dict.

    * ``urlchecks`` – one ``UrlCheck`` per distinct https URL: an existing check for the same URL (with or
      without a trailing slash) is linked; otherwise a new one is created **disabled**, on the site of the
      ingress, so nothing is probed until an operator enables it.
    * ``domains`` – one ``Domain`` per host: an existing record for the host, else for its registrable
      domain, is linked; otherwise the registrable domain is created with monitoring off.
    * ``allowed`` – optional ``f(site) -> bool``. Ingresses on a site it rejects are left alone and
      counted as ``forbidden`` (the Ingresses page passes the requesting user's permission here).
    """
    from domainmonitor.models import Domain, hostname_validator
    from django.core.exceptions import ValidationError
    from urlchecker.models import UrlCheck

    out = {"urlchecks_created": 0, "urlchecks_linked": 0, "domains_created": 0, "domains_linked": 0,
           "forbidden": 0}
    rows = list(ingresses)
    if allowed is not None:
        permitted = []
        for row in rows:
            if allowed(row.site):
                permitted.append(row)
            else:
                out["forbidden"] += 1
        rows = permitted
    changed = {}

    if urlchecks:
        wanted = {}                                  # normalised url -> (url, site)
        for row in rows:
            url = row.check_url
            if url.lower().startswith("https://") and "*" not in url:
                wanted.setdefault(url.rstrip("/"), (url.rstrip("/"), row.site))
        if wanted:
            existing = {}
            candidates = [u for u, _site in wanted.values()] + [u + "/" for u, _site in wanted.values()]
            for check in UrlCheck.objects.filter(tenant_key=tenant, url__in=candidates).order_by("pk"):
                existing.setdefault(check.url.rstrip("/"), check)
            taken = set(UrlCheck.objects.filter(tenant_key=tenant).values_list("name", flat=True))
            for key, (url, site) in sorted(wanted.items()):
                if key in existing:
                    continue
                host = re.sub(r"^https?://", "", url)
                check = UrlCheck.objects.create(
                    tenant_key=tenant, name=_unique_name(host, taken), url=url, site=site or "All Sites",
                    enabled=False, tags="kubernetes")
                taken.add(check.name)
                existing[key] = check
                out["urlchecks_created"] += 1
            for row in rows:
                url = row.check_url
                check = existing.get(url.rstrip("/")) if url.lower().startswith("https://") else None
                if check is not None and row.url_check_id != check.pk:
                    row.url_check = check
                    changed[row.pk] = row
                    out["urlchecks_linked"] += 1

    if domains:
        hosts = {}
        for row in rows:
            host = (row.hostname or "").lower()
            apex = registrable_domain(host)
            if apex:
                hosts.setdefault(host, (apex, row.site))
        wanted_names = {h.lstrip("*.") for h in hosts} | {apex for apex, _s in hosts.values()}
        existing = {d.hostname.lower(): d for d in Domain.objects.filter(tenant_key=tenant,
                                                                         hostname__in=sorted(wanted_names))}
        for host, (apex, site) in sorted(hosts.items()):
            if host.lstrip("*.") in existing or apex in existing:
                continue
            try:
                hostname_validator(apex)
            except ValidationError:
                continue
            try:
                with transaction.atomic():
                    domain = Domain.objects.create(
                        tenant_key=tenant, site_key=site or "All Sites", hostname=apex,
                        domain_type="Application", is_active=True, monitor_enabled=False,
                        description="Created from the Kubernetes ingress inventory; monitoring is off.")
            except IntegrityError:
                domain = Domain.objects.filter(tenant_key=tenant, hostname=apex).first()
                if domain is None:
                    continue
            else:
                out["domains_created"] += 1
            existing[apex] = domain
        for row in rows:
            host = (row.hostname or "").lower()
            apex = registrable_domain(host)
            domain = existing.get(host.lstrip("*.")) or existing.get(apex) if apex else None
            if domain is not None and row.domain_id != domain.pk:
                row.domain = domain
                changed[row.pk] = row
                out["domains_linked"] += 1

    if changed:
        Ingress.objects.bulk_update(list(changed.values()), ["url_check", "domain"])
    return out


def _drop_stale_links(tenant, seen_ids):
    """An ingress whose URL / host changed must not keep pointing at the old check or domain."""
    stale = []
    for row in Ingress.objects.filter(tenant_key=tenant, pk__in=seen_ids).exclude(
            url_check__isnull=True, domain__isnull=True).select_related("url_check", "domain"):
        touched = False
        if row.url_check_id and row.url_check.url.rstrip("/") != row.check_url.rstrip("/"):
            row.url_check = None
            touched = True
        if row.domain_id:
            apex = registrable_domain(row.hostname)
            current = row.domain.hostname.lower()
            host = (row.hostname or "").lower().lstrip("*.")
            if current != host and current != apex:
                row.domain = None
                touched = True
        if touched:
            stale.append(row)
    if stale:
        Ingress.objects.bulk_update(stale, ["url_check", "domain"])


# ----------------------------------------------------------------------------- 06_Ingress_URLs
def import_ingresses(ctx, sheet):
    cluster = ctx.cluster_for()
    seen = set()
    for row in sheet.rows:
        name = clean(_get(row, "Ingress", "Ingress Name"))
        hostname = clean(_get(row, "Hostname", "Host")).lower()
        namespace_name = clean(_get(row, "Namespace"))
        if not namespace_name or not (name or hostname):
            ctx.warn(INGRESS_SHEET, "a row has no namespace, or neither an ingress name nor a host, and was skipped")
            ctx.count(INGRESS_SHEET, "skipped")
            continue
        namespace = ctx.resolve_namespace(namespace_name, cluster=cluster, client_label=clean(_get(row, "Client")),
                                          environment=clean(_get(row, "Environment")), sheet=INGRESS_SHEET)
        tls_secret = clean(_get(row, "TLS Secret"))
        tls = parse_bool(_get(row, "TLS"))
        if tls is None:
            tls = bool(tls_secret)               # a TLS secret on the rule means the rule terminates TLS
        values = {
            "site": namespace.site,
            "environment": clean(_get(row, "Environment"))[:40],
            "application": clean(_get(row, "Application"))[:253],
            "ingress_class": clean(_get(row, "Ingress Class", "Class"))[:253],
            "url": parse_url(_get(row, "URL")),
            "ingress_ip": parse_ip(_get(row, "Ingress IP", "IP")),
            "ip_type": _ip_type(_get(row, "IP Type")),
            "backend_port": clean(_get(row, "Backend Port"))[:40],
            "tls": tls,
            "tls_secret": tls_secret[:253],
            "certificate_name": clean(_get(row, "Certificate"))[:253],
            "status": clean(_get(row, "Status"))[:40],
        }
        keys = {"namespace": namespace, "name": name[:253], "hostname": hostname[:253],
                "path": clean(_get(row, "Path"))[:500], "backend_service": clean(_get(row, "Backend Service"))[:253]}
        ingress, _created = ctx.upsert(Ingress, keys, values, sheet=INGRESS_SHEET)
        seen.add(ingress.pk)
    ctx.track(Ingress, INGRESS_SHEET, namespace__cluster=cluster)

    _drop_stale_links(ctx.tenant, seen)
    relink_certificates(ctx.tenant)
    want_urls, want_domains = _option(ctx, "urlchecks"), _option(ctx, "domains")
    if (want_urls or want_domains) and seen:
        result = link_ingresses(ctx.tenant, Ingress.objects.filter(tenant_key=ctx.tenant, pk__in=seen)
                                .select_related("url_check", "domain"), urlchecks=want_urls, domains=want_domains)
        for key, amount in result.items():
            if amount and key != "forbidden":
                ctx.count(INGRESS_SHEET, key, amount)
        ctx.note("ingress_crosslink", {"urlchecks": want_urls, "domains": want_domains, **result})


register_sheet(INGRESS_SHEET, 60, import_ingresses, namespaces=True)


# ----------------------------------------------------------------------------- 09_Certificate_Inventory
def import_certificates(ctx, sheet):
    cluster = ctx.cluster_for()
    for row in sheet.rows:
        domain = clean(_get(row, "Domain")).lower()
        tls_secret = clean(_get(row, "TLS Secret"))
        name = clean(_get(row, "Certificate", "Certificate Name")) or tls_secret or domain
        namespace_name = clean(_get(row, "Namespace"))
        if not namespace_name or not name:
            ctx.warn(CERT_SHEET, "a row has no namespace or no certificate name and was skipped")
            ctx.count(CERT_SHEET, "skipped")
            continue
        namespace = ctx.resolve_namespace(namespace_name, cluster=cluster, client_label=clean(_get(row, "Client")),
                                          sheet=CERT_SHEET)
        expiry = parse_timestamp(_get(row, "Expiry", "Expires"))
        if clean(_get(row, "Expiry", "Expires")) and expiry is None:
            ctx.warn(CERT_SHEET, "an expiry date could not be read; it was left empty")
        values = {
            "site": namespace.site,
            "domain": domain[:253],
            "tls_secret": tls_secret[:253],
            "issuer": clean(_get(row, "Issuer"))[:253],
            "ready": parse_bool(_get(row, "Ready")),
            "valid_from": parse_timestamp(_get(row, "Valid From", "Not Before")),
            "expiry": expiry,
            "risk": clean(_get(row, "Risk"))[:40],
            "remarks": clean(_get(row, "Remarks"))[:300],
        }
        ctx.upsert(Certificate, {"namespace": namespace, "name": name[:253]}, values, sheet=CERT_SHEET)
    ctx.track(Certificate, CERT_SHEET, namespace__cluster=cluster)
    relink_certificates(ctx.tenant)


register_sheet(CERT_SHEET, 70, import_certificates, namespaces=True)


# ----------------------------------------------------------------------------- 08_Credentials_References
_REFERENCED_BY = re.compile(r"^\s*referenced by\s+", re.IGNORECASE)


def split_remarks(text):
    """'Referenced by Deployment api; more' -> ('Deployment api', 'more'). Anything else is all remarks."""
    text = clean(text)
    if not text:
        return "", ""
    head, _sep, tail = text.partition(";")
    if _REFERENCED_BY.match(head):
        return _REFERENCED_BY.sub("", head).strip(), tail.strip()
    return "", text


def import_secret_refs(ctx, sheet):
    """Store which Secret NAMES workloads reference. No value is read, stored or shown."""
    cluster = ctx.cluster_for()
    suspicious = [c for c in sheet.columns if _VALUE_HEADER.match(clean(c) or "")]
    for column in suspicious:
        ctx.warn(SECRET_SHEET, f"column “{column}” looks like it could hold a secret value — it was ignored; "
                               f"only Secret names are ever imported")
    rejected = 0
    for row in sheet.rows:
        secret_name = clean(_get(row, "Secret Name"))
        namespace_name = clean(_get(row, "Secret Namespace", "Namespace"))
        if not secret_name or not namespace_name:
            ctx.count(SECRET_SHEET, "skipped")
            continue
        if not looks_like_name(secret_name):
            rejected += 1
            ctx.count(SECRET_SHEET, "skipped")
            continue
        namespace = ctx.resolve_namespace(namespace_name, cluster=cluster, client_label=clean(_get(row, "Client")),
                                          environment=clean(_get(row, "Environment")), sheet=SECRET_SHEET)
        referenced_by, remarks = split_remarks(_get(row, "Remarks"))
        values = {
            "site": namespace.site,
            "environment": clean(_get(row, "Environment"))[:40],
            "secret_namespace": namespace_name[:253],
            "remarks": remarks[:300],
        }
        keys = {"namespace": namespace, "application": clean(_get(row, "Application"))[:253],
                "secret_name": secret_name[:253], "referenced_by": referenced_by[:253]}
        ctx.upsert(SecretRef, keys, values, sheet=SECRET_SHEET)
    if rejected:
        ctx.warn(SECRET_SHEET, f"{rejected} row(s) were skipped because the Secret name column did not hold a "
                               f"Kubernetes object name")
    ctx.track(SecretRef, SECRET_SHEET, namespace__cluster=cluster)


register_sheet(SECRET_SHEET, 80, import_secret_refs, namespaces=True)
