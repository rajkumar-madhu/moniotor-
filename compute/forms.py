from django import forms
from django.core.exceptions import ValidationError

from . import services
from .models import (HYPERVISOR_KINDS, Cluster, ClusterKind, ClusterMember, MemberRole, VMPlacement, format_selector,
                     is_global, parse_selector)

K8S_ROLES = {MemberRole.CONTROL_PLANE, MemberRole.WORKER, MemberRole.STORAGE, MemberRole.OTHER}
HYPERVISOR_ROLES = {MemberRole.HOST, MemberRole.STORAGE, MemberRole.OTHER}


def same_site(a, b):
    """Two site names are compatible when either is global or they are equal."""
    return is_global(a) or is_global(b) or a == b


class DeviceChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        bits = [obj.name, obj.mgmt_ip]
        if obj.site:
            bits.append(obj.site)
        return " · ".join(bits)


class ClusterForm(forms.ModelForm):
    site = forms.ChoiceField(label="Site", required=False)

    class Meta:
        model = Cluster
        fields = ["name", "kind", "site", "prometheus_selector", "tags", "description"]
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "e.g. prod-vsphere-01", "maxlength": 120}),
            "prometheus_selector": forms.TextInput(attrs={"placeholder": 'cluster="prod-k8s"', "spellcheck": "false",
                                                          "autocapitalize": "off"}),
            "tags": forms.TextInput(attrs={"placeholder": "e.g. production, vmware"}),
            "description": forms.Textarea(attrs={"rows": 3, "placeholder": "Optional"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        sites = services.manageable_sites(user) if user is not None else ["All Sites"]
        current = self.instance.site if self.instance.pk else ""
        if current and current not in sites:
            sites = [current] + sites
        self.fields["site"].choices = [(s, s) for s in sites]
        if not self.instance.pk and not self.initial.get("site") and sites:
            self.initial["site"] = sites[0]

    def clean_name(self):
        name = (self.cleaned_data.get("name") or "").strip()
        if not name:
            raise ValidationError("Enter a name.")
        qs = Cluster.objects.filter(tenant_key=services.tenant(), name__iexact=name)
        if self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise ValidationError("A cluster with this name already exists.")
        return name

    def clean_site(self):
        site = (self.cleaned_data.get("site") or "").strip() or "All Sites"
        allowed = {c for c, _l in self.fields["site"].choices}
        if site not in allowed:
            raise ValidationError("Choose a site you manage.")
        return site

    def clean_prometheus_selector(self):
        return format_selector(parse_selector(self.cleaned_data.get("prometheus_selector")))

    def clean_tags(self):
        tags = [t.strip() for t in (self.cleaned_data.get("tags") or "").split(",") if t.strip()]
        out = ", ".join(dict.fromkeys(tags))
        if len(out) > 200:
            raise ValidationError("Tags are too long (200 characters at most).")
        return out

    def clean(self):
        data = super().clean()
        if not self.instance.pk:
            return data
        site, kind = data.get("site"), data.get("kind")
        tenant = services.tenant()
        members = ClusterMember.objects.filter(tenant_key=tenant, cluster=self.instance).select_related("device")
        if site and not is_global(site):
            off = [m.device.name for m in members if not same_site(site, m.device.site)]
            if off:
                self.add_error("site", f"Members on other sites: {', '.join(off[:5])}. Remove them first.")
            vms = [p.vm.name for p in VMPlacement.objects.filter(tenant_key=tenant, cluster=self.instance)
                   .select_related("vm") if not same_site(site, p.vm.site)]
            if vms:
                self.add_error("site", f"VMs on other sites are placed in this cluster: {', '.join(vms[:5])}.")
        if kind and kind != self.instance.kind:
            if kind == ClusterKind.KUBERNETES:
                if VMPlacement.objects.filter(tenant_key=tenant, cluster=self.instance).exists():
                    self.add_error("kind", "VM placements point at this cluster; a Kubernetes cluster cannot hold VMs.")
                if any(m.role == MemberRole.HOST for m in members):
                    self.add_error("kind", "Change the members' “Hypervisor host” role first.")
            else:
                if any(m.device.device_type != services.SERVER for m in members):
                    self.add_error("kind", "Only physical servers can be members of a hypervisor cluster; "
                                           "remove the VM members first.")
                if any(m.role in (MemberRole.CONTROL_PLANE, MemberRole.WORKER) for m in members):
                    self.add_error("kind", "Change the members' Kubernetes roles first.")
                other = ClusterMember.objects.filter(
                    tenant_key=tenant, device__in=[m.device_id for m in members],
                    cluster__kind__in=HYPERVISOR_KINDS).exclude(cluster=self.instance).select_related("device", "cluster")
                if other:
                    o = other[0]
                    self.add_error("kind", f"{o.device.name} is already a host of the hypervisor cluster “{o.cluster.name}”.")
        return data


class MemberForm(forms.ModelForm):
    device = DeviceChoiceField(queryset=None, label="Device")

    class Meta:
        model = ClusterMember
        fields = ["device", "role", "cpu_cores", "memory_gb"]
        widgets = {"cpu_cores": forms.NumberInput(attrs={"min": 1, "max": 4096, "placeholder": "from node_exporter"}),
                   "memory_gb": forms.NumberInput(attrs={"min": 1, "max": 65536, "step": "0.01",
                                                         "placeholder": "from node_exporter"})}

    def __init__(self, *args, cluster=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cluster, self.user = cluster, user
        types = [services.SERVER] + ([services.VM] if cluster.is_kubernetes else [])
        qs = services.devices_for(user).filter(device_type__in=types)
        if not is_global(cluster.site):
            qs = qs.filter(site__in=[cluster.site, "", "All Sites"])
        self.fields["device"].queryset = qs.order_by("name")
        roles = K8S_ROLES if cluster.is_kubernetes else HYPERVISOR_ROLES
        self.fields["role"].choices = [(v, l) for v, l in MemberRole.choices if v in roles]
        if not self.instance.pk:
            self.initial.setdefault("role", MemberRole.WORKER if cluster.is_kubernetes else MemberRole.HOST)
        else:
            self.fields["device"].disabled = True
            self.fields["device"].queryset = services.devices_for(user).filter(pk=self.instance.device_id)

    def clean(self):
        data = super().clean()
        device, role = data.get("device"), data.get("role")
        c = self.cluster
        if device is None:
            return data
        if device.device_type not in ((services.SERVER, services.VM) if c.is_kubernetes else (services.SERVER,)):
            self.add_error("device", "Hypervisor clusters take physical servers only.")
        if not same_site(c.site, device.site):
            self.add_error("device", f"{device.name} is on “{device.site}”, the cluster is on “{c.site}”.")
        if role and role not in (K8S_ROLES if c.is_kubernetes else HYPERVISOR_ROLES):
            self.add_error("role", "This role does not fit the cluster type.")
        tenant = services.tenant()
        dup = ClusterMember.objects.filter(tenant_key=tenant, cluster=c, device=device)
        if self.instance.pk:
            dup = dup.exclude(pk=self.instance.pk)
        if dup.exists():
            self.add_error("device", f"{device.name} is already a member of this cluster.")
        if not c.is_kubernetes:
            other = (ClusterMember.objects.filter(tenant_key=tenant, device=device, cluster__kind__in=HYPERVISOR_KINDS)
                     .exclude(cluster=c).select_related("cluster").first())
            if other:
                self.add_error("device", f"{device.name} is already a host of the hypervisor cluster “{other.cluster.name}”.")
        return data


class PlacementForm(forms.ModelForm):
    host = DeviceChoiceField(queryset=None, required=False, label="Host (physical server)")
    cluster = forms.ModelChoiceField(queryset=None, required=False, label="Cluster")

    class Meta:
        model = VMPlacement
        fields = ["host", "cluster", "power_state", "vcpus", "memory_gb", "disk_gb", "os", "notes"]
        widgets = {
            "vcpus": forms.NumberInput(attrs={"min": 1, "max": 768, "placeholder": "e.g. 4"}),
            "memory_gb": forms.NumberInput(attrs={"min": 0.25, "max": 24576, "step": "0.25", "placeholder": "e.g. 16"}),
            "disk_gb": forms.NumberInput(attrs={"min": 0, "max": 1048576, "step": "0.1", "placeholder": "e.g. 100"}),
            "os": forms.TextInput(attrs={"placeholder": "e.g. Ubuntu 22.04 LTS"}),
            "notes": forms.TextInput(attrs={"placeholder": "Optional"}),
        }

    def __init__(self, *args, vm=None, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.vm, self.user = vm, user
        hosts = services.servers_for(user)
        clusters = services.clusters_for(user).filter(kind__in=HYPERVISOR_KINDS)
        if not is_global(vm.site):
            hosts = hosts.filter(site__in=[vm.site, "", "All Sites"])
            clusters = clusters.filter(site__in=[vm.site, "", "All Sites"])
        self.fields["host"].queryset = hosts.order_by("name")
        self.fields["cluster"].queryset = clusters.order_by("name")
        self.fields["cluster"].help_text = "Leave empty to use the host's hypervisor cluster."

    def clean(self):
        data = super().clean()
        vm, host, cluster = self.vm, data.get("host"), data.get("cluster")
        tenant = services.tenant()
        if host is not None:
            if host.device_type != services.SERVER:
                self.add_error("host", "Choose a physical server.")
            elif not same_site(vm.site, host.site):
                self.add_error("host", f"{host.name} is on “{host.site}”, the VM is on “{vm.site}”.")
        if cluster is not None:
            if cluster.is_kubernetes:
                self.add_error("cluster", "VMs are placed in hypervisor clusters, not Kubernetes clusters.")
            elif not same_site(vm.site, cluster.site):
                self.add_error("cluster", f"The cluster is on “{cluster.site}”, the VM is on “{vm.site}”.")
        host_clusters = []
        if host is not None:
            host_clusters = list(ClusterMember.objects.filter(tenant_key=tenant, device=host,
                                                              cluster__kind__in=HYPERVISOR_KINDS).select_related("cluster"))
        if host is not None and cluster is not None and not self.errors:
            if cluster.pk not in [m.cluster_id for m in host_clusters]:
                self.add_error("host", f"{host.name} is not a member of the cluster “{cluster.name}”.")
        if host is not None and cluster is None and len(host_clusters) == 1:
            data["cluster"] = host_clusters[0].cluster
        vcpus = data.get("vcpus")
        if host is not None and vcpus:
            cores = max((m.cpu_cores or 0 for m in host_clusters), default=0)
            if cores and vcpus > cores:
                self.add_error("vcpus", f"A VM cannot have more vCPUs than its host has cores ({cores}).")
        mem = data.get("memory_gb")
        if host is not None and mem:
            host_mem = max((m.memory_gb or 0 for m in host_clusters), default=0)
            if host_mem and mem > host_mem:
                self.add_error("memory_gb", f"A VM cannot have more memory than its host ({host_mem:g} GB).")
        return data
