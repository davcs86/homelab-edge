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
import secret_keys  # noqa: E402

PEM = "-----BEGIN CERTIFICATE-----\nAAAA\nBBBB\n-----END CERTIFICATE-----\n"
OLD = {  # what the 2026-10-15 file held: no gatus_heartbeat_db_token
    "desec_token": "desec-OLD", "healthchecks_ping_url": "https://hc.example.test/ping/OLD",
    "admin_console_password_hash": "$6$salt$hash", "ntfy_david_password_bcrypt": "$2b$10$david",
    "ntfy_alerts_password_bcrypt": "$2b$10$alerts", "ntfy_alerts_token": "tk_oldoldoldoldoldoldoldoldold",
    "gatus_heartbeat_pve_token": "pve-OLD", "gatus_heartbeat_backup_token": "backup-OLD",
    **{k: PEM for k in init_secrets.PKI_KEYS},
}
OLD = {**OLD, "db_role_admin_password": "adm-OLD", "db_role_superadmin_password": "sup-OLD", "gatus_heartbeat_pgbackrest_token": "pgbr-OLD", "db_role_xstockstrat_production_password": "prod-OLD",
       "db_role_xstockstrat_staging_password": "stg-OLD", **{k: PEM for k in init_secrets.DB_TLS_KEYS + init_secrets.PGBACKREST_DB_KEYS}}


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

    def openssl(self, *args, stdin=None):
        return subprocess.run(["openssl", *args], input=stdin, capture_output=True, text=True, check=True).stdout

    def verify(self, ca, leaf):
        d = Path(tempfile.mkdtemp())
        (d / "ca.crt").write_text(ca)
        (d / "leaf.crt").write_text(leaf)
        return subprocess.run(["openssl", "verify", "-CAfile", str(d / "ca.crt"), str(d / "leaf.crt")], capture_output=True, text=True).returncode == 0

    def test_a_file_from_before_the_database_host_gets_passwords_and_both_certificate_sets(self):
        base = init_secrets.pki("edge.ts.example.test", "db-pgbackrest")
        old = {k: v for k, v in OLD.items() if k not in init_secrets.DB_TLS_KEYS + init_secrets.PGBACKREST_DB_KEYS
               and not k.startswith("db_role_") and k != "gatus_heartbeat_pgbackrest_token"}
        self.write({**old, **base})
        r = self.fill()
        self.assertEqual(r.returncode, 0, r.stderr)
        got = self.read()
        self.assertEqual({k: got[k] for k in old | base.keys()}, {**old, **base})  # nothing existing changed
        self.assertEqual(set(got) - set(old) - set(base),
                         {"gatus_heartbeat_db_token", "gatus_heartbeat_pgbackrest_token", "db_role_admin_password", "db_role_superadmin_password", "db_role_xstockstrat_production_password", "db_role_xstockstrat_staging_password",
                          *init_secrets.DB_TLS_KEYS, *init_secrets.PGBACKREST_DB_KEYS})
        host = init_secrets.db_host()
        self.assertEqual(host, "db.ts.natanddavehouse.com")
        # the database TLS CA is its own: it signs the server cert and is not the pgBackRest CA
        self.assertTrue(self.verify(got["db_tls_ca_crt"], got["db_tls_server_crt"]))
        self.assertFalse(self.verify(base["pgbackrest_tls_ca_crt"], got["db_tls_server_crt"]))
        self.assertNotEqual(got["db_tls_ca_crt"], base["pgbackrest_tls_ca_crt"])
        # the pull direction: a server cert for the database host and a client cert for the edge, from the pgBackRest CA
        self.assertTrue(self.verify(base["pgbackrest_tls_ca_crt"], got["pgbackrest_db_server_crt"]))
        self.assertTrue(self.verify(base["pgbackrest_tls_ca_crt"], got["pgbackrest_edge_client_crt"]))
        server = self.openssl("x509", "-noout", "-text", stdin=got["pgbackrest_db_server_crt"])
        self.assertIn(f"DNS:{host}", server)
        self.assertIn("TLS Web Server Authentication", server)
        client = self.openssl("x509", "-noout", "-subject", "-ext", "extendedKeyUsage", stdin=got["pgbackrest_edge_client_crt"])
        self.assertIn("CN=edge-pgbackrest", client.replace(" = ", "="))
        self.assertIn("TLS Web Client Authentication", client)
        self.assertNotIn("PRIVATE KEY", r.stdout + r.stderr)

    def test_leaf_serials_are_distinct(self):
        base = init_secrets.pki("edge.ts.example.test", "db-pgbackrest")
        a = init_secrets.pgbackrest_db_pki(base["pgbackrest_tls_ca_crt"], base["pgbackrest_tls_ca_key"])
        serials = [self.openssl("x509", "-noout", "-serial", stdin=a[k]) for k in ("pgbackrest_db_server_crt", "pgbackrest_edge_client_crt")]
        self.assertEqual(len(set(serials)), 2)

    def test_part_of_a_certificate_set_is_refused_and_the_file_is_left_alone(self):
        for group in (init_secrets.DB_TLS_KEYS, init_secrets.PGBACKREST_DB_KEYS):
            self.write({k: v for k, v in OLD.items() if k != group[0]})
            before = (self.tmp / self.file).read_bytes()
            r = self.fill()
            self.assertNotEqual(r.returncode, 0, group)
            self.assertIn("only part of the set", r.stderr)
            self.assertEqual((self.tmp / self.file).read_bytes(), before)

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

    def test_the_full_generator_writes_every_certificate_set(self):
        src = SCRIPT.read_text()
        self.assertIn("**db_tls_pki()", src)
        self.assertIn("**pgbackrest_db_pki(", src)

    def test_every_secret_the_roles_use_is_generated_or_known(self):
        known = (set(init_secrets.GENERATED) | set(init_secrets.OPERATOR_KEYS) | set(init_secrets.PKI_KEYS)
                 | set(init_secrets.DB_TLS_KEYS) | set(init_secrets.PGBACKREST_DB_KEYS))
        used = secret_keys.used_keys()
        self.assertTrue(used)
        self.assertEqual(used - known, set())


if __name__ == "__main__":
    unittest.main()
