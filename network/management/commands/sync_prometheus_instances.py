"""Set network.Device.prometheus_instance from live Prometheus /api/v1/query up targets.

Matches each device management IP (or the host part of the current instance) to the
``instance`` label on an ``up`` series. Prefers SNMP/router jobs, then node_exporter."""
from django.core.management.base import BaseCommand, CommandError

from core import prometheus
from network.models import Device

_JOB_RANK = (
    "snmp",
    "snmp_exporter",
    "router",
    "node-exporter",
    "node_exp",
    "windows_exp",
    "idrac",
)


def _host_key(instance):
    text = (instance or "").strip()
    if not text:
        return ""
    return text.split(":", 1)[0].strip()


def _rank(job, up_value):
    job_l = (job or "").lower()
    for i, needle in enumerate(_JOB_RANK):
        if needle in job_l:
            return (0, i)
    return (1, len(_JOB_RANK))


def _best_instance(candidates):
    """Pick one (instance, job, up) row for a management host."""
    def sort_key(item):
        inst, job, up = item
        tier, sub = _rank(job, up)
        has_port = 0 if ":" in inst else 1
        try:
            up_ok = -float(up)
        except (TypeError, ValueError):
            up_ok = 0
        return (tier, sub, has_port, up_ok)

    return min(candidates, key=sort_key)[0]


def _targets_by_host():
    if not prometheus.configured():
        raise CommandError("Prometheus is not connected (set LINKEDEYE_PROMETHEUS_URL and test Integrations).")
    try:
        rows = prometheus.query("up")
    except prometheus.PrometheusError as exc:
        raise CommandError(str(exc)) from exc
    by_host = {}
    for row in rows:
        metric = row.get("metric") or {}
        inst = str(metric.get("instance") or "").strip()
        if not inst:
            continue
        host = _host_key(inst)
        up = row.get("value")
        by_host.setdefault(host, []).append((inst, metric.get("job") or "", up))
    return by_host


class Command(BaseCommand):
    help = "Align device prometheus_instance labels with Prometheus up targets (by management IP)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Show changes without saving.")
        parser.add_argument("--site", default="", help="Only devices on this site name.")

    def handle(self, *args, **opts):
        by_host = _targets_by_host()
        qs = Device.objects.filter(enabled=True)
        if opts["site"].strip():
            qs = qs.filter(site=opts["site"].strip())
        updated = skipped = unmatched = 0
        for device in qs.order_by("name"):
            host = _host_key(device.mgmt_ip) or _host_key(device.prometheus_instance)
            if not host:
                unmatched += 1
                continue
            candidates = by_host.get(host)
            if not candidates:
                unmatched += 1
                self.stdout.write(f"  — {device.name}: no Prometheus target for {host}")
                continue
            new_inst = _best_instance(candidates)
            if new_inst == (device.prometheus_instance or "").strip():
                skipped += 1
                continue
            self.stdout.write(f"  {device.name}: {device.prometheus_instance!r} → {new_inst!r}")
            if not opts["dry_run"]:
                device.prometheus_instance = new_inst
                device.save(update_fields=["prometheus_instance"])
            updated += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Done: {updated} updated, {skipped} already matched, {unmatched} without a Prometheus target."
            )
        )
