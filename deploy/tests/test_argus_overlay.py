"""The argus overlay (deploy/kubernetes/overlays/argus) must stay consistent with the base manifests it patches."""
import json
import pathlib
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASE = ROOT / "deploy" / "kubernetes"
OVERLAY = BASE / "overlays" / "argus"
HOST = "argus.monitoring.wecrew.in"


def docs(path):
    return [d for d in yaml.safe_load_all(path.read_text()) if d]


class ArgusOverlayTests(unittest.TestCase):
    def test_every_file_parses(self):
        for f in OVERLAY.glob("*.yaml"):
            self.assertTrue(docs(f), f.name)
        json.loads((OVERLAY / "ingress-host-patch.json").read_text())

    def test_kustomization_points_at_the_base_and_patches_both_ingresses(self):
        k = docs(OVERLAY / "kustomization.yaml")[0]
        self.assertEqual(k["resources"], ["../../"])
        names = sorted(p["target"]["name"] for p in k["patches"] if "target" in p)
        self.assertEqual(names, ["linkedeye", "linkedeye-datasheet-upload"])
        for p in k["patches"]:
            self.assertTrue((OVERLAY / p["path"]).exists(), p["path"])

    def test_ingress_patch_paths_exist_in_the_base_and_set_the_host(self):
        patch = json.loads((OVERLAY / "ingress-host-patch.json").read_text())
        base = [d for d in docs(BASE / "ingress.yaml") if d["kind"] == "Ingress"]
        self.assertEqual(len(base), 2)
        for ing in base:
            self.assertEqual(ing["spec"]["tls"][0]["hosts"][0], "linkedeye.example.com")
            self.assertEqual(ing["spec"]["rules"][0]["host"], "linkedeye.example.com")
        paths = {op["path"] for op in patch}
        self.assertEqual(paths, {"/spec/tls/0/hosts/0", "/spec/rules/0/host"})
        for op in patch:
            self.assertEqual((op["op"], op["value"]), ("replace", HOST))

    def test_configmap_patch_names_the_host_consistently(self):
        cm = docs(OVERLAY / "configmap-patch.yaml")[0]
        self.assertEqual(cm["metadata"]["name"], "linkedeye-config")
        self.assertEqual(cm["metadata"]["namespace"], "fs-linkedeye")
        d = cm["data"]
        self.assertIn(HOST, d["LINKEDEYE_ALLOWED_HOSTS"].split(","))
        self.assertIn("linkedeye-web.fs-linkedeye.svc", d["LINKEDEYE_ALLOWED_HOSTS"])
        self.assertEqual(d["LINKEDEYE_CSRF_TRUSTED_ORIGINS"], "https://" + HOST)
        # the pods must refuse to start until the operator fills this in
        self.assertIn("<REPLACE", d["LINKEDEYE_TRUSTED_PROXIES"])

    def test_no_private_probe_range_is_pre_listed(self):
        # Every listed range can be reached by whoever may configure a probe: the operator lists narrow subnets deliberately.
        # (The finspot-dev overlay lists /16 ranges that include the cluster API-server address; this one must not copy them.)
        value = docs(OVERLAY / "configmap-patch.yaml")[0]["data"]["LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS"]
        self.assertEqual(value, "")

    def test_guide_tells_the_operator_to_restart_pods_after_a_settings_change(self):
        text = (ROOT / "docs" / "ARGUS_K8S.md").read_text()
        self.assertIn("rollout restart deploy/linkedeye-web deploy/linkedeye-probes", text)
        self.assertIn("never a whole `/16`", text)

    def test_no_secret_value_is_committed(self):
        for f in OVERLAY.iterdir():
            text = f.read_text()
            self.assertNotIn("BEGIN PRIVATE KEY", text, f.name)
            self.assertNotIn("kind: Secret", text, f.name)

    def test_optional_postgres_matches_what_the_base_expects(self):
        ds = docs(OVERLAY / "postgres.yaml")
        svc = next(d for d in ds if d["kind"] == "Service")
        sts = next(d for d in ds if d["kind"] == "StatefulSet")
        self.assertEqual(svc["metadata"]["name"], "postgres")          # LINKEDEYE_DB_HOST=postgres.fs-linkedeye.svc...
        label = "app.kubernetes.io/name"
        self.assertEqual(sts["spec"]["template"]["metadata"]["labels"][label], "linkedeye-postgres")   # NetworkPolicy
        np = [d for d in docs(BASE / "networkpolicy.yaml") if d["metadata"]["name"] == "linkedeye-db-ingress"][0]
        self.assertEqual(np["spec"]["podSelector"]["matchLabels"][label], "linkedeye-postgres")
        pod = sts["spec"]["template"]["spec"]
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        c = pod["containers"][0]
        self.assertFalse(c["securityContext"]["allowPrivilegeEscalation"])
        secret_ref = next(e for e in c["env"] if e["name"] == "POSTGRES_PASSWORD")["valueFrom"]["secretKeyRef"]
        self.assertEqual((secret_ref["name"], secret_ref["key"]), ("linkedeye-secrets", "LINKEDEYE_DB_PASSWORD"))

    def test_secret_script_is_executable_shell_and_never_echoes_keys(self):
        script = OVERLAY / "create-secrets.sh"
        text = script.read_text()
        self.assertTrue(text.startswith("#!/usr/bin/env bash"))
        self.assertIn("set -euo pipefail", text)
        self.assertIn("already exists", text)           # never overwrites an existing Secret
        self.assertNotIn("set -x", text)
        self.assertTrue(script.stat().st_mode & 0o111)


if __name__ == "__main__":
    unittest.main()
