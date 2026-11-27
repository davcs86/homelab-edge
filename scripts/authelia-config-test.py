#!/usr/bin/env python3
"""Renders Authelia's configuration, the SSO env files, their pre-write assertions and the compose services from the prod
group_vars plus fake secrets, and checks them against the SSO spec (§ 4.1, § 4.4–4.6, § 4.8, § 4.10)."""
import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import init_secrets  # noqa: E402
import secret_keys  # noqa: E402

VARS_DIR = ROOT / "inventories/prod/group_vars/all"
TEMPLATE = ROOT / "roles/stack/templates/authelia-config.yml.j2"
ENV_NAMES = ["EDGE_SESSION_SECRET", "EDGE_STORAGE_ENCRYPTION_KEY", "EDGE_RESET_PASSWORD_JWT_SECRET", "EDGE_OIDC_HMAC_SECRET",
             "EDGE_LDAP_BIND_PASSWORD"]
JWKS_TEMPLATE = '{{ env "EDGE_OIDC_JWKS_KEY_B64" | b64dec | mindent 10 "|" | msquote }}'
CLAUDE_CALLBACKS = ["https://claude.ai/api/mcp/auth_callback", "https://claude.com/api/mcp/auth_callback"]


def load_vars():
    raw = {}
    for f in sorted(VARS_DIR.glob("*.yml")):
        raw.update(yaml.safe_load(f.read_text()) or {})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)

    def resolve(v, ctx):  # nested too: the client registry's items reference hostnames
        if isinstance(v, str):
            return env.from_string(v).render(**ctx)
        if isinstance(v, list):
            return [resolve(x, ctx) for x in v]
        if isinstance(v, dict):
            return {k: resolve(x, ctx) for k, x in v.items()}
        return v

    resolved = dict(raw)
    for _ in range(10):
        nxt = {k: resolve(v, resolved) for k, v in resolved.items()}
        if nxt == resolved:
            break
        resolved = nxt
    return resolved


V = load_vars()
# trim_blocks as Ansible's template module; b64encode as Ansible's filter.
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
ENV.filters["b64encode"] = lambda s: base64.b64encode(s.encode()).decode()
ENV.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None  # Ansible's `match` test
ENV.filters["from_json"] = json.loads
ENV.filters["to_json"] = json.dumps
CLIENT_KEYS = [c["key"] for c in V["authelia_oidc_clients"] if "key" in c]


def fake_secrets():
    s = {k: f"$pbkdf2-sha512$310000$fakesalt{i}$fakedigest{i}" for i, k in enumerate(CLIENT_KEYS)}
    for k in ("authelia_session_secret", "authelia_storage_encryption_key", "authelia_reset_password_jwt_secret",
              "authelia_oidc_hmac_secret", "authelia_ldap_bind_password", "authelia_oidc_jwks_key"):
        s[k] = f"fake-{k}-value"
    for k in init_secrets.OIDC_CLIENT_KEYS:
        s[k] = f"fake-{k}-plaintext"
    return s


def render(s=None):
    return ENV.from_string(TEMPLATE.read_text()).render(**V, ansible_managed="test", edge_secrets=fake_secrets() if s is None else s)


def parse(text):
    """Authelia's own templates are not YAML; each becomes the scalar ENV:<name> for parsing."""
    return yaml.safe_load(re.sub(r'\{\{ env "(EDGE_[A-Z0-9_]+)"[^}]*\}\}', r"ENV:\1", text))


TEXT = render()
C = parse(TEXT)
OIDC = C["identity_providers"]["oidc"]
CLIENTS = {c["client_id"]: c for c in OIDC["clients"]}
AUTH = V["authelia_public_hostname"]


class Portal(unittest.TestCase):
    def test_portal_and_cookie_follow_the_public_name(self):
        self.assertEqual(AUTH, f"auth.{V['public_zone']}")
        self.assertEqual(C["session"]["cookies"], [{
            "domain": V["public_zone"], "authelia_url": f"https://{AUTH}", "name": "authelia_session",
            "expiration": "1h", "inactivity": "15m", "remember_me": "720h"}])
        self.assertEqual(C["session"]["secret"], "ENV:EDGE_SESSION_SECRET")
        self.assertNotIn("redis", C["session"])  # in-memory sessions

    def test_access_control_has_the_one_portal_rule(self):
        self.assertEqual(C["access_control"], {"default_policy": "deny", "rules": [
            {"domain": AUTH, "policy": "two_factor", "subject": "group:sso_people"}]})

    def test_authelias_oidc_limits_match_the_backchannel_zone(self):
        limits = C["server"]["endpoints"]["rate_limits"]
        names = {f"openid_connect_{n}" for n in ("token", "revocation", "userinfo", "introspection", "pushed_authorization_request")}
        self.assertEqual(set(limits), names)  # no reset-password, second-factor or session-elevation key: pinned defaults
        zone = V["ratelimit"]["authelia_backchannel"]
        self.assertEqual((zone["events"], zone["window"]), (600, "1m"))
        for name in names:
            self.assertEqual(limits[name], {"buckets": [{"period": zone["window"], "requests": zone["events"]}]}, name)
        self.assertEqual(list(C["server"]["endpoints"]), ["rate_limits"])

    def test_the_image_healthcheck_is_disabled(self):
        self.assertIs(C["server"]["disable_healthcheck"], True)
        self.assertEqual(C["server"]["address"], "tcp://0.0.0.0:9091/")
        self.assertEqual(set(C["server"]), {"address", "disable_healthcheck", "endpoints"})

    def test_policies_deny_by_default_and_require_two_factor(self):
        self.assertNotIn("one_factor", TEXT)
        self.assertNotIn("bypass", TEXT)
        for name, policy in OIDC["authorization_policies"].items():
            self.assertEqual(policy, {"default_policy": "deny", "rules": [{"policy": "two_factor", "subject": f"group:{name}"}]}, name)

    def test_regulation_matches_the_spec(self):
        self.assertEqual(C["regulation"], {"modes": ["user"], "max_retries": 3, "find_time": "2m", "ban_time": "5m"})

    def test_password_reset_and_elevated_session(self):
        self.assertNotIn("password_reset", C["authentication_backend"])  # 4.39.28 default: reset on
        self.assertEqual(C["identity_validation"], {
            "reset_password": {"jwt_lifespan": "1h", "jwt_secret": "ENV:EDGE_RESET_PASSWORD_JWT_SECRET"},
            "elevated_session": {"require_second_factor": True}})

    def test_ldap_logs_in_by_uid_only_and_only_sso_people(self):
        dn = V["lldap_base_dn"]
        self.assertEqual(dn, "dc=natanddavehouse,dc=com")
        self.assertEqual(C["authentication_backend"]["ldap"], {
            "implementation": "lldap", "address": "ldap://lldap:3890", "base_dn": dn,
            "users_filter": f"(&({{username_attribute}}={{input}})(objectClass=person)(memberOf=cn=sso_people,ou=groups,{dn}))",
            "user": f"uid={V['authelia_ldap_bind_user']},ou=people,{dn}", "password": "ENV:EDGE_LDAP_BIND_PASSWORD"})
        self.assertEqual((V["authelia_ldap_bind_user"], V["authelia_ldap_bind_email"]), ("authelia", "authelia@ldap.invalid"))
        self.assertNotIn("mail_attribute", TEXT)  # lldap's default username attribute (uid) stands; no login by email
        self.assertNotIn("file", C["authentication_backend"])

    def test_lifespans(self):
        self.assertEqual(OIDC["lifespans"], {"access_token": "1h", "refresh_token": "720h",
                                             "custom": {"webhooks-mcp": {"access_token": "5m", "refresh_token": "720h"}}})
        self.assertEqual({i: c["lifespan"] for i, c in CLIENTS.items() if "lifespan" in c}, {"webhooks-mcp-claude": "webhooks-mcp"})


class Clients(unittest.TestCase):
    def test_clients_match_the_spec_table(self):
        app, staging, hooks, zone = (V["public_app_hostname"], V["staging_public_hostname"], V["webhooks_mcp_public_hostname"],
                                     V["public_zone"])
        rw = ["xstockstrat.read", "xstockstrat.write", "xstockstrat.admin", "xstockstrat.trade", "offline_access"]
        web = {"xstockstrat-web": ("xstockstrat", f"https://{app}/api/auth/sso/callback"),
               "xstockstrat-staging-web": ("xstockstrat-staging", f"https://{staging}/api/auth/sso/callback"),
               "commsgun": ("commsgun", f"https://commsgun.{zone}/auth/callback"),
               "happybabytracker": ("happybabytracker", f"https://happybaby.{zone}/auth/callback")}
        mcp = {"xstockstrat-mcp-claude": ("xstockstrat", rw, f"https://{app}/agent"),
               "xstockstrat-staging-mcp-claude": ("xstockstrat-staging", rw, f"https://{staging}/agent"),
               "xstockstrat-mcp-claude-ro": ("xstockstrat", ["xstockstrat.read", "offline_access"], f"https://{app}/agent"),
               "xstockstrat-staging-mcp-claude-ro": ("xstockstrat-staging", ["xstockstrat.read", "offline_access"], f"https://{staging}/agent"),
               "webhooks-mcp-claude": ("webhooks-mcp", ["webhooks-mcp", "offline_access"], f"https://{hooks}/mcp")}
        self.assertEqual(list(CLIENTS), [c["client_id"] for c in OIDC["clients"]])  # no duplicate id
        self.assertEqual(set(CLIENTS), set(web) | set(mcp))
        self.assertEqual(len(CLIENTS), 9)
        common = {"consent_mode": "explicit", "require_pkce": True, "pkce_challenge_method": "S256"}
        hashes = fake_secrets()
        for cid, (group, redirect) in web.items():
            key = f"authelia_client_secret_{cid.replace('-', '_')}_pbkdf2"
            self.assertEqual(CLIENTS[cid], {"client_id": cid, "authorization_policy": group, **common, "public": False,
                                            "client_secret": hashes[key], "token_endpoint_auth_method": "client_secret_basic",
                                            "redirect_uris": [redirect], "scopes": ["openid", "profile", "email"],
                                            "grant_types": ["authorization_code"], "claims_policy": "web"}, cid)
        for cid, (group, scopes, audience) in mcp.items():
            extra = {"lifespan": "webhooks-mcp"} if cid == "webhooks-mcp-claude" else {}
            self.assertEqual(CLIENTS[cid], {"client_id": cid, "authorization_policy": group, **common, "public": True,
                                            "token_endpoint_auth_method": "none", "redirect_uris": CLAUDE_CALLBACKS,
                                            "scopes": scopes, "audience": [audience], "requested_audience_mode": "implicit",
                                            "access_token_signed_response_alg": "RS256",
                                            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"], **extra}, cid)
        self.assertEqual(OIDC["claims_policies"], {"web": {"id_token": ["email", "preferred_username"]}})
        self.assertNotIn("client_credentials", TEXT)
        self.assertFalse(re.search(r"localhost|127\.0\.0\.1|\[::1\]|http://", TEXT.split("clients:", 1)[1]))

    def test_every_client_group_is_declared_and_every_app_group_is_used(self):
        groups = V["sso_groups"]
        self.assertEqual(groups, ["sso_people", "xstockstrat", "xstockstrat-staging", "webhooks-mcp", "commsgun", "happybabytracker"])
        apps = set(groups) - {"sso_people"}
        self.assertEqual({c["group"] for c in V["authelia_oidc_clients"]}, apps)
        self.assertEqual(set(OIDC["authorization_policies"]), apps)
        self.assertEqual({c["authorization_policy"] for c in CLIENTS.values()}, apps)

    def test_the_client_keys_match_init_secrets(self):
        self.assertEqual(sorted(CLIENT_KEYS), sorted(f"{k}_pbkdf2" for k in init_secrets.OIDC_CLIENT_KEYS))
        self.assertEqual({c["id"] for c in V["authelia_oidc_clients"] if "key" in c},
                         {c["id"] for c in V["authelia_oidc_clients"] if c["kind"] == "web"})

    def test_confidential_clients_read_their_hash_through_the_registry(self):
        for value in (fake_secrets()[k] for k in CLIENT_KEYS):
            self.assertEqual(TEXT.count(value), 1, value)
        source = TEMPLATE.read_text()
        self.assertIn("{% for client in authelia_oidc_clients %}", source)
        self.assertEqual(re.findall(r"edge_secrets[.\[][^ }]*", source), ["edge_secrets[client.key]"])  # EDGE-08
        self.assertTrue(set(CLIENT_KEYS) <= secret_keys.used_keys())


class Secrets(unittest.TestCase):
    def test_no_secret_value_is_rendered_and_every_env_template_is_literal(self):
        for k, value in fake_secrets().items():
            if k not in CLIENT_KEYS:
                self.assertNotIn(value, TEXT, k)
        for name in ENV_NAMES:
            self.assertEqual(TEXT.count(f"'{{{{ env \"{name}\" }}}}'"), 1, name)
        self.assertEqual(TEXT.count(JWKS_TEMPLATE), 1)
        self.assertIn(f"        key: {JWKS_TEMPLATE}\n", TEXT)  # mindent 10: the key's value sits two columns deeper
        self.assertEqual(sorted(set(re.findall(r'env "([A-Z0-9_]+)"', TEXT))), sorted(ENV_NAMES + ["EDGE_OIDC_JWKS_KEY_B64"]))
        self.assertNotIn("AUTHELIA_", TEXT.replace("X_AUTHELIA_CONFIG_FILTERS", ""))  # AUTHELIA_* names map to config keys
        self.assertEqual(OIDC["hmac_secret"], "ENV:EDGE_OIDC_HMAC_SECRET")
        self.assertEqual(C["storage"]["encryption_key"], "ENV:EDGE_STORAGE_ENCRYPTION_KEY")
        self.assertEqual(OIDC["jwks"], [{"algorithm": "RS256", "key": "ENV:EDGE_OIDC_JWKS_KEY_B64"}])

    def test_the_notifier_is_the_file_notifier_until_p5(self):
        self.assertEqual(C["notifier"], {"filesystem": {"filename": "/data/notification.txt"}})
        self.assertNotIn("smtp", TEXT)
        self.assertEqual(C["storage"]["local"], {"path": "/data/db.sqlite3"})
        self.assertEqual(set(C["storage"]), {"encryption_key", "local"})


# ---------- compose services, env files, volume ownership (Task 5) ----------
PLAIN_KEYS = ["authelia_session_secret", "authelia_storage_encryption_key", "authelia_reset_password_jwt_secret",
              "authelia_oidc_hmac_secret", "authelia_ldap_bind_password", "lldap_jwt_secret", "lldap_key_seed", "lldap_admin_password"]
FAKE_PEM = "-----BEGIN PRIVATE KEY-----\nZmFrZS1qd2tzLWtleQ==\n-----END PRIVATE KEY-----\n"
BLOCK_NAME = "SSO secrets and configuration"
AUTHELIA_ENV_TASK, LLDAP_ENV_TASK, CONFIG_TASK = "Authelia environment", "Lldap environment", "Authelia configuration"
CREATE_TASK, OWNER_TASK = "Create the SSO containers and their volumes", "SSO volumes belong to uid 10001"
PINS = {"authelia_image": "authelia/authelia:4.39.28@sha256:bd97cff4fcbf715b5ff1f9ae286afbe6033afce385302520b0368122d43a6f54",
        "lldap_image": "lldap/lldap:v0.6.3-alpine-rootless@sha256:ba2c50930ea998eefd5454aa678a7977448019248b1827da87d330df0b71c284"}


def stack_secrets(**over):
    s = fake_secrets() | {k: f"fake-{k}-".ljust(64, "x") for k in PLAIN_KEYS} | {"authelia_oidc_jwks_key": FAKE_PEM}
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
COMPOSE_TEXT = (ROOT / "roles/stack/templates/compose.yml.j2").read_text()
COMPOSE = yaml.safe_load(ENV.from_string(COMPOSE_TEXT).render(**V, ansible_managed="test"))  # resolves `<<: *hardening`
S = COMPOSE["services"]


def render_env(task_name, s):
    return ENV.from_string(find_task(TASKS, task_name)["ansible.builtin.copy"]["content"]).render(**V, edge_secrets=s)


def parse_env(text):
    """Every line is NAME='value' with no quote or line break inside: Compose takes a single-quoted value verbatim."""
    out = {}
    for line in text.splitlines():
        m = re.fullmatch(r"([A-Z][A-Z0-9_]*)='([^'\n]*)'", line)
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


class Stack(unittest.TestCase):
    def test_services_are_hardened_read_only_and_loopback_only(self):
        hardening = COMPOSE["x-hardening"]
        self.assertEqual((hardening["cap_drop"], hardening["security_opt"]), (["ALL"], ["no-new-privileges:true"]))
        for name, image, env_file, volume in (("authelia", V["authelia_image"], "./authelia/authelia.env", "authelia-data:/data"),
                                              ("lldap", V["lldap_image"], "./lldap/lldap.env", "lldap-data:/data")):
            svc = S[name]
            self.assertEqual({k: svc[k] for k in hardening}, hardening, name)
            self.assertEqual((svc["user"], svc["read_only"], svc["mem_limit"]), ("10001:10001", True, "128m"), name)
            self.assertEqual((svc["image"], svc["env_file"], svc["networks"]), (image, [env_file], ["authelia"]), name)
            self.assertIn(volume, svc["volumes"], name)
            self.assertNotIn("environment", svc, name)
        self.assertEqual((V["authelia_port"], V["lldap_ui_port"]), (9091, 17170))
        self.assertEqual(S["authelia"]["ports"], ["127.0.0.1:9091:9091"])
        self.assertEqual(S["lldap"]["ports"], ["127.0.0.1:17170:17170"])
        self.assertIn('ports: ["127.0.0.1:{{ authelia_port }}:9091"]', COMPOSE_TEXT)
        self.assertIn('ports: ["127.0.0.1:{{ lldap_ui_port }}:17170"]', COMPOSE_TEXT)
        published = [str(p) for svc in S.values() for p in svc.get("ports", [])]
        self.assertFalse([p for p in published if "3890" in p], published)
        self.assertEqual(S["authelia"]["volumes"], ["./authelia/config:/config:ro", "authelia-data:/data"])
        self.assertEqual(S["lldap"]["volumes"], ["lldap-data:/data"])
        self.assertEqual(S["authelia"]["command"], ["authelia", "--config", "/config/configuration.yml"])
        self.assertEqual(S["authelia"]["depends_on"], {"lldap": {"condition": "service_healthy"}})
        self.assertEqual(COMPOSE["networks"]["authelia"], {})  # not internal: Authelia's startup NTP check goes out
        for name, svc in S.items():
            if name not in ("authelia", "lldap"):
                self.assertNotIn("authelia", svc.get("networks", []), name)
        self.assertIn("authelia-data", COMPOSE["volumes"])
        self.assertIn("lldap-data", COMPOSE["volumes"])

    def test_authelia_has_its_own_health_probe(self):
        ntfy = S["ntfy"]["healthcheck"]
        self.assertEqual(S["authelia"]["healthcheck"], {
            "test": ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:9091/api/health"],
            "interval": ntfy["interval"], "timeout": ntfy["timeout"], "retries": ntfy["retries"]})  # S3
        self.assertEqual(S["lldap"]["healthcheck"], {
            "test": ["CMD", "wget", "-qO-", "http://127.0.0.1:17170/health"],
            "interval": ntfy["interval"], "timeout": ntfy["timeout"], "retries": ntfy["retries"]})
        self.assertIs(C["server"]["disable_healthcheck"], True)

    def test_compose_holds_no_secret(self):
        for k, value in stack_secrets().items():
            self.assertNotIn(value, json.dumps(COMPOSE), k)
        self.assertNotIn("edge_secrets", COMPOSE_TEXT)

    def test_pins_keep_the_quoted_digest_shape_and_have_a_versions_row(self):
        versions = (VARS_DIR / "versions.yml").read_text()
        docs = (ROOT / "docs/versions.md").read_text()
        for var, pin in PINS.items():
            lines = re.findall(rf"^{var}:.*$", versions, re.M)
            self.assertEqual(lines, [f'{var}: "{pin}"'], var)  # EDGE-01: double quotes, no trailing comment
            self.assertRegex(lines[0], rf'^{var}: ".*@sha256:[0-9a-f]{{64}}"$')
            self.assertEqual(V[var], pin)
            self.assertEqual(len([r for r in docs.splitlines() if r.startswith("|") and f"`{var}`" in r]), 1, var)


class StackAssertions(unittest.TestCase):
    def test_assertions_run_before_any_file_is_written(self):
        self.assertTrue(BLOCK, f"no task named {BLOCK_NAME!r}")
        self.assertIs(BLOCK.get("no_log"), True)
        kinds = [next(iter(set(t) & {"ansible.builtin.assert", "ansible.builtin.copy", "ansible.builtin.template"}))
                 for t in BLOCK["block"]]
        writes = [k for k in kinds if k != "ansible.builtin.assert"]
        self.assertEqual(kinds, ["ansible.builtin.assert"] * (len(kinds) - len(writes)) + writes, kinds)
        self.assertEqual(sorted(writes), ["ansible.builtin.copy"] * 2 + ["ansible.builtin.template"])
        names = [t.get("name") for t in TASKS]
        self.assertLess(names.index("Happybabytracker secrets and configuration"), names.index(BLOCK_NAME))
        self.assertLess(names.index(BLOCK_NAME), names.index("Compose file"))
        dirs = find_task(TASKS, "Stack directories")["loop"]
        for d in ({"path": "{{ edge_root }}/authelia", "mode": "0750"}, {"path": "{{ edge_root }}/authelia/config", "mode": "0755"},
                  {"path": "{{ edge_root }}/lldap", "mode": "0750"}):
            self.assertIn(d, dirs)
        self.assertIsNone(first_failure(stack_secrets()))

    def test_each_secret_is_present_a_string_and_free_of_quote_backslash_cr_lf(self):
        for key in PLAIN_KEYS + CLIENT_KEYS:
            good = stack_secrets()[key]
            for bad in (None, "", 7, ["x" * 40], good[:2] + "'" + good[2:], good + "\\", good + "\r", good + "\n", "a\nb" + good):
                msg = first_failure(stack_secrets(**{key: bad}))
                self.assertIsNotNone(msg, (key, repr(bad)))
                self.assertIn(key, msg)
                if isinstance(bad, str) and bad:
                    self.assertNotIn(bad.strip(), msg)

    def test_the_jwks_key_must_be_a_pkcs8_pem(self):
        key = "authelia_oidc_jwks_key"
        for bad in (None, 7, "", "fake-key", "-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----\n"):
            msg = first_failure(stack_secrets(**{key: bad}))
            self.assertIsNotNone(msg, repr(bad))
            self.assertIn(key, msg)
            self.assertNotIn("ZmFrZQ", msg)

    def test_failure_messages_name_keys_only(self):
        for t in BLOCK["block"]:
            msg = t.get("ansible.builtin.assert", {}).get("fail_msg", "")
            for ref in re.findall(r"\{\{(.*?)\}\}", msg):
                self.assertEqual(ref.strip(), "item.key", msg)
        text = yaml.safe_dump(BLOCK)
        for ref in re.findall(r"edge_secrets(\.[a-z_0-9]+|\[[^\]]*\])", text):  # EDGE-08: literal reads or `key:` loops
            self.assertTrue(ref[1:] in PLAIN_KEYS + ["authelia_oidc_jwks_key"] or ref == "[item.key]", ref)
        loop_keys = {i["key"] for t in BLOCK["block"] if isinstance(t.get("loop"), list) for i in t["loop"]}
        self.assertEqual(loop_keys, set(PLAIN_KEYS))
        self.assertTrue(set(PLAIN_KEYS) | {"authelia_oidc_jwks_key"} <= secret_keys.used_keys())


class StackEnvFiles(unittest.TestCase):
    def test_env_files_hold_exactly_the_contracted_variables(self):
        s = stack_secrets()
        authelia_env = render_env(AUTHELIA_ENV_TASK, s)
        self.assertEqual(authelia_env.splitlines()[0], "X_AUTHELIA_CONFIG_FILTERS='template'")  # B1
        self.assertNotIn("X_AUTHELIA_CFG_FILTERS", authelia_env)
        self.assertEqual(parse_env(authelia_env), {
            "X_AUTHELIA_CONFIG_FILTERS": "template",
            "EDGE_SESSION_SECRET": s["authelia_session_secret"],
            "EDGE_STORAGE_ENCRYPTION_KEY": s["authelia_storage_encryption_key"],
            "EDGE_RESET_PASSWORD_JWT_SECRET": s["authelia_reset_password_jwt_secret"],
            "EDGE_OIDC_HMAC_SECRET": s["authelia_oidc_hmac_secret"],
            "EDGE_LDAP_BIND_PASSWORD": s["authelia_ldap_bind_password"],
            "EDGE_OIDC_JWKS_KEY_B64": base64.b64encode(FAKE_PEM.encode()).decode()})
        self.assertEqual(set(parse_env(authelia_env)) - {"X_AUTHELIA_CONFIG_FILTERS"}, set(ENV_NAMES + ["EDGE_OIDC_JWKS_KEY_B64"]))
        self.assertEqual(parse_env(render_env(LLDAP_ENV_TASK, s)), {
            "LLDAP_JWT_SECRET": s["lldap_jwt_secret"], "LLDAP_KEY_SEED": s["lldap_key_seed"],
            "LLDAP_LDAP_USER_PASS": s["lldap_admin_password"], "LLDAP_LDAP_BASE_DN": V["lldap_base_dn"],
            "LLDAP_DATABASE_URL": "sqlite:///data/users.db?mode=rwc"})
        self.assertIs(BLOCK.get("no_log"), True)
        self.assertNotIn("notify", BLOCK)
        for name, dest in ((AUTHELIA_ENV_TASK, "authelia/authelia.env"), (LLDAP_ENV_TASK, "lldap/lldap.env")):
            task = find_task(TASKS, name)
            copy = task["ansible.builtin.copy"]
            self.assertEqual((copy["dest"], copy["mode"]), ("{{ edge_root }}/" + dest, "0600"))
            self.assertNotIn("notify", task, name)  # EDGE-04: env_file changes recreate the container
        config = find_task(TASKS, CONFIG_TASK)
        self.assertEqual(config["ansible.builtin.template"], {
            "src": "authelia-config.yml.j2", "dest": "{{ edge_root }}/authelia/config/configuration.yml", "mode": "0644"})
        self.assertNotIn("notify", config)  # § 4.8: the hash-compare restart, never a handler

    def test_volume_ownership_is_set_before_lldap_starts(self):
        names = [t.get("name") for t in TASKS]
        self.assertLess(names.index("Compose file"), names.index(CREATE_TASK))
        self.assertLess(names.index(CREATE_TASK), names.index(OWNER_TASK))
        self.assertLess(names.index(OWNER_TASK), names.index("Start the stack"))
        create = find_task(TASKS, CREATE_TASK)
        self.assertEqual(create["ansible.builtin.command"],
                         "docker compose -f {{ edge_root }}/compose.yml create --no-recreate lldap authelia")
        self.assertIs(create["changed_when"], True)  # EDGE-03
        owner = find_task(TASKS, OWNER_TASK)
        self.assertEqual(owner["ansible.builtin.file"], {
            "path": "/var/lib/docker/volumes/edge_{{ item }}/_data", "state": "directory",
            "owner": "10001", "group": "10001", "mode": "0700"})
        self.assertEqual(owner["loop"], ["authelia-data", "lldap-data"])

    @unittest.skipUnless(shutil.which("docker") or os.environ.get("SSO_STACK_TEST_REQUIRE_DOCKER"), "docker is required")
    def test_docker_compose_config_reads_back(self):
        d = Path(tempfile.mkdtemp())
        try:
            s = stack_secrets()
            for sub in ("authelia", "lldap"):
                (d / sub).mkdir()
            (d / "authelia/authelia.env").write_text(render_env(AUTHELIA_ENV_TASK, s))
            (d / "lldap/lldap.env").write_text(render_env(LLDAP_ENV_TASK, s))
            mini = {"name": "edge", "services": {"authelia": S["authelia"], "lldap": S["lldap"]},
                    "networks": {"authelia": COMPOSE["networks"]["authelia"]},
                    "volumes": {v: COMPOSE["volumes"][v] for v in ("authelia-data", "lldap-data")}}
            (d / "compose.yml").write_text(yaml.safe_dump(mini))
            r = subprocess.run(["docker", "compose", "-f", str(d / "compose.yml"), "config", "--format", "json"],
                               capture_output=True, text=True, check=True, env={**os.environ, "HOME": os.environ.get("HOME", "/")})
            cfg = json.loads(r.stdout)
            svc = cfg["services"]
            self.assertEqual(svc["authelia"]["environment"], parse_env(render_env(AUTHELIA_ENV_TASK, s)))
            self.assertEqual(svc["lldap"]["environment"], parse_env(render_env(LLDAP_ENV_TASK, s)))
            self.assertEqual(base64.b64decode(svc["authelia"]["environment"]["EDGE_OIDC_JWKS_KEY_B64"]).decode(), FAKE_PEM)
            self.assertEqual([(p["host_ip"], p["published"], p["target"]) for p in svc["authelia"]["ports"]], [("127.0.0.1", "9091", 9091)])
            self.assertEqual([(p["host_ip"], p["published"], p["target"]) for p in svc["lldap"]["ports"]], [("127.0.0.1", "17170", 17170)])
            self.assertEqual(cfg["volumes"]["authelia-data"]["name"], "edge_authelia-data")
            self.assertEqual(cfg["volumes"]["lldap-data"]["name"], "edge_lldap-data")
            self.assertEqual(cfg["networks"]["authelia"].get("internal", False), False)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def flat(tasks):
    """Every task in play order, blocks expanded."""
    for t in tasks:
        yield t
        yield from flat(t.get("block", []))


FLAT = list(flat(TASKS))
ORDER = [t.get("name") for t in FLAT]
YAML_TASK, IMAGES_TASK = "PyYAML for the SSO sync (Ubuntu archive)", "Images of the existing SSO containers"
DIGEST_BLOCK = "Back up, then stop Authelia, before an Authelia or lldap digest change"
BACKUP_TASK, STOP_TASK = "Back up before the digest change", "Stop Authelia before the digest change"
LLDAP_UP_TASK = "Start lldap first"
SYNC_COPY_TASK, SYNC_BIN = "SSO sync script", "/usr/local/sbin/edge-sso-sync"
SYNC = {"ensure": ("Ensure lldap's SSO groups, its sso-sub attribute and Authelia's bind account", "Lldap ensure summary",
                   "Stop on a failed lldap ensure", "_sso_ensure"),
        "reconcile": ("Reconcile each sub between Authelia and lldap", "Sub reconcile summary", "Stop on a failed sub reconcile",
                      "_sso_reconcile")}
HASH_TASK, RESTART_BLOCK = "Authelia configuration, on disk and as applied", "Restart Authelia on a configuration change"
START, FLUSH = "Start the stack", "Flush handlers so config changes apply before verification"
COMPOSE_CMD = "docker compose -f {{ edge_root }}/compose.yml "


def before(a, b):
    return ORDER.index(a) < ORDER.index(b)


def when_holds(task, **facts):
    return all(ENV.compile_expression(w)(**V, **facts) for w in ([task["when"]] if isinstance(task["when"], str) else task["when"]))


class DeployOrder(unittest.TestCase):
    def test_python3_yaml_is_installed_before_the_sync(self):
        apt = find_task(TASKS, YAML_TASK)["ansible.builtin.apt"]
        self.assertEqual(apt, {"name": "python3-yaml", "state": "present"})  # the Ubuntu archive, as age (N4)
        self.assertTrue(before(YAML_TASK, SYNC["ensure"][0]))
        rows = [r for r in (ROOT / "docs/versions.md").read_text().splitlines() if r.startswith("| python3-yaml (on edge)")]
        self.assertEqual(len(rows), 1)
        self.assertIn("Ubuntu archive", rows[0])

    def test_a_digest_change_backs_up_then_stops_authelia_before_the_sync(self):
        images = find_task(TASKS, IMAGES_TASK)
        script = images["ansible.builtin.shell"]
        self.assertEqual(COMPOSE["name"], "edge")  # one-off `compose run` containers carry the same service label
        self.assertIn('docker ps -aq --filter label=com.docker.compose.project=edge --filter label=com.docker.compose.service="$s"',
                      script)
        self.assertIn("--filter label=com.docker.compose.oneoff=False", script)
        self.assertNotIn("compose.yml ps", script)
        self.assertIn(".Config.Image", script)
        self.assertEqual((images["register"], images["changed_when"]), ("_sso_images", False))
        block = find_task(TASKS, DIGEST_BLOCK)
        self.assertEqual([t["name"] for t in block["block"]], [BACKUP_TASK, STOP_TASK])
        self.assertEqual(find_task(TASKS, BACKUP_TASK)["ansible.builtin.command"], "systemctl start edge-backup.service")
        stop = find_task(TASKS, STOP_TASK)
        self.assertEqual(stop["ansible.builtin.command"], COMPOSE_CMD + "stop authelia")
        for t in (find_task(TASKS, BACKUP_TASK), stop):
            self.assertIs(t["changed_when"], True)  # EDGE-03
        for later in (LLDAP_UP_TASK, SYNC["ensure"][0], SYNC["reconcile"][0], START):
            self.assertTrue(before(IMAGES_TASK, DIGEST_BLOCK) and before(STOP_TASK, later), later)
        a, ld = "authelia " + V["authelia_image"], "lldap " + V["lldap_image"]
        old_a, old_l = a.replace("@sha256:bd", "@sha256:00"), ld.replace("@sha256:ba", "@sha256:00")
        cases = {(): False, (a, ld): False, (a,): False, (ld,): False,  # fresh volume, or neither digest changed (Q7)
                 (old_a, ld): True, (a, old_l): True, (old_l,): True, (old_a,): True}
        for lines, runs in cases.items():
            self.assertIs(when_holds(block, _sso_images={"stdout_lines": list(lines)}), runs, lines)

    def test_lldap_is_up_and_synced_before_the_stack_starts(self):
        up = find_task(TASKS, LLDAP_UP_TASK)
        self.assertEqual(up["ansible.builtin.command"], COMPOSE_CMD + "up -d --wait lldap")
        self.assertIs(up["changed_when"], True)  # N9
        self.assertTrue(before(OWNER_TASK, LLDAP_UP_TASK))
        copy_task = find_task(TASKS, SYNC_COPY_TASK)
        self.assertEqual(copy_task["ansible.builtin.copy"], {"src": "{{ playbook_dir }}/../scripts/sso_sync.py", "dest": SYNC_BIN,
                                                            "owner": "root", "group": "root", "mode": "0750"})
        steps = [LLDAP_UP_TASK, SYNC_COPY_TASK]
        fake = {"lldap_admin_password": "-fake-admin", "authelia_ldap_bind_password": "-fake-bind"}
        for cmd, (run, summary, stop, reg) in SYNC.items():
            steps += [run, summary, stop]
            t = find_task(TASKS, run)
            c = t["ansible.builtin.command"]
            argv = c["argv"]
            argv = (ENV.compile_expression(argv.strip()[2:-2])(**V) if isinstance(argv, str)
                    else [ENV.from_string(a).render(**V) for a in argv])
            self.assertEqual(argv[:3], ["python3", SYNC_BIN, cmd])
            opts = {"--lldap-url": ["http://127.0.0.1:17170"], "--compose": [V["edge_root"] + "/compose.yml"]}
            want = {"admin_password": fake["lldap_admin_password"]}
            if cmd == "ensure":
                want["bind_password"] = fake["authelia_ldap_bind_password"]
                opts |= {"--groups": V["sso_groups"], "--bind-user": [V["authelia_ldap_bind_user"]],
                         "--bind-email": [V["authelia_ldap_bind_email"]]}
            got, key = {}, None
            for a in argv[3:]:
                key = a if a.startswith("--") else key
                got.setdefault(key, []) if a.startswith("--") else got[key].append(a)
            self.assertEqual(got, opts)
            # environment: and argv both land on sudo's command line; stdin does not.
            self.assertNotIn("environment", t)
            self.assertEqual(json.loads(ENV.from_string(c["stdin"]).render(edge_secrets=fake)), want)
            self.assertIn("edge_secrets.lldap_admin_password", c["stdin"])  # EDGE-08: literal reads
            if cmd == "ensure":
                self.assertIn("edge_secrets.authelia_ldap_bind_password", c["stdin"])
            self.assertNotIn("edge_secrets", json.dumps(c["argv"]))
            self.assertEqual((t["no_log"], t["failed_when"], t["register"]), (True, False, reg))
            # no_log censors a failing task completely (ansible-core 2.21.x): print the summary, then fail.
            dbg = find_task(TASKS, summary)
            self.assertIn(f"{reg}.stdout_lines", dbg["ansible.builtin.debug"]["msg"])
            self.assertNotIn("no_log", dbg)
            fail = find_task(TASKS, stop)
            self.assertIn("ansible.builtin.fail", fail)
            self.assertEqual(fail["when"], f"{reg}.rc | default(1) != 0")
            self.assertNotIn("edge_secrets", yaml.safe_dump([dbg, fail]))
        steps.append(START)
        self.assertEqual([n for n in ORDER if n in steps], steps)
        self.assertEqual(ORDER.index(START) - ORDER.index(LLDAP_UP_TASK), len(steps) - 1)

    def test_no_task_passes_an_lldap_secret_through_the_environment(self):
        reads = re.compile(r"edge_secrets(\.|\[\s*['\"])(lldap_[a-z_]+|authelia_ldap_bind_password)")
        readers = [t for t in FLAT if "block" not in t and reads.search(yaml.safe_dump(t))]
        self.assertTrue(readers)
        for t in readers:
            self.assertNotIn("environment", t, t.get("name"))
            for module in ("ansible.builtin.command", "ansible.builtin.shell", "ansible.builtin.script"):
                body = t.get(module)
                if body is not None:
                    self.assertNotRegex(json.dumps(body.get("argv", body.get("cmd", body)) if isinstance(body, dict) else body),
                                        reads, t.get("name"))  # argv is on the command line too

    def test_the_config_restart_is_an_unconditional_hash_compare(self):
        self.assertTrue(before(START, FLUSH) and before(FLUSH, HASH_TASK))
        h = find_task(TASKS, HASH_TASK)
        self.assertNotIn("when", h)
        self.assertIn("sha256sum {{ edge_root }}/authelia/config/configuration.yml", h["ansible.builtin.shell"])
        self.assertIn("{{ edge_root }}/authelia/config.applied.sha256", h["ansible.builtin.shell"])
        block = find_task(TASKS, RESTART_BLOCK)
        self.assertTrue(before(HASH_TASK, RESTART_BLOCK))
        self.assertIs(when_holds(block, _authelia_config_hash={"stdout_lines": ["a" * 64, "none"]}), True)
        self.assertIs(when_holds(block, _authelia_config_hash={"stdout_lines": ["a" * 64, "b" * 64]}), True)
        self.assertIs(when_holds(block, _authelia_config_hash={"stdout_lines": ["a" * 64, "a" * 64]}), False)
        restart, wait, record = block["block"]
        self.assertEqual(restart["ansible.builtin.command"], COMPOSE_CMD + "restart authelia")
        self.assertIs(restart["changed_when"], True)  # EDGE-03
        self.assertEqual(wait["ansible.builtin.command"], "curl -fsS --max-time 5 http://127.0.0.1:{{ authelia_port }}/api/health")
        self.assertEqual(wait["until"], f"{wait['register']}.rc == 0")  # S3
        self.assertGreaterEqual(wait["retries"] * wait["delay"], 60)
        self.assertEqual(record["ansible.builtin.copy"]["dest"], "{{ edge_root }}/authelia/config.applied.sha256")
        self.assertIn("_authelia_config_hash.stdout_lines[0]", record["ansible.builtin.copy"]["content"])
        self.assertNotIn("notify", find_task(TASKS, CONFIG_TASK))  # § 4.8: never a handler

    def test_the_deploy_checks_the_portal_on_its_public_name(self):
        resolve = "--resolve {{ authelia_public_hostname }}:443:127.0.0.1 https://{{ authelia_public_hostname }}"
        health = find_task(TASKS, "The portal answers on its public name")["ansible.builtin.command"]
        self.assertIn("curl -fsS", health)
        self.assertIn(resolve + "/api/health", health)
        disc = find_task(TASKS, "The portal's discovery names its public issuer exactly")
        self.assertIn("curl -fsS", disc["ansible.builtin.command"])
        self.assertIn(resolve + "/.well-known/openid-configuration", disc["ansible.builtin.command"])
        fw = ENV.compile_expression(disc["failed_when"])
        exact = json.dumps({"issuer": f"https://{AUTH}"})
        self.assertFalse(fw(**V, _authelia_discovery={"rc": 0, "stdout": exact}))
        for bad in (f"https://{AUTH}/", f"http://{AUTH}", f"https://{AUTH.upper()}", "https://auth.example"):
            self.assertTrue(fw(**V, _authelia_discovery={"rc": 0, "stdout": json.dumps({"issuer": bad})}), bad)
        self.assertTrue(fw(**V, _authelia_discovery={"rc": 22, "stdout": ""}))
        self.assertTrue(before(START, "The portal answers on its public name"))


if __name__ == "__main__":
    unittest.main()
