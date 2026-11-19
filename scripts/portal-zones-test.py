#!/usr/bin/env python3
"""Checks the *.public / *.private zone names, their first tenants, and the tailnet-side wiring of the private zone."""
import re
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
VARS_DIR = ROOT / "inventories/prod/group_vars/all"


def load_vars():
    raw = {}
    for f in sorted(VARS_DIR.glob("*.yml")):
        raw.update(yaml.safe_load(f.read_text()) or {})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    resolved = dict(raw)
    for _ in range(10):  # values reference each other ("xstockstrat.{{ domain }}")
        nxt = {k: env.from_string(v).render(**resolved) if isinstance(v, str) else v for k, v in resolved.items()}
        if nxt == resolved:
            break
        resolved = nxt
    return resolved


V = load_vars()


def render(path):
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string((ROOT / path).read_text()).render(**V, ansible_managed="test")


def resolved_sites():
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return [{k: env.from_string(v).render(**V) if isinstance(v, str) else v for k, v in s.items()} for s in V["edge_sites"]]


class PortalZones(unittest.TestCase):
    def test_zone_and_tenant_names(self):
        d = V["domain"]
        self.assertEqual((V["public_zone"], V["private_zone"]), (f"public.{d}", f"private.{d}"))
        self.assertEqual(V["commsgun_ui_private_hostname"], f"commsgun.private.{d}")
        self.assertEqual(V["staging_portal_hostname"], f"xstockstrat-staging.public.{d}")

    def test_staging_portal_is_a_public_zone_twin_of_staging(self):
        sites = {s["name"]: s for s in resolved_sites()}
        old, new = sites["xstockstrat-staging"], sites["xstockstrat-staging-portal"]
        self.assertEqual(new["zone"], "public")
        self.assertEqual(new["hostname"], V["staging_portal_hostname"])
        self.assertNotIn("zone", old)
        for k in ("kind", "upstream", "ui_public", "isolate_staging_vm"):
            self.assertEqual(new[k], old[k], k)
        self.assertEqual([s["name"] for s in V["edge_sites"] if s.get("zone")], ["xstockstrat-staging-portal"])

    def test_existing_names_are_unchanged(self):
        d = V["domain"]
        self.assertEqual(V["staging_public_hostname"], f"xstockstrat-staging.{d}")
        self.assertEqual(V["commsgun_ui_hostname"], f"commsgun.ts.{d}")

    def test_headscale_routes_the_private_zone_and_nothing_else(self):
        dns = yaml.safe_load(render("roles/stack/templates/headscale-config.yaml.j2"))["dns"]
        self.assertEqual(dns["nameservers"]["split"], {V["private_zone"]: ["1.1.1.1"]})
        self.assertEqual(dns["nameservers"]["global"], [])
        self.assertIs(dns["override_local_dns"], False)

    def test_private_name_gets_a_record_and_a_certificate_probe(self):
        names = re.search(r"_names: >-\n(.*?)\n    _records", (ROOT / "roles/tailscale/tasks/main.yml").read_text(), re.S).group(1)
        self.assertIn("commsgun_ui_private_hostname", names)
        check = re.search(r"_cert_check: >-\n(.*?)\n  block:", (ROOT / "roles/stack/tasks/main.yml").read_text(), re.S).group(1)
        self.assertIn("commsgun_ui_private_hostname", check)


if __name__ == "__main__":
    unittest.main()
