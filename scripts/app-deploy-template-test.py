#!/usr/bin/env python3
"""Offline checks of templates/app-deploy/deploy.yml: pins, secret handling, and the 1Password source/report shell steps."""
import os
import re
import subprocess
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "templates/app-deploy/deploy.yml").read_text()
WORKFLOW = yaml.safe_load(TEMPLATE)
VERSIONS = (ROOT / "docs/versions.md").read_text()
STEPS = WORKFLOW["jobs"]["deploy"]["steps"]
SECRETS = ["TS_AUTHKEY", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "APP_ENV"]


def step(name_start):
    return next(s for s in STEPS if str(s.get("name", "")).startswith(name_start))


def run_step(s, env, **extra):
    with tempfile.TemporaryDirectory() as d:
        out, summary = Path(d, "out"), Path(d, "summary")
        out.touch()
        summary.touch()
        full = {"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary), **env}
        r = subprocess.run(["bash", "-e", "-c", s["run"]], env=full, capture_output=True, text=True, check=False, **extra)
        return r, out.read_text(), summary.read_text()


class Pins(unittest.TestCase):
    def test_every_action_is_pinned_by_commit(self):
        refs = re.findall(r"uses:\s*(\S+)", TEMPLATE)
        self.assertTrue(refs)
        for ref in refs:
            self.assertRegex(ref, r"@[0-9a-f]{40}$", ref)

    def test_pins_match_the_version_table(self):
        load = step("Load secrets from 1Password")["uses"].split("@")[1]
        self.assertIn(load, VERSIONS)
        self.assertEqual(WORKFLOW["env"]["OP_CLI_VERSION"], "2.40.0")
        self.assertIn(WORKFLOW["env"]["OP_CLI_SHA256"], VERSIONS)
        self.assertIn(WORKFLOW["env"]["OP_CLI_VERSION"], VERSIONS)

    def test_the_cli_is_installed_before_the_action_so_the_action_never_downloads_latest(self):
        names = [s.get("name", "") for s in STEPS]
        self.assertLess(names.index("1Password CLI (pinned, checksum-verified)"), names.index("Load secrets from 1Password"))
        self.assertIn("sha256sum -c -", step("1Password CLI")["run"])


class SecretHandling(unittest.TestCase):
    def test_the_token_is_only_in_the_source_check_and_the_load_step(self):
        holders = [s.get("name") for s in STEPS if "OP_SERVICE_ACCOUNT_TOKEN" in yaml.dump(s)]
        self.assertEqual(holders, ["Choose the secret source (1Password when its token is usable, else GitHub secrets)", "Load secrets from 1Password"])
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", yaml.dump(WORKFLOW["jobs"]["deploy"].get("env", {})))
        self.assertNotIn("OP_SERVICE_ACCOUNT_TOKEN", yaml.dump(WORKFLOW.get("env", {})))

    def test_the_load_step_exports_nothing_to_the_environment_and_falls_back(self):
        s = step("Load secrets from 1Password")
        self.assertIs(s["with"]["export-env"], False)
        self.assertIs(s["continue-on-error"], True)
        refs = {k: v for k, v in s["env"].items() if str(v).startswith("op://")}
        self.assertEqual(set(refs), {"TS_AUTHKEY", "TS_AUTHKEY_EXPIRES", "DEPLOY_SSH_KEY", "DEPLOY_KNOWN_HOSTS", "APP_ENV"})
        for ref in refs.values():
            self.assertTrue(ref.startswith("op://${{ vars.OP_VAULT }}/"), ref)
        self.assertTrue(refs["DEPLOY_SSH_KEY"].endswith("?ssh-format=openssh"))

    def test_every_use_of_a_secret_goes_through_the_dual_source_expression(self):
        for name in SECRETS:
            bare = [m for m in re.finditer(rf"secrets\.{name}\b", TEMPLATE) if not TEMPLATE[max(0, m.start() - 40):m.start()].endswith(f"steps.op.outputs.{name} || ")
                    and "!= ''" not in TEMPLATE[max(0, m.start() - 60):m.start() + 40]]
            self.assertEqual(bare, [], name)
        self.assertIn("steps.op.outputs.TS_AUTHKEY_EXPIRES || vars.TS_AUTHKEY_EXPIRES", TEMPLATE)

    def test_no_secret_is_exported_to_later_steps(self):
        self.assertNotIn("GITHUB_ENV", step("Choose the secret source")["run"])
        for s in STEPS:
            if "run" in s and s.get("name") != "SSH credentials (files only, host key pinned)":
                self.assertNotIn("DEPLOY_SSH_KEY >>", s["run"])


class SourceStep(unittest.TestCase):
    S = step("Choose the secret source")

    def op(self, token="", expires="", **kw):
        r, out, _ = run_step(self.S, {"OP_TOKEN": token, "OP_EXPIRES": expires}, **kw)
        return r, out.strip()

    def test_no_token_means_github_secrets(self):
        r, out = self.op()
        self.assertEqual((r.returncode, out), (0, "op=false"))

    def test_token_without_expiry_uses_1password(self):
        self.assertEqual(self.op("t")[1], "op=true")

    def test_far_expiry_is_quiet(self):
        r, out = self.op("t", (date.today() + timedelta(days=200)).isoformat())
        self.assertEqual(out, "op=true")
        self.assertNotIn("::warning::", r.stdout)

    def test_expiry_inside_14_days_warns_but_still_uses_1password(self):
        r, out = self.op("t", (date.today() + timedelta(days=5)).isoformat())
        self.assertEqual(out, "op=true")
        self.assertIn("::warning::The 1Password service account token expires in", r.stdout)

    def test_expired_token_falls_back_with_a_warning(self):
        r, out = self.op("t", (date.today() - timedelta(days=3)).isoformat())
        self.assertEqual(out, "op=false")
        self.assertIn("::warning::The 1Password service account token expired", r.stdout)

    def test_an_unreadable_date_fails_loudly(self):
        r, _ = self.op("t", "not-a-date")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("::error::OP_TOKEN_EXPIRES is not a date: not-a-date", r.stdout)


class ReportStep(unittest.TestCase):
    S = step("Report where each secret came from")

    def report(self, wanted="true", outcome="success", **sources):
        env = {"OP_WANTED": wanted, "OP_OUTCOME": outcome, **{f"S_{n}": sources.get(n, "1password") for n in SECRETS}}
        r, _, summary = run_step(self.S, env)
        return r, summary

    def test_all_from_1password_is_clean(self):
        r, summary = self.report()
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("::warning::", r.stdout)
        self.assertIn("- DEPLOY_SSH_KEY: 1password", summary)

    def test_a_failed_load_that_fell_back_is_visible(self):
        r, summary = self.report(outcome="failure", **{n: "github" for n in SECRETS})
        self.assertEqual(r.returncode, 0)
        self.assertIn("1Password was configured but the load did not succeed", r.stdout)
        self.assertEqual(r.stdout.count("came from the GitHub secret"), 4)
        self.assertIn("- APP_ENV: github", summary)

    def test_github_only_setups_are_not_nagged(self):
        r, _ = self.report(wanted="false", outcome="skipped", **{n: "github" for n in SECRETS})
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("::warning::", r.stdout)

    def test_a_secret_in_neither_source_fails_the_job(self):
        r, _ = self.report(wanted="false", outcome="skipped", **{**{n: "github" for n in SECRETS}, "APP_ENV": "MISSING"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("::error::APP_ENV is in neither 1Password nor the GitHub secrets", r.stdout)


if __name__ == "__main__":
    unittest.main()
