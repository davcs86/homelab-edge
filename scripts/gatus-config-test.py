#!/usr/bin/env python3
"""Renders roles/stack/templates/gatus-config.yaml.j2 with the prod group_vars and checks the endpoint set, URLs and alerts."""
import json
import subprocess
import sys
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
TEXT = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
    (ROOT / "roles/stack/templates/gatus-config.yaml.j2").read_text()).render(**V, ansible_managed="test")
CONFIG = yaml.safe_load(TEXT)
ENDPOINTS = {e["name"]: e for e in CONFIG["endpoints"]}
EXTERNAL = {e["name"]: e for e in CONFIG["external-endpoints"]}


class Gatus(unittest.TestCase):
    def test_keys_are_unique_across_polled_and_pushed_endpoints(self):
        keys = [f"{e['group']}_{e['name']}" for e in CONFIG["endpoints"] + CONFIG["external-endpoints"]]
        self.assertEqual(len(keys), len(set(keys)), keys)

    def test_xstockstrat_probes_follow_the_workload_tags_and_no_app_names_remain(self):
        for name in ("xstockstrat-ui", "xstockstrat-agent", "xstockstrat-staging-ui", "xstockstrat-staging-agent"):
            self.assertIn(name, ENDPOINTS)
            self.assertEqual(ENDPOINTS[name]["group"], "home")
        self.assertFalse([n for n in {*ENDPOINTS, *EXTERNAL} if n.startswith("app-")])

    def test_prod_and_staging_probe_their_own_node_on_the_same_ports(self):
        d = V["magicdns_base_domain"]
        self.assertEqual(ENDPOINTS["xstockstrat-ui"]["url"], f"http://xstockstrat.{d}:3000/api/health")
        self.assertEqual(ENDPOINTS["xstockstrat-staging-ui"]["url"], f"http://xstockstrat-staging.{d}:3000/api/health")
        self.assertTrue(ENDPOINTS["xstockstrat-agent"]["url"].startswith(f"http://xstockstrat.{d}:9000/"))
        self.assertTrue(ENDPOINTS["xstockstrat-staging-agent"]["url"].startswith(f"http://xstockstrat-staging.{d}:9000/"))

    def test_staging_is_probed_over_the_tailnet_resolver_like_prod(self):
        for name in ("xstockstrat-ui", "xstockstrat-staging-ui", "xstockstrat-agent", "xstockstrat-staging-agent"):
            self.assertEqual(ENDPOINTS[name]["client"]["dns-resolver"], "udp://100.100.100.100:53", name)

    def test_every_polled_endpoint_alerts_through_ntfy(self):
        for name, e in ENDPOINTS.items():
            self.assertEqual(e["alerts"], [{"type": "ntfy"}], name)

    def test_probed_ports_are_ones_the_edge_may_reach_on_both_tags(self):
        policy = json.loads(subprocess.run([sys.executable, str(ROOT / "scripts/hujson2json.py"), str(ROOT / "policy/policy.hujson")],
                                           capture_output=True, text=True, check=True).stdout)
        accepted = {a for t in policy["tests"] if t["src"] == "tag:edge" for a in t["accept"]}
        for tag in ("tag:xstockstrat", "tag:xstockstrat-staging"):
            for port in (V["app_ui_port"], V["app_agent_port"]):
                self.assertIn(f"{tag}:{port}", accepted)

    def test_heartbeats_are_unchanged(self):
        self.assertEqual(set(EXTERNAL), {"proxmox-heartbeat", "db-heartbeat", "nightly-backup"})


if __name__ == "__main__":
    unittest.main()
