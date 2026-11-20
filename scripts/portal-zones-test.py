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


# Service names still under the machine zone. This list may only shrink: a migration removes its entry here.
LEGACY_TS_SERVICES = {"status_hostname", "ntfy_hostname", "webhooks_ingest_hostname", "commsgun_enqueue_hostname", "commsgun_ui_hostname"}


def under(value, zone):
    return isinstance(value, str) and value.endswith("." + zone)


class ZoneRules(unittest.TestCase):
    """*.ts holds machines; *.private tailnet-only and *.public internet services, served by the edge wherever they run."""

    def test_only_machines_and_the_legacy_services_live_under_the_machine_zone(self):
        services = {k for k, v in V.items() if under(v, V["magicdns_base_domain"]) and not k.endswith("_tailnet_host")}
        self.assertEqual(services, LEGACY_TS_SERVICES)

    def test_caddy_serves_only_the_legacy_services_under_the_machine_zone(self):
        template = (ROOT / "roles/stack/templates/Caddyfile.j2").read_text()
        sites = re.findall(r"^\*\.\{\{ magicdns_base_domain \}\}[^\n]*\{\n(.*?)^\}", template, re.M | re.S)
        self.assertEqual(len(sites), 2)
        hosts = {h for body in sites for h in re.findall(r"^\s+(?:@\w+ host|hostnames) (.+)$", body, re.M)}
        self.assertTrue(hosts)
        self.assertLessEqual(hosts, {"{{ %s }}" % name for name in LEGACY_TS_SERVICES})

    def test_no_machine_lives_under_a_portal_zone(self):
        machines = {k: v for k, v in V.items() if k.endswith("_tailnet_host")}
        self.assertTrue(machines)
        for name, value in machines.items():
            self.assertTrue(under(value, V["magicdns_base_domain"]), name)
        hosts = (ROOT / "inventories/prod/hosts.yml").read_text()
        for zone in ("public_zone", "private_zone"):
            self.assertNotIn(zone, hosts)

    def test_the_three_zones_are_distinct_and_headscale_names_only_machines(self):
        zones = [V["magicdns_base_domain"], V["public_zone"], V["private_zone"]]
        for a in zones:
            for b in zones:
                if a is not b:
                    self.assertFalse(a == b or a.endswith("." + b), (a, b))
        dns = yaml.safe_load(render("roles/stack/templates/headscale-config.yaml.j2"))["dns"]
        self.assertEqual(dns["base_domain"], V["magicdns_base_domain"])


if __name__ == "__main__":
    unittest.main()
