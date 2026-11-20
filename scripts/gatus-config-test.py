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
        self.assertEqual(ENDPOINTS["xstockstrat-ui"]["url"], f"http://xstockstrat.{d}:3000/trader/api/health")
        self.assertEqual(ENDPOINTS["xstockstrat-staging-ui"]["url"], f"http://xstockstrat-staging.{d}:3000/trader/api/health")
        self.assertTrue(ENDPOINTS["xstockstrat-agent"]["url"].startswith(f"http://xstockstrat.{d}:9000/"))
        self.assertTrue(ENDPOINTS["xstockstrat-staging-agent"]["url"].startswith(f"http://xstockstrat-staging.{d}:9000/"))

    def test_staging_is_probed_over_the_tailnet_resolver_like_prod(self):
        for name in ("xstockstrat-ui", "xstockstrat-staging-ui", "xstockstrat-agent", "xstockstrat-staging-agent"):
            self.assertEqual(ENDPOINTS[name]["client"]["dns-resolver"], "udp://100.100.100.100:53", name)

    def test_ui_probes_require_the_health_json_and_never_follow_a_login_redirect(self):
        for name in ("xstockstrat-ui", "xstockstrat-staging-ui"):
            self.assertIs(ENDPOINTS[name]["client"].get("ignore-redirect"), True, name)
            self.assertIn("[STATUS] == 200", ENDPOINTS[name]["conditions"], name)
            self.assertIn("[BODY].status == ok", ENDPOINTS[name]["conditions"], name)

    def test_agent_probes_pin_each_environments_public_resource_url(self):
        self.assertIn(f"[BODY].resource == https://{V['public_app_hostname']}/agent",
                      ENDPOINTS["xstockstrat-agent"]["conditions"])
        self.assertIn(f"[BODY].resource == https://{V['staging_public_hostname']}/agent",
                      ENDPOINTS["xstockstrat-staging-agent"]["conditions"])

    def test_staging_public_route_is_probed_from_the_internet_resolver(self):
        base = f"https://{V['staging_public_hostname']}"
        for name in ("staging-mcp-oauth-metadata", "staging-mcp-requires-auth"):
            self.assertEqual(ENDPOINTS[name]["group"], "public", name)
            self.assertEqual(ENDPOINTS[name]["client"]["dns-resolver"], "tcp://1.1.1.1:53", name)
            self.assertTrue(ENDPOINTS[name]["url"].startswith(base + "/"), name)
        self.assertIn(f"[BODY].resource == {base}/agent", ENDPOINTS["staging-mcp-oauth-metadata"]["conditions"])
        self.assertIn("[STATUS] == 401", ENDPOINTS["staging-mcp-requires-auth"]["conditions"])

    def test_each_xstockstrat_ui_is_probed_publicly_for_its_health_body(self):
        for name, host in (("xstockstrat-ui-public", V["public_app_hostname"]),
                           ("xstockstrat-staging-ui-public", V["staging_public_hostname"])):
            e = ENDPOINTS[name]
            self.assertEqual(e["group"], "public", name)
            self.assertEqual(e["url"], f"https://{host}/trader/api/health", name)
            self.assertEqual(e["client"]["dns-resolver"], "tcp://1.1.1.1:53", name)
            self.assertNotIn("ignore-redirect", e["client"], name)
            self.assertEqual(e["conditions"], ["[STATUS] == 200", "[BODY].status == ok", "[CERTIFICATE_EXPIRATION] > 336h"], name)

    def test_the_legacy_ingest_name_keeps_the_tailnet_wildcard_expiry_check(self):
        ing = ENDPOINTS["webhooks-ingest"]
        self.assertEqual((ing["group"], ing["url"]), ("edge", f"https://{V['webhooks_ingest_hostname']}/healthz"))
        self.assertEqual(ing["client"]["dns-resolver"], "udp://100.100.100.100:53")
        self.assertEqual(ing["conditions"], ["[STATUS] == 200", "[CERTIFICATE_EXPIRATION] > 336h"])

    def test_the_staging_ingest_listener_is_probed_over_the_tailnet(self):
        e = ENDPOINTS["webhooks-ingest-staging"]
        self.assertEqual((e["group"], e["url"]), ("edge", f"https://{V['webhooks_ingest_hostname']}:{V['staging_edge_port']}/healthz"))
        self.assertEqual(e["client"]["dns-resolver"], "udp://100.100.100.100:53")
        self.assertEqual(e["conditions"], ["[STATUS] == 200"])

    def test_the_tailnet_wildcard_certificate_expiry_is_probed(self):
        wildcard = "." + V["magicdns_base_domain"]
        probed = [n for n, e in ENDPOINTS.items() if e["url"].startswith("https://") and e["url"].split("/")[2].split(":")[0].endswith(wildcard)
                  and "[CERTIFICATE_EXPIRATION] > 336h" in e["conditions"]]
        self.assertTrue(probed)

    def test_the_commsgun_backlog_is_probed_on_the_private_name(self):
        e = ENDPOINTS["commsgun-backlog"]
        self.assertEqual((e["group"], e["url"], e["interval"]), ("edge", f"https://{V['commsgun_enqueue_private_hostname']}/healthz/backlog", "1m"))
        self.assertEqual(e["conditions"], ["[STATUS] == 200"])
        self.assertEqual(e["client"]["dns-resolver"], "udp://100.100.100.100:53")

    # Probes still aimed at a *.ts service name. This list may only shrink; it empties when the *.ts wildcard is retired.
    LEGACY_TS_PROBES = {"webhooks-ingest", "webhooks-ingest-staging"}

    def test_no_probe_targets_a_ts_service_name_except_the_listed_ones(self):
        machines = {v for k, v in V.items() if k.endswith("_tailnet_host")}
        on_ts = set()
        for name, e in ENDPOINTS.items():
            host = e["url"].split("//", 1)[1].split("/")[0].split(":")[0]
            if host.endswith("." + V["magicdns_base_domain"]) and host not in machines:
                on_ts.add(name)
        self.assertEqual(on_ts, self.LEGACY_TS_PROBES)

    def test_services_with_a_portal_zone_probe_are_not_probed_twice(self):
        for old in ("commsgun-api", "commsgun-ui", "webhooks-mcp-requires-auth"):
            self.assertFalse(old in ENDPOINTS, old)

    def test_the_private_dashboard_name_is_probed_over_the_tailnet(self):
        e = ENDPOINTS["commsgun-ui-private"]
        self.assertEqual((e["group"], e["url"], e["interval"]), ("edge", f"https://{V['commsgun_ui_private_hostname']}/healthz", "1m"))
        self.assertEqual(e["conditions"], ["[STATUS] == 200", "[CERTIFICATE_EXPIRATION] > 336h"])
        self.assertEqual(e["client"]["dns-resolver"], "udp://100.100.100.100:53")

    def test_alerts_link_to_the_status_page_under_the_private_zone(self):
        self.assertEqual(CONFIG["alerting"]["ntfy"]["click"], f"https://{V['status_private_hostname']}")

    def test_the_private_enqueue_name_is_probed_over_the_tailnet(self):
        e = ENDPOINTS["commsgun-api-private"]
        self.assertEqual((e["group"], e["url"], e["interval"]), ("edge", f"https://{V['commsgun_enqueue_private_hostname']}/healthz", "1m"))
        self.assertEqual(e["conditions"], ["[STATUS] == 200"])
        self.assertEqual(e["client"]["dns-resolver"], "udp://100.100.100.100:53")

    def test_the_webhooks_mcp_public_zone_name_is_probed_for_401(self):
        e = ENDPOINTS["webhooks-mcp-portal-requires-auth"]
        self.assertEqual((e["group"], e["method"], e["url"]), ("public", "POST", f"https://{V['webhooks_mcp_public_hostname']}/mcp"))
        self.assertEqual(e["client"]["dns-resolver"], "tcp://1.1.1.1:53")
        self.assertEqual(e["conditions"], ["[STATUS] == 401"])

    def test_an_unknown_public_zone_name_is_probed_for_404(self):
        e = ENDPOINTS["public-default-404"]
        self.assertEqual((e["group"], e["url"]), ("public", f"https://unknown.{V['public_zone']}/"))
        self.assertEqual(e["conditions"], ["[STATUS] == 404"])

    def test_production_is_probed_on_its_public_zone_name(self):
        for name in ("xstockstrat-ui-public", "mcp-oauth-metadata", "mcp-requires-auth"):
            self.assertTrue(ENDPOINTS[name]["url"].startswith(f"https://xstockstrat.public.{V['domain']}/"), name)

    def test_the_private_ingest_name_is_probed_on_443_only(self):
        e = ENDPOINTS["webhooks-ingest-private"]
        self.assertEqual((e["group"], e["url"], e["interval"]), ("edge", f"https://{V['webhooks_ingest_private_hostname']}/healthz", "1m"))
        self.assertEqual(e["conditions"], ["[STATUS] == 200"])
        self.assertEqual(e["client"]["dns-resolver"], "udp://100.100.100.100:53")
        self.assertNotIn("webhooks-ingest-staging-private", ENDPOINTS)

    def test_staging_is_probed_once_per_check_on_its_public_zone_name(self):
        self.assertNotIn("xstockstrat-staging-portal-ui-public", ENDPOINTS)
        for name in ("xstockstrat-staging-ui-public", "staging-mcp-oauth-metadata", "staging-mcp-requires-auth"):
            self.assertTrue(ENDPOINTS[name]["url"].startswith(f"https://xstockstrat-staging.public.{V['domain']}/"), name)

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

    def test_the_pgbackrest_heartbeat_waits_longer_than_a_daily_backup(self):
        self.assertEqual(EXTERNAL["pgbackrest-backup"]["group"], "edge")
        self.assertEqual(EXTERNAL["pgbackrest-backup"]["heartbeat"]["interval"], "26h")
        self.assertEqual(EXTERNAL["pgbackrest-backup"]["token"], "${HEARTBEAT_PGBACKREST_TOKEN}")

    def test_heartbeats_are_unchanged(self):
        self.assertEqual(set(EXTERNAL), {"proxmox-heartbeat", "db-heartbeat", "nightly-backup", "pgbackrest-backup"})


if __name__ == "__main__":
    unittest.main()
