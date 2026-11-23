#!/usr/bin/env python3
"""Renders the happybabytracker env files, their pre-write assertions and compose services from the prod group_vars
plus fake secrets."""
import json
import os
import re
import shutil
import subprocess
import tempfile
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
    for _ in range(10):  # values reference each other ("happybaby.{{ private_zone }}")
        nxt = {k: env.from_string(v).render(**resolved) if isinstance(v, str) else v for k, v in resolved.items()}
        if nxt == resolved:
            break
        resolved = nxt
    return resolved


V = load_vars()
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, loader=jinja2.FileSystemLoader(ROOT / "roles/stack/templates"))
ENV.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None  # Ansible's `match` test
KEYS = ["happybabytracker_admin_username", "happybabytracker_admin_password", "happybabytracker_internal_api_token"]
API_ENV_TASK, UI_ENV_TASK = "Happybabytracker API environment", "Happybabytracker UI environment"
BLOCK_NAME = "Happybabytracker secrets and configuration"
ORIGIN = f"https://{V['happybabytracker_ui_private_hostname']}"


def secrets(**over):
    s = {"happybabytracker_admin_username": "fake-admin",
         "happybabytracker_admin_password": "fake-admin-password-".ljust(24, "x"),
         "happybabytracker_internal_api_token": "fake-internal-api-token-".ljust(48, "x")}
    s |= over
    return {k: v for k, v in s.items() if v is not None}


def find_task(tasks, name):
    for t in tasks:
        if t.get("name") == name:
            return t
        found = find_task(t.get("block", []), name)
        if found:
            return found
    return None


TASKS = yaml.safe_load((ROOT / "roles/stack/tasks/main.yml").read_text())
BLOCK = find_task(TASKS, BLOCK_NAME) or {}


def render_env(task_name, s):
    content = find_task(TASKS, task_name)["ansible.builtin.copy"]["content"]
    return ENV.from_string(content).render(**V, edge_secrets=s)


def parse_env(text):
    """Every line is NAME='value' with no quote or line break inside: Compose takes a single-quoted value verbatim."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([A-Z_]+)='([^'\n]*)'", line)
        if not m:
            raise ValueError(f"not a single-quoted env line: {line.split('=')[0]}")
        out[m.group(1)] = m.group(2)
    return out


def first_failure(s):
    """Evaluates the block's assert tasks the way Ansible does: per loop item, conditions in order.
    Returns the rendered fail_msg of the first failing assertion, or None."""
    ctx = {**V, "edge_secrets": s}
    for t in BLOCK["block"]:
        a = t.get("ansible.builtin.assert")
        if a is None:
            continue
        items = t.get("loop", [None])
        if isinstance(items, str):
            items = ENV.compile_expression(items.strip()[2:-2])(**ctx)
        that = a["that"] if isinstance(a["that"], list) else [a["that"]]
        for item in items:
            for cond in that:
                if not ENV.compile_expression(cond)(**ctx, item=item):
                    return ENV.from_string(a["fail_msg"]).render(**ctx, item=item)
    return None


COMPOSE = yaml.safe_load(ENV.get_template("compose.yml.j2").render(**V, ansible_managed="test"))  # resolves `<<: *hardening`
S = COMPOSE["services"]
API, UI = "happybabytracker-api", "happybabytracker-ui"


class Compose(unittest.TestCase):
    def test_api_is_never_published_and_ui_binds_loopback_only(self):
        self.assertNotIn("ports", S[API])
        self.assertEqual(V["happybabytracker_ui_port"], 9212)
        self.assertEqual(S[UI]["ports"], ["127.0.0.1:9212:9212"])
        text = (ROOT / "roles/stack/templates/compose.yml.j2").read_text()
        self.assertIn('ports: ["127.0.0.1:{{ happybabytracker_ui_port }}:9212"]', text)
        published = [str(p) for svc in S.values() for p in svc.get("ports", [])]
        self.assertFalse([p for p in published if "9210" in p], published)

    def test_services_are_hardened_read_only_and_on_their_own_network(self):
        hardening = COMPOSE["x-hardening"]
        self.assertEqual((hardening["cap_drop"], hardening["security_opt"]), (["ALL"], ["no-new-privileges:true"]))
        for name, image, env_file, mem in ((API, V["happybabytracker_api_image"], "./happybabytracker/api.env", "96m"),
                                           (UI, V["happybabytracker_ui_image"], "./happybabytracker/ui.env", "192m")):
            svc = S[name]
            self.assertEqual({k: svc[k] for k in hardening}, hardening, name)
            self.assertIs(svc["read_only"], True, name)
            self.assertEqual(svc["image"], image, name)
            self.assertEqual(svc["env_file"], [env_file], name)
            self.assertEqual(svc["networks"], ["happybabytracker"], name)
            self.assertEqual(svc["mem_limit"], mem, name)
        self.assertEqual(S[API]["stop_grace_period"], "30s")
        self.assertNotIn("healthcheck", S[API])  # distroless: no shell, no node
        self.assertEqual(S[UI]["depends_on"], [API])  # a list: no condition, the API has no healthcheck
        self.assertEqual(S[UI]["healthcheck"]["test"], [
            "CMD", "node", "-e",
            "fetch('http://127.0.0.1:9212/healthz').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"])
        self.assertIn("happybabytracker", COMPOSE["networks"])
        for name, svc in S.items():
            if not name.startswith("happybabytracker-"):
                self.assertNotIn("happybabytracker", svc.get("networks", []), name)
        self.assertIn("happybabytracker-data", COMPOSE["volumes"])
        mounts = {n: [v for v in svc.get("volumes", []) if str(v).startswith("happybabytracker-data:")] for n, svc in S.items()}
        self.assertEqual({n: m for n, m in mounts.items() if m}, {API: ["happybabytracker-data:/data"]})
        self.assertEqual(S[API]["volumes"], ["happybabytracker-data:/data"])
        self.assertNotIn("volumes", S[UI])

    @unittest.skipUnless(shutil.which("docker") or os.environ.get("HAPPYBABYTRACKER_TEST_REQUIRE_DOCKER"), "docker is required")
    def test_docker_compose_config_reads_back(self):
        d = Path(tempfile.mkdtemp())
        try:
            s = secrets()
            (d / "happybabytracker").mkdir()
            (d / "happybabytracker/api.env").write_text(render_env(API_ENV_TASK, s))
            (d / "happybabytracker/ui.env").write_text(render_env(UI_ENV_TASK, s))
            mini = {"name": "edge", "services": {API: S[API], UI: S[UI]},
                    "networks": {"happybabytracker": COMPOSE["networks"]["happybabytracker"]},
                    "volumes": {"happybabytracker-data": COMPOSE["volumes"]["happybabytracker-data"]}}
            (d / "compose.yml").write_text(yaml.safe_dump(mini))
            r = subprocess.run(["docker", "compose", "-f", str(d / "compose.yml"), "config", "--format", "json"],
                               capture_output=True, text=True, check=True, env={**os.environ, "HOME": os.environ.get("HOME", "/")})
            cfg = json.loads(r.stdout)
            svc = cfg["services"]
            self.assertEqual(svc[API]["environment"], parse_env(render_env(API_ENV_TASK, s)))
            self.assertEqual(svc[UI]["environment"], parse_env(render_env(UI_ENV_TASK, s)))
            self.assertNotIn("ports", svc[API])
            self.assertEqual([(p["host_ip"], p["published"], p["target"]) for p in svc[UI]["ports"]], [("127.0.0.1", "9212", 9212)])
            self.assertEqual([(v["source"], v["target"]) for v in svc[API]["volumes"]], [("happybabytracker-data", "/data")])
            self.assertEqual(cfg["volumes"]["happybabytracker-data"]["name"], "edge_happybabytracker-data")
            self.assertEqual(list(svc[API]["networks"]), ["happybabytracker"])
            self.assertEqual(list(svc[UI]["networks"]), ["happybabytracker"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Assertions(unittest.TestCase):
    def test_assertions_run_before_any_file_is_written(self):
        self.assertTrue(BLOCK, f"no task named {BLOCK_NAME!r}")
        kinds = [next(iter(set(t) & {"ansible.builtin.assert", "ansible.builtin.copy"})) for t in BLOCK["block"]]
        self.assertEqual(kinds, sorted(kinds), kinds)  # every assert precedes every copy
        self.assertEqual(kinds.count("ansible.builtin.copy"), 2)
        names = [t.get("name") for t in TASKS]
        self.assertLess(names.index("Commsgun secrets and configuration"), names.index(BLOCK_NAME))
        self.assertLess(names.index(BLOCK_NAME), names.index("Compose file"))
        dirs = find_task(TASKS, "Stack directories")["loop"]
        self.assertIn({"path": "{{ edge_root }}/happybabytracker", "mode": "0750"}, dirs)
        self.assertIsNone(first_failure(secrets()))

    def test_each_secret_is_present_a_string_and_free_of_quote_backslash_cr_lf(self):
        self.assertIsNone(first_failure(secrets()))
        for key in KEYS:
            good = secrets()[key]
            for bad in (None, "", 7, ["x" * 40], good[:2] + "'" + good[2:], good + "\\", good + "\r", good + "\n", "a\nb" + good):
                msg = first_failure(secrets(**{key: bad}))
                self.assertIsNotNone(msg, (key, repr(bad)))
                self.assertIn(key, msg)

    def test_minimum_lengths_and_username_shape_mirror_the_apps_startup_checks(self):
        # happybabytracker api/internal/constants: MinAdminPasswordLen 16, MinSecretLen 32, UsernamePattern.
        cases = {"happybabytracker_admin_username": (["Qzadmin", "zq", "zq'x", "zq admin", "u" * 33, "zq@admin"],
                                                     ["a.b", "u" * 32, "fake_admin-1.x"]),
                 "happybabytracker_admin_password": (["x" * 15], ["x" * 16]),
                 "happybabytracker_internal_api_token": (["x" * 31], ["x" * 32])}
        for key, (bad, good) in cases.items():
            for value in bad:
                msg = first_failure(secrets(**{key: value}))
                self.assertIsNotNone(msg, (key, value))
                self.assertIn(key, msg)
                self.assertNotIn(value, msg.replace(key, ""))
            for value in good:
                self.assertIsNone(first_failure(secrets(**{key: value})), (key, value))
        for t in BLOCK["block"]:
            msg = t.get("ansible.builtin.assert", {}).get("fail_msg", "")
            for ref in re.findall(r"\{\{(.*?)\}\}", msg):
                self.assertEqual(ref.strip(), "item.key", msg)
            for value in secrets().values():
                self.assertNotIn(value, msg)
        self.assertIs(BLOCK.get("no_log"), True)

    def test_secret_keys_are_literal(self):
        text = yaml.safe_dump(BLOCK)
        for ref in re.findall(r"edge_secrets(\.[a-z_0-9]+|\[[^\]]*\])", text):
            self.assertTrue(ref.startswith(".happybabytracker_") or ref == "[item.key]", ref)
        loop_keys = {i["key"] for t in BLOCK["block"] if isinstance(t.get("loop"), list) for i in t["loop"]}
        self.assertEqual(loop_keys, set(KEYS))


class EnvFiles(unittest.TestCase):
    def test_env_files_hold_exactly_the_contracted_variables(self):
        s = secrets()
        self.assertEqual(parse_env(render_env(API_ENV_TASK, s)), {
            "ADMIN_USERNAME": s["happybabytracker_admin_username"], "ADMIN_PASSWORD": s["happybabytracker_admin_password"],
            "INTERNAL_API_TOKEN": s["happybabytracker_internal_api_token"], "PUBLIC_ORIGIN": ORIGIN})
        self.assertEqual(parse_env(render_env(UI_ENV_TASK, s)), {
            "INTERNAL_API_TOKEN": s["happybabytracker_internal_api_token"],
            "API_BASE_URL": "http://happybabytracker-api:9210", "ORIGIN": ORIGIN})
        self.assertIs(BLOCK.get("no_log"), True)
        self.assertNotIn("notify", BLOCK)
        for name, file in ((API_ENV_TASK, "api.env"), (UI_ENV_TASK, "ui.env")):
            task = find_task(TASKS, name)
            copy = task["ansible.builtin.copy"]
            self.assertEqual((copy["dest"], copy["mode"]), ("{{ edge_root }}/happybabytracker/" + file, "0600"))
            self.assertNotIn("notify", task, name)  # EDGE-04: env_file changes recreate the container
            names = set(parse_env(render_env(name, s)))
            self.assertFalse(names & {"DATA_DIR", "PORT", "GOMEMLIMIT"}, name)
            self.assertFalse([n for n in names if "FORWARD" in n or "PROXY" in n or n.endswith("_HEADER")], name)

    def test_vapid_is_not_rendered_in_plan_2(self):
        s = secrets(happybabytracker_vapid_private_key="fake-vapid-key")
        for name in (API_ENV_TASK, UI_ENV_TASK):
            self.assertFalse([n for n in parse_env(render_env(name, s)) if "VAPID" in n], name)
        self.assertNotIn("vapid", yaml.safe_dump(BLOCK).lower())
        self.assertNotIn("vapid", (ROOT / "roles/stack/templates/compose.yml.j2").read_text().lower())
        for svc in (S[API], S[UI]):
            self.assertNotIn("environment", svc)


if __name__ == "__main__":
    unittest.main()
