"""The sync: records, transitions (events once), placement, failure back-off, retention, atomicity."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from compute.models import Cluster, VMPlacement
from core.models import AuditEvent, Event
from hypervisor import providers, sync
from hypervisor.models import Datastore, HvHost, VCenterSource, VmEvent, VmInfo, VmSnapshot
from hypervisor.tests.base import FakeProvider, HvCase, capture_logs, provider_factory, snap
from hypervisor.tests.fake_vcenter import FakeVCenter, PASSWORD

GB = 1024 ** 3


class SyncCase(HvCase):
    def setUp(self):
        super().setUp()
        self.fake = FakeVCenter().start()
        self.addCleanup(self.fake.stop)
        self.src = self.make_source(self.fake)

    def events(self, fingerprint_part=""):
        return list(Event.objects.filter(source="hypervisor", fingerprint__contains=fingerprint_part).order_by("id"))

    def vm(self, moref="vm-101"):
        return VmInfo.objects.get(moref=moref)

    def sync(self, now=None, **kw):
        return self.run_sync(self.src, now=now, **kw)


class Records(SyncCase):
    def test_first_sync_stores_the_inventory(self):
        out = self.sync()
        self.assertIn("5 VMs", out)
        self.src.refresh_from_db()
        self.assertEqual((self.src.vm_count, self.src.host_count, self.src.datastore_count, self.src.cluster_count), (5, 2, 3 - 1, 1))
        self.assertEqual(self.src.state[0], "connected")
        self.assertEqual(self.src.api_version, "8.0.2.00000")
        vm = self.vm()
        self.assertEqual((vm.power_state, vm.cpu_count, vm.memory_mib, vm.host_name, vm.cluster_name, vm.ha_status),
                         ("on", 4, 16384, "esxi-host-01.example.test", "Compute-01", "protected"))
        self.assertEqual(vm.datastore_names, ["DS01"])
        self.assertEqual(vm.guest_ips, ["10.20.1.15"])
        self.assertEqual(vm.tools_run_state, "RUNNING")
        self.assertEqual(vm.disks[0]["capacity_bytes"], 100 * GB)
        self.assertEqual(vm.nics[0]["ips"], ["10.20.1.15"])
        ds = Datastore.objects.get(name="DS01")
        self.assertEqual((ds.used_pct, ds.ds_type, ds.thin_provisioning), (61.8, "VMFS", True))

    def test_second_identical_sync_changes_nothing_and_emits_nothing(self):
        self.sync()
        before = (VmInfo.objects.count(), VmEvent.objects.count(), Event.objects.count(), HvHost.objects.count())
        self.sync()
        self.assertEqual(before, (VmInfo.objects.count(), VmEvent.objects.count(), Event.objects.count(), HvHost.objects.count()))

    def test_vm_names_are_cleaned_on_the_way_in(self):
        self.fake.inv["vms"]["vm-101"]["name"] = "evil\nname\x1b[2J<img src=x>"
        self.sync()
        self.assertEqual(self.vm().name, "evil name[2J<img src=x>")

    def test_vms_that_vanish_are_marked_and_come_back(self):
        self.sync()
        gone = self.fake.inv["vms"].pop("vm-104")
        self.sync()
        self.assertIsNotNone(self.vm("vm-104").missing_since)
        self.fake.inv["vms"]["vm-104"] = gone
        self.sync()
        self.assertIsNone(self.vm("vm-104").missing_since)

    def test_details_are_fetched_in_batches_most_overdue_first(self):
        with override_settings(HYPERVISOR_MAX_DETAIL=2):
            self.sync()
            self.assertEqual(VmInfo.objects.filter(last_detail_at__isnull=False).count(), 2)
            self.sync()
            self.sync()
        self.assertEqual(VmInfo.objects.filter(last_detail_at__isnull=False).count(), 5)

    def test_a_store_failure_leaves_nothing_half_written(self):
        with mock.patch.object(sync, "_apply_provider", side_effect=RuntimeError("boom")):
            out = self.sync()
        self.assertEqual(out, "failed: store")
        self.assertEqual((VmInfo.objects.count(), Datastore.objects.count(), HvHost.objects.count()), (0, 0, 0))
        self.src.refresh_from_db()
        self.assertEqual(self.src.consecutive_failures, 1)
        self.assertIsNone(self.src.sync_started_at)

    def test_a_running_sync_blocks_a_second_one(self):
        VCenterSource.objects.filter(pk=self.src.pk).update(sync_started_at=timezone.now())
        self.assertEqual(self.sync(), "busy")
        self.assertEqual(VmInfo.objects.count(), 0)


class Transitions(SyncCase):
    def test_power_off_alerts_once_and_recovery_once(self):
        self.sync()
        self.fake.inv["vms"]["vm-101"]["power"] = "POWERED_OFF"
        self.sync()
        self.sync()
        self.sync()
        evs = self.events(f"vm:{self.vm().pk}:power")
        self.assertEqual([(e.severity) for e in evs], ["warning"])
        self.assertIn("unexpectedly", evs[0].title)
        self.assertTrue(VmEvent.objects.filter(vm=self.vm(), kind="power").exists())
        self.fake.inv["vms"]["vm-101"]["power"] = "POWERED_ON"
        self.sync()
        self.sync()
        self.assertEqual([e.severity for e in self.events(f"vm:{self.vm().pk}:power")], ["warning", "ok"])

    def test_a_vm_first_seen_powered_off_does_not_alert(self):
        self.sync()
        self.assertEqual(self.events(f"vm:{self.vm('vm-103').pk}:power"), [])

    def test_approved_power_off_request_is_info_not_a_warning(self):
        from hypervisor import services
        self.sync()
        req, _ = services.create_request(self.mum_op, self.vm(), "power_off", "maintenance")
        services.decide(req, self.mum_admin, True)
        self.fake.inv["vms"]["vm-101"]["power"] = "POWERED_OFF"
        self.sync()
        (ev,) = self.events(f"vm:{self.vm().pk}:power")
        self.assertEqual(ev.severity, "info")

    def test_tools_not_running_alerts_after_the_grace_period_once(self):
        t0 = timezone.now()
        self.sync(now=t0)
        pk = self.vm("vm-105").pk
        self.sync(now=t0 + timedelta(minutes=2))
        self.assertEqual(self.events(f"vm:{pk}:tools"), [])
        self.sync(now=t0 + timedelta(minutes=7))
        self.sync(now=t0 + timedelta(minutes=12))
        self.assertEqual([e.severity for e in self.events(f"vm:{pk}:tools")], ["warning"])
        self.fake.inv["vms"]["vm-105"]["tools"] = "RUNNING"
        self.sync(now=t0 + timedelta(minutes=17))
        self.sync(now=t0 + timedelta(minutes=22))
        self.assertEqual([e.severity for e in self.events(f"vm:{pk}:tools")], ["warning", "ok"])

    def test_datastore_thresholds_with_hysteresis(self):
        self.sync()
        ds = self.fake.inv["datastores"]["datastore-21"]
        total = ds["capacity"]
        seq = [(92, 1), (93, 0), (96, 1), (94, 0), (91, 1), (85, 1), (84, 0)]
        for pct, new in seq:
            ds["free"] = int(total * (100 - pct) / 100)
            before = len(self.events("datastore:"))
            self.sync()
            self.assertEqual(len(self.events("datastore:")) - before, new, pct)
        sev = [e.severity for e in self.events("datastore:")]
        self.assertEqual(sev, ["warning", "critical", "warning", "ok"])

    def test_old_snapshot_alerts_once_and_clears(self):
        now = timezone.now()
        prov = FakeProvider({"vm-101": {"snapshots": [snap("s1", "pre-upgrade", 10, now)]}})
        self.src.use_provider = True
        self.src.save()
        for _ in range(3):
            self.sync(provider_factory=provider_factory(prov))
        pk = self.vm().pk
        self.assertEqual([e.severity for e in self.events(f"vm:{pk}:snapshots")], ["warning"])
        self.assertEqual(VmSnapshot.objects.get(vm_id=pk).name, "pre-upgrade")
        prov.data["vm-101"]["snapshots"] = []
        self.sync(provider_factory=provider_factory(prov))
        self.assertEqual([e.severity for e in self.events(f"vm:{pk}:snapshots")], ["warning", "ok"])
        self.assertFalse(VmSnapshot.objects.filter(vm_id=pk).exists())

    def test_a_fresh_snapshot_does_not_alert(self):
        prov = FakeProvider({"vm-101": {"snapshots": [snap("s1", "new", 1)]}})
        self.src.use_provider = True
        self.src.save()
        self.sync(provider_factory=provider_factory(prov))
        self.assertEqual(self.events("snapshots"), [])

    def test_events_go_to_the_device_and_carry_its_site(self):
        dev = self.make_vm_device("app-01", site="Mumbai")
        self.sync()
        self.fake.inv["vms"]["vm-101"]["power"] = "POWERED_OFF"
        self.sync()
        (ev,) = self.events("power")
        self.assertEqual((ev.object_type, ev.object_id, ev.site), ("network.device", str(dev.pk), "Mumbai"))

    def test_observed_events_are_recorded_for_host_moves_and_power(self):
        self.sync()
        self.fake.inv["vms"]["vm-101"]["host"] = "host-12"
        self.fake.inv["vms"]["vm-101"]["power"] = "POWERED_OFF"
        self.sync()
        kinds = set(VmEvent.objects.filter(vm=self.vm()).values_list("kind", flat=True))
        self.assertEqual(kinds, {"host", "power"})


class Failures(SyncCase):
    def test_login_failure_backoff_one_alert_and_recovery(self):
        self.fake.fail_login = True
        t = timezone.now()
        for i in range(5):
            self.sync(now=t + timedelta(minutes=10 * i))
        self.src.refresh_from_db()
        self.assertEqual((self.src.consecutive_failures, self.src.error_kind, self.src.state[0]), (5, "auth", "auth"))
        evs = self.events(f"source:{self.src.pk}")
        self.assertEqual([e.severity for e in evs], ["warning"])
        self.fake.fail_login = False
        self.sync(now=t + timedelta(minutes=60))
        self.src.refresh_from_db()
        self.assertEqual((self.src.consecutive_failures, self.src.last_error), (0, ""))
        self.assertEqual([e.severity for e in self.events(f"source:{self.src.pk}")], ["warning", "ok"])

    def test_due_backs_off_while_failing(self):
        now = timezone.now()
        self.src.last_sync_at = now
        self.assertFalse(sync.due(self.src, now + timedelta(seconds=100)))
        self.assertTrue(sync.due(self.src, now + timedelta(seconds=301)))
        self.src.consecutive_failures = 3
        self.assertFalse(sync.due(self.src, now + timedelta(seconds=1000)))
        self.assertTrue(sync.due(self.src, now + timedelta(seconds=2500)))
        self.src.sync_requested_at = now + timedelta(seconds=5)
        self.assertTrue(sync.due(self.src, now + timedelta(seconds=10)))
        self.src.enabled = False
        self.assertFalse(sync.due(self.src, now + timedelta(days=1)))

    def test_no_secret_in_logs_errors_audit_or_events_after_a_failed_login(self):
        self.src.refresh_from_db()
        from hypervisor import services
        services.store_login(None, self.src, "svc-linkedeye-ro", PASSWORD + "-wrong")
        with capture_logs() as buf:
            self.sync()
            self.sync()
        self.src.refresh_from_db()
        blob = " ".join([buf.getvalue(), self.src.last_error, self.src.error_kind,
                         " ".join(str(a.details) for a in AuditEvent.objects.all()), " ".join(f"{e.title}{e.message}{e.data}" for e in Event.objects.all())])
        self.assertNotIn(PASSWORD, blob)
        self.assertNotIn(PASSWORD + "-wrong", blob)
        self.assertEqual(self.src.state[0], "auth")

    def test_password_is_encrypted_at_rest(self):
        from hypervisor import services
        row = services.credential_row(self.src)
        self.assertNotIn(PASSWORD, row.secret)
        self.assertEqual(row.username, "svc-linkedeye-ro")
        self.assertEqual(row.kind, "basic_auth")

    def test_missing_vault_login_is_a_clear_failure(self):
        from onboarding.models import Credential
        Credential.objects.all().delete()
        self.sync()
        self.src.refresh_from_db()
        self.assertEqual(self.src.error_kind, "auth")
        self.assertIn("No login", self.src.last_error)

    def test_provider_failure_does_not_fail_the_sync(self):
        self.src.use_provider = True
        self.src.save()
        self.sync(provider_factory=provider_factory(FakeProvider(fail=providers.ProviderError(providers.INSTALL_HINT))))
        self.src.refresh_from_db()
        self.assertEqual(self.src.consecutive_failures, 0)
        self.assertIn("requirements-vsphere.txt", self.src.last_provider_error)
        self.assertEqual(VmInfo.objects.count(), 5)

    def test_provider_events_and_extras_are_stored(self):
        now = timezone.now()
        prov = FakeProvider({"vm-101": {"events": [{"key": "77", "ts": now, "severity": "warning", "kind": "VmFailedEvent", "message": "High memory\nusage"}],
                                        "extras": {"created_on": now - timedelta(days=90), "description": "app server"}}})
        self.src.use_provider = True
        self.src.save()
        self.sync(provider_factory=provider_factory(prov))
        self.sync(provider_factory=provider_factory(prov))
        vm = self.vm()
        self.assertEqual(VmEvent.objects.get(vm=vm, key="vc:77").message, "High memory usage")
        self.assertEqual(vm.description, "app server")
        self.assertIsNotNone(vm.created_on)
        self.assertTrue(prov.closed)


class Placement(SyncCase):
    def setUp(self):
        super().setUp()
        self.dev = self.make_vm_device("app-01", ip="10.20.1.15")

    def test_placement_is_created_from_the_sync(self):
        self.sync()
        p = VMPlacement.objects.get(vm=self.dev)
        self.assertEqual((p.vcpus, p.memory_gb, p.power_state, p.os, p.disk_gb), (4, Decimal("16.00"), "running", "Ubuntu Linux (64-bit)", Decimal("100.0")))
        self.assertEqual(p.cluster.name, "Compute-01")
        self.assertEqual(p.cluster.kind, "vmware")
        self.assertIsNone(p.host)       # the ESXi host is not a known physical-server device

    def test_the_host_follows_when_it_is_a_known_server(self):
        from network.models import Device
        host = Device.objects.create(tenant_key="default", name="esxi-host-01", device_type="physical_server", mgmt_ip="10.20.1.10")
        self.sync()
        self.assertEqual(VMPlacement.objects.get(vm=self.dev).host_id, host.pk)

    def test_values_the_sync_wrote_follow_vcenter_but_a_manual_edit_wins(self):
        self.sync()
        p = VMPlacement.objects.get(vm=self.dev)
        p.vcpus = 6                      # an operator's own value
        p.save()
        self.fake.inv["vms"]["vm-101"]["cpu"] = 8
        self.fake.inv["vms"]["vm-101"]["mem"] = 32768
        self.fake.inv["vms"]["vm-101"]["power"] = "POWERED_OFF"
        self.sync()
        self.sync()
        p.refresh_from_db()
        self.assertEqual(p.vcpus, 6)                            # kept
        self.assertEqual(p.memory_gb, Decimal("32.00"))          # never edited by hand: follows vCenter
        self.assertEqual(p.power_state, "stopped")

    def test_the_overwrite_flag_replaces_manual_values(self):
        self.sync()
        p = VMPlacement.objects.get(vm=self.dev)
        p.vcpus = 6
        p.save()
        self.fake.inv["vms"]["vm-101"]["cpu"] = 8
        with override_settings(HYPERVISOR_OVERWRITE_PLACEMENT=True):
            self.sync()
        p.refresh_from_db()
        self.assertEqual(p.vcpus, 8)

    def test_an_existing_cluster_of_another_kind_is_not_taken_over(self):
        Cluster.objects.create(tenant_key="default", name="Compute-01", kind="proxmox")
        self.sync()
        self.assertIsNone(VMPlacement.objects.get(vm=self.dev).cluster)
        self.assertEqual(Cluster.objects.get(name="Compute-01").kind, "proxmox")

    def test_unmatched_vms_get_no_placement(self):
        self.sync()
        self.assertEqual(VMPlacement.objects.count(), 1)


class Retention(SyncCase):
    def test_old_events_requests_and_long_missing_records_are_deleted(self):
        from hypervisor import services
        self.sync()
        vm = self.vm()
        VmEvent.objects.create(tenant_key="default", vm=vm, key="old", ts=self.ago(days=45), message="old")
        VmEvent.objects.create(tenant_key="default", vm=vm, key="new", ts=self.ago(days=2), message="new")
        stale = services.create_request(self.mum_op, vm, "reset")[0]
        stale.requested_at = self.ago(days=9)
        stale.save()
        VmInfo.objects.filter(moref="vm-104").update(missing_since=self.ago(days=40))
        Datastore.objects.filter(moref="datastore-22").update(missing_since=self.ago(days=40))
        out = sync.retention()
        self.assertFalse(VmEvent.objects.filter(key="old").exists())
        self.assertTrue(VmEvent.objects.filter(key="new").exists())
        self.assertFalse(VmInfo.objects.filter(moref="vm-104").exists())
        self.assertFalse(Datastore.objects.filter(moref="datastore-22").exists())
        stale.refresh_from_db()
        self.assertEqual(stale.state, "expired")
        self.assertGreaterEqual(out["missing"], 2)

    def test_event_cap_per_vm(self):
        self.sync()
        vm = self.vm()
        VmEvent.objects.bulk_create([VmEvent(tenant_key="default", vm=vm, key=f"k{i}", ts=timezone.now() - timedelta(minutes=i), message="m")
                                     for i in range(sync.MAX_EVENTS_PER_VM + 25)])
        sync.retention()
        self.assertEqual(VmEvent.objects.filter(vm=vm).count(), sync.MAX_EVENTS_PER_VM)

    def test_job_functions_never_raise(self):
        from hypervisor import jobs
        with mock.patch.object(sync, "sync_due", side_effect=RuntimeError("x")), mock.patch.object(sync, "retention", side_effect=RuntimeError("x")):
            self.assertEqual(jobs.sync(), "error")
            self.assertEqual(jobs.retention(), "error")

    def test_job_registration(self):
        from core.jobs import JOBS
        self.assertEqual(JOBS["hypervisor.sync"]["interval"], 60)
        self.assertIn("hypervisor.retention", JOBS)

    def test_sync_due_picks_only_due_enabled_sources(self):
        other = self.make_source(self.fake, name="vc-off", enabled=False)
        out = sync.sync_due()
        self.assertIn(self.src.name, out)
        self.assertNotIn("vc-off", out)
        self.assertEqual(sync.sync_due(), "nothing due")
