#!/usr/bin/env python3
"""Fails when the roles read a key that the committed secrets/edge.sops.yaml does not hold (the cause of deploy #11)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import secret_keys  # noqa: E402


def tree(files):
    d = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return d


class Extraction(unittest.TestCase):
    def test_dotted_subscript_and_loop_forms(self):
        d = tree({
            "roles/a/tasks/main.yml": "x: '{{ edge_secrets.alpha_token }}'\ny: \"{{ edge_secrets['beta_key'] }}\"\nz: {{ edge_secrets[ \"gamma\" ] }}\n",
            "roles/b/tasks/main.yml": "c: '{{ edge_secrets[item.key] }}'\nloop:\n  - {name: ca.crt, key: pki_ca_crt, mode: '0644'}\n  - {name: s.key, key: pki_server_key}\n",
            "playbooks/p.yml": "- community.sops.load_vars:\n    name: edge_secrets\n",
            "roles/c/templates/t.j2": "{{ edge_secrets.from_template }}",
        })
        self.assertEqual(secret_keys.used_keys(d), {"alpha_token", "beta_key", "gamma", "pki_ca_crt", "pki_server_key", "from_template"})

    def test_password_keys_named_in_group_vars_and_roles(self):
        d = tree({
            "inventories/prod/group_vars/all/main.yml": "w:\n  - {role: a, password_key: db_role_a_password}\n",
            "roles/x/tasks/main.yml": "l: \"{{ [{'role': 'admin', 'password_key': 'db_role_admin_password'}] }}\"\n",
        })
        self.assertEqual(secret_keys.used_keys(d), {"db_role_a_password", "db_role_admin_password"})

    def test_gate_keys_named_in_the_inventory(self):
        d = tree({"inventories/prod/hosts.yml": "h:\n  gate_sops_key: app_gate_x_staging_public_key\n"})
        self.assertEqual(secret_keys.used_keys(d), {"app_gate_x_staging_public_key"})

    def test_key_entries_in_files_that_do_not_index_by_item_key_are_ignored(self):
        d = tree({"roles/a/tasks/main.yml": "- {name: x, key: unrelated_thing}\nvalue: '{{ edge_secrets.real }}'\n"})
        self.assertEqual(secret_keys.used_keys(d), {"real"})

    def test_other_file_types_are_ignored(self):
        d = tree({"roles/a/README.md": "edge_secrets.documented_only", "roles/a/tasks/main.yml": "x: 1\n"})
        self.assertEqual(secret_keys.used_keys(d), set())


class Repository(unittest.TestCase):
    def test_the_extractor_still_sees_every_form_in_the_real_roles(self):
        used = secret_keys.used_keys()
        self.assertIn("gatus_heartbeat_db_token", used)   # dotted
        self.assertTrue({"pgbackrest_tls_ca_crt", "pgbackrest_tls_server_crt", "pgbackrest_tls_server_key"} <= used)  # edge_secrets[item.key]
        self.assertIn("db_role_xstockstrat_production_password", used)  # password_key in group_vars

    def test_every_key_the_roles_read_is_in_the_committed_file(self):
        missing = secret_keys.used_keys() - secret_keys.file_keys()
        self.assertEqual(missing, set(),
                         f"secrets/edge.sops.yaml lacks {sorted(missing)}: run Actions -> init-secrets with fill_missing "
                         "(generated credentials) or add the operator value by hand; see secrets/README.md")

    def test_a_file_without_the_db_token_is_reported(self):
        """The failure this check exists for: the file from before PR #20."""
        old = tree({"secrets/edge.sops.yaml": "".join(f"{k}: ENC[x]\n" for k in sorted(secret_keys.file_keys() - {"gatus_heartbeat_db_token"})) + "sops: {}\n"})
        self.assertIn("gatus_heartbeat_db_token", secret_keys.used_keys() - secret_keys.file_keys(old / "secrets/edge.sops.yaml"))


if __name__ == "__main__":
    unittest.main()
