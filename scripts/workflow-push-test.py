#!/usr/bin/env python3
"""Checks how the workflows that commit to main do it: App token only when something changed, key confined to `production`, pins, staging shell."""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github/workflows"
PUSHERS = {"deploy": "deploy", "init-secrets": "init", "node-admin": "run"}  # workflow file -> job
VERSIONS = (ROOT / "docs/versions.md").read_text()
TOKEN_ACTION = re.compile(r"^actions/create-github-app-token@([0-9a-f]{40})$")


def load(name):
    return yaml.safe_load((WF / f"{name}.yml").read_text())


def steps(name):
    return load(name)["jobs"][PUSHERS[name]]["steps"]


def step_by_id(name, sid):
    return next(s for s in steps(name) if s.get("id") == sid)


class Wiring(unittest.TestCase):
    def test_no_workflow_pushes_with_the_default_token(self):
        for name in PUSHERS:
            text = (WF / f"{name}.yml").read_text()
            self.assertNotIn("git push origin", text, name)
            self.assertEqual(len(re.findall(r"git push ", text)), 1, name)
            for s in steps(name):
                if "git push" in s.get("run", ""):
                    self.assertEqual(s["env"]["PUSH_TOKEN"], "${{ steps.push-token.outputs.token }}", name)
                    self.assertIn("steps.push-token.outcome == 'success'", s["if"], name)

    def test_checkout_does_not_persist_the_default_token(self):
        for name in PUSHERS:
            checkout = next(s for s in steps(name) if str(s.get("uses", "")).startswith("actions/checkout@"))
            self.assertIs(checkout["with"]["persist-credentials"], False, name)

    def test_the_token_is_minted_only_when_something_changed_and_with_contents_write_only(self):
        for name in PUSHERS:
            t = step_by_id(name, "push-token")
            self.assertRegex(t["uses"].split("  ")[0], TOKEN_ACTION)
            self.assertIn("steps.stage.outputs.changed == 'true'", t["if"], name)
            perms = {k for k in t["with"] if k.startswith("permission-")}
            self.assertEqual(perms, {"permission-contents"}, name)
            self.assertEqual(t["with"]["permission-contents"], "write", name)
            self.assertEqual(t["with"]["client-id"], "${{ vars.PUSH_APP_CLIENT_ID }}", name)
            self.assertNotIn("repositories", t["with"])  # defaults to this repository only

    def test_the_pin_is_recorded_in_the_version_table(self):
        sha = TOKEN_ACTION.match(step_by_id("deploy", "push-token")["uses"]).group(1)
        self.assertIn(sha, VERSIONS)
        for name in PUSHERS:
            self.assertEqual(step_by_id(name, "push-token")["uses"], step_by_id("deploy", "push-token")["uses"])

    def test_the_private_key_exists_only_in_jobs_that_run_in_the_production_environment(self):
        holders = []
        for f in WF.glob("*.yml"):
            for jname, job in yaml.safe_load(f.read_text())["jobs"].items():
                if "PUSH_APP_PRIVATE_KEY" in yaml.dump(job):
                    holders.append(f.stem)
                    self.assertEqual(job.get("environment"), "production", f"{f.stem}/{jname}")
        self.assertEqual(sorted(holders), sorted(PUSHERS))

    def test_a_missing_app_is_an_explicit_error_not_a_confusing_one(self):
        for name in PUSHERS:
            msg = next(s for s in steps(name) if s.get("name") == "Push token is not configured")
            self.assertIn("vars.PUSH_APP_CLIENT_ID == ''", msg["if"])
            self.assertIn("github-push-app.md", msg["run"])

    def test_deploy_still_records_state_after_a_failed_run(self):
        self.assertEqual(step_by_id("deploy", "stage")["if"], "always()")
        self.assertIn("always()", step_by_id("deploy", "push-token")["if"])
        push = next(s for s in steps("deploy") if "git push" in s.get("run", ""))
        self.assertIn("always()", push["if"])


class StageStep(unittest.TestCase):
    """Runs each staging script in a throwaway repo: no change -> changed=false and exit 0; a change -> one commit and changed=true."""

    def run_stage(self, name, files, env=None, change=True):
        script = re.sub(r"\$\{\{[^}]*\}\}", "X", step_by_id(name, "stage")["run"])
        with tempfile.TemporaryDirectory() as d:
            sh = lambda *a: subprocess.run(a, cwd=d, check=True, capture_output=True, text=True)  # noqa: E731
            sh("git", "init", "-q", "-b", "main")
            for rel in files:
                p = Path(d, rel)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("old\n")
            sh("git", "add", "-A")
            sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
            if change:
                Path(d, files[0]).write_text("new\n")
            out = Path(d, "out")
            out.touch()
            r = subprocess.run(["bash", "-e", "-c", script], cwd=d, env={**os.environ, "GITHUB_OUTPUT": str(out), **(env or {})},
                               capture_output=True, text=True, check=False)
            commits = sh("git", "rev-list", "--count", "HEAD").stdout.strip()
            return r, out.read_text().strip(), commits

    def test_each_workflow_commits_only_when_its_file_changed(self):
        cases = {"init-secrets": ["secrets/edge.sops.yaml"], "node-admin": ["secrets/ci-authkey.sops.yaml"],
                 "deploy": ["inventories/prod/group_vars/all/generated.yml"]}
        for name, files in cases.items():
            r, out, commits = self.run_stage(name, files, {"FILL_MISSING": "false"}, change=True)
            self.assertEqual((r.returncode, out, commits), (0, "changed=true", "2"), (name, r.stderr))
            r, out, commits = self.run_stage(name, files, {"FILL_MISSING": "false"}, change=False)
            self.assertEqual((r.returncode, out, commits), (0, "changed=false", "1"), (name, r.stderr))

    def test_deploy_ignores_state_files_that_do_not_exist(self):
        r, out, _ = self.run_stage("deploy", ["inventories/prod/group_vars/all/generated.yml"], change=False)
        self.assertEqual((r.returncode, out), (0, "changed=false"))

    def test_fill_missing_gets_its_own_commit_message(self):
        script = step_by_id("init-secrets", "stage")["run"]
        self.assertIn("add missing generated credentials", script)


if __name__ == "__main__":
    unittest.main()
