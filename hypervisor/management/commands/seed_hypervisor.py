"""Create (or refresh) a clearly marked example vCenter with SYNTHETIC data so the VM tabs and the datastore page can be demoed
without a vCenter. Idempotent: running it again rebuilds only the example source and its example VM device. ``--remove`` deletes
them. Everything here is invented (RFC 5737 addresses, example.test names); the source is disabled, so the sync job never
connects anywhere, and no events, alerts or incidents are created."""
from datetime import timedelta

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from hypervisor.models import EXAMPLE_MARK, Datastore, HvHost, VCenterSource, VmEvent, VmInfo, VmSnapshot

NAME = "Example vCenter (synthetic)"
DEVICE = "VM-APP-01"
MARK = "[hypervisor-example]"
GB = 1024 ** 3


def _device_site():
    from core.models import Site
    site = Site.objects.order_by("name").first()
    return site.name if site else ""


class Command(BaseCommand):
    help = "Create example vCenter data (synthetic, marked [example]); --remove deletes it again."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="Delete the example source and the example VM device.")

    def handle(self, *args, **opts):
        tenant = settings.LINKEDEYE_TENANT
        Device = apps.get_model("network", "Device")
        if opts["remove"]:
            with transaction.atomic():
                # only the synthetic source: a real vCenter source that happens to carry the same name is never touched
                srcs = VCenterSource.objects.filter(tenant_key=tenant, name=NAME, description__startswith=EXAMPLE_MARK)
                n = srcs.count()
                srcs.delete()
                devs = list(Device.objects.filter(tenant_key=tenant, description__startswith=MARK))
                for d in devs:
                    d.delete()
            self.stdout.write(f"Removed {n} example vCenter source and {len(devs)} example device(s).")
            return
        now = timezone.now()
        with transaction.atomic():
            site = _device_site()
            src, _ = VCenterSource.objects.update_or_create(
                tenant_key=tenant, name=NAME, defaults={
                    "site": "All Sites", "description": f"{EXAMPLE_MARK} Synthetic data from manage.py seed_hypervisor: nothing is read from a vCenter.",
                    "enabled": False, "host": "vcsa.example.test", "port": 443, "tls_mode": "verify", "sync_interval_s": 300,
                    "last_sync_at": now - timedelta(minutes=2), "last_ok_at": now - timedelta(minutes=2), "last_error": "",
                    "consecutive_failures": 0, "api_version": "8.0.2 (example)", "vm_count": 1, "host_count": 2, "datastore_count": 3,
                    "cluster_count": 1})
            for model in (HvHost, Datastore, VmInfo):
                model.objects.filter(source=src).delete()
            for ref, name, state in (("host-11", "esxi-host-01.example.test", "CONNECTED"), ("host-12", "esxi-host-02.example.test", "CONNECTED")):
                HvHost.objects.create(tenant_key=tenant, source=src, moref=ref, name=name, connection_state=state, power_state="POWERED_ON",
                                      cluster_name="Compute-01", cluster_moref="domain-c8", match_reason="synthetic")
            for ref, name, typ, cap, used, level in (("datastore-21", "DS01", "VMFS6", 1200, 742, 0), ("datastore-22", "DS02", "NFS", 2000, 760, 0),
                                                      ("datastore-23", "DS-ARCHIVE", "VMFS6", 4000, 3660, 90)):
                Datastore.objects.create(tenant_key=tenant, source=src, moref=ref, name=name, ds_type=typ, capacity_bytes=cap * GB,
                                         free_bytes=(cap - used) * GB, accessible=True, thin_provisioning=True, alert_level=level)
            name = DEVICE
            dev = Device.objects.filter(tenant_key=tenant, name=name).first()
            if dev is not None and not dev.description.startswith(MARK):
                name = f"{DEVICE} (example)"
                dev = Device.objects.filter(tenant_key=tenant, name=name).first()
            if dev is None:
                dev = Device(tenant_key=tenant, name=name, device_type="virtual_machine", mgmt_ip="203.0.113.115", site=site)
            dev.vendor, dev.model, dev.firmware, dev.role = "VMware", "ESXi 7.0", "Ubuntu 22.04 LTS", "Application server"
            dev.tags, dev.description = "example, production", f"{MARK} Example VM created by manage.py seed_hypervisor."
            dev.save()
            vm = VmInfo.objects.create(
                tenant_key=tenant, source=src, moref="vm-101", name=dev.name, device=dev, match_state="matched", match_reason="created by seed_hypervisor",
                matched_by="seed", matched_at=now, bios_uuid="4237a1b2-0000-4000-8000-0000000e0101", power_state="on", cpu_count=4,
                cores_per_socket=2, memory_mib=16384, guest_id="UBUNTU_64", guest_full_name="Ubuntu 22.04 LTS", guest_family="LINUX",
                guest_hostname="vm-app-01.example.test", guest_ips=["203.0.113.115"], tools_run_state="RUNNING", tools_version_status="CURRENT",
                tools_version="12.4.5", host_moref="host-11", host_name="esxi-host-01", cluster_name="Compute-01", ha_status="protected",
                datastore_names=["DS01"], created_on=timezone.make_aware(timezone.datetime(2025, 1, 12, 10, 15, 22)),
                description="LinkedEye application server (example)", last_detail_at=now - timedelta(minutes=2), last_synced_at=now - timedelta(minutes=2),
                snapshots_synced_at=now - timedelta(minutes=2), events_synced_at=now - timedelta(minutes=2),
                disks=[{"id": "2000", "label": "Hard disk 1", "type": "SCSI", "capacity_bytes": 100 * GB, "datastore": "DS01", "file": "VM-APP-01/VM-APP-01.vmdk"},
                       {"id": "2001", "label": "Hard disk 2", "type": "SCSI", "capacity_bytes": 200 * GB, "datastore": "DS01", "file": "VM-APP-01/VM-APP-01_1.vmdk"}],
                nics=[{"id": "4000", "label": "Network adapter 1", "type": "VMXNET3", "mac": "00:50:56:aa:bb:15", "state": "CONNECTED",
                       "network": "VM Network", "backing": "STANDARD_PORTGROUP", "ips": ["203.0.113.115"]}])
            for snap_id, snap_name, days, size, desc in (("snapshot-1", "pre-upgrade", 12, 12, "Before system upgrade"),
                                                        ("snapshot-2", "weekly-backup", 3, 10, "Weekly backup"),
                                                        ("snapshot-3", "post-config", 1, 11, "After configuration")):
                VmSnapshot.objects.create(tenant_key=tenant, vm=vm, snap_id=snap_id, name=snap_name, description=desc,
                                          created_at=now - timedelta(days=days, hours=2), size_bytes=size * GB, is_current=snap_id == "snapshot-3")
            for key, mins, sev, kind, msg in (("e1", 5, "info", "power", "VM power state changed to powered on"),
                                              ("e2", 8, "info", "tools", "VMware Tools heartbeat OK"),
                                              ("e3", 41, "warning", "alarm", "High memory usage (82%)"),
                                              ("e4", 126, "info", "snapshot", "Snapshot created: weekly-backup"),
                                              ("e5", 191, "info", "host", "VM migrated to esxi-host-01")):
                VmEvent.objects.create(tenant_key=tenant, vm=vm, key=key, ts=now - timedelta(minutes=mins), severity=sev, kind=kind, message=msg,
                                       origin="sync")
        self.stdout.write(f"Example vCenter ready: source “{NAME}”, VM device “{dev.name}” (id {dev.pk}). Remove with --remove.")
