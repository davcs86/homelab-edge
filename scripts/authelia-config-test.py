#!/usr/bin/env python3
"""Renders Authelia's configuration from the prod group_vars plus fake secrets and checks it against the SSO spec (§ 4.4–4.6)."""
import base64
import re
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


if __name__ == "__main__":
    unittest.main()
