#!/usr/bin/env python3
"""Runs scripts/init_secrets.py --fill-missing against a throwaway SOPS file (real sops, throwaway ssh-ed25519 key)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/init_secrets.py"
sys.path.insert(0, str(ROOT / "scripts"))
import init_secrets  # noqa: E402

PEM = "-----BEGIN CERTIFICATE-----\nAAAA\nBBBB\n-----END CERTIFICATE-----\n"
OLD = {  # what the 2026-10-15 file held: no gatus_heartbeat_db_token
    "desec_token": "desec-OLD", "healthchecks_ping_url": "https://hc.example.test/ping/OLD",
    "admin_console_password_hash": "$6$salt$hash", "ntfy_david_password_bcrypt": "$2b$10$david",
    "ntfy_alerts_password_bcrypt": "$2b$10$alerts", "ntfy_alerts_token": "tk_oldoldoldoldoldoldoldoldold",
    "gatus_heartbeat_pve_token": "pve-OLD", "gatus_heartbeat_backup_token": "backup-OLD",
    **{k: PEM for k in init_secrets.PKI_KEYS},
}


@unittest.skipUnless(shutil.which("sops") and shutil.which("ssh-keygen"), "sops and ssh-keygen are required")
class FillMissing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        key = self.tmp / "key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        (self.tmp / ".sops.yaml").write_text(yaml.safe_dump({"creation_rules": [
            {"path_regex": r"secrets/.*\.sops\.yaml$", "age": (self.tmp / "key.pub").read_text().strip()}]}))
        (self.tmp / "secrets").mkdir()
        self.env = {**os.environ, "SOPS_AGE_SSH_PRIVATE_KEY_FILE": str(key)}
        self.file = "secrets/edge.sops.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def sops(self, *args, stdin=None):
        return subprocess.run(["sops", *args], cwd=self.tmp, env=self.env, input=stdin, capture_output=True, text=True, check=True).stdout

    def write(self, doc):
        (self.tmp / self.file).write_text(self.sops("encrypt", "--input-type", "yaml", "--output-type", "yaml", "--filename-override", self.file,
                                                    "/dev/stdin", stdin=yaml.safe_dump(doc, sort_keys=True)))

    def read(self):
        return yaml.safe_load(self.sops("decrypt", "--output-type", "yaml", self.file))

    def fill(self):
        return subprocess.run([sys.executable, str(SCRIPT), "--fill-missing", self.file], cwd=self.tmp, env=self.env, capture_output=True, text=True, check=False)

    def test_adds_only_the_missing_token_and_changes_nothing_else(self):
        self.write(OLD)
        r = self.fill()
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.read()
        self.assertEqual({k: v for k, v in got.items() if k in OLD}, OLD)
        self.assertEqual(set(got) - set(OLD), {"gatus_heartbeat_db_token"})
        self.assertRegex(got["gatus_heartbeat_db_token"], r"^[A-Za-z0-9_-]{43}$")
        self.assertIn("added gatus_heartbeat_db_token", r.stdout)

    def test_the_new_value_is_never_printed_or_left_in_plaintext(self):
        self.write(OLD)
        r = self.fill()
        token = self.read()["gatus_heartbeat_db_token"]
        self.assertNotIn(token, r.stdout + r.stderr)
        self.assertNotIn(token, (self.tmp / self.file).read_text())
        self.assertEqual(sorted(p.name for p in (self.tmp / "secrets").iterdir()), ["edge.sops.yaml"])

    def test_a_second_run_does_nothing_and_does_not_rewrite_the_file(self):
        self.write(OLD)
        self.fill()
        before = (self.tmp / self.file).read_bytes()
        r = self.fill()
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "nothing to add"))
        self.assertEqual((self.tmp / self.file).read_bytes(), before)

    def test_several_missing_credentials_are_all_generated_with_the_right_shape(self):
        self.write({k: v for k, v in OLD.items() if k not in ("ntfy_alerts_token", "ntfy_alerts_password_bcrypt", "gatus_heartbeat_pve_token")})
        r = self.fill()
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.read()
        self.assertRegex(got["ntfy_alerts_token"], r"^tk_[a-z0-9]{29}$")
        self.assertTrue(got["ntfy_alerts_password_bcrypt"].startswith("$2b$10$"))
        self.assertRegex(got["gatus_heartbeat_pve_token"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(got["gatus_heartbeat_backup_token"], "backup-OLD")

    def test_refuses_when_an_operator_value_or_the_pki_is_missing_and_leaves_the_file_alone(self):
        for dropped in ("desec_token", "pgbackrest_tls_client_key"):
            self.write({k: v for k, v in OLD.items() if k != dropped})
            before = (self.tmp / self.file).read_bytes()
            r = self.fill()
            self.assertNotEqual(r.returncode, 0, dropped)
            self.assertIn(dropped, r.stderr)
            self.assertEqual((self.tmp / self.file).read_bytes(), before, dropped)

    def test_a_file_sops_cannot_decrypt_fails_without_writing(self):
        self.write(OLD)
        self.env["SOPS_AGE_SSH_PRIVATE_KEY_FILE"] = str(self.tmp / "key.pub")  # wrong key
        before = (self.tmp / self.file).read_bytes()
        self.assertNotEqual(self.fill().returncode, 0)
        self.assertEqual((self.tmp / self.file).read_bytes(), before)


class RequireSops(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("INIT_SECRETS_TEST_REQUIRE_SOPS"), "only enforced in CI")
    def test_ci_has_sops_so_the_tests_above_cannot_skip(self):
        self.assertTrue(shutil.which("sops") and shutil.which("ssh-keygen"))


class Registry(unittest.TestCase):
    def test_the_full_generator_and_the_fill_mode_agree_on_the_key_set(self):
        src = SCRIPT.read_text()
        full = set(re.findall(r'^        "([a-z_]+)": ', src, re.M))  # operator keys written in main()
        self.assertEqual(full, set(init_secrets.OPERATOR_KEYS))
        self.assertEqual(set(init_secrets.GENERATED) & set(init_secrets.OPERATOR_KEYS + init_secrets.PKI_KEYS), set())

    def test_every_secret_the_roles_use_is_generated_or_known(self):
        known = set(init_secrets.GENERATED) | set(init_secrets.OPERATOR_KEYS) | set(init_secrets.PKI_KEYS)
        used = set()
        for f in list((ROOT / "roles").rglob("*")) + list((ROOT / "playbooks").rglob("*")):
            if f.is_file() and f.suffix in (".yml", ".yaml", ".j2"):
                used |= set(re.findall(r"edge_secrets\.([a-z_0-9]+)", f.read_text()))
        self.assertTrue(used)
        self.assertEqual(used - known, set())


if __name__ == "__main__":
    unittest.main()
