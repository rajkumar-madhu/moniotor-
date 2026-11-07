"""Create clearly-marked example compute data (`manage.py seed_compute`): example physical servers and VMs as
network devices (tag "example"), hypervisor and Kubernetes clusters, memberships and VM placements.

Addresses come from the RFC 5737 documentation range 203.0.113.0/24, so nothing points at a real host.
Idempotent: re-running updates the same records; `--remove` deletes them again. No metrics, Kubernetes
summaries or reachability history are invented: Kubernetes clusters stay "Not collected" until Prometheus
answers for their selector."""
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from compute.models import Cluster, ClusterMember, VMPlacement

NOTE = "Example record created by manage.py seed_compute – documentation IP range, not a real host."
MUM = "Example DC Mumbai"
DR = "Example DR Chennai"
TAGS = "example, compute"

# name, site, vendor, model, mgmt_ip, role, prometheus_instance
SERVERS = [
    ("ex-esx-01", MUM, "Dell", "PowerEdge R760", "203.0.113.11", "VMware ESXi host", "203.0.113.11:9100"),
    ("ex-esx-02", MUM, "Dell", "PowerEdge R760", "203.0.113.12", "VMware ESXi host", "203.0.113.12:9100"),
    ("ex-esx-03", MUM, "Dell", "PowerEdge R760", "203.0.113.13", "VMware ESXi host", ""),
    ("ex-pve-01", DR, "HPE", "ProLiant DL380 Gen11", "203.0.113.21", "Proxmox VE host", "203.0.113.21:9100"),
    ("ex-pve-02", DR, "HPE", "ProLiant DL380 Gen11", "203.0.113.22", "Proxmox VE host", ""),
    ("ex-k8s-cp-01", MUM, "Supermicro", "SYS-121H", "203.0.113.31", "Kubernetes control plane", ""),
    ("ex-k8s-wk-01", MUM, "Supermicro", "SYS-121H", "203.0.113.32", "Kubernetes worker", ""),
    ("ex-k8s-wk-02", MUM, "Supermicro", "SYS-121H", "203.0.113.33", "Kubernetes worker", ""),
    ("ex-bare-01", DR, "Lenovo", "ThinkSystem SR650", "203.0.113.41", "Bare-metal backup server", ""),
]
# name, site, mgmt_ip, host, power, vcpus, memory_gb, disk_gb, os
VMS = [
    ("ex-web-01", MUM, "203.0.113.101", "ex-esx-01", "running", 4, "8", "80", "Ubuntu 22.04 LTS"),
    ("ex-web-02", MUM, "203.0.113.102", "ex-esx-02", "running", 4, "8", "80", "Ubuntu 22.04 LTS"),
    ("ex-db-01", MUM, "203.0.113.103", "ex-esx-01", "running", 16, "64", "500", "RHEL 9.3"),
    ("ex-app-01", MUM, "203.0.113.104", "ex-esx-03", "running", 8, "32", "200", "RHEL 9.3"),
    ("ex-app-02", MUM, "203.0.113.105", "ex-esx-03", "running", 8, "32", "200", "RHEL 9.3"),
    ("ex-mon-01", MUM, "203.0.113.106", "ex-esx-02", "running", 2, "4", "60", "Debian 12"),
    ("ex-build-01", MUM, "203.0.113.107", "ex-esx-03", "stopped", 8, "16", "250", "Windows Server 2022"),
    ("ex-dr-web-01", DR, "203.0.113.111", "ex-pve-01", "running", 4, "8", "80", "Ubuntu 22.04 LTS"),
    ("ex-dr-db-01", DR, "203.0.113.112", "ex-pve-02", "running", 8, "32", "500", "RHEL 9.3"),
    ("ex-dr-app-01", DR, "203.0.113.113", "ex-pve-01", "suspended", 4, "16", "120", "RHEL 9.3"),
    ("ex-legacy-01", MUM, "203.0.113.120", None, None, None, None, None, None),  # no placement data on purpose
]
# name, kind, site, selector, description
CLUSTERS = [
    ("ex-vsphere-mumbai", "vmware", MUM, 'cluster="ex-vsphere-mumbai"', "Example – production vSphere cluster"),
    ("ex-proxmox-chennai", "proxmox", DR, "", "Example – DR Proxmox VE cluster"),
    ("ex-k8s-mumbai", "kubernetes", MUM, 'cluster="ex-k8s-mumbai"', "Example – Kubernetes platform cluster"),
]
# cluster, device, role, cpu_cores, memory_gb
MEMBERS = [
    ("ex-vsphere-mumbai", "ex-esx-01", "host", 32, "256"), ("ex-vsphere-mumbai", "ex-esx-02", "host", 32, "256"),
    ("ex-vsphere-mumbai", "ex-esx-03", "host", 32, "256"),
    ("ex-proxmox-chennai", "ex-pve-01", "host", 24, "128"), ("ex-proxmox-chennai", "ex-pve-02", "host", 24, "128"),
    ("ex-k8s-mumbai", "ex-k8s-cp-01", "control-plane", 8, "32"), ("ex-k8s-mumbai", "ex-k8s-wk-01", "worker", 16, "64"),
    ("ex-k8s-mumbai", "ex-k8s-wk-02", "worker", 16, "64"),
]
HOST_CLUSTER = {m[1]: m[0] for m in MEMBERS if m[2] == "host"}


def Device():  # noqa: N802
    return apps.get_model("network", "Device")


class Command(BaseCommand):
    help = "Create (or remove with --remove) clearly-marked example servers, VMs, clusters and placements."

    def add_arguments(self, parser):
        parser.add_argument("--remove", action="store_true", help="Delete the example compute data instead.")

    def handle(self, *args, remove=False, **opts):
        t = settings.LINKEDEYE_TENANT
        D = Device()
        names = [s[0] for s in SERVERS] + [v[0] for v in VMS]
        if remove:
            with transaction.atomic():
                n = Cluster.objects.filter(tenant_key=t, name__in=[c[0] for c in CLUSTERS], tags__icontains="example").delete()[0]
                pks = [d.pk for d in D.objects.filter(tenant_key=t, name__in=names) if d.is_example]
                n += D.objects.filter(pk__in=pks).delete()[0]
            self.stdout.write(self.style.SUCCESS(f"Removed example compute data ({n} row(s))."))
            return
        made = {"servers": 0, "vms": 0, "clusters": 0, "members": 0, "placements": 0}
        skipped = []
        devices = {}

        def upsert_device(name, dtype, **fields):
            existing = D.objects.filter(tenant_key=t, name__iexact=name).first()
            if existing is not None and not existing.is_example:
                skipped.append(name)
                return None
            fields.update(device_type=dtype, description=NOTE, tags=TAGS, environment="prod", enabled=False)
            if existing is None:
                obj = D.objects.create(tenant_key=t, name=name, **fields)
                made["servers" if dtype == "physical_server" else "vms"] += 1
            else:
                for k, v in fields.items():
                    setattr(existing, k, v)
                existing.save()
                obj = existing
            devices[name] = obj
            return obj

        with transaction.atomic():
            for name, site, vendor, model, ip, role, inst in SERVERS:
                upsert_device(name, "physical_server", site=site, vendor=vendor, model=model, mgmt_ip=ip, role=role,
                              prometheus_instance=inst, check_port=22, serial=f"EXAMPLE-{name.upper()}")
            for name, site, ip, host, *_rest in VMS:
                upsert_device(name, "virtual_machine", site=site, vendor="", model="Virtual Machine", mgmt_ip=ip,
                              role="Example VM", check_port=22)
            clusters = {}
            for name, kind, site, selector, desc in CLUSTERS:
                c, created = Cluster.objects.update_or_create(
                    tenant_key=t, name=name,
                    defaults={"kind": kind, "site": site, "prometheus_selector": selector, "description": desc,
                              "tags": "example"})
                clusters[name] = c
                made["clusters"] += created
            for cname, dname, role, cores, mem in MEMBERS:
                dev = devices.get(dname)
                if dev is None:
                    continue
                _m, created = ClusterMember.objects.update_or_create(
                    tenant_key=t, cluster=clusters[cname], device=dev,
                    defaults={"role": role, "cpu_cores": cores, "memory_gb": Decimal(mem)})
                made["members"] += created
            for name, _site, _ip, host, power, vcpus, mem, disk, os_name in VMS:
                vm = devices.get(name)
                if vm is None or host is None:
                    continue
                hdev = devices.get(host)
                _p, created = VMPlacement.objects.update_or_create(
                    tenant_key=t, vm=vm,
                    defaults={"host": hdev, "cluster": clusters.get(HOST_CLUSTER.get(host, "")), "power_state": power,
                              "vcpus": vcpus, "memory_gb": Decimal(mem), "disk_gb": Decimal(disk), "os": os_name,
                              "notes": "Example placement (seed_compute)"})
                made["placements"] += created
        for name in skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {name}: a real device with that name exists."))
        self.stdout.write(self.style.SUCCESS(
            "Example compute data: {servers} servers, {vms} VMs, {clusters} clusters, {members} members, "
            "{placements} placements created (existing ones updated; all tagged 'example').".format(**made)))
