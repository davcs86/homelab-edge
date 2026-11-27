#!/usr/bin/env python3
"""Runs the pinned Authelia image against the rendered configuration (S20, B1) and the generated client hashes (S6).
SSO_STACK_TEST_REQUIRE_DOCKER=1 turns a skip for want of Docker into a failure."""
import base64
import importlib.util
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import init_secrets  # noqa: E402

_spec = importlib.util.spec_from_file_location("authelia_config_test", ROOT / "scripts/authelia-config-test.py")
cfg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cfg)

# Must equal the Authelia pin; versions.yml holds no authelia_image yet.
IMAGE = "authelia/authelia:4.39.28@sha256:bd97cff4fcbf715b5ff1f9ae286afbe6033afce385302520b0368122d43a6f54"
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


if __name__ == "__main__":
    unittest.main()
