#!/usr/bin/env python3
"""Checks scripts/bump_image_pin.py (offline, injected registry) and the bump-webhooks-mcp workflow that runs it."""
import contextlib
import http.client
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
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


class RepoFiles(unittest.TestCase):
    def test_the_real_docs_and_versions_files_are_accepted_by_the_edits(self):
        docs = (ROOT / "docs/versions.md").read_text()
        versions = (ROOT / "inventories/prod/group_vars/all/versions.yml").read_text()
        old = bump.current_pin(versions, VAR, IMAGE)
        self.assertNotEqual(bump.replace_docs_row(docs, VAR, IMAGE, NEW, REV, "2026-12-01"), docs)
        self.assertIn(NEW, bump.replace_pin(versions, VAR, IMAGE, NEW))
        self.assertNotIn(old, bump.replace_pin(versions, VAR, IMAGE, NEW))


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
        req = urllib.request.Request("https://ghcr.io/x", headers={"Authorization": "Bearer t"})
        with self.assertRaises(bump.BumpError):
            bump.SafeRedirect().redirect_request(req, None, 302, "Found", {}, "http://evil.test/x")

    def test_authorization_is_dropped_on_a_cross_host_redirect_and_kept_on_the_same_host(self):
        req = urllib.request.Request("https://ghcr.io/x", headers={"Authorization": "Bearer t"})
        h = bump.SafeRedirect()
        cross = h.redirect_request(req, None, 302, "Found", {}, "https://pkg-containers.example.test/y")
        self.assertNotIn("Authorization", cross.headers)
        same = h.redirect_request(req, None, 302, "Found", {}, "https://ghcr.io/z")
        self.assertEqual(same.headers["Authorization"], "Bearer t")

    def test_malformed_digest_headers_are_rejected(self):
        for bad in (NEW + "\n", "sha256:abc", "", "md5:" + "a" * 64):
            reg = registry(extra={("HEAD", M + "main"): (200, {"Docker-Content-Digest": bad}, b"")})
            with self.assertRaisesRegex(bump.BumpError, "Docker-Content-Digest"):
                bump.Registry(IMAGE, reg).tag_digest("main")

    def test_a_trailing_newline_does_not_pass_digest_validation(self):
        with self.assertRaises(bump.BumpError):
            bump.Registry(IMAGE, registry()).verify_digest(NEW + "\n")

    def test_index_without_linux_amd64_gives_unknown(self):
        idx = {"manifests": [{"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}}]}
        reg = registry(extra={("GET", M + NEW): (200, {}, idx)})
        self.assertEqual(bump.Registry(IMAGE, reg).revision(NEW), "unknown")

    def test_any_failure_reading_the_label_gives_unknown(self):
        cases = {
            "manifest without config": {("GET", M + "sha256:amd"): (200, {}, {})},
            "blob not json": {("GET", B + "sha256:cfg"): (200, {}, b"<html>")},
            "blob http 500": {("GET", B + "sha256:cfg"): (500, {}, b"")},
            "labels is a list": {("GET", B + "sha256:cfg"): (200, {}, {"config": {"Labels": []}})},
        }
        for name, extra in cases.items():
            self.assertEqual(bump.Registry(IMAGE, registry(extra=extra)).revision(NEW), "unknown", name)

    def test_token_problems_are_errors(self):
        for name, resp in {"not json": (200, {}, b"nope"), "no token": (200, {}, {}), "http 503": (503, {}, b"")}.items():
            reg = registry(extra={("GET", TOKEN_URL): resp})
            with self.assertRaises(bump.BumpError, msg=name):
                bump.Registry(IMAGE, reg).tag_digest("main")

    def test_manifest_that_is_not_json_gives_an_unknown_revision(self):
        reg = registry(extra={("GET", M + NEW): (200, {}, b"nope")})
        self.assertEqual(bump.Registry(IMAGE, reg).revision(NEW), "unknown")

    def test_network_failures_become_bump_errors_with_a_timeout_on_every_call(self):
        seen = []

        class Opener:
            def __init__(self, exc):
                self.exc = exc

            def open(self, request, timeout=None):
                seen.append(timeout)
                raise self.exc

        orig = urllib.request.build_opener
        try:
            for exc in (urllib.error.URLError("down"), TimeoutError("slow"), http.client.InvalidURL("bad"), ConnectionResetError("reset")):
                urllib.request.build_opener = lambda *a, e=exc: Opener(e)
                with self.assertRaises(bump.BumpError, msg=repr(exc)):
                    bump.http_fetch("GET", "https://ghcr.io/x", {})
        finally:
            urllib.request.build_opener = orig
        self.assertTrue(seen and all(t for t in seen))


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

    def test_a_network_error_is_one_error_line_and_writes_nothing(self):
        def down(method, url, headers):
            raise bump.BumpError("cannot reach registry: down")

        rc, _, err = self.run_cli(reg=down)
        self.assertEqual((rc, self.v.read_text(), self.d.read_text()), (1, VERSIONS, DOCS))
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertFalse(self.out.exists())

    def test_a_failed_label_lookup_does_not_fail_the_bump(self):
        rc, _, err = self.run_cli(reg=registry(extra={("GET", B + "sha256:cfg"): (500, {}, b"")}))
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.outputs()["revision"], "unknown")
        self.assertIn("(commit `unknown`, 2026-12-01)", self.d.read_text())

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
        self.assertEqual(wf["concurrency"], {"group": "bump-webhooks-mcp", "cancel-in-progress": True})
        self.assertEqual(list(wf["jobs"]), ["bump"])

    def test_no_environment_and_only_the_workflow_token(self):
        self.assertNotIn("environment", bump_job())
        text = WF.read_text()
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", text)), {"GITHUB_TOKEN"})
        self.assertNotIn("PUSH_TOKEN", text)

    def test_job_runs_only_from_main_and_is_time_boxed(self):
        self.assertEqual(bump_job()["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(bump_job()["timeout-minutes"], 10)

    def test_checkout_keeps_no_credentials_and_the_token_is_never_echoed(self):
        checkout = next(s for s in bump_job()["steps"] if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertNotRegex(pr_step()["run"], r"(echo|printf|set -x)[^\n]*GH_TOKEN")

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
for a in sys.argv:
    if a.startswith("body=@"):
        entry["body"] = open(a[len("body=@"):]).read()
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps(entry) + "\\n")
if name == "gh" and sys.argv[1:3] == ["pr", "list"]:
    print(os.environ.get("FAKE_PR_LIST", "[]"))
if name == "gh" and sys.argv[1:3] == ["run", "list"]:
    print(os.environ.get("FAKE_RUN_LIST", "[]"))
if name == "gh" and sys.argv[1:3] == ["pr", "create"]:
    print("https://github.com/owner/repo/pull/7")
if name == os.environ.get("FAKE_FAIL_CMD") and os.environ.get("FAKE_FAIL_VERB") in sys.argv:
    sys.exit(1)
"""

SECRET = "ghs_SECRET_TOKEN_VALUE"
HEAD_SHA = "f" * 40


class OpenPrStep(unittest.TestCase):
    """Runs the PR step with fake git and gh first in PATH."""

    NEW_FULL = "sha256:" + "ab" * 32
    BRANCH = "bump/webhooks-mcp"
    TITLE = "Bump webhooks-mcp to sha256:abababababab…"
    OTHER = "Bump webhooks-mcp to sha256:cdcdcdcdcdcd…"
    URL = f"https://x-access-token:{SECRET}@github.com/owner/repo.git"

    @staticmethod
    def pr(state, number, title, fork=False):
        return {"state": state, "number": number, "url": f"https://github.com/owner/repo/pull/{number}", "title": title,
                "headRefOid": HEAD_SHA, "isCrossRepository": fork}

    @staticmethod
    def dispatches(calls):
        return [c["argv"] for c in calls if c["cmd"] == "gh" and c["argv"][:2] == ["workflow", "run"]]

    def run_step(self, revision=REV, prs=None, fail=None, ci_runs=None):
        with tempfile.TemporaryDirectory() as d:
            for n in ("git", "gh"):
                p = Path(d, n)
                p.write_text(FAKE)
                p.chmod(0o755)
            log, summary = Path(d, "log"), Path(d, "summary")
            env = {"PATH": f"{d}:{os.environ['PATH']}", "FAKE_LOG": str(log), "GH_TOKEN": SECRET,
                   "FAKE_PR_LIST": json.dumps(prs or []), "FAKE_RUN_LIST": json.dumps(ci_runs or []), "GITHUB_REPOSITORY": "owner/repo",
                   "SHORT": "abababababab", "NEW_DIGEST": self.NEW_FULL, "PREVIOUS": OLD, "REVISION": revision,
                   "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": d}
            if fail:
                env["FAKE_FAIL_CMD"], env["FAKE_FAIL_VERB"] = fail
            r = subprocess.run(["bash", "-e", "-c", pr_step()["run"]], cwd=d, env=env, capture_output=True, text=True, check=False)
            calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            return r, calls, summary.read_text() if summary.exists() else ""

    @staticmethod
    def sub(calls, cmd, verb):
        return [c for c in calls if c["cmd"] == cmd and verb in c["argv"]]

    def assert_nothing_written(self, calls):
        for verb in ("checkout", "commit", "push"):
            self.assertEqual(self.sub(calls, "git", verb), [], verb)
        for verb in ("create", "api"):
            self.assertEqual(self.sub(calls, "gh", verb), [], verb)

    def assert_branch_rebuilt_and_ci_started(self, calls):
        self.assertEqual(self.sub(calls, "git", "checkout")[0]["argv"], ["checkout", "-B", self.BRANCH])
        commits = self.sub(calls, "git", "commit")
        self.assertEqual(len(commits), 1)
        self.assertIn("github-actions[bot]", " ".join(commits[0]["argv"]))
        self.assertEqual(commits[0]["argv"][commits[0]["argv"].index("-m") + 1], self.TITLE)
        self.assertEqual([c["argv"] for c in self.sub(calls, "git", "push")], [["push", "--force", self.URL, f"HEAD:refs/heads/{self.BRANCH}"]])
        self.assertEqual(self.dispatches(calls), [["workflow", "run", "ci.yml", "--ref", self.BRANCH]])
        kinds = [(c["cmd"], c["argv"][0]) for c in calls]
        self.assertLess(kinds.index(("git", "push")), max(i for i, k in enumerate(kinds) if k in (("gh", "pr"), ("gh", "api"))))

    def assert_created(self, calls):
        self.assert_branch_rebuilt_and_ci_started(calls)
        self.assertEqual(self.sub(calls, "gh", "api"), [])
        prs = self.sub(calls, "gh", "create")
        self.assertEqual(len(prs), 1)
        argv = prs[0]["argv"]
        self.assertEqual(argv[argv.index("--base") + 1], "main")
        self.assertEqual(argv[argv.index("--head") + 1], self.BRANCH)
        self.assertEqual(argv[argv.index("--title") + 1], self.TITLE)
        return prs[0]

    def test_pr_lookup_is_for_the_one_bump_branch_in_every_state(self):
        _, calls, _ = self.run_step()
        argv = self.sub(calls, "gh", "list")[0]["argv"]
        self.assertEqual(argv[argv.index("--head") + 1], self.BRANCH)
        self.assertEqual(argv[argv.index("--state") + 1], "all")
        self.assertEqual(argv[argv.index("--json") + 1], "state,number,url,title,headRefOid,isCrossRepository")

    def test_no_pr_yet_gets_one_commit_one_push_one_pr_and_a_ci_dispatch(self):
        r, calls, summary = self.run_step()
        self.assertEqual(r.returncode, 0, r.stderr)
        pr = self.assert_created(calls)
        for needle in (OLD, self.NEW_FULL, f"https://github.com/davcs86/webhooks-mcp/commit/{REV}", "deploy", "not verified", "by hand are lost"):
            self.assertIn(needle, pr["body"])
        self.assertIn("Opened https://github.com/owner/repo/pull/7", summary)

    def test_nothing_targets_main_or_leaks_the_token(self):
        for prs in ([], [self.pr("OPEN", 3, self.OTHER)]):
            r, calls, summary = self.run_step(prs=prs)
            self.assertEqual(r.returncode, 0, r.stderr)
            for c in self.sub(calls, "git", "push"):
                self.assertFalse(any(a in ("main", "refs/heads/main", "HEAD:main", "HEAD:refs/heads/main") for a in c["argv"]))
            self.assertNotIn(SECRET, r.stdout + r.stderr + summary)

    def test_unknown_revision_has_no_commit_link(self):
        r, calls, _ = self.run_step(revision="unknown")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("/commit/", self.sub(calls, "gh", "create")[0]["body"])

    def test_open_pr_for_this_digest_with_ci_on_its_head_means_nothing_to_do(self):
        r, calls, summary = self.run_step(prs=[self.pr("OPEN", 7, self.TITLE)], ci_runs=[{"headSha": HEAD_SHA}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_nothing_written(calls)
        self.assertEqual(self.dispatches(calls), [])
        self.assertIn("https://github.com/owner/repo/pull/7", summary)
        self.assertIn("https://github.com/owner/repo/pull/7", r.stdout)

    def test_open_pr_for_this_digest_without_ci_on_its_head_gets_ci_started(self):
        r, calls, _ = self.run_step(prs=[self.pr("OPEN", 7, self.TITLE)], ci_runs=[{"headSha": "0" * 40}])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_nothing_written(calls)
        self.assertEqual(self.dispatches(calls), [["workflow", "run", "ci.yml", "--ref", self.BRANCH]])

    def test_fork_prs_on_a_branch_of_the_same_name_are_ignored(self):
        r, calls, _ = self.run_step(prs=[self.pr("OPEN", 4, self.OTHER, fork=True), self.pr("CLOSED", 5, self.TITLE, fork=True)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("::warning::", r.stdout)
        self.assert_created(calls)

    def test_open_pr_for_an_older_digest_is_reused_not_duplicated(self):
        r, calls, summary = self.run_step(prs=[self.pr("OPEN", 3, self.OTHER), self.pr("MERGED", 2, "older")])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_branch_rebuilt_and_ci_started(calls)
        self.assertEqual(self.sub(calls, "gh", "create"), [])
        edits = self.sub(calls, "gh", "api")
        self.assertEqual(len(edits), 1)
        argv = edits[0]["argv"]
        self.assertEqual(argv[:4], ["api", "-X", "PATCH", "repos/owner/repo/pulls/3"])
        self.assertIn(f"title={self.TITLE}", argv)
        self.assertIn(self.NEW_FULL, edits[0]["body"])
        self.assertIn("Updated https://github.com/owner/repo/pull/3", summary)

    def test_a_merged_or_closed_pr_for_another_digest_does_not_block_a_new_pr(self):
        r, calls, _ = self.run_step(prs=[self.pr("MERGED", 8, self.OTHER), self.pr("CLOSED", 9, self.OTHER)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("::warning::", r.stdout)
        self.assert_created(calls)

    def test_a_closed_pr_for_this_digest_is_a_rejection_and_warns(self):
        r, calls, summary = self.run_step(prs=[self.pr("CLOSED", 9, self.TITLE)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_nothing_written(calls)
        self.assertIn("::warning::", r.stdout)
        self.assertIn("rejected", summary)
        self.assertIn("https://github.com/owner/repo/pull/9", summary)

    def test_a_rejected_digest_does_not_overwrite_an_open_pr_for_another(self):
        r, calls, _ = self.run_step(prs=[self.pr("CLOSED", 9, self.TITLE), self.pr("OPEN", 10, self.OTHER)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_nothing_written(calls)
        self.assertIn("::warning::", r.stdout)

    def test_an_open_pr_for_this_digest_wins_over_its_earlier_rejection(self):
        r, calls, _ = self.run_step(prs=[self.pr("CLOSED", 1, self.TITLE), self.pr("OPEN", 2, self.TITLE)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("::warning::", r.stdout)
        self.assert_nothing_written(calls)

    def test_a_failing_pr_lookup_stops_before_anything_is_written(self):
        r, calls, _ = self.run_step(fail=("gh", "list"))
        self.assertNotEqual(r.returncode, 0)
        self.assert_nothing_written(calls)
        self.assertNotIn(SECRET, r.stdout + r.stderr)

    def test_a_failing_push_opens_no_pr_and_starts_no_ci(self):
        r, calls, _ = self.run_step(fail=("git", "push"))
        self.assertNotEqual(r.returncode, 0)
        for verb in ("create", "api", "workflow"):
            self.assertEqual(self.sub(calls, "gh", verb), [], verb)
        self.assertNotIn(SECRET, r.stdout + r.stderr)


CWF = ROOT / ".github/workflows/bump-commsgun.yml"
API_VAR, UI_VAR = "commsgun_api_image", "commsgun_ui_image"
API_IMG, UI_IMG = "ghcr.io/davcs86/commsgun-api", "ghcr.io/davcs86/commsgun-ui"
API_OLD, UI_OLD = "sha256:" + "a1" * 32, "sha256:" + "b2" * 32
API_NEW, UI_NEW = "sha256:" + "c3" * 32, "sha256:" + "d4" * 32
CREV = "145add0" + "0" * 33


def csteps():
    return yaml.safe_load(CWF.read_text())["jobs"]["bump"]["steps"]


def cstep(sid=None, needle=None):
    return next(s for s in csteps() if (sid and s.get("id") == sid) or (needle and needle in s.get("run", "")))


class CommsgunWorkflow(unittest.TestCase):
    def test_triggers_permissions_and_concurrency(self):
        wf = yaml.safe_load(CWF.read_text())
        self.assertEqual(sorted(wf[True]), ["schedule", "workflow_dispatch"])
        self.assertEqual(wf[True]["schedule"], [{"cron": "41 15 * * *"}])
        self.assertEqual(list(wf[True]["workflow_dispatch"]["inputs"]), ["api_digest", "ui_digest"])
        for i in wf[True]["workflow_dispatch"]["inputs"].values():
            self.assertIs(i.get("required", False), False)
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertEqual(wf["jobs"]["bump"]["permissions"], {"contents": "write", "pull-requests": "write", "actions": "write"})
        self.assertEqual(wf["concurrency"], {"group": "bump-commsgun", "cancel-in-progress": True})
        self.assertEqual(wf["jobs"]["bump"]["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(set(re.findall(r"secrets\.(\w+)", CWF.read_text())), {"GITHUB_TOKEN"})

    def test_script_runs_once_per_image_in_order(self):
        api, ui = cstep("api"), cstep("ui")
        self.assertIn(f"bump_image_pin.py --var {API_VAR} --image {API_IMG}", api["run"])
        self.assertIn(f"bump_image_pin.py --var {UI_VAR} --image {UI_IMG}", ui["run"])
        self.assertEqual(api["env"], {"DIGEST": "${{ inputs.api_digest }}"})
        self.assertEqual(ui["env"], {"DIGEST": "${{ inputs.ui_digest }}"})
        ids = [s.get("id") for s in csteps()]
        self.assertLess(ids.index("api"), ids.index("ui"))
        self.assertLess(ids.index("ui"), ids.index("guard"))
        for run in (s["run"] for s in csteps() if "run" in s):
            self.assertNotIn("${{", run)

    def test_actions_pinned_and_checkout_keeps_no_credentials(self):
        self.assertEqual([u for u in re.findall(r"uses:\s*(\S+)", CWF.read_text()) if not re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u)], [])
        co = next(s for s in csteps() if str(s.get("uses", "")).startswith("actions/checkout@"))
        self.assertIs(co["with"]["persist-credentials"], False)

    def test_pr_step_needs_the_guard_and_gets_its_data_through_env(self):
        pr = cstep(needle="gh pr create")
        self.assertEqual(pr["if"], "steps.guard.outputs.proceed == 'true'")
        self.assertEqual(pr["env"]["GH_TOKEN"], "${{ secrets.GITHUB_TOKEN }}")
        want = {"API_PREV": "steps.api.outputs.previous", "UI_PREV": "steps.ui.outputs.previous",
                "API_DIGEST": "steps.guard.outputs.api_digest", "UI_DIGEST": "steps.guard.outputs.ui_digest",
                "REVISION": "steps.guard.outputs.revision"}
        for k, v in want.items():
            self.assertEqual(pr["env"][k], "${{ %s }}" % v)
        guard = cstep("guard")
        self.assertEqual(guard["if"], "steps.api.outputs.changed == 'true' || steps.ui.outputs.changed == 'true'")
        self.assertEqual(guard["env"]["API_REV"], "${{ steps.api.outputs.revision }}")
        self.assertEqual(guard["env"]["UI_REV"], "${{ steps.ui.outputs.revision }}")
        done = next(s for s in csteps() if s.get("name") == "Already pinned")
        self.assertEqual(done["if"], "steps.api.outputs.changed == 'false' && steps.ui.outputs.changed == 'false'")


def run_guard(api_rev, ui_rev, docs_commits=("145add0", "145add0")):
    with tempfile.TemporaryDirectory() as d:
        Path(d, "inventories/prod/group_vars/all").mkdir(parents=True)
        Path(d, "docs").mkdir()
        Path(d, "inventories/prod/group_vars/all/versions.yml").write_text(
            f'{API_VAR}: "{API_IMG}@{API_NEW}"\n{UI_VAR}: "{UI_IMG}@{UI_NEW}"\n')
        Path(d, "docs/versions.md").write_text(
            f"| a (`{API_VAR}`) | `{API_IMG}@sha256:c3c3c3c3…`, digest only (commit `{docs_commits[0]}`, 2026-11-13) | x |\n"
            f"| u (`{UI_VAR}`) | `{UI_IMG}@sha256:d4d4d4d4…`, digest only (commit `{docs_commits[1]}`, 2026-11-13) | x |\n")
        out, summary = Path(d, "out"), Path(d, "summary")
        env = {"PATH": os.environ["PATH"], "API_REV": api_rev, "UI_REV": ui_rev, "GITHUB_OUTPUT": str(out), "GITHUB_STEP_SUMMARY": str(summary)}
        r = subprocess.run(["bash", "-e", "-c", cstep("guard")["run"]], cwd=d, env=env, capture_output=True, text=True, check=False)
        outputs = dict(l.split("=", 1) for l in out.read_text().splitlines()) if out.exists() else {}
        return r, outputs, summary.read_text() if summary.exists() else ""


class CommsgunGuard(unittest.TestCase):
    def test_different_revisions_refuse_the_pair_and_name_both(self):
        r, out, summary = run_guard("a" * 40, "b" * 40)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out, {"proceed": "false"})
        self.assertIn("::warning::", r.stdout)
        for rev in ("a" * 40, "b" * 40):
            self.assertIn(rev, summary)

    def test_an_unlabelled_image_refuses_the_pair(self):
        for a, u in (("unknown", "unknown"), (CREV, "unknown"), ("unknown", CREV)):
            r, out, _ = run_guard(a, u)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(out, {"proceed": "false"}, (a, u))

    def test_same_revision_proceeds_and_reports_pins_and_revision(self):
        r, out, _ = run_guard(CREV, CREV)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(out, {"proceed": "true", "api_digest": API_NEW, "ui_digest": UI_NEW, "revision": CREV})

    def test_an_unchanged_image_is_checked_against_the_commit_in_the_docs_row(self):
        r, out, _ = run_guard("", CREV)
        self.assertEqual(out.get("proceed"), "true", r.stdout + r.stderr)
        self.assertEqual(out["revision"], CREV)
        r, out, _ = run_guard(CREV, "", docs_commits=("145add0", "145add0"))
        self.assertEqual(out.get("proceed"), "true")
        r, out, _ = run_guard("", "e" * 40)
        self.assertEqual(out, {"proceed": "false"})
        r, out, _ = run_guard("", CREV, docs_commits=("145add0", "unknown"))
        self.assertEqual(out, {"proceed": "true", "api_digest": API_NEW, "ui_digest": UI_NEW, "revision": CREV})
        r, out, _ = run_guard("", CREV, docs_commits=("unknown", "145add0"))
        self.assertEqual(out, {"proceed": "false"})


class CommsgunPrStep(unittest.TestCase):
    BRANCH = "bump/commsgun"
    TITLE = f"Bump commsgun to api sha256:{API_NEW[7:19]}… / ui sha256:{UI_NEW[7:19]}…"
    OTHER = "Bump commsgun to api sha256:000000000000… / ui sha256:111111111111…"
    pr = staticmethod(OpenPrStep.pr)
    sub = staticmethod(OpenPrStep.sub)
    dispatches = staticmethod(OpenPrStep.dispatches)

    def run_step(self, prs=None, api_prev=API_OLD, ui_prev=UI_OLD, revision=CREV):
        step = cstep(needle="gh pr create")
        with tempfile.TemporaryDirectory() as d:
            for n in ("git", "gh"):
                Path(d, n).write_text(FAKE)
                Path(d, n).chmod(0o755)
            log, summary = Path(d, "log"), Path(d, "summary")
            env = {"PATH": f"{d}:{os.environ['PATH']}", "FAKE_LOG": str(log), "GH_TOKEN": SECRET, "FAKE_PR_LIST": json.dumps(prs or []),
                   "FAKE_RUN_LIST": "[]", "GITHUB_REPOSITORY": "owner/repo", "API_PREV": api_prev, "UI_PREV": ui_prev,
                   "API_DIGEST": API_NEW, "UI_DIGEST": UI_NEW, "REVISION": revision, "GITHUB_STEP_SUMMARY": str(summary), "RUNNER_TEMP": d}
            r = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=d, env=env, capture_output=True, text=True, check=False)
            calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
            return r, calls, summary.read_text() if summary.exists() else ""

    def assert_nothing_written(self, calls):
        for verb in ("checkout", "commit", "push"):
            self.assertEqual(self.sub(calls, "git", verb), [], verb)
        for verb in ("create", "api"):
            self.assertEqual(self.sub(calls, "gh", verb), [], verb)

    def test_only_the_ui_changed_still_pushes_the_branch_and_opens_one_pr(self):
        r, calls, summary = self.run_step(api_prev="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sub(calls, "git", "checkout")[0]["argv"], ["checkout", "-B", self.BRANCH])
        self.assertEqual([c["argv"] for c in self.sub(calls, "git", "push")],
                         [["push", "--force", f"https://x-access-token:{SECRET}@github.com/owner/repo.git", f"HEAD:refs/heads/{self.BRANCH}"]])
        self.assertEqual(self.dispatches(calls), [["workflow", "run", "ci.yml", "--ref", self.BRANCH]])
        commit = self.sub(calls, "git", "commit")[0]["argv"]
        self.assertEqual(commit[commit.index("-m") + 1], self.TITLE)
        pr = self.sub(calls, "gh", "create")
        self.assertEqual(len(pr), 1)
        self.assertEqual(pr[0]["argv"][pr[0]["argv"].index("--title") + 1], self.TITLE)
        body = pr[0]["body"]
        for needle in (API_NEW, UI_NEW, UI_OLD, f"https://github.com/davcs86/commsgun/commit/{CREV}", "not verified", "deploy", "by hand are lost"):
            self.assertIn(needle, body)
        self.assertNotIn(API_OLD, body)
        self.assertIn("Opened https://github.com/owner/repo/pull/7", summary)
        self.assertNotIn(SECRET, r.stdout + r.stderr + summary)

    def test_both_changed_lists_both_previous_digests(self):
        r, calls, _ = self.run_step()
        self.assertEqual(r.returncode, 0, r.stderr)
        body = self.sub(calls, "gh", "create")[0]["body"]
        for needle in (API_OLD, UI_OLD, API_NEW, UI_NEW):
            self.assertIn(needle, body)

    def test_open_pr_with_the_same_title_means_nothing_pushed(self):
        r, calls, summary = self.run_step(prs=[self.pr("OPEN", 7, self.TITLE)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_nothing_written(calls)
        self.assertIn("https://github.com/owner/repo/pull/7", summary)

    def test_closed_unmerged_pr_with_the_same_title_is_not_proposed_again(self):
        r, calls, summary = self.run_step(prs=[self.pr("CLOSED", 9, self.TITLE)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assert_nothing_written(calls)
        self.assertIn("::warning::", r.stdout)
        self.assertIn("rejected", summary)

    def test_open_pr_for_older_digests_is_reused(self):
        r, calls, _ = self.run_step(prs=[self.pr("OPEN", 3, self.OTHER)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.sub(calls, "gh", "create"), [])
        self.assertEqual(self.sub(calls, "gh", "api")[0]["argv"][:4], ["api", "-X", "PATCH", "repos/owner/repo/pulls/3"])


class CiWorkflow(unittest.TestCase):
    def test_ci_can_be_dispatched_and_this_test_runs_in_lint(self):
        ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
        self.assertIn("workflow_dispatch", ci[True])
        self.assertTrue(any("bump-image-pin-test.py" in s.get("run", "") for s in ci["jobs"]["lint"]["steps"]))


if __name__ == "__main__":
    unittest.main()
