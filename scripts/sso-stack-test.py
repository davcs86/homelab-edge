#!/usr/bin/env python3
"""Runs the pinned Authelia image against the rendered configuration (S20, B1) and the generated client hashes (S6), and
starts the rendered authelia and lldap services hardened on fresh volumes (Review Focus 1).
SSO_STACK_TEST_REQUIRE_DOCKER=1 turns a skip for want of Docker into a failure."""
import base64
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import init_secrets  # noqa: E402

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


def stack_test_secrets():
    s = {k: secrets.token_urlsafe(48) for k in cfg.PLAIN_KEYS}
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
        start = time.monotonic()
        # Authelia's startup check binds as its account and exits on failure: lldap and that account come first (Task 6).
        up = cls.compose("up", "-d", "--wait", "--wait-timeout", "120", "lldap", check=False)
        if up.returncode == 0:
            cls.create_bind_user(s)
            up = cls.compose("up", "-d", "--wait", "--wait-timeout", "120", check=False)
        cls.up_seconds = time.monotonic() - start
        cls.up = up
        if up.returncode != 0:
            print(f"\ncompose up failed:\n{up.stdout}{up.stderr}\n{cls.compose('logs', '--no-color', check=False).stdout}",
                  file=sys.stderr)

    @classmethod
    def create_bind_user(cls, s):
        base = HEALTH_URLS["lldap"].rsplit("/", 1)[0]
        login = urllib.request.Request(f"{base}/auth/simple/login", method="POST", headers={"Content-Type": "application/json"},
                                       data=json.dumps({"username": "admin", "password": s["lldap_admin_password"]}).encode())
        with NO_PROXY.open(login, timeout=10) as r:
            token = json.load(r)["token"]
        user = {"id": cfg.V["authelia_ldap_bind_user"], "email": cfg.V["authelia_ldap_bind_email"]}
        query = {"query": "mutation($user: CreateUserInput!) { createUser(user: $user) { id } }", "variables": {"user": user}}
        gql = urllib.request.Request(f"{base}/api/graphql", method="POST", data=json.dumps(query).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with NO_PROXY.open(gql, timeout=10) as r:
            body = json.load(r)
        assert "errors" not in body, body
        cls.compose("exec", "-T", "lldap", "/app/lldap_set_password", "--base-url", "http://localhost:17170",
                    "--admin-username", "admin", f"--admin-password={s['lldap_admin_password']}",
                    "--username", user["id"], f"--password={s['authelia_ldap_bind_password']}")

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


if __name__ == "__main__":
    unittest.main()
