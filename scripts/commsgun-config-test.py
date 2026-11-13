#!/usr/bin/env python3
"""Renders COMMSGUN_CONFIG, the commsgun env files, their pre-write assertions and compose services from the prod
group_vars plus fake secrets (example.com senders only)."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
VARS_DIR = ROOT / "inventories/prod/group_vars/all"
sys.path.insert(0, str(ROOT / "scripts"))
import bump_image_pin  # noqa: E402


def load_vars():
    raw = {}
    for f in sorted(VARS_DIR.glob("*.yml")):
        raw.update(yaml.safe_load(f.read_text()) or {})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    resolved = dict(raw)
    for _ in range(10):  # values reference each other ("comms.{{ magicdns_base_domain }}")
        nxt = {k: env.from_string(v).render(**resolved) if isinstance(v, str) else v for k, v in resolved.items()}
        if nxt == resolved:
            break
        resolved = nxt
    return resolved


V = load_vars()
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, loader=jinja2.FileSystemLoader(ROOT / "roles/stack/templates"))
ENV.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None  # Ansible's `match` test
TRICKY = "O'Brien \"Ops\" $HOME #1 <ops@example.com>"  # quote, double quotes, $ and # must survive the env file
BACKSLASH = r'"Ops\\Team \'q\'" <team@example.com>'  # backslashes and a backslash before a quote
SENDERS = {
    "xstockstrat": {"senders": ["Alerts <alerts@example.com>", TRICKY], "default_sender": "Alerts <alerts@example.com>"},
    "xstockstrat-staging": {"senders": ["Staging <staging@example.com>"], "default_sender": "Staging <staging@example.com>"},
    "ops": {"senders": [TRICKY, BACKSLASH], "default_sender": TRICKY},
}
GENERATED = ["commsgun_admin_password", "commsgun_session_secret", "commsgun_admin_api_token"]


def fake(key):
    return f"fake-{key}-".ljust(48, "x")


def secrets(**over):
    s = {k: fake(k) for k in GENERATED + [a["key"] for a in V.get("commsgun_apps", [])]}
    s |= {"resend_api_key": "re_fake_resend_key", "commsgun_senders": json.loads(json.dumps(SENDERS))}
    s |= over
    return {k: v for k, v in s.items() if v is not None}


def render_config(s):
    return ENV.get_template("commsgun-config.json.j2").render(**V, edge_secrets=s)


def find_task(tasks, name):
    for t in tasks:
        if t.get("name") == name:
            return t
        found = find_task(t.get("block", []), name)
        if found:
            return found
    return None


TASKS = yaml.safe_load((ROOT / "roles/stack/tasks/main.yml").read_text())
BLOCK = find_task(TASKS, "Commsgun secrets and configuration")


def render_env(task_name, s):
    content = find_task(TASKS, task_name)["ansible.builtin.copy"]["content"]
    lookup = lambda kind, name: ENV.get_template(name).render(**V, edge_secrets=s)  # noqa: E731
    return ENV.from_string(content).render(**V, edge_secrets=s, lookup=lookup)


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
    """Evaluates the role's assert tasks the way Ansible does: per loop item, conditions in order.
    Returns the rendered fail_msg of the first failing assertion, or None."""
    ctx = {**V, "edge_secrets": s}
    ctx |= {k: ENV.compile_expression(v.strip()[2:-2])(**ctx) for k, v in BLOCK.get("vars", {}).items()}
    for t in BLOCK["block"]:
        a = t.get("ansible.builtin.assert")
        if a is None:
            continue
        items = [None]
        if "loop" in t:
            items = ENV.compile_expression(t["loop"].strip()[2:-2])(**ctx)
        that = a["that"] if isinstance(a["that"], list) else [a["that"]]
        for item in items:
            for cond in that:
                if not ENV.compile_expression(cond)(**ctx, item=item):
                    return ENV.from_string(a["fail_msg"]).render(**ctx, item=item)
    return None


def assertions_pass(s):
    return first_failure(s) is None


COMPOSE = yaml.safe_load(ENV.get_template("compose.yml.j2").render(**V, ansible_managed="test"))


class Inventory(unittest.TestCase):
    def test_names_ports_and_body_cap(self):
        d = V["magicdns_base_domain"]
        self.assertEqual((V["commsgun_enqueue_hostname"], V["commsgun_ui_hostname"]), (f"comms.{d}", f"commsgun.{d}"))
        self.assertEqual((V["commsgun_enqueue_port"], V["commsgun_ui_port"]), (9200, 9202))
        self.assertEqual(V["body_limits"]["commsgun_enqueue"], 262144)

    def test_image_pins_are_in_the_form_the_bump_script_rewrites(self):
        versions = (VARS_DIR / "versions.yml").read_text()
        docs = (ROOT / "docs/versions.md").read_text()
        for var, image in (("commsgun_api_image", "ghcr.io/davcs86/commsgun-api"), ("commsgun_ui_image", "ghcr.io/davcs86/commsgun-ui")):
            digest = bump_image_pin.current_pin(versions, var, image)
            self.assertEqual(V[var], f"{image}@{digest}")
            new = bump_image_pin.replace_docs_row(docs, var, image, "sha256:" + "0" * 64, "1" * 40, "2026-01-01")
            self.assertNotEqual(new, docs, var)

    def test_both_names_get_split_horizon_records(self):
        names = re.search(r"_names: >-\n(.*?)\n    _records", (ROOT / "roles/tailscale/tasks/main.yml").read_text(), re.S).group(1)
        self.assertIn("commsgun_enqueue_hostname", names)
        self.assertIn("commsgun_ui_hostname", names)


class Config(unittest.TestCase):
    def setUp(self):
        self.text = render_config(secrets())
        self.cfg = json.loads(self.text)
        self.apps = {a["id"]: a for a in self.cfg["apps"]}

    def test_one_line_of_json(self):
        self.assertEqual(len(self.text.strip().splitlines()), 1)
        self.assertNotIn("'", self.text)

    def test_registered_apps_and_their_keys(self):
        self.assertEqual([a["id"] for a in self.cfg["apps"]], ["xstockstrat", "xstockstrat-staging", "ops"])
        for app in V["commsgun_apps"]:
            self.assertEqual(self.apps[app["id"]]["key"], fake(app["key"]))
        self.assertEqual(set(self.cfg), {"apps", "providers"})
        for a in self.cfg["apps"]:
            self.assertEqual(set(a), {"id", "key", "channels"})

    def test_email_carries_the_senders_and_sms_only_limits(self):
        self.assertEqual(self.apps["xstockstrat"]["channels"]["email"],
                         {"per_minute": 10, "per_day": 80, **SENDERS["xstockstrat"]})
        self.assertEqual(self.apps["xstockstrat-staging"]["channels"]["email"],
                         {"per_minute": 2, "per_day": 10, **SENDERS["xstockstrat-staging"]})
        self.assertEqual(self.apps["ops"]["channels"]["email"], {"per_minute": 2, "per_day": 5, **SENDERS["ops"]})
        for app in self.apps.values():
            self.assertEqual(app["channels"]["sms"], {"per_minute": 5, "per_day": 50})

    def test_providers(self):
        self.assertEqual(self.cfg["providers"], {"email": {"name": "resend", "per_minute": 60, "per_day": 100}, "sms": None})


class EnvFiles(unittest.TestCase):
    def test_api_env(self):
        env = parse_env(render_env("Commsgun API environment", secrets()))
        self.assertEqual(list(env), ["COMMSGUN_CONFIG", "RESEND_API_KEY", "ADMIN_API_TOKEN"])
        self.assertEqual(json.loads(env["COMMSGUN_CONFIG"]), json.loads(render_config(secrets())))
        self.assertEqual(env["RESEND_API_KEY"], "re_fake_resend_key")
        self.assertEqual(env["ADMIN_API_TOKEN"], fake("commsgun_admin_api_token"))

    def test_ui_env(self):
        env = parse_env(render_env("Commsgun dashboard environment", secrets()))
        self.assertEqual(env, {
            "ADMIN_PASSWORD": fake("commsgun_admin_password"), "SESSION_SECRET": fake("commsgun_session_secret"),
            "ADMIN_API_TOKEN": fake("commsgun_admin_api_token"), "API_BASE_URL": "http://commsgun-api:9201",
            "ORIGIN": f"https://{V['commsgun_ui_hostname']}"})

    def test_files_are_private_and_the_block_hides_its_output(self):
        self.assertIs(BLOCK.get("no_log"), True)
        for name, file in (("Commsgun API environment", "api.env"), ("Commsgun dashboard environment", "ui.env")):
            copy = find_task(TASKS, name)["ansible.builtin.copy"]
            self.assertEqual((copy["dest"], copy["mode"]), ("{{ edge_root }}/commsgun/" + file, "0600"))

    def test_assertions_run_before_any_file_is_written(self):
        kinds = [next(iter(set(t) & {"ansible.builtin.assert", "ansible.builtin.copy"})) for t in BLOCK["block"]]
        self.assertEqual(kinds, sorted(kinds), kinds)  # every assert precedes every copy
        names = [t.get("name") for t in TASKS]
        self.assertLess(names.index("Commsgun secrets and configuration"), names.index("Compose file"))

    @unittest.skipUnless(shutil.which("docker") or os.environ.get("COMMSGUN_TEST_REQUIRE_DOCKER"), "docker is required")
    def test_compose_reads_both_files_back_unchanged(self):
        d = Path(tempfile.mkdtemp())
        try:
            s = secrets()
            (d / "api.env").write_text(render_env("Commsgun API environment", s))
            (d / "ui.env").write_text(render_env("Commsgun dashboard environment", s))
            (d / "compose.yml").write_text("services:\n  a: {image: x, env_file: [./api.env]}\n  u: {image: x, env_file: [./ui.env]}\n")
            r = subprocess.run(["docker", "compose", "-f", str(d / "compose.yml"), "config", "--format", "json"],
                               capture_output=True, text=True, check=True, env={**os.environ, "HOME": os.environ.get("HOME", "/")})
            svc = json.loads(r.stdout)["services"]
            # `config` prints a literal $ as $$ so its output stays interpolation-safe.
            got = {n: {k: v.replace("$$", "$") for k, v in svc[n]["environment"].items()} for n in ("a", "u")}
            self.assertEqual(got["a"], parse_env((d / "api.env").read_text()))
            self.assertEqual(got["u"], parse_env((d / "ui.env").read_text()))
            self.assertEqual(json.loads(got["a"]["COMMSGUN_CONFIG"])["apps"][2]["channels"]["email"]["senders"], [TRICKY, BACKSLASH])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class Assertions(unittest.TestCase):
    """Review Focus 1 and 2: a deploy without the owner's values, or with a registry/senders mismatch, stops early."""

    def test_complete_secrets_pass(self):
        self.assertTrue(assertions_pass(secrets()))

    def test_missing_or_empty_secrets_fail(self):
        for key in ["resend_api_key", "commsgun_senders", *GENERATED, *[a["key"] for a in V["commsgun_apps"]]]:
            self.assertFalse(assertions_pass(secrets(**{key: None})), key)
            self.assertFalse(assertions_pass(secrets(**{key: ""})), key)

    def test_a_value_that_would_break_its_env_line_fails(self):
        self.assertFalse(assertions_pass(secrets(resend_api_key="re_a'b")))
        self.assertFalse(assertions_pass(secrets(commsgun_admin_api_token="a\nb" + "x" * 40)))
        self.assertFalse(assertions_pass(secrets(commsgun_admin_api_token="a\rb" + "x" * 40)))
        self.assertFalse(assertions_pass(secrets(resend_api_key="re_x\\")))
        self.assertFalse(assertions_pass(secrets(commsgun_session_secret="x" * 40 + "\\'y")))
        for key in GENERATED + [a["key"] for a in V["commsgun_apps"]]:
            self.assertFalse(assertions_pass(secrets(**{key: fake(key) + "\\"})), key)

    def test_the_json_line_escapes_quotes_and_backslashes_for_a_single_quoted_value(self):
        text = render_config(secrets())
        self.assertNotIn("'", text)
        self.assertIn(r"\u0027", text)
        self.assertIn(r"Ops\\\\Team \\\u0027q\\\u0027", text)  # each backslash doubled, each quote \u0027
        self.assertEqual(json.loads(text)["apps"][2]["channels"]["email"]["senders"], [TRICKY, BACKSLASH])

    def test_an_email_app_without_senders_fails(self):
        senders = {k: v for k, v in SENDERS.items() if k != "ops"}
        self.assertFalse(assertions_pass(secrets(commsgun_senders=senders)))

    def test_an_empty_sender_list_fails(self):
        self.assertFalse(assertions_pass(secrets(commsgun_senders={**SENDERS, "ops": {"senders": [], "default_sender": TRICKY}})))

    def test_a_default_sender_outside_the_list_fails(self):
        bad = {**SENDERS, "ops": {"senders": [TRICKY], "default_sender": "Other <other@example.com>"}}
        self.assertFalse(assertions_pass(secrets(commsgun_senders=bad)))

    def test_senders_for_an_unregistered_app_fail(self):
        extra = {**SENDERS, "nobody": {"senders": ["N <n@example.com>"], "default_sender": "N <n@example.com>"}}
        self.assertFalse(assertions_pass(secrets(commsgun_senders=extra)))

    def test_a_sender_that_is_not_a_single_line_string_fails(self):
        for bad in (["A <a@example.com>\nBcc: x@example.com"], ["A <a@example.com>\r"], [7], [["A <a@example.com>"]]):
            senders = {**SENDERS, "ops": {"senders": [TRICKY, *bad], "default_sender": TRICKY}}
            self.assertFalse(assertions_pass(secrets(commsgun_senders=senders)), bad)
        for bad in (7, "A <a@example.com>\n", None):
            entry = {"senders": [TRICKY], **({} if bad is None else {"default_sender": bad})}
            self.assertFalse(assertions_pass(secrets(commsgun_senders={**SENDERS, "ops": entry})), bad)

    def test_senders_must_be_a_map(self):
        self.assertFalse(assertions_pass(secrets(commsgun_senders="Alerts <alerts@example.com>")))

    def test_secrets_shorter_than_the_service_accepts_fail(self):
        # api/internal/constants MinSecretLen = 32; ui/src/lib/server/env.ts MIN_ADMIN_PASSWORD = 16
        for key in ["commsgun_session_secret", "commsgun_admin_api_token", *[a["key"] for a in V["commsgun_apps"]]]:
            self.assertFalse(assertions_pass(secrets(**{key: "x" * 31})), key)
            self.assertTrue(assertions_pass(secrets(**{key: "x" * 32})), key)
        self.assertFalse(assertions_pass(secrets(commsgun_admin_password="x" * 15)))
        self.assertTrue(assertions_pass(secrets(commsgun_admin_password="x" * 16)))

    def test_senders_given_as_a_map_fail(self):
        entry = {"senders": {"a": "Alerts <alerts@example.com>"}, "default_sender": "a"}
        self.assertFalse(assertions_pass(secrets(commsgun_senders={**SENDERS, "ops": entry})))

    def test_a_sender_without_an_address_shape_fails(self):
        for bad in ("Alerts", "alerts@", "@example.com", "Alerts alerts@example.com", "Alerts <alerts@example.com",
                    "a@b@example.com", "Alerts <alerts@example.com> x", "<>"):
            senders = {**SENDERS, "ops": {"senders": [TRICKY, bad], "default_sender": TRICKY}}
            self.assertFalse(assertions_pass(secrets(commsgun_senders=senders)), bad)
            senders = {**SENDERS, "ops": {"senders": [TRICKY, bad], "default_sender": bad}}
            self.assertFalse(assertions_pass(secrets(commsgun_senders=senders)), bad)
        good = {**SENDERS, "ops": {"senders": ["ops@example.com", TRICKY], "default_sender": "ops@example.com"}}
        self.assertTrue(assertions_pass(secrets(commsgun_senders=good)))

    def test_the_unknown_entries_message_counts_them_without_naming_them(self):
        extra = {**SENDERS, "leak@example.com": {"senders": ["N <n@example.com>"], "default_sender": "N <n@example.com>"},
                 "nobody": {"senders": ["N <n@example.com>"], "default_sender": "N <n@example.com>"}}
        msg = first_failure(secrets(commsgun_senders=extra))
        self.assertIsNotNone(msg)
        self.assertNotIn("leak", msg)
        self.assertNotIn("nobody", msg)
        self.assertIn("2", msg)

    def test_failure_messages_name_keys_or_app_ids_only(self):
        for t in BLOCK["block"]:
            msg = t.get("ansible.builtin.assert", {}).get("fail_msg", "")
            for ref in re.findall(r"\{\{(.*?)\}\}", msg):
                self.assertIn(ref.strip(), {"item", "item.id", "edge_secrets.commsgun_senders | list | reject('in', _email_apps) | list | length"}, msg)


class Compose(unittest.TestCase):
    S = COMPOSE["services"]

    def test_hardened_read_only_and_pinned(self):
        hardening = COMPOSE["x-hardening"]
        for name, image in (("commsgun-api", V["commsgun_api_image"]), ("commsgun-ui", V["commsgun_ui_image"])):
            svc = self.S[name]
            self.assertEqual({k: svc[k] for k in hardening}, hardening, name)
            self.assertIs(svc["read_only"], True, name)
            self.assertEqual(svc["image"], image, name)
        text = (ROOT / "roles/stack/templates/compose.yml.j2").read_text()
        self.assertIn("image: {{ commsgun_api_image }}", text)
        self.assertIn("image: {{ commsgun_ui_image }}", text)

    def test_api(self):
        api = self.S["commsgun-api"]
        self.assertEqual(api["ports"], ["127.0.0.1:9200:9200"])
        self.assertEqual(api["volumes"], ["commsgun-data:/data"])
        self.assertEqual((api["mem_limit"], api["stop_grace_period"]), ("96m", "30s"))
        self.assertEqual(api["env_file"], ["./commsgun/api.env"])
        self.assertIn("commsgun-data", COMPOSE["volumes"])

    def test_ui(self):
        ui = self.S["commsgun-ui"]
        self.assertEqual(ui["ports"], ["127.0.0.1:9202:9202"])
        self.assertEqual(ui["mem_limit"], "192m")
        self.assertEqual(ui["depends_on"], ["commsgun-api"])
        self.assertEqual(ui["env_file"], ["./commsgun/ui.env"])

    def test_the_admin_port_is_never_published(self):
        published = [p for svc in self.S.values() for p in svc.get("ports", [])]
        self.assertFalse([p for p in published if "9201" in str(p)])

    def test_own_network_shared_with_nobody_else(self):
        self.assertEqual(COMPOSE["networks"], {"commsgun": {}})
        for name, svc in self.S.items():
            if name.startswith("commsgun-"):
                self.assertEqual(svc["networks"], ["commsgun"], name)
            else:
                self.assertNotIn("commsgun", svc.get("networks", []), name)


if __name__ == "__main__":
    unittest.main()
