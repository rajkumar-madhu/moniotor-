"""The optional pyvmomi provider: read-only by construction, tolerant, and never required."""
import ast
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

from django.test import SimpleTestCase

from hypervisor import providers, pyvmomi_provider
from hypervisor.providers import ProviderError, clean_event, clean_snapshot

FORBIDDEN = ("Power", "Reconfig", "Create", "Remove", "Revert", "Clone", "Migrate", "Relocate", "Destroy", "Rename", "Unregister", "Suspend",
             "Reset", "Shutdown", "Reboot", "Standby", "Mark", "Upgrade", "Mount", "Customize", "Delete", "Set", "Update", "Write", "Put")
ALLOWED_CALLS = {"ProviderError", "RetrieveContent", "QueryEvents", "VirtualMachine", "EventFilterSpec", "ByEntity", "ByTime", "SmartConnect", "Disconnect",
                 "_disconnect", "create_default_context", "_create_unverified_context", "_resolve", "fetch_fingerprint", "normalise_fingerprint"}


class ReadOnly(SimpleTestCase):
    def tree(self):
        return ast.parse(Path(pyvmomi_provider.__file__).read_text())

    def test_only_read_calls_into_pyvmomi(self):
        names = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Call):
                f = node.func
                names.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        pyvmomi_calls = {n for n in names if n[:1].isupper() or n.startswith("_dis")}
        self.assertLessEqual(pyvmomi_calls, ALLOWED_CALLS, pyvmomi_calls - ALLOWED_CALLS)

    def test_no_task_or_write_method_name_appears_anywhere(self):
        idents = set()
        for node in ast.walk(self.tree()):
            if isinstance(node, ast.Attribute):
                idents.add(node.attr)
            elif isinstance(node, ast.Name):
                idents.add(node.id)
        bad = {i for i in idents for w in FORBIDDEN if w in i and i not in ("ByTime",) and not i.startswith("_")}
        self.assertEqual(bad, set())


class Pure(SimpleTestCase):
    def test_flatten_snapshots_walks_children_and_marks_current(self):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        child = NS(id=2, name="child", description="d", createTime=t, snapshot=NS(_moId="snapshot-2"), childSnapshotList=[])
        root = NS(id=1, name="root", description="", createTime=t, snapshot=NS(_moId="snapshot-1"), childSnapshotList=[child])
        out = pyvmomi_provider.flatten_snapshots([root], "snapshot-2")
        self.assertEqual([(s["snap_id"], s["is_current"]) for s in out], [("1", False), ("2", True)])

    def test_snapshot_sizes_sum_the_files_of_a_snapshot(self):
        layout = NS(file=[NS(key=1, size=100), NS(key=2, size=50), NS(key=3, size=7)],
                    snapshot=[NS(key=NS(_moId="snapshot-1"), dataKey=1, memoryKey=-1, disk=[NS(chain=[NS(fileKey=[2])])])])
        self.assertEqual(pyvmomi_provider.snapshot_sizes(layout), {"snapshot-1": 150})

    def test_event_severity_from_the_class_name(self):
        Failed, Warn, Plain = type("VmFailedToPowerOnEvent", (), {}), type("AlarmStatusChangedEvent", (), {}), type("VmPoweredOnEvent", (), {})
        self.assertEqual([pyvmomi_provider.event_severity(c()) for c in (Failed, Warn, Plain)], ["error", "warning", "info"])

    def test_clean_snapshot_and_event_reject_unusable_rows_and_hostile_text(self):
        now = datetime.now(timezone.utc)
        self.assertIsNone(clean_snapshot({"snap_id": "", "created_at": now}))
        self.assertIsNone(clean_snapshot({"snap_id": "1", "created_at": "yesterday"}))
        s = clean_snapshot({"snap_id": "1", "name": "n\x1b[0m\nx", "created_at": datetime(2026, 1, 1), "size_bytes": -5})
        self.assertEqual((s["name"], s["size_bytes"], s["created_at"].tzinfo is not None), ("n[0m x", None, True))
        self.assertIsNone(clean_event({"key": "1", "ts": now, "message": ""}))
        self.assertEqual(clean_event({"key": "1", "ts": now, "message": "x", "severity": "fatal"})["severity"], "info")

    def test_get_provider_is_none_unless_asked_for_and_explains_a_missing_package(self):
        off = NS(use_provider=False)
        self.assertIsNone(providers.get_provider(off, lambda: ("u", "p")))
        with mock.patch.object(providers, "pyvmomi_available", return_value=False):
            with self.assertRaises(ProviderError) as cm:
                providers.get_provider(NS(use_provider=True), lambda: ("u", "p"))
        self.assertIn("requirements-vsphere.txt", str(cm.exception))


class FakePyvmomi(SimpleTestCase):
    """Drive PyvmomiProvider.fetch with stand-in pyVim / pyVmomi modules (the real package is optional and not installed in CI)."""

    def modules(self, vm_obj, events):
        vim = types.ModuleType("pyVmomi.vim")
        vim.VirtualMachine = lambda ref, stub: vm_obj
        spec = type("EventFilterSpec", (), {"__init__": lambda self, **kw: self.__dict__.update(kw)})
        spec.ByEntity = lambda **kw: NS(**kw)
        spec.ByTime = lambda **kw: NS(**kw)
        vim.event = NS(EventFilterSpec=spec)
        pyvmomi = types.ModuleType("pyVmomi")
        pyvmomi.vim = vim
        content = NS(eventManager=NS(QueryEvents=lambda s: events))
        si = NS(RetrieveContent=lambda: content, _stub=object())
        connect = types.ModuleType("pyVim.connect")
        connect.SmartConnect = lambda **kw: si
        connect.Disconnect = lambda si: None
        pyvim = types.ModuleType("pyVim")
        pyvim.connect = connect
        return {"pyVmomi": pyvmomi, "pyVim": pyvim, "pyVim.connect": connect}

    def test_fetch_reads_snapshots_events_and_extras(self):
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        node = NS(id=7, name="pre-upgrade", description="d", createTime=t, snapshot=NS(_moId="snapshot-7"), childSnapshotList=[])
        vm = NS(snapshot=NS(rootSnapshotList=[node], currentSnapshot=NS(_moId="snapshot-7")), config=NS(createDate=t, annotation="notes\n"),
                layoutEx=NS(file=[NS(key=1, size=2048)], snapshot=[NS(key=NS(_moId="snapshot-7"), dataKey=1, memoryKey=-1, disk=[])]))
        ev = type("VmPoweredOnEvent", (), {})()
        ev.key, ev.createdTime, ev.fullFormattedMessage = 99, t, "VM powered on"
        with mock.patch.dict(sys.modules, self.modules(vm, [ev])), mock.patch.object(pyvmomi_provider.vsphere, "_resolve", return_value="127.0.0.1"):
            p = pyvmomi_provider.PyvmomiProvider("vc.example.test", 443, "u", "p", "skip")
            out = p.fetch(["vm-1"], datetime.now(timezone.utc) - timedelta(days=1))
            p.close()
        (s,) = out["snapshots"]["vm-1"]
        self.assertEqual((s["name"], s["size_bytes"], s["is_current"]), ("pre-upgrade", 2048, True))
        self.assertEqual(out["events"]["vm-1"][0]["message"], "VM powered on")
        self.assertEqual(out["extras"]["vm-1"], {"created_on": t, "description": "notes"})

    def test_an_unreadable_vm_is_skipped_not_fatal(self):
        class Boom:
            @property
            def snapshot(self):
                raise RuntimeError("no")
        with mock.patch.dict(sys.modules, self.modules(Boom(), [])), mock.patch.object(pyvmomi_provider.vsphere, "_resolve", return_value="127.0.0.1"):
            p = pyvmomi_provider.PyvmomiProvider("vc.example.test", 443, "u", "p", "skip")
            self.assertEqual(p.fetch(["vm-1"], datetime.now(timezone.utc))["snapshots"], {})

    def test_login_failure_never_echoes_the_password(self):
        mods = self.modules(NS(), [])
        def fail(**kw):
            raise RuntimeError("bad credentials for Sup3r-Secret")
        mods["pyVim.connect"].SmartConnect = fail
        with mock.patch.dict(sys.modules, mods), mock.patch.object(pyvmomi_provider.vsphere, "_resolve", return_value="127.0.0.1"):
            with self.assertRaises(ProviderError) as cm:
                pyvmomi_provider.PyvmomiProvider("vc.example.test", 443, "user1", "Sup3r-Secret", "skip")
        self.assertNotIn("Sup3r-Secret", str(cm.exception))
