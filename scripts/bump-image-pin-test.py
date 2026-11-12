#!/usr/bin/env python3
"""Checks scripts/bump_image_pin.py (offline, injected registry) and the bump-webhooks-mcp workflow that runs it."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bump_image_pin as bump  # noqa: E402

WF = ROOT / ".github/workflows/bump-webhooks-mcp.yml"
VAR = "webhooks_mcp_image"
IMAGE = "ghcr.io/davcs86/webhooks-mcp"
OLD = "sha256:" + "09e1e5f2" + "0" * 56
NEW = "sha256:" + "37a12ff2" + "1" * 56
REV = "1115593c86e8f9c384745d2bb275d7c6501b3750"
VERSIONS = f'other_image: "x.test/o@sha256:{"a" * 64}"\n{VAR}: "{IMAGE}@{OLD}"\nnext: 1\n'
DOCS = (
    "| Component | Pin | Why |\n|---|---|---|\n"
    f"| other (`other_image`) | `x.test/o@sha256:aaaaaaaa…`, digest only (commit `1234567`, 2026-01-01) | keep |\n"
    f"| webhooks-mcp (`{VAR}`) | `{IMAGE}@sha256:09e1e5f2…`, digest only (commit `64c15cc`, 2026-11-11) | why. To bump: by hand. |\n"
)


class FakeRegistry:
    """Stands in for the network: maps (method, path) to (status, headers, body)."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers):
        self.calls.append((method, url, dict(headers)))
        key = (method, url)
        if key not in self.routes and ("GET", url) in self.routes and method == "HEAD":
            key = ("GET", url)
        status, hdrs, body = self.routes.get(key, (404, {}, b""))
        return bump.Response(status, hdrs, body if isinstance(body, bytes) else json.dumps(body).encode())


BASE = "https://ghcr.io"
M = f"{BASE}/v2/davcs86/webhooks-mcp/manifests/"
B = f"{BASE}/v2/davcs86/webhooks-mcp/blobs/"
TOKEN = (200, {}, {"token": "tok"})
TOKEN_URL = f"{BASE}/token?scope=repository:davcs86/webhooks-mcp:pull"


def registry(tag_digest=NEW, label=REV, index=True, extra=None):
    cfg = {"config": {"Labels": {"org.opencontainers.image.revision": label} if label else {}}}
    manifest = {"config": {"digest": "sha256:cfg"}}
    routes = {
        ("GET", TOKEN_URL): TOKEN,
        ("HEAD", M + "main"): (200, {"Docker-Content-Digest": tag_digest}, b""),
        ("HEAD", M + NEW): (200, {"Docker-Content-Digest": NEW}, b""),
        ("GET", B + "sha256:cfg"): (200, {}, cfg),
    }
    if index:
        routes[("GET", M + tag_digest)] = (200, {}, {"manifests": [
            {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}}]})
        routes[("GET", M + "sha256:amd")] = (200, {}, manifest)
    else:
        routes[("GET", M + tag_digest)] = (200, {}, manifest)
    routes[("GET", M + "main")] = routes[("GET", M + tag_digest)]
    routes.update(extra or {})
    return FakeRegistry(routes)


class Edits(unittest.TestCase):
    def test_pin_replacement_touches_only_that_line(self):
        out = bump.replace_pin(VERSIONS, VAR, IMAGE, NEW)
        self.assertEqual(out, VERSIONS.replace(OLD, NEW))
        self.assertEqual(bump.current_pin(out, VAR, IMAGE), NEW)

    def test_pin_errors(self):
        with self.assertRaises(bump.BumpError):
            bump.current_pin(VERSIONS, "missing_var", IMAGE)
        with self.assertRaises(bump.BumpError):
            bump.current_pin(VERSIONS, VAR, "ghcr.io/davcs86/other")
        with self.assertRaises(bump.BumpError):
            bump.current_pin(VERSIONS + f'{VAR}: "{IMAGE}@{OLD}"\n', VAR, IMAGE)
        with self.assertRaises(bump.BumpError):
            bump.current_pin(VERSIONS.replace(OLD, "sha256:abc"), VAR, IMAGE)

    def test_docs_row_gets_short_digest_revision_and_date(self):
        out = bump.replace_docs_row(DOCS, VAR, IMAGE, NEW, REV, "2026-12-01")
        self.assertIn(f"`{IMAGE}@sha256:37a12ff2…`, digest only (commit `1115593`, 2026-12-01)", out)
        self.assertEqual(out.replace("37a12ff2…", "09e1e5f2…").replace("1115593", "64c15cc").replace("2026-12-01", "2026-11-11"), DOCS)

    def test_unknown_revision_is_written_as_unknown(self):
        out = bump.replace_docs_row(DOCS, VAR, IMAGE, NEW, "unknown", "2026-12-01")
        self.assertIn("(commit `unknown`, 2026-12-01)", out)
        again = bump.replace_docs_row(out, VAR, IMAGE, OLD, REV, "2026-12-02")
        self.assertIn("(commit `1115593`, 2026-12-02)", again)

    def test_docs_errors_when_row_or_pattern_is_not_exactly_once(self):
        with self.assertRaises(bump.BumpError):
            bump.replace_docs_row(DOCS.replace("webhooks_mcp_image", "x"), VAR, IMAGE, NEW, REV, "2026-12-01")
        with self.assertRaises(bump.BumpError):
            bump.replace_docs_row(DOCS.replace("digest only", "pinned"), VAR, IMAGE, NEW, REV, "2026-12-01")
        with self.assertRaises(bump.BumpError):
            bump.replace_docs_row(DOCS + DOCS.splitlines()[-1] + "\n", VAR, IMAGE, NEW, REV, "2026-12-01")


class Lookups(unittest.TestCase):
    def test_tag_digest_comes_from_the_header_with_a_bearer_token(self):
        reg = registry()
        self.assertEqual(bump.Registry(IMAGE, reg).tag_digest("main"), NEW)
        method, url, headers = next(c for c in reg.calls if c[1] == M + "main")
        self.assertEqual((method, headers["Authorization"]), ("HEAD", "Bearer tok"))
        self.assertIn("application/vnd.oci.image.index.v1+json", headers["Accept"])

    def test_missing_tag_is_a_clear_error(self):
        reg = registry(extra={("HEAD", M + "main"): (404, {}, b"")})
        with self.assertRaisesRegex(bump.BumpError, r"tag 'main'.*not found"):
            bump.Registry(IMAGE, reg).tag_digest("main")

    def test_digest_must_be_well_formed_and_present_in_the_registry(self):
        r = bump.Registry(IMAGE, registry())
        self.assertEqual(r.verify_digest(NEW), NEW)
        with self.assertRaisesRegex(bump.BumpError, "sha256:"):
            r.verify_digest("sha256:abc")
        with self.assertRaisesRegex(bump.BumpError, "not in the registry"):
            r.verify_digest("sha256:" + "f" * 64)

    def test_revision_via_index_amd64_manifest_and_config_labels(self):
        reg = registry()
        self.assertEqual(bump.Registry(IMAGE, reg).revision(NEW), REV)
        self.assertIn(("GET", M + "sha256:amd"), [(c[0], c[1]) for c in reg.calls])
        self.assertNotIn(("GET", M + "sha256:arm"), [(c[0], c[1]) for c in reg.calls])

    def test_revision_from_a_plain_manifest(self):
        self.assertEqual(bump.Registry(IMAGE, registry(index=False)).revision(NEW), REV)

    def test_missing_or_malformed_label_is_unknown(self):
        self.assertEqual(bump.Registry(IMAGE, registry(label=None)).revision(NEW), "unknown")
        self.assertEqual(bump.Registry(IMAGE, registry(label="not a sha")).revision(NEW), "unknown")

    def test_redirects_must_stay_on_https(self):
        handler = bump.SafeRedirect()
        req = type("R", (), {"full_url": "https://ghcr.io/x", "headers": {"Authorization": "Bearer t"}})()
        with self.assertRaises(bump.BumpError):
            handler.redirect_request(req, None, 302, "Found", {}, "http://evil.test/x")


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.v, self.d, self.out = self.tmp / "versions.yml", self.tmp / "versions.md", self.tmp / "out"
        self.v.write_text(VERSIONS)
        self.d.write_text(DOCS)

    def run_cli(self, *extra, reg=None, env=True):
        argv = ["--var", VAR, "--image", IMAGE, "--versions", str(self.v), "--docs", str(self.d), *extra]
        old = dict(os.environ)
        if env:
            os.environ["GITHUB_OUTPUT"] = str(self.out)
        else:
            os.environ.pop("GITHUB_OUTPUT", None)
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = bump.main(argv, fetch=reg or registry(), today="2026-12-01")
        finally:
            os.environ.clear()
            os.environ.update(old)
        return rc, out.getvalue(), err.getvalue()

    def outputs(self):
        return dict(l.split("=", 1) for l in self.out.read_text().splitlines()) if self.out.exists() else {}

    def test_up_to_date_changes_nothing(self):
        self.v.write_text(VERSIONS.replace(OLD, NEW))
        before = (self.v.read_bytes(), self.d.read_bytes())
        rc, out, _ = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("up to date", out)
        self.assertEqual(self.outputs(), {"changed": "false"})
        self.assertEqual((self.v.read_bytes(), self.d.read_bytes()), before)

    def test_change_updates_both_files_and_writes_outputs(self):
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.v.read_text(), VERSIONS.replace(OLD, NEW))
        self.assertIn("(commit `1115593`, 2026-12-01)", self.d.read_text())
        self.assertEqual(self.outputs(), {"changed": "true", "digest": NEW, "short": NEW[7:19], "previous": OLD, "revision": REV})

    def test_works_without_github_output(self):
        rc, _, err = self.run_cli(env=False)
        self.assertEqual(rc, 0, err)

    def test_docs_failure_leaves_the_versions_file_unmodified(self):
        self.d.write_text(DOCS.replace("digest only", "pinned"))
        rc, _, err = self.run_cli()
        self.assertEqual(rc, 1)
        self.assertEqual(self.v.read_text(), VERSIONS)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertFalse(self.out.exists())

    def test_digest_flag_is_validated_and_checked_against_the_registry(self):
        rc, _, err = self.run_cli("--digest", "sha256:xyz")
        self.assertEqual((rc, self.v.read_text()), (1, VERSIONS))
        self.assertIn("sha256:", err)
        rc, _, err = self.run_cli("--digest", "sha256:" + "f" * 64)
        self.assertEqual((rc, self.v.read_text()), (1, VERSIONS))
        self.assertIn("not in the registry", err)
        rc, _, err = self.run_cli("--digest", NEW)
        self.assertEqual((rc, self.v.read_text()), (0, VERSIONS.replace(OLD, NEW)))

    def test_unpublished_main_tag_exits_nonzero_with_a_clear_line(self):
        rc, _, err = self.run_cli(reg=registry(extra={("HEAD", M + "main"): (404, {}, b"")}))
        self.assertEqual(rc, 1)
        self.assertIn("main", err)
        self.assertIn("not found", err)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertEqual(self.v.read_text(), VERSIONS)


def load_wf():
    return yaml.safe_load(WF.read_text())


def bump_job():
    return load_wf()["jobs"]["bump"]


def runs():
    return [s["run"] for s in bump_job()["steps"] if "run" in s]


class Workflow(unittest.TestCase):
    def test_triggers_are_schedule_and_dispatch_only(self):
        on = load_wf()[True]  # PyYAML reads the key `on` as True
        self.assertEqual(sorted(on), ["schedule", "workflow_dispatch"])
        self.assertEqual(on["schedule"], [{"cron": "23 15 * * *"}])
        self.assertEqual(list(on["workflow_dispatch"]["inputs"]), ["digest"])
        self.assertIs(on["workflow_dispatch"]["inputs"]["digest"].get("required", False), False)

    def test_permissions_are_read_by_default_and_write_only_in_the_job(self):
        wf = load_wf()
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertEqual(bump_job()["permissions"], {"contents": "write", "pull-requests": "write", "actions": "write"})
        self.assertEqual(wf["concurrency"], {"group": "bump-webhooks-mcp", "cancel-in-progress": False})
        self.assertEqual(list(wf["jobs"]), ["bump"])

    def test_no_environment_and_only_the_workflow_token(self):
        self.assertNotIn("environment", bump_job())
        text = WF.read_text()
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", text)), {"GITHUB_TOKEN"})
        self.assertNotIn("PUSH_TOKEN", text)

    def test_every_action_is_pinned_to_a_full_sha(self):
        uses = re.findall(r"uses:\s*(\S+)", WF.read_text())
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, r"^[\w./-]+@[0-9a-f]{40}$")

    def test_checkout_sha_matches_the_other_workflows(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        self.assertIn(re.search(r"actions/checkout@\w{40}", WF.read_text()).group(0), ci)

    def test_manual_input_reaches_the_script_only_through_env(self):
        for run in runs():
            self.assertNotIn("${{", run)
        step = next(s for s in bump_job()["steps"] if "bump_image_pin.py" in s.get("run", ""))
        self.assertEqual(step["env"], {"DIGEST": "${{ inputs.digest }}"})
        self.assertIn('${DIGEST:+--digest "$DIGEST"}', step["run"])

    def test_pr_step_runs_only_when_changed_and_gets_its_data_through_env(self):
        step = pr_step()
        self.assertEqual(step["if"], "steps.bump.outputs.changed == 'true'")
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        for k, out in {"SHORT": "short", "NEW_DIGEST": "digest", "PREVIOUS": "previous", "REVISION": "revision"}.items():
            self.assertEqual(step["env"][k], "${{ steps.bump.outputs.%s }}" % out)


def pr_step():
    return next(s for s in bump_job()["steps"] if "gh pr create" in s.get("run", ""))


FAKE = """#!/usr/bin/env python3
import json, os, sys
name = os.path.basename(sys.argv[0])
entry = {"cmd": name, "argv": sys.argv[1:]}
if "--body-file" in sys.argv:
    entry["body"] = open(sys.argv[sys.argv.index("--body-file") + 1]).read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
if name == "git" and "ls-remote" in sys.argv:
    sys.exit(int(os.environ.get("FAKE_LS_REMOTE_RC", "2")))
if name == "gh" and sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/owner/repo/pull/7")
"""


class OpenPrStep(unittest.TestCase):
    """Runs the PR step with fake git and gh first in PATH."""

    NEW_FULL = "sha256:" + "ab" * 32

    def run_step(self, ls_rc="2", revision=REV):
        with tempfile.TemporaryDirectory() as d:
            for n in ("git", "gh"):
                p = Path(d, n)
                p.write_text(FAKE)
                p.chmod(0o755)
            log, summary = Path(d, "log"), Path(d, "summary")
            env = {"PATH": f"{d}:{os.environ['PATH']}", "FAKE_LOG": str(log), "FAKE_LS_REMOTE_RC": ls_rc, "GH_TOKEN": "t",
                   "SHORT": "abababababab", "NEW_DIGEST": self.NEW_FULL, "PREVIOUS": OLD, "REVISION": revision,
                   "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": d}
            r = subprocess.run(["bash", "-e", "-c", pr_step()["run"]], cwd=d, env=env, capture_output=True, text=True, check=False)
            calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            return r, calls, summary.read_text() if summary.exists() else ""

    @staticmethod
    def sub(calls, cmd, verb):
        return [c for c in calls if c["cmd"] == cmd and verb in c["argv"]]

    def test_opens_one_branch_commit_push_pr_and_ci_dispatch(self):
        r, calls, summary = self.run_step()
        self.assertEqual(r.returncode, 0, r.stderr)
        branch = "bump/webhooks-mcp-abababababab"
        self.assertEqual(self.sub(calls, "git", "checkout")[0]["argv"], ["checkout", "-b", branch])
        commits = self.sub(calls, "git", "commit")
        self.assertEqual(len(commits), 1)
        self.assertIn("github-actions[bot]", " ".join(commits[0]["argv"]))
        self.assertEqual(commits[0]["argv"][commits[0]["argv"].index("-m") + 1], "Bump webhooks-mcp to sha256:abababababab…")
        self.assertEqual([c["argv"] for c in self.sub(calls, "git", "push")], [["push", "origin", branch]])
        prs = self.sub(calls, "gh", "create")
        self.assertEqual(len(prs), 1)
        argv = prs[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertEqual(argv[argv.index("--head") + 1], branch)
        for needle in (OLD, self.NEW_FULL, f"https://github.com/davcs86/webhooks-mcp/commit/{REV}", "deploy"):
            self.assertIn(needle, prs[0]["body"])
        self.assertEqual([c["argv"] for c in self.sub(calls, "gh", "run")], [["workflow", "run", "ci.yml", "--ref", branch]])
        self.assertIn("https://github.com/owner/repo/pull/7", summary)
        order = [c["argv"][0] for c in calls if c["cmd"] == "git"] + [c["argv"][0] for c in calls if c["cmd"] == "gh"]
        self.assertEqual(order.count("push"), 1)
        self.assertLess([(c["cmd"], c["argv"][0]) for c in calls].index(("git", "push")), [(c["cmd"], c["argv"][0]) for c in calls].index(("gh", "pr")))

    def test_unknown_revision_has_no_commit_link(self):
        r, calls, _ = self.run_step(revision="unknown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("/commit/", self.sub(calls, "gh", "create")[0]["body"])

    def test_skips_when_the_branch_already_exists_on_the_remote(self):
        r, calls, summary = self.run_step(ls_rc="0")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c["cmd"] for c in calls], ["git"])
        self.assertIn("ls-remote", calls[0]["argv"])
        self.assertIn("already", r.stdout)
        self.assertIn("already", summary)

    def test_a_failing_ls_remote_is_not_mistaken_for_a_missing_branch(self):
        r, calls, _ = self.run_step(ls_rc="128")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(self.sub(calls, "git", "push"), [])


class CiWorkflow(unittest.TestCase):
    def test_ci_can_be_dispatched_and_this_test_runs_in_lint(self):
        ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        self.assertIn("workflow_dispatch", ci[True])
        self.assertTrue(any("bump-image-pin-test.py" in s.get("run", "") for s in ci["jobs"]["lint"]["steps"]))


if __name__ == "__main__":
    unittest.main()
