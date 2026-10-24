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


def load(name):
    return yaml.safe_load((WF / f"{name}.yml").read_text())


def steps(name):
    return load(name)["jobs"][PUSHERS[name]]["steps"]


def step_by_id(name, sid):
    return next(s for s in steps(name) if s.get("id") == sid)


def push_step(name):
    return next(s for s in steps(name) if "git push" in s.get("run", ""))


class Wiring(unittest.TestCase):
    def test_the_only_push_uses_the_push_token_after_staging(self):
        for name in PUSHERS:
            text = (WF / f"{name}.yml").read_text()
            self.assertNotIn("git push origin", text, name)
            self.assertEqual(len(re.findall(r"git push ", text)), 1, name)
            p = push_step(name)
            self.assertEqual(p["env"], {"PUSH_TOKEN": "${{ secrets.PUSH_TOKEN }}"}, name)
            self.assertIn("steps.stage.outputs.changed == 'true'", p["if"], name)
            self.assertIn("github-push-token.md", p["run"], name)

    def test_the_default_token_cannot_push_and_checkout_does_not_keep_it(self):
        for name in PUSHERS:
            self.assertEqual(load(name)["permissions"], {"contents": "read"}, name)
            checkout = next(s for s in steps(name) if str(s.get("uses", "")).startswith("actions/checkout@"))
            self.assertIs(checkout["with"]["persist-credentials"], False, name)

    def test_no_app_token_action_remains(self):
        for f in WF.glob("*.yml"):
            self.assertNotIn("create-github-app-token", f.read_text(), f.name)
            self.assertNotIn("PUSH_APP", f.read_text(), f.name)

    def test_the_token_exists_only_in_jobs_that_run_in_the_production_environment(self):
        holders = []
        for f in WF.glob("*.yml"):
            for jname, job in yaml.safe_load(f.read_text())["jobs"].items():
                if "secrets.PUSH_TOKEN" in yaml.dump(job):
                    holders.append(f.stem)
                    self.assertEqual(job.get("environment"), "production", f"{f.stem}/{jname}")
        self.assertEqual(sorted(holders), sorted(PUSHERS))

    def test_deploy_still_records_state_after_a_failed_run(self):
        self.assertEqual(step_by_id("deploy", "stage")["if"], "always()")
        self.assertIn("always()", push_step("deploy")["if"])


class PushStep(unittest.TestCase):
    """Runs the push script with a fake git first in PATH, to see exactly what it would push and where."""

    def run_push(self, name, token):
        script = push_step(name)["run"]
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d, "git")
            fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_GIT_LOG"\n')
            fake.chmod(0o755)
            log = Path(d, "log")
            env = {"PATH": f"{d}:{os.environ['PATH']}", "FAKE_GIT_LOG": str(log), "PUSH_TOKEN": token,
                   "GITHUB_REPOSITORY": "owner/repo", "GITHUB_REF_NAME": "main"}
            r = subprocess.run(["bash", "-e", "-c", script], env=env, capture_output=True, text=True, check=False)
            return r, (log.read_text().split() if log.exists() else None)

    def test_pushes_head_to_the_current_branch_with_the_token_in_the_url_only(self):
        for name in PUSHERS:
            r, argv = self.run_push(name, "TOKEN123")
            self.assertEqual(r.returncode, 0, (name, r.stderr))
            self.assertEqual(argv, ["push", "https://x-access-token:TOKEN123@github.com/owner/repo.git", "HEAD:main"], name)
            self.assertNotIn("TOKEN123", r.stdout + r.stderr)

    def test_an_empty_token_is_an_explicit_error_and_nothing_is_pushed(self):
        for name in PUSHERS:
            r, argv = self.run_push(name, "")
            self.assertEqual(r.returncode, 1, name)
            self.assertIn("::error::main only accepts PRs", r.stdout)
            self.assertIsNone(argv, name)


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
