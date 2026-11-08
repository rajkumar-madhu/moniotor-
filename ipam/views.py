import ipaddress

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST

from core.access import can, require, visible_sites
from core.audit import audit
from core.client_sites import safe_pk
from core.events import emit

from . import csvio, dns, services, sync
from .forms import DNSRecordForm, DNSZoneForm, IPAddressForm, ImportForm, SubnetForm, VLANForm
from .models import (GLOBAL_SITE, AddressStatus, DNSRecord, DNSZone, IPAddress, RecordType, Role, Source, Subnet,
                     SyncRun, VLAN, ip_key)

FULL_PCT = 90.0
STATUS_COLORS = {"active": "#0f7a55", "reserved": "#7a4fd6", "dhcp": "#0e8a7a", "deprecated": "#e08a00", "free": "#ddd6c8"}


# ------------------------------------------------------------------ helpers
def _per_page(request, default=20):
    try:
        return max(5, min(100, int(request.GET.get("per", default))))
    except (TypeError, ValueError):
        return default


def _query(request, *drop):
    q = request.GET.copy()
    for k in ("page",) + drop:
        q.pop(k, None)
    return q


def _safe_url(request, url):
    return bool(url) and url_has_allowed_host_and_scheme(url, allowed_hosts={request.get_host()}, require_https=request.is_secure())


def _next(request, fallback):
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    return nxt if _safe_url(request, nxt) else fallback


def _save_or_error(form, field, action, message=None):
    """Run `action()` (a save) in its own transaction. A unique constraint hit by a concurrent writer between
    validation and save becomes a form error instead of an HTTP 500. True when saved."""
    try:
        with transaction.atomic():
            action()
    except IntegrityError:
        form.add_error(field, message or "The same entry was saved by someone else a moment ago (or by the IPAM sync). "
                                         "Check the list and try again.")
        return False
    return True


def _deny(request, text="You do not have permission to do that.", fallback="ipam:overview"):
    if request.headers.get("Accept", "").startswith("application/json"):
        return JsonResponse({"error": text}, status=403)
    messages.error(request, text)
    return redirect(_next(request, reverse(fallback)))


def _perms(request):
    u = request.user
    return {"can_operate": can(u, "operate"), "can_admin": can(u, "admin")}


def _site(request):
    """The ?site= filter (top-bar site picker), ignored when the user cannot see that site."""
    site = request.GET.get("site", "").strip()
    if not site:
        return ""
    allowed = visible_sites(request.user)
    return site if allowed is None or site in allowed else ""


def _mark(request, objs, site_attr="site"):
    for o in objs:
        site = getattr(o, site_attr) if isinstance(site_attr, str) else site_attr(o)
        o.can_edit = services.can_manage(request.user, "operate", site)
        o.can_delete = services.can_manage(request.user, "admin", site)
    return objs


def _require_site(request, perm, site):
    if not services.can_manage(request.user, perm, site):
        raise PermissionDenied


GLOBAL_VRF = "__global__"


def _vrf_filter(qs, value):
    value = (value or "").strip()
    if value == GLOBAL_VRF:
        return qs.filter(vrf="")
    return qs.filter(vrf=value) if value else qs


def _ctx(request, **extra):
    return {**_perms(request), "query": _query(request), "site_filter": _site(request), "global_vrf": GLOBAL_VRF, **extra}


def _full_alert(subnet, used_before):
    """Warn once when a subnet crosses FULL_PCT utilisation."""
    used = services.used_count(subnet)
    pct, before = services.utilisation(subnet, used), services.utilisation(subnet, used_before)
    if pct >= FULL_PCT > before:
        emit(source="ipam", title=f"Subnet {subnet.cidr} is {pct:.0f}% utilised", severity="warning",
             message=f"{used:,} of {services.fmt_count(subnet.usable)} usable addresses are recorded in {subnet}.",
             site=subnet.site, obj=subnet, fingerprint=f"ipam:subnet-full:{subnet.pk}", used=used, pct=round(pct, 1))


# ------------------------------------------------------------------ overview
@require("view")
def overview(request):
    site = _site(request)
    subnets = services.site_filter(services.subnets_for(request.user), site)
    addresses = services.site_filter(services.addresses_for(request.user), site)
    vlans = services.site_filter(services.vlans_for(request.user), site)
    zones = services.site_filter(services.zones_for(request.user), site)
    records = services.site_filter(services.records_for(request.user), site, field="zone__site")
    total = subnets.count()
    v4 = subnets.filter(version=4).count()
    stats = services.subnet_stats(subnets.exclude(role=Role.CONTAINER))
    used_v4 = sum(u for s, u, _ in stats if s.version == 4)
    usable_v4 = sum(s.usable for s, _, _ in stats if s.version == 4)
    status_counts = {r["status"]: r["n"] for r in addresses.order_by().values("status").annotate(n=Count("id"))}
    v4_status = {r["status"]: r["n"] for r in addresses.filter(version=4, subnet__isnull=False)
                 .exclude(subnet__role=Role.CONTAINER).order_by().values("status").annotate(n=Count("id"))}
    top = sorted((x for x in stats if x[1]), key=lambda x: (-x[2], x[0].cidr))[:8]
    buckets = [0, 0, 0, 0, 0]
    for s, u, pct in stats:
        if s.version == 4:
            buckets[0 if pct < 25 else 1 if pct < 50 else 2 if pct < 75 else 3 if pct < 90 else 4] += 1
    found = [c for c in sync.conflicts(user=request.user) if not site or c["site"] in (site, GLOBAL_SITE, "")]
    by_kind = {k: sum(1 for c in found if c["kind"] == k) for k in sync.KIND_LABELS}
    free_v4 = max(0, usable_v4 - used_v4)
    donut = {"segments": [{"key": k, "label": AddressStatus(k).label, "value": v4_status.get(k, 0), "color": STATUS_COLORS[k]}
                          for k in AddressStatus.values] + [{"key": "free", "label": "Free", "value": free_v4, "color": STATUS_COLORS["free"]}],
             "center": {"value": f"{(100.0 * used_v4 / usable_v4) if usable_v4 else 0:.0f}%", "label": "IPv4 used"}}
    dist = {"labels": ["0–25%", "25–50%", "50–75%", "75–90%", "90–100%"],
            "series": [{"name": "Subnets", "color": "#0f7a55", "values": [buckets[0], buckets[1], buckets[2], 0, 0]},
                       {"name": "Subnets ≥ 75%", "color": "#e08a00", "values": [0, 0, 0, buckets[3], 0]},
                       {"name": "Subnets ≥ 90%", "color": "#d92d20", "values": [0, 0, 0, 0, buckets[4]]}],
            "yTicks": 4}
    runs = list(SyncRun.objects.filter(tenant_key=services.tenant()).order_by("-ran_at", "-id")[:5])
    return render(request, "ipam/overview.html", _ctx(
        request, total=total, v4=v4, v6=total - v4, containers=subnets.filter(role=Role.CONTAINER).count(),
        used_v4=used_v4, usable_v4=usable_v4, pct_v4=(100.0 * used_v4 / usable_v4) if usable_v4 else None,
        address_total=addresses.count(), status_counts=status_counts, statuses=AddressStatus.choices,
        vlan_total=vlans.count(), zone_total=zones.count(), record_total=records.count(),
        top=[(s, u, p, services.util_tone(p)) for s, u, p in top], full=buckets[4], has_v4_leaf=any(s.version == 4 for s, _, _ in stats),
        conflicts=len(found), by_kind=[(k, sync.KIND_LABELS[k], n) for k, n in by_kind.items()],
        donut=donut, dist=dist, runs=runs, last_run=runs[0] if runs else None,
        recent=addresses.select_related("subnet").order_by("-updated_at")[:8],
    ))


# ------------------------------------------------------------------ subnets
def _subnet_filters(request, qs):
    g = request.GET
    q = g.get("q", "").strip()
    if q:
        cond = Q(cidr__icontains=q) | Q(description__icontains=q) | Q(vrf__icontains=q)
        try:  # an address finds the subnets that contain it
            ip = ipaddress.ip_address(q)
            k = ip_key(ip)
            cond |= Q(version=ip.version, net_start__lte=k, net_end__gte=k)
        except ValueError:
            pass
        qs = qs.filter(cond)
    qs = services.site_filter(qs, _site(request))
    qs = _vrf_filter(qs, g.get("vrf", ""))
    if g.get("version") in ("4", "6"):
        qs = qs.filter(version=int(g["version"]))
    if g.get("role") in Role.values or g.get("role") == "none":
        qs = qs.filter(role="" if g["role"] == "none" else g["role"])
    if safe_pk(g.get("vlan")):
        qs = qs.filter(vlan_id=safe_pk(g["vlan"]))
    return qs.order_by("vrf", "version", "net_start", "prefixlen")


@require("view")
def subnets(request):
    base = services.site_filter(services.subnets_for(request.user), _site(request))
    qs = _subnet_filters(request, services.subnets_for(request.user)).select_related("vlan")
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    items = list(page)
    used = services.used_counts(items)
    depth = services.depths(items)
    for s in items:
        s.used = used[s.pk]
        s.pct = services.utilisation(s, s.used)
        s.tone = services.util_tone(s.pct)
        s.depth = min(depth[s.pk], 6)
    _mark(request, items)
    leaf_stats = services.subnet_stats(services.leaf_ipv4(base))
    used_v4 = sum(u for _, u, _ in leaf_stats)
    usable_v4 = sum(s.usable for s, _, _ in leaf_stats)
    vlan = None
    if safe_pk(request.GET.get("vlan")):
        vlan = services.vlans_for(request.user).filter(pk=safe_pk(request.GET["vlan"])).first()
    return render(request, "ipam/subnets.html", _ctx(
        request, page=page, items=items, total=base.count(), containers=base.filter(role=Role.CONTAINER).count(),
        used_v4=used_v4, usable_v4=usable_v4, pct_v4=(100.0 * used_v4 / usable_v4) if usable_v4 else None,
        full=sum(1 for _, _, p in leaf_stats if p >= FULL_PCT), roles=Role.choices, vrfs=services.vrf_names(),
        sites=services.site_names(request.user), vlan=vlan, filtered=any(request.GET.get(k) for k in ("q", "vrf", "version", "role", "vlan")),
    ))


@require("view")
def subnets_export(request):
    qs = _subnet_filters(request, services.subnets_for(request.user)).select_related("vlan")
    items = list(qs)
    used = services.used_counts(items)
    rows = ([s.cidr, s.vrf, s.site, s.get_role_display() if s.role else "", s.vlan.vid if s.vlan else "",
             s.vlan.name if s.vlan else "", s.gateway or "", s.size, s.usable, used[s.pk],
             f"{services.utilisation(s, used[s.pk]):.2f}", s.description] for s in items)
    return csvio.response("ipam-subnets.csv", ["cidr", "vrf", "site", "role", "vlan_id", "vlan_name", "gateway", "size",
                                               "usable", "used", "utilisation_pct", "description"], rows)


def _subnet(request, pk):
    return get_object_or_404(services.subnets_for(request.user).select_related("vlan"), pk=pk)


@require("view")
def subnet_detail(request, pk):
    subnet = _subnet(request, pk)
    nested = services.contained(subnet)
    kids = services.children(subnet, nested)
    visible = set(services.subnets_for(request.user).filter(pk__in=[k.pk for k in kids]).values_list("pk", flat=True))
    kids = [k for k in kids if k.pk in visible]
    kid_used = services.used_counts(kids)
    for k in kids:
        k.used = kid_used[k.pk]
        k.pct = services.utilisation(k, k.used)
        k.tone = services.util_tone(k.pct)
    # a global container holds the addresses and subnets of other sites: show only what this user may see
    used = services.used_count(subnet, request.user)
    pct = services.utilisation(subnet, used)
    can_edit = services.can_manage(request.user, "operate", subnet.site)
    amap, map_note = services.address_map(subnet, can_add=can_edit, user=request.user)
    addr_qs = services.addresses_in(subnet, request.user).select_related("device", "ci", "subnet")
    st = request.GET.get("status")
    if st in AddressStatus.values:
        addr_qs = addr_qs.filter(status=st)
    page = Paginator(addr_qs, _per_page(request, 50)).get_page(request.GET.get("page"))
    status_counts = {r["status"]: r["n"] for r in
                     services.addresses_in(subnet, request.user).order_by().values("status").annotate(n=Count("id"))}
    parent = services.parent(subnet)
    if parent and not services.subnets_for(request.user).filter(pk=parent.pk).exists():
        parent = None
    return render(request, "ipam/subnet_detail.html", _ctx(
        request, subnet=subnet, kids=kids, used=used, pct=pct, tone=services.util_tone(pct),
        next_free=services.next_free(subnet, nested), coverage=services.child_coverage(subnet, nested, visible),
        amap=amap, map_note=map_note, page=page, status_counts=status_counts, statuses=AddressStatus.choices,
        parent=parent, can_edit=can_edit, can_delete=services.can_manage(request.user, "admin", subnet.site),
        size_text=services.fmt_count(subnet.size), usable_text=services.fmt_count(subnet.usable),
        free_text=services.fmt_count(max(0, subnet.usable - used)),
    ))


def _vrf_list():
    return services.vrf_names()


@require("operate")
@require_http_methods(["GET", "POST"])
def subnet_form(request, pk=None):
    subnet = _subnet(request, pk) if pk else None
    if subnet is not None:
        _require_site(request, "operate", subnet.site)
    initial = {}
    if subnet is None:
        for k in ("cidr", "vrf", "site"):
            if request.GET.get(k):
                initial[k] = request.GET[k]
        if safe_pk(request.GET.get("vlan")):
            initial["vlan"] = safe_pk(request.GET["vlan"])
    old = None if subnet is None else {"cidr": subnet.cidr, "vrf": subnet.vrf, "version": subnet.version,
                                       "net_start": subnet.net_start, "net_end": subnet.net_end, "site": subnet.site}
    form = SubnetForm(request.POST or None, instance=subnet, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        obj = form.save(commit=False)
        obj.tenant_key = services.tenant()

        def save_subnet():
            obj.save()
            services.reassign(obj.tenant_key, obj.vrf, obj.version, obj.net_start, obj.net_end)
            if old and (old["cidr"] != obj.cidr or old["vrf"] != obj.vrf):
                services.reassign(obj.tenant_key, old["vrf"], old["version"], old["net_start"], old["net_end"])

        if _save_or_error(form, "cidr", save_subnet):
            if subnet is None:
                audit(request, "create", obj, cidr=obj.cidr, vrf=obj.vrf, site=obj.site, role=obj.role)
                messages.success(request, f"Added subnet {obj}.")
            else:
                audit(request, "update", obj, changed=changed, previous_cidr=old["cidr"], previous_site=old["site"])
                messages.success(request, f"Saved subnet {obj}." if changed else f"No changes to {obj}.")
            return redirect(obj.get_absolute_url())
    title = f"Edit subnet {subnet}" if subnet else "Add subnet"
    return render(request, "ipam/form.html", _ctx(
        request, form=form, title=title, obj=subnet, kind="subnet", icon="layers",
        intro="Overlapping subnets in the same VRF are rejected; nest subnets inside one marked Container (supernet).",
        cancel=subnet.get_absolute_url() if subnet else reverse("ipam:subnets"),
        datalists={"ipam-vrfs": _vrf_list()}, submit="Save changes" if subnet else "Add subnet"))


@require("admin")
@require_POST
def subnet_delete(request, pk):
    subnet = _subnet(request, pk)
    if not services.can_manage(request.user, "admin", subnet.site):
        return _deny(request, fallback="ipam:subnets")
    label, n = str(subnet), subnet.addresses.count()
    t, vrf, version, start, end = subnet.tenant_key, subnet.vrf, subnet.version, subnet.net_start, subnet.net_end
    audit(request, "delete", subnet, cidr=subnet.cidr, vrf=vrf, site=subnet.site, addresses=n)
    with transaction.atomic():
        subnet.delete()
        moved = services.reassign(t, vrf, version, start, end)
    orphans = IPAddress.objects.filter(tenant_key=t, vrf=vrf, version=version, addr_key__gte=start, addr_key__lte=end,
                                       subnet__isnull=True).count()
    msg = f"Deleted subnet {label}."
    if n:
        msg += f" {moved} address(es) moved to the parent subnet." if moved else ""
        msg += f" {orphans} address(es) now have no subnet (see Conflicts)." if orphans else ""
    messages.success(request, msg)
    return redirect(reverse("ipam:subnets"))


@require("operate")
@require_POST
def subnet_reserve(request, pk):
    """Reserve the next free address of the subnet (status reserved)."""
    subnet = _subnet(request, pk)
    if not services.can_manage(request.user, "operate", subnet.site):
        return _deny(request, fallback="ipam:subnets")
    hostname = request.POST.get("hostname", "").strip()
    if hostname:
        try:
            hostname = dns.hostname(hostname, "host name")
        except dns.DNSError as e:
            messages.error(request, str(e))
            return redirect(subnet.get_absolute_url())
    a = None
    for _ in range(5):   # another writer (a manual add, an import, the sync job) may take the address first: pick the next
        with transaction.atomic():
            Subnet.objects.select_for_update().filter(pk=subnet.pk).first()
            before = services.used_count(subnet)
            ip = services.next_free(subnet)
            if ip is None:
                messages.warning(request, f"{subnet} has no free address left.")
                return redirect(subnet.get_absolute_url())
            a = IPAddress(tenant_key=subnet.tenant_key, address=str(ip), vrf=subnet.vrf, subnet=subnet, site=subnet.site,
                          status=AddressStatus.RESERVED, source=Source.MANUAL, hostname=hostname,
                          description=f"Reserved by {request.user.get_username()}"[:300])
            try:
                with transaction.atomic():
                    a.save()
            except IntegrityError:
                a = None
        if a is not None:
            break
    if a is None:
        messages.error(request, f"Could not reserve an address in {subnet}: it is being changed by someone else. Try again.")
        return redirect(subnet.get_absolute_url())
    sync.relink_records(a.tenant_key, [a.address])
    audit(request, "create", a, address=a.address, subnet=subnet.cidr, status=a.status, via="next free")
    _full_alert(subnet, before)
    messages.success(request, f"Reserved {a.address} in {subnet}.")
    return redirect(reverse("ipam:address_edit", args=[a.pk]))


# ------------------------------------------------------------------ addresses
def _address_filters(request, qs):
    g = request.GET
    qs = qs.filter(services.search_q(g.get("q", "")))
    qs = services.site_filter(qs, _site(request))
    if g.get("status") in AddressStatus.values:
        qs = qs.filter(status=g["status"])
    if g.get("subnet", "").isdigit():
        sub = Subnet.objects.filter(tenant_key=services.tenant(), pk=int(g["subnet"])).first()
        qs = qs.filter(pk__in=services.addresses_in(sub).values("pk")) if sub else qs.none()
    qs = _vrf_filter(qs, g.get("vrf", ""))
    if g.get("within"):
        try:
            net = ipaddress.ip_network(g["within"].strip(), strict=False)
        except ValueError:
            qs = qs.none()
        else:
            qs = qs.filter(version=net.version, addr_key__gte=ip_key(net.network_address),
                           addr_key__lte=ip_key(net.broadcast_address))
    if g.get("source") in Source.values:
        qs = qs.filter(source=g["source"])
    if g.get("linked") == "device":
        qs = qs.filter(device__isnull=False)
    elif g.get("linked") == "ci":
        qs = qs.filter(ci__isnull=False)
    elif g.get("linked") == "none":
        qs = qs.filter(device__isnull=True, ci__isnull=True)
    return qs.order_by("vrf", "version", "addr_key")


@require("view")
def addresses(request):
    base = services.site_filter(services.addresses_for(request.user), _site(request))
    qs = _address_filters(request, services.addresses_for(request.user)).select_related("subnet", "device", "ci")
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    _mark(request, page)
    counts = {r["status"]: r["n"] for r in base.order_by().values("status").annotate(n=Count("id"))}
    subnet = None
    if request.GET.get("subnet", "").isdigit():
        subnet = services.subnets_for(request.user).filter(pk=int(request.GET["subnet"])).first()
    return render(request, "ipam/addresses.html", _ctx(
        request, page=page, total=base.count(), counts=counts, statuses=AddressStatus.choices, sources=Source.choices,
        vrfs=services.vrf_names(), subnet=subnet,
        filtered=any(request.GET.get(k) for k in ("q", "status", "subnet", "vrf", "source", "linked", "within")),
    ))


@require("view")
def addresses_export(request):
    qs = _address_filters(request, services.addresses_for(request.user)).select_related("subnet", "device", "ci")
    rows = ([a.address, a.vrf, a.status, a.hostname, a.description, a.device.name if a.device else "",
             a.ci.name if a.ci else "", a.subnet.cidr if a.subnet else "", a.site, a.get_source_display(),
             a.last_seen.isoformat() if a.last_seen else ""] for a in qs.iterator())
    return csvio.response("ipam-addresses.csv", csvio.IMPORT_COLUMNS + ["subnet", "site", "source", "last_seen"], rows)


@require("operate")
@require_http_methods(["GET", "POST"])
def addresses_import(request):
    form = ImportForm(request.POST or None, request.FILES or None)
    result = None
    if request.method == "POST" and form.is_valid():
        result = csvio.import_addresses(form.cleaned_data["file"], request.user, form.cleaned_data["update_existing"])
        if result.ok:
            touched = result.created + result.updated
            sync.relink_records(services.tenant(), [a.address for a in touched])
            audit(request, "import", None, object_type="ipam.ipaddress", created=len(result.created),
                  updated=len(result.updated), addresses=[a.address for a in touched][:200])
            for subnet in {a.subnet for a in result.created if a.subnet}:
                n_new = sum(1 for a in result.created if a.subnet_id == subnet.pk)
                _full_alert(subnet, services.used_count(subnet) - n_new)
            messages.success(request, f"Imported {len(result.created)} new and updated {len(result.updated)} address(es).")
            return redirect(reverse("ipam:addresses"))
    return render(request, "ipam/import.html", _ctx(request, form=form, result=result, columns=csvio.IMPORT_COLUMNS,
                                                     statuses=AddressStatus.values))


def _address(request, pk):
    return get_object_or_404(services.addresses_for(request.user).select_related("subnet", "device", "ci"), pk=pk)


@require("view")
def address_detail(request, pk):
    a = _address(request, pk)
    records = services.records_for(request.user).filter(Q(ip=a) | Q(rtype__in=["A", "AAAA"], value=a.address)).select_related("zone")
    from core.models import AuditEvent
    history = AuditEvent.objects.filter(object_type="ipam.ipaddress", object_id=str(a.pk))[:15]
    return render(request, "ipam/address_detail.html", _ctx(
        request, a=a, records=records.distinct(), history=history,
        can_edit=services.can_manage(request.user, "operate", a.site),
        can_delete=services.can_manage(request.user, "admin", a.site)))


@require("operate")
@require_http_methods(["GET", "POST"])
def address_form(request, pk=None):
    a = _address(request, pk) if pk else None
    if a is not None:
        _require_site(request, "operate", a.site)
    initial = {}
    if a is None:
        for k in ("address", "vrf", "hostname"):
            if request.GET.get(k):
                initial[k] = request.GET[k]
        if request.GET.get("status") in AddressStatus.values:
            initial["status"] = request.GET["status"]
    before = None if a is None else {"address": a.address, "vrf": a.vrf, "status": a.status, "site": a.site}
    form = IPAddressForm(request.POST or None, instance=a, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        subnet = form.subnet
        if not services.can_manage(request.user, "operate", subnet.site):
            form.add_error("address", f"You cannot add or change addresses on “{subnet.site}”.")
        else:
            used_before = services.used_count(subnet)
            changed = list(form.changed_data)
            obj = form.save(commit=False)
            obj.tenant_key = services.tenant()
            if _save_or_error(form, "address", obj.save,
                              f"{obj.address} is already recorded: someone else (or the IPAM sync) added it a moment ago."):
                sync.relink_records(obj.tenant_key, [obj.address])
                if a is None:
                    audit(request, "create", obj, address=obj.address, vrf=obj.vrf, subnet=subnet.cidr, status=obj.status,
                          hostname=obj.hostname)
                    messages.success(request, f"Added {obj} in {subnet}.")
                else:
                    audit(request, "update", obj, changed=changed, previous=before)
                    if before["status"] != obj.status:
                        emit(source="ipam", title=f"{obj} is now {obj.get_status_display()}", severity="info",
                             message=f"Status changed from {before['status']} to {obj.status} by {request.user.get_username()}.",
                             site=obj.site, obj=obj, fingerprint=f"ipam:address:{obj.pk}", status=obj.status)
                    messages.success(request, f"Saved {obj}." if changed else f"No changes to {obj}.")
                if a is None or before["address"] != obj.address or before["vrf"] != obj.vrf:
                    _full_alert(subnet, used_before)
                return redirect(obj.get_absolute_url())
    title = f"Edit {a}" if a else "Add IP address"
    return render(request, "ipam/form.html", _ctx(
        request, form=form, title=title, obj=a, kind="address", icon="tag",
        intro="The subnet is found automatically: the most specific subnet in the chosen VRF that contains the address.",
        cancel=a.get_absolute_url() if a else reverse("ipam:addresses"), datalists={},
        submit="Save changes" if a else "Add address"))


@require("admin")
@require_POST
def address_delete(request, pk):
    a = _address(request, pk)
    if not services.can_manage(request.user, "admin", a.site):
        return _deny(request, fallback="ipam:addresses")
    label, subnet = str(a), a.subnet
    audit(request, "delete", a, address=a.address, vrf=a.vrf, status=a.status, hostname=a.hostname,
          subnet=subnet.cidr if subnet else "")
    a.delete()
    messages.success(request, f"Deleted {label}.")
    return redirect(_next(request, subnet.get_absolute_url() if subnet else reverse("ipam:addresses")))


# ------------------------------------------------------------------ VLANs
def _vlan_filters(request, qs):
    q = request.GET.get("q", "").strip()
    if q:
        cond = Q(name__icontains=q) | Q(description__icontains=q)
        if q.isdigit():
            cond |= Q(vid=int(q))
        qs = qs.filter(cond)
    return services.site_filter(qs, _site(request)).order_by("site", "vid")


@require("view")
def vlans(request):
    base = services.site_filter(services.vlans_for(request.user), _site(request))
    page = Paginator(_vlan_filters(request, services.vlans_for(request.user)), _per_page(request)).get_page(request.GET.get("page"))
    items = list(page)
    counts = {r["vlan_id"]: r["n"] for r in services.subnets_for(request.user).filter(vlan__in=items).order_by()
              .values("vlan_id").annotate(n=Count("id"))}
    for v in items:
        v.subnet_count = counts.get(v.pk, 0)
    _mark(request, items)
    in_use = services.subnets_for(request.user).filter(vlan__in=base).values("vlan_id").distinct().count()
    total = base.count()
    return render(request, "ipam/vlans.html", _ctx(request, page=page, items=items, total=total, in_use=in_use,
                                                    unused=total - in_use,
                                                    sites_count=len({v for v in base.values_list("site", flat=True)}),
                                                    filtered=bool(request.GET.get("q"))))


@require("view")
def vlans_export(request):
    qs = _vlan_filters(request, services.vlans_for(request.user))
    counts = {r["vlan_id"]: r["n"] for r in services.subnets_for(request.user).filter(vlan__isnull=False).order_by()
              .values("vlan_id").annotate(n=Count("id"))}
    return csvio.response("ipam-vlans.csv", ["vid", "name", "site", "subnets", "description"],
                          ([v.vid, v.name, v.site, counts.get(v.pk, 0), v.description] for v in qs))


def _vlan(request, pk):
    return get_object_or_404(services.vlans_for(request.user), pk=pk)


@require("operate")
@require_http_methods(["GET", "POST"])
def vlan_form(request, pk=None):
    vlan = _vlan(request, pk) if pk else None
    if vlan is not None:
        _require_site(request, "operate", vlan.site)
    initial = {"site": request.GET["site"]} if vlan is None and request.GET.get("site") else {}
    form = VLANForm(request.POST or None, instance=vlan, initial=initial, user=request.user)
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        obj = form.save(commit=False)
        obj.tenant_key = services.tenant()
        if _save_or_error(form, "vid", obj.save):
            if vlan is None:
                audit(request, "create", obj, vid=obj.vid, name=obj.name, site=obj.site)
                messages.success(request, f"Added {obj}.")
            else:
                audit(request, "update", obj, changed=changed)
                messages.success(request, f"Saved {obj}.")
            return redirect(reverse("ipam:vlans"))
    return render(request, "ipam/form.html", _ctx(
        request, form=form, title=f"Edit {vlan}" if vlan else "Add VLAN", obj=vlan, kind="vlan", icon="network",
        intro="VLAN IDs 1–4094, unique per site. Global VLANs (All Sites) can be used by subnets on any site.",
        cancel=reverse("ipam:vlans"), datalists={}, submit="Save changes" if vlan else "Add VLAN"))


@require("admin")
@require_POST
def vlan_delete(request, pk):
    vlan = _vlan(request, pk)
    if not services.can_manage(request.user, "admin", vlan.site):
        return _deny(request, fallback="ipam:vlans")
    label, n = str(vlan), vlan.subnets.count()
    audit(request, "delete", vlan, vid=vlan.vid, name=vlan.name, site=vlan.site, subnets=n)
    vlan.delete()
    messages.success(request, f"Deleted {label}." + (f" {n} subnet(s) no longer have a VLAN." if n else ""))
    return redirect(reverse("ipam:vlans"))


# ------------------------------------------------------------------ DNS
def _zone_filters(request, qs):
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(description__icontains=q))
    if request.GET.get("kind") in ("forward", "reverse"):
        qs = qs.filter(kind=request.GET["kind"])
    return services.site_filter(qs, _site(request)).order_by("kind", "name")


@require("view")
def dns_zones(request):
    base = services.site_filter(services.zones_for(request.user), _site(request))
    qs = _zone_filters(request, services.zones_for(request.user)).annotate(
        n_records=Count("records"), n_addr=Count("records", filter=Q(records__rtype__in=["A", "AAAA", "PTR"])))
    page = Paginator(qs, _per_page(request)).get_page(request.GET.get("page"))
    _mark(request, page)
    return render(request, "ipam/zones.html", _ctx(
        request, page=page, total=base.count(), forward=base.filter(kind="forward").count(),
        reverse_count=base.filter(kind="reverse").count(),
        record_total=DNSRecord.objects.filter(zone__in=base).count(),
        filtered=any(request.GET.get(k) for k in ("q", "kind"))))


@require("view")
def zones_export(request):
    qs = _zone_filters(request, services.zones_for(request.user)).annotate(n_records=Count("records"))
    return csvio.response("ipam-dns-zones.csv", ["zone", "kind", "site", "default_ttl", "records", "description"],
                          ([z.name, z.kind, z.site, z.default_ttl, z.n_records, z.description] for z in qs))


def _zone(request, pk):
    return get_object_or_404(services.zones_for(request.user), pk=pk)


def _record_filters(request, qs):
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(value__icontains=q) | Q(description__icontains=q))
    if request.GET.get("type") in RecordType.values:
        qs = qs.filter(rtype=request.GET["type"])
    return qs.order_by("name", "rtype", "value")


@require("view")
def zone_detail(request, pk):
    zone = _zone(request, pk)
    qs = _record_filters(request, zone.records.select_related("ip"))
    page = Paginator(qs, _per_page(request, 50)).get_page(request.GET.get("page"))
    type_counts = {r["rtype"]: r["n"] for r in zone.records.order_by().values("rtype").annotate(n=Count("id"))}
    # only this page's own targets are looked up (not every address of the tenant)
    targets = {r.value for r in page if r.rtype in ("A", "AAAA")}
    recorded = set(IPAddress.objects.filter(tenant_key=zone.tenant_key, address__in=targets).order_by()
                   .values_list("address", flat=True)) if targets else set()
    for r in page:
        r.free_target = r.rtype in ("A", "AAAA") and r.value not in recorded
    return render(request, "ipam/zone_detail.html", _ctx(
        request, zone=zone, page=page, type_counts=sorted(type_counts.items()), types=RecordType.choices,
        record_total=sum(type_counts.values()), can_edit=services.can_manage(request.user, "operate", zone.site),
        can_delete=services.can_manage(request.user, "admin", zone.site),
        filtered=any(request.GET.get(k) for k in ("q", "type"))))


@require("view")
def zone_records_export(request, pk):
    zone = _zone(request, pk)
    qs = _record_filters(request, zone.records.all())
    return csvio.response(f"dns-{zone.name}.csv", ["name", "fqdn", "type", "value", "ttl", "description"],
                          ([r.name, r.fqdn, r.rtype, r.value, r.effective_ttl, r.description] for r in qs))


@require("operate")
@require_http_methods(["GET", "POST"])
def zone_form(request, pk=None):
    zone = _zone(request, pk) if pk else None
    if zone is not None:
        _require_site(request, "operate", zone.site)
    form = DNSZoneForm(request.POST or None, instance=zone, user=request.user,
                       initial={"kind": request.GET["kind"]} if request.GET.get("kind") in ("forward", "reverse") else {})
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        obj = form.save(commit=False)
        obj.name = form.cleaned_data["name"]
        obj.tenant_key = services.tenant()
        if _save_or_error(form, "name", obj.save):
            if zone is not None and "name" in changed:
                for r in obj.records.filter(rtype="PTR"):
                    r.ip = sync.link_record_ip(r)
                    r.save(update_fields=["ip"])
            if zone is None:
                audit(request, "create", obj, name=obj.name, kind=obj.kind, site=obj.site)
                messages.success(request, f"Added zone {obj}.")
            else:
                audit(request, "update", obj, changed=changed)
                messages.success(request, f"Saved zone {obj}.")
            return redirect(obj.get_absolute_url())
    return render(request, "ipam/form.html", _ctx(
        request, form=form, title=f"Edit zone {zone}" if zone else "Add DNS zone", obj=zone, kind="zone", icon="globe",
        intro="Zones are records of what your DNS servers serve; LinkedEye does not push them to a DNS server.",
        cancel=zone.get_absolute_url() if zone else reverse("ipam:dns"), datalists={},
        submit="Save changes" if zone else "Add zone"))


@require("admin")
@require_POST
def zone_delete(request, pk):
    zone = _zone(request, pk)
    if not services.can_manage(request.user, "admin", zone.site):
        return _deny(request, fallback="ipam:dns")
    label, n = zone.name, zone.records.count()
    audit(request, "delete", zone, name=zone.name, kind=zone.kind, site=zone.site, records=n)
    zone.delete()
    messages.success(request, f"Deleted zone {label} and its {n} record(s).")
    return redirect(reverse("ipam:dns"))


def _record(request, pk):
    return get_object_or_404(services.records_for(request.user).select_related("zone"), pk=pk)


@require("operate")
@require_http_methods(["GET", "POST"])
def record_form(request, zone_pk=None, pk=None):
    record = _record(request, pk) if pk else None
    zone = record.zone if record else _zone(request, zone_pk)
    _require_site(request, "operate", zone.site)
    initial = {}
    if record is None:
        for k in ("name", "value"):
            if request.GET.get(k):
                initial[k] = request.GET[k]
        if request.GET.get("type") in RecordType.values:
            initial["rtype"] = request.GET["type"]
    form = DNSRecordForm(request.POST or None, instance=record, zone=zone, initial=initial)
    if request.method == "POST" and form.is_valid():
        changed = list(form.changed_data)
        obj = form.save(commit=False)
        obj.zone, obj.tenant_key = zone, zone.tenant_key
        obj.name, obj.value = form.cleaned_data["name"], form.cleaned_data["value"]
        obj.ip = sync.link_record_ip(obj)
        if _save_or_error(form, None, obj.save):
            if record is None:
                audit(request, "create", obj, zone=zone.name, name=obj.name, type=obj.rtype, value=obj.value)
                messages.success(request, f"Added {obj.rtype} record {obj.fqdn}.")
            else:
                audit(request, "update", obj, changed=changed, zone=zone.name)
                messages.success(request, f"Saved {obj.rtype} record {obj.fqdn}.")
            return redirect(zone.get_absolute_url())
    return render(request, "ipam/form.html", _ctx(
        request, form=form, title=f"Edit record in {zone}" if record else f"Add record to {zone}", obj=record, kind="record",
        icon="globe", zone=zone,
        intro=("A: IPv4 · AAAA: IPv6 · CNAME: host name · MX: “10 mail.example.com” · SRV: “10 5 5060 sip.example.com” · TXT: text"
               if zone.kind == "forward" else "PTR: enter the address or the relative reverse name, and the host name as value."),
        cancel=zone.get_absolute_url(), datalists={}, submit="Save changes" if record else "Add record"))


@require("admin")
@require_POST
def record_delete(request, pk):
    record = _record(request, pk)
    zone = record.zone
    if not services.can_manage(request.user, "admin", zone.site):
        return _deny(request, fallback="ipam:dns")
    label = f"{record.rtype} {record.fqdn}"
    audit(request, "delete", record, zone=zone.name, name=record.name, type=record.rtype, value=record.value)
    record.delete()
    messages.success(request, f"Deleted {label}.")
    return redirect(zone.get_absolute_url())


# ------------------------------------------------------------------ conflicts & sync
def _conflicts(request):
    site = _site(request)
    found = [c for c in sync.conflicts(user=request.user) if not site or c["site"] in (site, GLOBAL_SITE, "")]
    kind = request.GET.get("kind")
    shown = [c for c in found if c["kind"] == kind] if kind in sync.KIND_LABELS else found
    return found, shown, kind if kind in sync.KIND_LABELS else ""


@require("view")
def conflicts(request):
    found, shown, kind = _conflicts(request)
    for c in shown:
        c["label"] = sync.KIND_LABELS[c["kind"]]
    page = Paginator(shown, _per_page(request, 50)).get_page(request.GET.get("page"))
    counts = [(k, label, sum(1 for c in found if c["kind"] == k)) for k, label in sync.KIND_LABELS.items()]
    return render(request, "ipam/conflicts.html", _ctx(request, page=page, total=len(found), counts=counts, kind=kind,
                                                        query_nokind=_query(request, "kind")))


@require("view")
def conflicts_export(request):
    _, shown, _ = _conflicts(request)
    return csvio.response("ipam-conflicts.csv", ["kind", "address", "site", "problem", "detail"],
                          ([sync.KIND_LABELS[c["kind"]], c["address"], c["site"], c["title"], c["detail"]] for c in shown))


@require("operate")
@require_POST
def sync_now(request):
    run = sync.sync(user=request.user, trigger="manual", actor_name=request.user.get_username())
    audit(request, "run", run, object_type="ipam.syncrun", created=run.created, updated=run.updated,
          skipped=run.skipped, conflicts=run.conflicts, new_conflicts=run.new_conflicts)
    msg = (f"Sync finished: {run.created} address(es) created, {run.updated} updated, {run.unchanged} unchanged, "
           f"{run.skipped} skipped; {run.conflicts} conflict(s)" + (f", {run.new_conflicts} new." if run.new_conflicts else "."))
    (messages.warning if run.new_conflicts else messages.success)(request, msg)
    return redirect(run.get_absolute_url())


@require("view")
def sync_run(request, pk):
    run = get_object_or_404(SyncRun.objects.filter(tenant_key=services.tenant()), pk=pk)
    allowed = visible_sites(request.user)

    def visible(items):
        return [i for i in items if allowed is None or i.get("site", GLOBAL_SITE) in allowed | {GLOBAL_SITE, ""}]

    report = run.report or {}
    sections = [("created", "Created", "ok", visible(report.get("created", []))),
                ("updated", "Updated", "info", visible(report.get("updated", []))),
                ("duplicate", "Address held by another device", "crit", visible(report.get("duplicate", []))),
                ("outside", "Outside every subnet (not created)", "warn", visible(report.get("outside", []))),
                ("denied", "On sites you cannot operate (not created)", "mute", visible(report.get("denied", [])))]
    return render(request, "ipam/sync_run.html", _ctx(request, run=run, sections=sections, not_ip=report.get("not_ip", 0),
                                                       scoped=bool(run.sites), now=timezone.now()))

