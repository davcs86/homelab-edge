#!/usr/bin/env python3
"""Runs the pinned Authelia image against the rendered configuration (S20, B1) and the generated client hashes (S6), and
starts the rendered authelia and lldap services hardened on fresh volumes (Review Focus 1), in the deploy's order: lldap,
`sso_sync.py ensure`, `sso_sync.py reconcile`, then Authelia; then runs the sync against the live pair (Task 6 Step 4).
SSO_STACK_TEST_REQUIRE_DOCKER=1 turns a skip for want of Docker into a failure."""
import base64
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import init_secrets  # noqa: E402
import sso_sync  # noqa: E402

_spec = importlib.util.spec_from_file_location("authelia_config_test", ROOT / "scripts/authelia-config-test.py")
cfg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfg)

IMAGE = cfg.V["authelia_image"]
REQUIRE = os.environ.get("SSO_STACK_TEST_REQUIRE_DOCKER") == "1"
MATCH, NO_MATCH = "The password matches the digest.", "The password does not match the digest."


def docker_ready():
    return shutil.which("docker") is not None and subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def authelia(*args, env_file=None, config_dir=None):
    cmd = ["docker", "run", "--rm"]
    if env_file:
        cmd += ["--env-file", str(env_file)]
    if config_dir:
        cmd += ["-v", f"{config_dir}:/config:ro"]
    return subprocess.run(cmd + [IMAGE, "authelia", *args], capture_output=True, text=True, timeout=120)


def test_env():
    """docker run's --env-file takes NAME=value verbatim, so no quotes."""
    values = {name: secrets.token_urlsafe(48) for name in cfg.ENV_NAMES}
    values["EDGE_OIDC_JWKS_KEY_B64"] = base64.b64encode(init_secrets.rsa_private_key_pem().encode()).decode()
    return values


@unittest.skipUnless(REQUIRE or docker_ready(), "docker is required")
class PinnedImage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.dir.chmod(0o755)
        (cls.dir / "config").mkdir(mode=0o755)
        pairs = init_secrets.oidc_client_pairs()
        conf = cls.dir / "config/configuration.yml"
        conf.write_text(cfg.render(pairs))
        conf.chmod(0o644)
        env = test_env()
        lines = "".join(f"{k}={v}\n" for k, v in env.items())
        (cls.dir / "with-filter.env").write_text(lines + "X_AUTHELIA_CONFIG_FILTERS=template\n")
        (cls.dir / "without-filter.env").write_text(lines)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def validate(self, env_name):
        return authelia("validate-config", "--config", "/config/configuration.yml",
                        env_file=self.dir / env_name, config_dir=self.dir / "config")

    def test_validate_config_passes_only_with_the_template_filter(self):
        ok = self.validate("with-filter.env")
        print(f"\nvalidate-config (exit {ok.returncode}):\n{ok.stdout}{ok.stderr}", file=sys.stderr)  # S20: warnings shown, not failed
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        bare = self.validate("without-filter.env")
        self.assertNotEqual(bare.returncode, 0, "the env templates must stay literal without X_AUTHELIA_CONFIG_FILTERS")  # B1
        self.assertIn('env \\"EDGE_', bare.stdout + bare.stderr)

    def test_crypto_hash_validate_accepts_the_generated_pairs(self):
        key = init_secrets.OIDC_CLIENT_KEYS[0]
        pair = init_secrets.oidc_client_pairs([key])
        dash = "-" + secrets.token_urlsafe(48)
        for secret, digest in ((pair[key], pair[f"{key}_pbkdf2"]), (dash, init_secrets.pbkdf2_sha512_digest(dash))):
            r = authelia("crypto", "hash", "validate", f"--password={secret}", "--", digest)
            self.assertEqual((r.returncode, r.stdout.strip()), (0, MATCH), r.stderr)  # exit 0 either way: the message decides
            wrong = authelia("crypto", "hash", "validate", f"--password={secret}x", "--", digest)
            self.assertEqual((wrong.returncode, wrong.stdout.strip()), (0, NO_MATCH), wrong.stderr)


PROJECT = "sso-stack-test"
SERVICES = ("lldap", "authelia")
HEALTH_URLS = {"authelia": f"http://127.0.0.1:{cfg.V['authelia_port']}/api/health",
               "lldap": f"http://127.0.0.1:{cfg.V['lldap_ui_port']}/health"}
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LLDAP_URL = f"http://127.0.0.1:{cfg.V['lldap_ui_port']}"


def stack_test_secrets():
    s = {k: secrets.token_urlsafe(48) for k in cfg.PLAIN_KEYS}
    for k in ("lldap_admin_password", "authelia_ldap_bind_password"):  # S6: the --opt=value form is what makes these work
        s[k] = "-" + s[k][1:]
    s["authelia_oidc_jwks_key"] = init_secrets.rsa_private_key_pem()
    return s | init_secrets.oidc_client_pairs()


@unittest.skipUnless(REQUIRE or docker_ready(), "docker is required")
class HardenedStart(unittest.TestCase):
    """The deploy's create -> chown 10001 0700 -> up sequence on the rendered services, fresh volumes, real env files."""

    @classmethod
    def setUpClass(cls):
        cls.dir = Path(tempfile.mkdtemp())
        cls.dir.chmod(0o750)
        s = stack_test_secrets()
        (cls.dir / "authelia").mkdir(mode=0o750)
        (cls.dir / "authelia/config").mkdir(mode=0o755)
        (cls.dir / "lldap").mkdir(mode=0o750)
        conf = cls.dir / "authelia/config/configuration.yml"
        conf.write_text(cfg.render(s))
        conf.chmod(0o644)
        for task, path in ((cfg.AUTHELIA_ENV_TASK, "authelia/authelia.env"), (cfg.LLDAP_ENV_TASK, "lldap/lldap.env")):
            (cls.dir / path).write_text(cfg.render_env(task, s))  # compose's env_file: the deploy's single-quoted shape
            (cls.dir / path).chmod(0o600)
        mini = {"name": PROJECT, "services": {n: cfg.S[n] for n in SERVICES},
                "networks": {"authelia": cfg.COMPOSE["networks"]["authelia"]},
                "volumes": {f"{n}-data": cfg.COMPOSE["volumes"][f"{n}-data"] for n in SERVICES}}
        (cls.dir / "compose.yml").write_text(yaml.safe_dump(mini))
        cls.compose("down", "-v", check=False)  # a stale project from a crashed run
        cls.compose("create", "--no-recreate", "lldap", "authelia")
        for name in SERVICES:
            data = cls.docker("volume", "inspect", "--format", "{{.Mountpoint}}", f"{PROJECT}_{name}-data").stdout.strip()
            subprocess.run(["sudo", "-n", "chown", "10001:10001", data], check=True)
            subprocess.run(["sudo", "-n", "chmod", "0700", data], check=True)
        cls.secrets = s
        start = time.monotonic()
        # Authelia's startup check binds as its account and exits on failure: lldap, ensure and reconcile come first.
        cls.ensure_first = cls.reconcile_first = None
        up = cls.compose("up", "-d", "--wait", "--wait-timeout", "120", "lldap", check=False)
        if up.returncode == 0:
            cls.ensure_first = cls.sync("ensure")
            cls.reconcile_first = cls.sync("reconcile")
            up = cls.compose("up", "-d", "--wait", "--wait-timeout", "120", check=False)
        cls.up_seconds = time.monotonic() - start
        cls.up = up
        if up.returncode != 0:
            print(f"\ncompose up failed:\n{up.stdout}{up.stderr}\n{cls.compose('logs', '--no-color', check=False).stdout}",
                  file=sys.stderr)

    @classmethod
    def sync(cls, cmd):
        """The deploy's call: the real script, its secrets on stdin only."""
        args = [sys.executable, str(ROOT / "scripts/sso_sync.py"), cmd, "--lldap-url", LLDAP_URL,
                "--compose", str(cls.dir / "compose.yml")]
        if cmd == "ensure":
            args += ["--groups", *cfg.V["sso_groups"], "--bind-user", cfg.V["authelia_ldap_bind_user"],
                     "--bind-email", cfg.V["authelia_ldap_bind_email"]]
        stdin = {"admin_password": cls.secrets["lldap_admin_password"]}
        if cmd == "ensure":
            stdin["bind_password"] = cls.secrets["authelia_ldap_bind_password"]
        since = time.time()
        r = subprocess.run(args, capture_output=True, text=True, input=json.dumps(stdin), timeout=600)
        if cmd == "ensure":  # what docker recorded of this run's execs
            time.sleep(1)
            cls.ensure_events = subprocess.run(
                ["docker", "events", "--since", f"{since - 1:.0f}", "--until", f"{time.time() + 1:.0f}", "--format", "{{json .}}"],
                capture_output=True, text=True, timeout=60).stdout
            cls.ensure_events = "".join(line + "\n" for line in cls.ensure_events.splitlines()
                                        if json.loads(line)["timeNano"] >= since * 1e9)
        return r

    @classmethod
    def lldap(cls):
        return sso_sync.Lldap(LLDAP_URL, cls.secrets["lldap_admin_password"])

    @classmethod
    def tearDownClass(cls):
        cls.compose("down", "-v", check=False)
        shutil.rmtree(cls.dir, ignore_errors=True)

    @classmethod
    def compose(cls, *args, check=True):
        return subprocess.run(["docker", "compose", "-f", str(cls.dir / "compose.yml"), *args],
                              capture_output=True, text=True, check=check, timeout=300)

    @staticmethod
    def docker(*args):
        return subprocess.run(["docker", *args], capture_output=True, text=True, check=True, timeout=60)

    def inspect(self, name):
        cid = self.compose("ps", "-aq", name).stdout.strip()
        return json.loads(self.docker("inspect", cid).stdout)[0]

    def wait_healthy(self, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            states = {n: self.inspect(n)["State"].get("Health", {}).get("Status") for n in SERVICES}
            if set(states.values()) == {"healthy"}:
                return time.monotonic() - (deadline - timeout)
            time.sleep(2)
        self.fail(f"not healthy within {timeout}s: {states}\n{self.compose('logs', '--no-color', check=False).stdout}")

    def get(self, url):
        with NO_PROXY.open(url, timeout=10) as r:
            return r.status

    def test_both_start_healthy_read_only_as_10001_and_again_after_a_restart(self):
        self.assertEqual(self.up.returncode, 0, self.up.stdout + self.up.stderr)
        self.assertLessEqual(self.up_seconds, 120)
        print(f"\nhealthy after up: {self.up_seconds:.0f}s", file=sys.stderr)
        for name in SERVICES:
            c = self.inspect(name)
            self.assertEqual(c["State"]["Health"]["Status"], "healthy", name)
            self.assertEqual(c["Config"]["Image"], cfg.V[f"{name}_image"], name)  # the pins in versions.yml
            self.assertEqual(c["Config"]["Healthcheck"]["Test"], cfg.S[name]["healthcheck"]["test"], name)
            self.assertEqual(c["Config"]["User"], "10001:10001", name)
            self.assertEqual(self.compose("exec", "-T", name, "id", "-u").stdout.strip(), "10001", name)
            self.assertIs(c["HostConfig"]["ReadonlyRootfs"], True, name)
            self.assertEqual(c["HostConfig"]["CapDrop"], ["ALL"], name)
            self.assertEqual(c["HostConfig"]["SecurityOpt"], ["no-new-privileges:true"], name)
            self.assertEqual(self.get(HEALTH_URLS[name]), 200, name)
        self.assertEqual(self.inspect("authelia")["Config"]["Healthcheck"]["Test"],
                         ["CMD", "wget", "-q", "--spider", "http://127.0.0.1:9091/api/health"])  # not the image's healthcheck.sh
        self.compose("restart", *SERVICES)
        print(f"healthy after restart: {self.wait_healthy():.0f}s", file=sys.stderr)
        for name in SERVICES:
            self.assertEqual(self.get(HEALTH_URLS[name]), 200, name)


    def assertSummary(self, r, want, rc=0):
        self.assertEqual(r.returncode, rc, r.stdout + r.stderr)
        self.assertEqual(json.loads(r.stdout), want)
        for text in (r.stdout, r.stderr):  # N10: usernames and counts only
            self.assertNotRegex(text, r"[0-9a-f]{8}-[0-9a-f]{4}-")
            self.assertNotIn("@", text)
            for v in self.secrets.values():
                self.assertNotIn(v, text)

    def people_state(self):
        bind = cfg.V["authelia_ldap_bind_user"]
        st = self.lldap().state()
        users = [u for u in st["users"] if u["id"] != bind]
        bind_groups = [u["groups"] for u in st["users"] if u["id"] == bind]
        return st["groups"], users, bind_groups, st["schema"]

    def add_person(self, uid, groups):
        ld = self.lldap()
        ld.gql("mutation($u: CreateUserInput!) { createUser(user: $u) { id } }", {"u": {"id": uid, "email": f"{uid}@example.test"}})
        ids = {g["displayName"]: g["id"] for g in ld.state()["groups"]}
        for g in groups:
            ld.gql("mutation($u: String!, $g: Int!) { addUserToGroup(userId: $u, groupId: $g) { ok } }", {"u": uid, "g": ids[g]})
        password = "-" + secrets.token_urlsafe(24)
        self.set_password(uid, password)
        return password

    def set_password(self, uid, password):
        ld = self.lldap()
        subprocess.run(["docker", "compose", "-f", str(self.dir / "compose.yml"), "exec", "-T", "-e", "LLDAP_USER_PASSWORD", "lldap",
                        "/app/lldap_set_password", "--base-url", "http://localhost:17170", f"--token={ld.token}", "--username", uid],
                       env={**os.environ, "LLDAP_USER_PASSWORD": password}, capture_output=True, check=True, timeout=60)
        ld.logout()

    def sub_in_lldap(self, uid):
        u = next(u for u in self.lldap().state()["users"] if u["id"] == uid)
        return next((a["value"][0] for a in u["attributes"] if a["name"] == sso_sync.SUB_ATTRIBUTE), None)

    def set_sub_in_lldap(self, uid, sub):
        self.lldap().gql("mutation($u: UpdateUserInput!) { updateUser(user: $u) { ok } }",
                         {"u": {"id": uid, "insertAttributes": [{"name": sso_sync.SUB_ATTRIBUTE, "value": [sub]}]}})

    def export(self):
        return sso_sync.authelia_export(str(self.dir / "compose.yml"))

    def first_factor(self, username, password):
        req = urllib.request.Request(f"http://127.0.0.1:{cfg.V['authelia_port']}/api/firstfactor", method="POST",
                                     data=json.dumps({"username": username, "password": password, "keepMeLoggedIn": False}).encode(),
                                     headers={"Content-Type": "application/json", "Host": cfg.AUTH, "X-Forwarded-Proto": "https"})
        try:
            with NO_PROXY.open(req, timeout=10) as r:
                return r.status, json.load(r).get("status")
        except urllib.error.HTTPError as e:
            return e.code, json.load(e).get("status")

    def wipe_authelia_data(self):
        self.compose("down")
        self.docker("volume", "rm", f"{PROJECT}_authelia-data")
        self.compose("create", "--no-recreate", "lldap", "authelia")
        data = self.docker("volume", "inspect", "--format", "{{.Mountpoint}}", f"{PROJECT}_authelia-data").stdout.strip()
        subprocess.run(["sudo", "-n", "chown", "10001:10001", data], check=True)
        subprocess.run(["sudo", "-n", "chmod", "0700", data], check=True)
        self.compose("up", "-d", "--wait", "--wait-timeout", "120", "lldap")

    def test_the_sync_ensures_lldap_and_reconciles_each_sub_on_the_live_pair(self):
        self.assertEqual(self.up.returncode, 0, self.up.stdout + self.up.stderr)
        self.assertSummary(self.ensure_first, {"created_groups": cfg.V["sso_groups"], "created_attribute": True,
                                               "created_bind_user": True})
        self.assertSummary(self.reconcile_first, {"copied_to_lldap": [], "added_to_authelia": [], "mismatched": [], "unchanged": 0})
        person_pw = self.add_person("t6person", ["sso_people"])
        outsider_pw = self.add_person("t6outsider", ["commsgun"])

        before = self.people_state()  # S22: a second ensure changes no person, membership, group or attribute
        self.assertSummary(self.sync("ensure"), {"created_groups": [], "created_attribute": False, "created_bind_user": False})
        self.assertEqual(self.people_state(), before)
        print("\nensure twice: second run changed nothing", file=sys.stderr)

        execs = [e for e in map(json.loads, self.ensure_events.splitlines()) if e.get("Action", "").startswith("exec_")
                 and "lldap_set_password" in e["Action"]]
        self.assertTrue(execs, "no exec event recorded for lldap_set_password")
        for name in ("lldap_admin_password", "authelia_ldap_bind_password"):
            self.assertFalse(self.secrets[name] in self.ensure_events, name)  # no value in the failure message
        tokens = set(re.findall(r"--token=([A-Za-z0-9._-]+)", self.ensure_events))
        self.assertTrue(tokens)
        for token in tokens:  # revoked before ensure exits
            req = urllib.request.Request(f"{LLDAP_URL}/api/graphql", method="POST", data=b'{"query":"{ groups { id } }"}',
                                         headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            with self.assertRaises(urllib.error.HTTPError) as e:
                NO_PROXY.open(req, timeout=10)
            self.assertEqual(e.exception.code, 401)
        print(f"docker events: {len(execs)} exec events, no lldap password in them, their token rejected (401)", file=sys.stderr)

        refused = {u: self.first_factor(u, pw) for u, pw in (
            ("t6outsider", outsider_pw), ("admin", self.secrets["lldap_admin_password"]),
            (cfg.V["authelia_ldap_bind_user"], self.secrets["authelia_ldap_bind_password"]))}
        accepted = self.first_factor("t6person", person_pw)  # the control: the same call succeeds for sso_people
        print(f"firstfactor: refused {refused}, sso_people member {accepted}", file=sys.stderr)
        self.assertEqual(accepted, (200, "OK"))
        for u, (code, status) in refused.items():
            self.assertEqual((code, status), (401, "KO"), u)

        # Case 1: a first login minted the sub in Authelia; lldap has none -> copied.
        sub = str(uuid.uuid4())
        add = self.compose("run", "--rm", "--no-deps", "-T", "authelia", "authelia", "storage", "user", "identifiers", "add",
                           "t6person", "--identifier", sub, "--service", "openid", "--config", "/config/configuration.yml",
                           check=False)
        self.assertEqual(add.returncode, 0, "identifiers add failed")
        self.assertSummary(self.sync("reconcile"), {"copied_to_lldap": ["t6person"], "added_to_authelia": [], "mismatched": [],
                                                    "unchanged": 0})
        self.assertEqual(self.sub_in_lldap("t6person"), sub)
        print("reconcile case 1: copied to lldap, read back equal", file=sys.stderr)

        # Case 2: Authelia's database lost -> added back from lldap, the same sub.
        self.wipe_authelia_data()
        self.assertSummary(self.sync("reconcile"), {"copied_to_lldap": [], "added_to_authelia": ["t6person"], "mismatched": [],
                                                    "unchanged": 0})
        self.assertEqual(self.export(), {"t6person": sub})
        self.compose("up", "-d", "--wait", "--wait-timeout", "120")
        print("reconcile case 2: added back to Authelia, export shows the same sub", file=sys.stderr)

        # Case 3: both present and different -> exit 2, neither store changes.
        other = str(uuid.uuid4())
        self.set_sub_in_lldap("t6person", other)
        self.assertSummary(self.sync("reconcile"), {"copied_to_lldap": [], "added_to_authelia": [], "mismatched": ["t6person"],
                                                    "unchanged": 0}, rc=2)
        self.assertEqual((self.export(), self.sub_in_lldap("t6person")), ({"t6person": sub}, other))
        print("reconcile case 3: mismatch refused with exit 2, both stores unchanged", file=sys.stderr)


if __name__ == "__main__":
    unittest.main()
