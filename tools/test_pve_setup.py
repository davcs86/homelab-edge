"""Offline tests for pve-setup.py: dry-run output only; no Proxmox host, no root, nothing is executed."""
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

spec = importlib.util.spec_from_file_location("pve_setup", Path(__file__).with_name("pve-setup.py"))
pve = importlib.util.module_from_spec(spec)
sys.modules["pve_setup"] = pve
spec.loader.exec_module(pve)
runner = CliRunner()

RESOURCES = [{"vmid": 100, "name": "tpl", "template": 1}, {"vmid": 101, "name": "old"}]


def fake_pvesh(path, *args):
    if path == "/cluster/resources":
        return RESOURCES
    if path == "/cluster/nextid":
        return "102"
    if path.endswith("/storage"):
        return [{"storage": "local-lvm", "type": "lvmthin", "avail": 50 * 2**30}]
    if path.endswith("/network"):
        return [{"iface": "vmbr0", "cidr": "192.168.1.2/24"}]
    raise AssertionError(path)


class DryRun(unittest.TestCase):
    def invoke(self, args, text):
        with mock.patch.object(pve, "pvesh", fake_pvesh), mock.patch.object(pve, "node", lambda: "pve"), \
                mock.patch("subprocess.run") as run:
            r = runner.invoke(pve.app, ["--dry-run", *args], input=text)
            run.assert_not_called()
        return r

    def test_join_hides_the_key(self):
        r = self.invoke(["tailscale-join"], "https://hs.example.test\nxstock\ny\nSECRET-KEY-VALUE\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("SECRET-KEY-VALUE", r.output)
        self.assertIn("--auth-key file:/run/pve-setup-authkey", r.output)

    def test_heartbeat_hides_the_token_and_uses_a_curl_config(self):
        r = self.invoke(["heartbeat"], "status.ts.example.test\n\n60\nTOKEN-VALUE\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("TOKEN-VALUE", r.output)
        self.assertIn("-K /etc/edge-heartbeat.curl https://status.ts.example.test/api/v1/endpoints/home_proxmox-heartbeat/external", r.output)
        self.assertIn("mode 0600", r.output)

    def test_vm_create_rejects_a_used_id_then_accepts_a_free_one(self):
        r = self.invoke(["vm-create"], "1\n101\n102\nweb\n1\n\n\n\ny\n\ny\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("101 is in use", r.output)
        self.assertIn("qm clone 100 102 --name web", r.output)

    def test_bad_name_is_re_asked(self):
        r = self.invoke(["vm-create"], "1\n\nBad_Name\nweb\n1\n\n\n\ny\n\ny\n")
        self.assertIn("lowercase letters", r.output)

    def test_sha256_is_required_to_be_hex(self):
        self.assertEqual(pve.sha256_of(__file__) == pve.sha256_of(__file__), True)
        with mock.patch("typer.prompt", side_effect=["nothex", "a" * 64]):
            self.assertEqual(pve.ask_sha256(), "a" * 64)

    def test_fetch_checksum_mismatch_fails(self):
        with mock.patch("urllib.request.urlretrieve"), mock.patch.object(pve, "sha256_of", return_value="b" * 64):
            self.assertFalse(pve.run_step(pve.Fetch("https://x.test/i", "/nonexistent/i", "a" * 64)))


if __name__ == "__main__":
    unittest.main()
