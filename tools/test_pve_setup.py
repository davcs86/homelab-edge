"""Offline tests for pve-setup.py: probes and commands are faked, nothing touches a Proxmox host or needs root."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from datetime import timedelta
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

spec = importlib.util.spec_from_file_location("pve_setup", Path(__file__).with_name("pve-setup.py"))
pve = importlib.util.module_from_spec(spec)
sys.modules["pve_setup"] = pve
spec.loader.exec_module(pve)
REAL_PINNED_KERNEL = pve.pinned_kernel
runner = CliRunner()

RES = [
    {"vmid": 100, "name": "tpl", "type": "qemu", "status": "stopped", "template": 1, "tags": "pve-setup"},
    {"vmid": 101, "name": "old", "type": "qemu", "status": "running", "tags": "prod"},
    {"vmid": 110, "name": "mine", "type": "lxc", "status": "running", "tags": "pve-setup;x"},
]
JOB = {"id": "pve-setup-nightly", "schedule": "03:30", "storage": "bk", "mode": "snapshot", "all": 1,
       "prune-backups": "keep-weekly=4,keep-daily=7", "enabled": 1}
TS_UP = {"package": "1.98.10", "held": True, "backend": "Running", "hostname": "pve", "tags": ["tag:pve"],
         "control": "https://hs.example.test", "dns": False}
LIST = "deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian bookworm main\n"


def setUpModule():
    pve.LOG = Path(tempfile.mkdtemp()) / "pve-setup.log"


class Isolated(unittest.TestCase):
    """The dry-run flag is module state set by the CLI callback; start each test from a clean slate."""

    def setUp(self):
        pve.STATE["dry_run"] = False


class Env:
    """Fakes every probe; records the steps that would run."""

    def __init__(self, sh=None, soft=None, texts=None, ts=None, root=True):
        self.sh = {"/cluster/resources": RES, "/cluster/nextid": "102", **(sh or {})}
        self.softmap = soft or {}
        self.texts = texts or {}
        self.ts = ts
        self.root = root
        self.steps = []
        self.stack = contextlib.ExitStack()

    def __enter__(self):
        e = self.stack
        e.enter_context(mock.patch.object(pve, "pvesh_soft", lambda path, *a: self.sh.get(path)))
        e.enter_context(mock.patch.object(pve, "pvesh", self._hard))
        e.enter_context(mock.patch.object(pve, "soft", lambda argv: self.softmap.get(" ".join(argv))))
        e.enter_context(mock.patch.object(pve, "read_text", lambda p: self.texts.get(p)))
        e.enter_context(mock.patch.object(pve, "tailscale_state", lambda: self.ts))
        e.enter_context(mock.patch.object(pve, "node", lambda: "pve"))
        e.enter_context(mock.patch.object(pve, "storages", lambda c: [("local-lvm", "local-lvm (lvmthin, 50 GiB free)")]))
        e.enter_context(mock.patch.object(pve, "bridges", lambda: [("vmbr0", "vmbr0 (192.168.1.2/24)")]))
        e.enter_context(mock.patch.object(pve, "run_step", self._record))
        e.enter_context(mock.patch.object(pve.os, "geteuid", lambda: 0 if self.root else 1000))
        return self

    def __exit__(self, *a):
        self.stack.close()

    def _hard(self, path, *a):
        return self.sh[path]

    def _record(self, step):
        self.steps.append(step)
        return True

    def run(self, args, text="", dry=False):
        return runner.invoke(pve.app, (["--dry-run"] if dry else []) + args, input=text)

    def cmds(self):
        return [" ".join(s.argv) for s in self.steps if isinstance(s, pve.Cmd)]


class Heartbeat(unittest.TestCase):
    ANSWERS = "status.ts.example.test\n\n60\n\n\nTOKEN-VALUE\n100.64.0.1\n"

    def desired(self):
        return pve.heartbeat_files("status.ts.example.test", "home_proxmox-heartbeat", 60, "TOKEN-VALUE", "100.64.0.1")

    def test_fresh_install_plans_everything_and_hides_the_token(self):
        with Env() as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("TOKEN-VALUE", r.output)
        self.assertIn("-K /etc/pve-heartbeat.curl", r.output)
        self.assertIn("STATUS_HOST=status.ts.example.test\nENDPOINT=home_proxmox-heartbeat\nPOOL=rpool\nNVME=/dev/nvme0", r.output)
        self.assertIn("mode 0600", r.output)
        self.assertIn("/usr/local/sbin/pve-heartbeat", r.output)

    def test_curl_config_pins_the_edge_address_so_dns_can_stay_off(self):
        content = self.desired()[pve.HEARTBEAT_FILES[0]][0]
        self.assertIn('resolve = "status.ts.example.test:443:100.64.0.1"', content)
        self.assertIn('header = "Authorization: Bearer TOKEN-VALUE"', content)
        self.assertNotIn("resolve", pve.heartbeat_files("h", "e", 60, "T")[pve.HEARTBEAT_FILES[0]][0])

    def test_edge_address_must_be_a_tailnet_address(self):
        with Env() as env:
            r = env.run(["heartbeat"], "status.ts.example.test\n\n60\n\n\nT\n8.8.8.8\nnope\n100.64.0.1\n", dry=True)
        self.assertEqual(r.output.count("a tailnet address in 100.64.0.0/10"), 2)
        self.assertNotIn("8.8.8.8\n", r.output.split("== ")[-1])

    def test_unchanged_install_does_nothing(self):
        texts = {p: c for p, (c, _, _) in self.desired().items()}
        soft = {"systemctl is-enabled pve-heartbeat.timer": "enabled\n", "systemctl is-active pve-heartbeat.timer": "active\n"}
        with Env(texts=texts, soft=soft) as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertIn("Nothing to do", r.output)
        self.assertNotIn("write ", r.output)

    def test_only_the_changed_file_is_rewritten(self):
        texts = {p: c for p, (c, _, _) in self.desired().items()}
        texts["/etc/systemd/system/pve-heartbeat.timer"] = "old\n"
        soft = {"systemctl is-enabled pve-heartbeat.timer": "enabled\n", "systemctl is-active pve-heartbeat.timer": "active\n"}
        with Env(texts=texts, soft=soft) as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertIn("write /etc/systemd/system/pve-heartbeat.timer", r.output)
        self.assertNotIn("write /etc/systemd/system/pve-heartbeat.service", r.output)
        self.assertNotIn("TOKEN-VALUE", r.output)


class Tailscale(Isolated):
    OS = mock.patch.object(pve.Path, "read_text", return_value='VERSION_CODENAME="trixie"\n')
    REPO = {pve.TAILSCALE_LIST: LIST.replace("bookworm", "trixie")}

    def install(self, ts, text="\n", soft=None):
        with Env(ts=ts, texts=self.REPO, soft=soft) as env, mock.patch.object(pve.os.path, "exists", return_value=True), self.OS:
            return env.run(["tailscale-install"], text, dry=True)

    def test_pin_matches_the_edge_version_file(self):
        versions = (Path(__file__).parent.parent / "inventories/prod/group_vars/all/versions.yml").read_text()
        pinned = next(l.split('"')[1] for l in versions.splitlines() if l.startswith("tailscale_package_version:"))
        self.assertEqual(pve.TAILSCALE_VERSION, pinned)

    def test_install_defaults_to_the_edge_pin(self):
        r = self.install(None, "\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("tailscale=1.98.10", r.output)
        self.assertIn("check that tailscale 1.98.10 exists in apt", r.output)
        self.assertIn("systemctl enable --now tailscaled", r.output)

    def test_install_is_a_no_op_at_the_pinned_version(self):
        r = self.install(TS_UP, "\n", soft={"systemctl is-active tailscaled": "active\n"})
        self.assertIn("Nothing to do", r.output, r.output)

    def test_install_upgrades_with_held_package_flags(self):
        r = self.install({**TS_UP, "package": "1.90.0"}, "\n", soft={"systemctl is-active tailscaled": "active\n"})
        self.assertIn("--allow-change-held-packages --allow-downgrades tailscale=1.98.10", r.output, r.output)
        self.assertNotIn("apt update", r.output)

    def test_another_version_warns(self):
        self.assertIn("the edge pins 1.98.10", self.install(None, "1.100.0\n").output)

    def test_availability_check_stops_on_a_missing_version(self):
        with mock.patch.object(pve, "soft", lambda argv: " tailscale | 1.98.9 | https://x trixie/main amd64 Packages\n"):
            self.assertFalse(pve.apt_has_version("1.98.10"))
        with mock.patch.object(pve, "soft", lambda argv: " tailscale | 1.98.10 | https://x trixie/main amd64 Packages\n"):
            self.assertTrue(pve.apt_has_version("1.98.10"))

    def join(self, text, ts=None, env=None, dry=True):
        with Env(ts=ts) as e:
            return runner.invoke(pve.app, (["--dry-run"] if dry else []) + ["tailscale-join"], input=text, env=env)

    def test_join_uses_the_guide_defaults_and_hides_the_key(self):
        r = self.join("https://hs.example.test\n\n\nSECRET-KEY-VALUE\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("SECRET-KEY-VALUE", r.output)
        self.assertIn("--hostname pve --accept-dns=false --accept-routes=false --auth-key file:/run/pve-setup-authkey", r.output)
        self.assertNotIn("--reset", r.output)
        self.assertIn("tailscale ping --c 3 --timeout 10s edge", r.output)
        self.assertIn("tailscale ip -4", r.output)
        self.assertIn("registered as 'pve', not 'pve-1'", r.output)

    def test_a_bare_domain_or_a_path_is_not_a_control_url(self):
        r = self.join("natanddavehouse.com\nhttp://hs.example.test\nhttps://hs.example.test/api\nhttps://localhost\nhttps://hs.example.test/\n\n\nKEY\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(r.output.count("the full https:// URL of headscale"), 4)
        self.assertIn("--login-server https://hs.example.test --hostname", r.output)

    def test_the_plan_checks_headscale_before_anything_is_written_or_run(self):
        out = self.join("https://hs.example.test\n\n\nSECRET-KEY-VALUE\n").output
        order = [out.index(x) for x in ("answers like headscale", "write /run/pve-setup-authkey", "$ tailscale up")]
        self.assertEqual(order, sorted(order), out)

    def test_headscale_health_check(self):
        def reply(body, boom=None):
            class R(io.BytesIO):
                def __enter__(s): return s
                def __exit__(s, *a): return False
            def fake(url, timeout):
                if boom:
                    raise boom
                self.assertEqual((url, timeout), ("https://hs.example.test/health", 10))
                return R(body)
            return mock.patch.object(pve.urllib.request, "urlopen", fake)
        with reply(b'{"status":"pass"}'):
            self.assertTrue(pve.headscale_answers("https://hs.example.test"))
        for body, boom in ((b'{"status":"fail"}', None), (b"<html>", None), (b"[1]", None), (b"", OSError("timed out")),
                           (b"", pve.http.client.BadStatusLine("x"))):
            with reply(body, boom):
                self.assertFalse(pve.headscale_answers("https://hs.example.test"), (body, boom))

    def test_other_name_warns(self):
        r = self.join("https://hs.example.test\nworkbench\n\nKEY\n")
        self.assertIn("expect the name 'pve'", r.output)
        self.assertIn("--hostname workbench", r.output)

    def test_dns_can_be_overridden(self):
        r = self.join("https://hs.example.test\n\ny\nKEY\n")
        self.assertIn("--accept-dns=true", r.output)

    def test_empty_key_means_login_url(self):
        r = self.join("https://hs.example.test\n\n\n\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("approve-device", r.output)
        self.assertNotIn("--auth-key", r.output)
        self.assertNotIn("/run/pve-setup-authkey", r.output)
        self.assertIn("--timeout 15m", r.output)

    def test_join_already_joined_asks_for_no_key(self):
        r = self.join("https://hs.example.test\n\n\n", ts=TS_UP)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Already joined", r.output)
        self.assertIn("Nothing to do", r.output)

    def test_join_with_other_settings_reauthenticates(self):
        r = self.join("https://hs.example.test\nnew-name\n\nKEY\n", ts=TS_UP)
        self.assertIn("--reset --force-reauth", r.output)

    def test_reauth_over_a_tailnet_ssh_session_needs_a_yes(self):
        env = {"SSH_CONNECTION": "100.64.0.5 50000 100.64.0.9 22"}
        with Env(ts=TS_UP) as e:
            r = runner.invoke(pve.app, ["tailscale-join"], input="https://hs.example.test\nnew-name\n\nn\n", env=env)
        self.assertIn("can cut it", r.output)
        self.assertIn("Cancelled", r.output)
        self.assertEqual(e.steps, [])

    def test_lan_ssh_session_is_not_warned(self):
        env = {"SSH_CONNECTION": "192.168.1.5 50000 192.168.1.10 22"}
        r = self.join("https://hs.example.test\nnew-name\n\nKEY\n", ts=TS_UP, env=env)
        self.assertNotIn("can cut it", r.output)

    def test_stale_node_is_caught_after_joining(self):
        with mock.patch.object(pve, "tailscale_state", lambda: {"hostname": "pve-1"}):
            self.assertFalse(pve.node_registered_as("pve"))
        with mock.patch.object(pve, "tailscale_state", lambda: {"hostname": "pve"}):
            self.assertTrue(pve.node_registered_as("pve"))

    def test_join_requires_tailscale_outside_dry_run(self):
        with Env(ts=None) as env:
            r = env.run(["tailscale-join"], "")
        self.assertEqual(r.exit_code, 1)


class Storage(unittest.TestCase):
    ANSWERS = "1\nbk\nbackup\n/mnt/bk\n"

    def test_existing_identical_storage_is_skipped(self):
        with Env(sh={"/storage/bk": {"storage": "bk", "type": "dir", "path": "/mnt/bk", "content": "backup"}}) as env:
            r = env.run(["storage-add"], self.ANSWERS, dry=True)
        self.assertIn("already exists", r.output)
        self.assertIn("Nothing to do", r.output)

    def test_existing_different_storage_is_an_error(self):
        with Env(sh={"/storage/bk": {"storage": "bk", "type": "dir", "path": "/other", "content": "backup"}}) as env:
            r = env.run(["storage-add"], self.ANSWERS, dry=True)
        self.assertEqual(r.exit_code, 1)

    def test_new_storage_has_a_rollback(self):
        with Env() as env:
            r = env.run(["storage-add"], self.ANSWERS + "y\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("pvesm add dir bk --path /mnt/bk --content backup", env.cmds())


class BackupJob(unittest.TestCase):
    def answers(self, scope="1", ids="", keep="keep-daily=7,keep-weekly=4"):
        return f"nightly\n\n03:30\n{scope}\n{ids}\n1\n{keep}\n"

    def test_new_job_has_an_id_and_marker(self):
        with Env(sh={"/cluster/backup": []}) as env:
            r = env.run(["backup-job"], self.answers(), dry=True)
        self.assertIn("--id pve-setup-nightly", r.output)
        self.assertIn("--comment pve-setup", r.output)

    def test_identical_job_is_skipped_regardless_of_prune_order(self):
        with Env(sh={"/cluster/backup": [{**JOB, "storage": "local-lvm"}]}) as env:
            r = env.run(["backup-job"], self.answers(), dry=True)
        self.assertIn("already exists", r.output)
        self.assertIn("Nothing to do", r.output)

    def test_changed_job_is_updated_in_place(self):
        with Env(sh={"/cluster/backup": [{**JOB, "storage": "local-lvm", "schedule": "01:00"}]}) as env:
            r = env.run(["backup-job"], self.answers(), dry=True)
        self.assertIn("pvesh set /cluster/backup/pve-setup-nightly", r.output)
        self.assertNotIn("pvesh create", r.output)

    def test_switching_scope_drops_the_old_selector(self):
        with Env(sh={"/cluster/backup": [{**JOB, "storage": "local-lvm"}]}) as env:
            r = env.run(["backup-job"], self.answers(scope="2", ids="100,101"), dry=True)
        self.assertIn("--vmid 100,101", r.output)
        self.assertIn("--delete all", r.output)


class ApiToken(unittest.TestCase):
    ANSWERS = "auto@pve\nci\n1\n/\n"

    def test_fresh_creates_user_token_and_acl(self):
        with Env(sh={"/access/users": [], "/access/acl": []}) as env:
            r = env.run(["api-token"], self.ANSWERS, dry=True)
        self.assertIn("pveum user add auto@pve", r.output)
        self.assertIn("pveum user token add auto@pve ci", r.output)
        self.assertIn("pveum acl modify / --tokens 'auto@pve!ci'", r.output)

    def test_everything_present_is_a_no_op(self):
        sh = {"/access/users": [{"userid": "auto@pve"}], "/access/users/auto@pve/token": [{"tokenid": "ci"}],
              "/access/acl": [{"ugid": "auto@pve!ci", "path": "/", "roleid": "PVEAuditor"}]}
        with Env(sh=sh) as env:
            r = env.run(["api-token"], self.ANSWERS, dry=True)
        self.assertIn("Nothing to do", r.output)
        self.assertIn("cannot be shown again", r.output)

    def test_half_finished_run_resumes(self):
        sh = {"/access/users": [{"userid": "auto@pve"}], "/access/users/auto@pve/token": [], "/access/acl": []}
        with Env(sh=sh) as env:
            r = env.run(["api-token"], self.ANSWERS, dry=True)
        self.assertNotIn("pveum user add", r.output)
        self.assertIn("pveum user token add", r.output)
        self.assertIn("pveum acl modify", r.output)


class Guests(Isolated):
    ANSWERS = "\n\n{name}\n{dup}\n\n\n\ny\n\ny\n"

    def test_clone_is_tagged_and_unique(self):
        with Env() as env:
            r = env.run(["vm-create"], "\n\nweb\n\n\n\n\ny\n\ny\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("qm clone 100 102 --name web", r.output)
        self.assertIn("--tags pve-setup", r.output)

    def test_duplicate_name_is_refused_by_default(self):
        with Env() as env:
            r = env.run(["vm-create"], "\n\nold\n\n", dry=True)
        self.assertIn("already exists: ID 101", r.output)
        self.assertNotIn("qm clone", r.output)

    def test_bad_name_is_re_asked(self):
        with Env() as env:
            r = env.run(["vm-create"], "\n\nBad_Name\nweb\n\n\n\n\ny\n\ny\n", dry=True)
        self.assertIn("lowercase letters", r.output)

    def test_used_id_is_rejected(self):
        with Env() as env:
            r = env.run(["vm-create"], "\n101\n102\nweb\n\n\n\n\ny\n\ny\n", dry=True)
        self.assertIn("101 is in use", r.output)

    def test_failed_create_offers_the_rollback(self):
        destroy = pve.Cmd(["qm", "destroy", "9", "--purge", "1"])
        plan = pve.Plan("t", [pve.Cmd(["qm", "clone"], undo=destroy, undo_on_fail=True), pve.Cmd(["qm", "set"])])
        ran = []
        outcomes = iter([False])
        with mock.patch.object(pve, "run_step", lambda s: ran.append(s) or next(outcomes, True)), \
                mock.patch.object(pve.typer, "confirm", return_value=True):
            ok = pve.execute(plan)
        self.assertFalse(ok)
        self.assertIn(destroy, ran)
        self.assertEqual(len(ran), 2)  # the failed clone, then its undo; "qm set" never ran

    def test_declined_rollback_runs_nothing_more(self):
        destroy = pve.Cmd(["qm", "destroy", "9"])
        plan = pve.Plan("t", [pve.Cmd(["qm", "clone"], undo=destroy, undo_on_fail=True)])
        ran = []
        with mock.patch.object(pve, "run_step", lambda s: ran.append(s) or False), \
                mock.patch.object(pve.typer, "confirm", side_effect=[True, False]):
            pve.execute(plan)
        self.assertEqual(len(ran), 1)

    def test_sha256_must_be_hex(self):
        with mock.patch("typer.prompt", side_effect=["nothex", "a" * 64]):
            self.assertEqual(pve.ask_sha256(), "a" * 64)

    def test_fetch_checksum_mismatch_fails(self):
        with tempfile.TemporaryDirectory() as d, mock.patch("urllib.request.urlretrieve"), \
                mock.patch.object(pve, "sha256_of", return_value="b" * 64):
            self.assertFalse(pve.run_step(pve.Fetch("https://x.test/i", f"{d}/sub/i", "a" * 64)))


class Status(unittest.TestCase):
    SH = {"/storage": [{"storage": "bk", "type": "dir", "content": "backup"}], "/cluster/backup": [JOB],
          "/access/users": [{"userid": "auto@pve", "comment": pve.USER_COMMENT}, {"userid": "root@pam"}],
          "/access/users/auto@pve/token": [{"tokenid": "ci"}]}

    def test_json_marks_only_what_the_toolkit_created(self):
        with Env(sh=self.SH, ts=TS_UP) as env:
            r = env.run(["status", "--json"])
        data = json.loads(r.output)
        self.assertEqual({g["vmid"]: g["managed"] for g in data["guests"]}, {100: True, 101: False, 110: True})
        self.assertTrue(data["backup_jobs"][0]["managed"])
        self.assertEqual(data["api_users"], [{"userid": "auto@pve", "tokens": ["ci"]}])

    def test_text_view(self):
        with Env(sh=self.SH, ts=TS_UP) as env:
            r = env.run(["status"])
        self.assertIn("* ", r.output)
        self.assertIn("Tailscale: 1.98.10 (held), Running", r.output)
        self.assertIn("auto@pve [ci]", r.output)

    def test_status_with_nothing_installed(self):
        with Env() as env:
            r = env.run(["status"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Tailscale: not installed", r.output)


class Undo(unittest.TestCase):
    def test_vm_destroy_offers_only_toolkit_guests(self):
        with Env() as env:
            r = env.run(["vm-destroy"], "\n110\n", dry=True)
        self.assertIn("110 mine", r.output)
        self.assertNotIn("101 old", r.output)

    def test_wrong_confirmation_runs_nothing(self):
        with Env() as env:
            r = env.run(["vm-destroy"], "2\nwrong\n")
        self.assertEqual(env.steps, [])
        self.assertIn("Cancelled", r.output)

    def test_right_confirmation_stops_then_destroys(self):
        with Env() as env:
            env.run(["vm-destroy"], "2\n110\ny\n")
        self.assertEqual(env.cmds(), ["pct stop 110", "pct destroy 110 --purge 1 --destroy-unreferenced-disks 1"])

    def test_stopped_template_is_destroyed_without_stop(self):
        with Env() as env:
            env.run(["vm-destroy"], "1\n100\ny\n")
        self.assertEqual(env.cmds(), ["qm destroy 100 --purge 1 --destroy-unreferenced-disks 1"])

    def test_nothing_to_destroy(self):
        with Env(sh={"/cluster/resources": [RES[1]]}) as env:
            r = env.run(["vm-destroy"], "")
        self.assertIn("only destroys what it created", r.output)

    def test_backup_job_remove_ignores_foreign_jobs(self):
        with Env(sh={"/cluster/backup": [{"id": "backup-abc", "schedule": "x", "storage": "y"}, JOB]}) as env:
            env.run(["backup-job-remove"], "\npve-setup-nightly\ny\n")
        self.assertEqual(env.cmds(), ["pvesh delete /cluster/backup/pve-setup-nightly"])

    def test_foreign_jobs_only_means_nothing_to_remove(self):
        with Env(sh={"/cluster/backup": [{"id": "backup-abc"}]}) as env:
            r = env.run(["backup-job-remove"], "")
        self.assertIn("only removes what it created", r.output)

    def test_heartbeat_remove(self):
        files = {p: True for p in pve.HEARTBEAT_FILES}
        soft = {"systemctl is-enabled pve-heartbeat.timer": "enabled\n", "systemctl is-active pve-heartbeat.timer": "active\n"}
        with Env(soft=soft) as env, mock.patch.object(pve.os.path, "exists", lambda p: files.get(p, False)):
            env.run(["heartbeat-remove"], "heartbeat\ny\n")
        self.assertEqual(env.cmds(), ["systemctl disable --now pve-heartbeat.timer", "systemctl daemon-reload"])
        self.assertEqual([s.path for s in env.steps if isinstance(s, pve.Remove)], list(pve.HEARTBEAT_FILES))

    def test_token_revoke_and_user_delete(self):
        sh = {"/access/users": [{"userid": "auto@pve", "comment": pve.USER_COMMENT}, {"userid": "root@pam"}],
              "/access/users/auto@pve/token": [{"tokenid": "ci"}]}
        with Env(sh=sh) as env:
            env.run(["api-token-revoke"], "\n2\nauto@pve!ci\ny\n")
        self.assertEqual(env.cmds(), ["pveum user token remove auto@pve ci"])
        with Env(sh=sh) as env:
            env.run(["api-token-revoke"], "\n1\nauto@pve\ny\n")
        self.assertEqual(env.cmds(), ["pveum user delete auto@pve"])

    def test_tailscale_leave_keeps_the_package_by_default(self):
        with Env(ts=TS_UP) as env:
            env.run(["tailscale-leave"], "n\npve\ny\n")
        self.assertEqual(env.cmds(), ["tailscale logout"])


KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKApHPR1HHqSoai6FXgCUwxfeeDChmSuhagQGqcsILm laptop"
KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGOuroREm2DF2lSNSCIX5dUsHdhR2Yk3pPWJ79E45zZR other"
SSHD_T = "sshd -T -f /etc/ssh/sshd_config"
HARD = "passwordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin without-password\nallowagentforwarding no\n"
SOFT = "passwordauthentication yes\nkbdinteractiveauthentication no\npermitrootlogin without-password\nallowagentforwarding no\n"
PROOF = "ssh -o IdentitiesOnly=yes -i <public key file>"
NET = {"tailscale ip -4": "100.64.0.9\n", "hostname -I": "192.168.1.10 100.64.0.9 fd00::1\n", "systemctl cat ssh.service": "[Unit]\n"}


def fake_fp(line):
    return "256 SHA256:FP-" + line.split()[-1] if pve.key_ident(line) else None


class Keys(Isolated):
    def setUp(self):
        super().setUp()
        self.fp = mock.patch.object(pve, "key_fingerprint", fake_fp)
        self.fp.start()

    def tearDown(self):
        self.fp.stop()

    def test_ident_accepts_plain_keys_only(self):
        self.assertEqual(pve.key_ident(KEY)[0], "ssh-ed25519")
        self.assertIsNone(pve.key_ident("hello world"))
        self.assertIsNone(pve.key_ident('command="x" ' + KEY))
        self.assertIsNone(pve.key_ident("# " + KEY))

    def test_paste_dedupes_and_skips_junk(self):
        with Env() as env:
            r = env.run(["ssh-harden"], f"3\n{KEY}\n{KEY}\nnot a key\n{KEY2}\n\ny\ny\n", dry=True)
        self.assertIn("new     256 SHA256:FP-laptop", r.output)
        self.assertEqual(r.output.count("SHA256:FP-laptop"), 2)  # once in the list, once in the per-key prompt
        self.assertEqual(r.output.count("SHA256:FP-other"), 2)
        self.assertIn("skipping a line that is not a plain public key", r.output)
        self.assertIn("append 2 line(s) to /root/.ssh/authorized_keys in place (a symlink is kept)", r.output)

    def test_several_new_keys_are_chosen_one_by_one(self):
        with Env() as env:
            r = env.run(["ssh-harden"], f"3\n{KEY}\n{KEY2}\n\ny\nn\n", dry=True)
        self.assertIn("append 1 line(s)", r.output)
        self.assertIn("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKApHPR1", r.output)
        self.assertNotIn("AAAAIGOuroREm2DF2", r.output.split("== Add authorized keys")[-1])
        with Env() as env:
            r = env.run(["ssh-harden"], f"3\n{KEY}\n{KEY2}\n\nn\nn\n", dry=True)
        self.assertNotIn("append", r.output)

    def test_only_https_urls_and_a_size_cap(self):
        with self.assertRaises(ValueError):
            pve.fetch_key_text("http://github.com/x.keys")
        big = mock.MagicMock()
        big.__enter__.return_value.read.return_value = b"x" * 70000
        with mock.patch.object(pve.urllib.request, "urlopen", return_value=big), self.assertRaises(ValueError):
            pve.fetch_key_text("https://github.com/x.keys")

    def test_fetch_error_stops_cleanly(self):
        with Env() as env, mock.patch.object(pve, "fetch_key_text", side_effect=ValueError("only https:// URLs are fetched")):
            r = env.run(["ssh-harden"], "1\nhttp://x\n", dry=True)
        self.assertEqual(r.exit_code, 1)
        self.assertIn("cannot read the keys", r.output)

    def test_append_keeps_a_symlink_and_adds_a_missing_newline(self):
        d = Path(tempfile.mkdtemp())
        (d / "cluster").write_text("old")
        (d / "ak").symlink_to(d / "cluster")
        pve.append_lines(str(d / "ak"), ["new"])
        self.assertTrue((d / "ak").is_symlink())
        self.assertEqual((d / "cluster").read_text().splitlines(), ["old", "new"])
        pve.append_lines(str(d / "fresh/.ssh/ak"), ["k"])
        self.assertEqual(oct((d / "fresh/.ssh/ak").stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((d / "fresh/.ssh").stat().st_mode & 0o777), "0o700")


class SshHarden(Isolated):
    TEXTS = {pve.AUTHORIZED_KEYS: KEY + "\n"}

    def env(self, eff=SOFT, texts=None, soft=None):
        return Env(texts={**self.TEXTS, **(texts or {})}, soft={SSHD_T: eff, **NET, **(soft or {})})

    def test_effective_parse_accepts_both_spellings(self):
        for spelling in ("without-password", "prohibit-password"):
            eff = {"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "permitrootlogin": spelling, "allowagentforwarding": "no"}
            self.assertTrue(pve.ssh_is_hardened(eff))
            self.assertFalse(pve.ssh_is_hardened({**eff, "allowagentforwarding": "yes"}))
            self.assertFalse(pve.ssh_is_hardened({k: v for k, v in eff.items() if k != "allowagentforwarding"}))
        self.assertFalse(pve.ssh_is_hardened({"passwordauthentication": "yes", "kbdinteractiveauthentication": "no", "permitrootlogin": "without-password", "allowagentforwarding": "no"}))
        self.assertFalse(pve.ssh_is_hardened({"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "permitrootlogin": "yes", "allowagentforwarding": "no"}))
        self.assertFalse(pve.ssh_is_hardened(None))

    def test_plan_shows_proof_commands_dropin_revert_and_checks(self):
        with self.env() as env:
            r = env.run(["ssh-harden"], "\n\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        for text in (f"{PROOF} root@192.168.1.10", f"{PROOF} root@100.64.0.9", "write /etc/ssh/sshd_config.d/10-hardening.conf",
                     "PermitRootLogin prohibit-password", "AllowAgentForwarding no", "Too many authentication", "validate with sshd -t", "systemd-run --unit pve-setup-ssh-revert --on-active=10m",
                     "systemctl reload ssh", "check the effective settings with sshd -T"):
            self.assertIn(text, r.output)

    def test_step_order_arms_the_revert_before_the_reload(self):
        with self.env() as env:
            env.run(["ssh-harden"], "\nproven\ny\ny\n")
        kinds = [type(s).__name__ for s in env.steps]
        self.assertEqual(kinds, ["Write", "Do", "Cmd", "Cmd", "Do"])
        self.assertIn("systemd-run", env.cmds()[0])
        self.assertEqual(env.cmds()[1], "systemctl reload ssh")
        self.assertIsNotNone(env.steps[0].undo)

    def test_declining_the_revert_leaves_it_out(self):
        with self.env() as env:
            env.run(["ssh-harden"], "\nproven\nn\ny\n")
        self.assertEqual(env.cmds(), ["systemctl reload ssh"])

    def test_an_armed_revert_is_not_armed_twice(self):
        with self.env(soft={"systemctl is-active pve-setup-ssh-revert.timer": "active\n"}) as env:
            env.run(["ssh-harden"], "\nproven\ny\n")
        self.assertEqual(env.cmds(), ["systemctl reload ssh"])

    def test_wrong_proof_changes_nothing(self):
        with self.env() as env:
            r = env.run(["ssh-harden"], "\nnope\n")
        self.assertEqual(env.steps, [])
        self.assertIn("Cancelled", r.output)

    def test_already_hardened_is_a_no_op(self):
        with self.env(eff=HARD, texts={pve.SSH_DROPIN: pve.SSH_DROPIN_TEXT}) as env:
            r = env.run(["ssh-harden"], "\nproven\n")
        self.assertIn("already disabled", r.output)
        self.assertEqual(env.steps, [])

    def test_refuses_without_a_key_or_without_the_tailnet(self):
        with self.env(texts={pve.AUTHORIZED_KEYS: ""}) as env, mock.patch.object(pve, "collect_keys", return_value=[]):
            r = env.run(["ssh-harden"], "")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("refusing to disable passwords", r.output)
        with self.env(soft={"tailscale ip -4": None}) as env:
            r = env.run(["ssh-harden"], "\n")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("not on the tailnet", r.output)

    def test_new_key_is_appended_before_the_proof(self):
        with self.env(texts={pve.AUTHORIZED_KEYS: ""}) as env, mock.patch.object(pve, "key_fingerprint", fake_fp):
            env.run(["ssh-harden"], f"3\n{KEY}\n\ny\nproven\nn\ny\n")
        self.assertIsInstance(env.steps[0], pve.Append)
        self.assertEqual(env.steps[0].lines, [KEY])

    def test_failed_verification_offers_to_remove_the_dropin(self):
        undo = pve.Do("remove the drop-in and reload sshd", lambda: True)
        ran = []
        done = [pve.Write(pve.SSH_DROPIN, "x", undo=undo), pve.Do("validate", lambda: True)]
        with mock.patch.object(pve, "run_step", lambda s: ran.append(s) or True), mock.patch.object(pve.typer, "confirm", return_value=True):
            pve.offer_rollback(done, pve.Do("verify", lambda: False))
        self.assertEqual(ran, [undo])

    def test_validate_removes_the_dropin_when_sshd_rejects_it(self):
        d = Path(tempfile.mkdtemp())
        with mock.patch.object(pve, "SSH_DROPIN", str(d / "10-hardening.conf")):
            (d / "10-hardening.conf").write_text("x")
            with mock.patch.object(pve.subprocess, "run", return_value=mock.Mock(returncode=255)):
                self.assertFalse(pve.validate_or_remove())
            self.assertFalse((d / "10-hardening.conf").exists())
            (d / "10-hardening.conf").write_text("x")
            with mock.patch.object(pve.subprocess, "run", return_value=mock.Mock(returncode=0)):
                self.assertTrue(pve.validate_or_remove())
            self.assertTrue((d / "10-hardening.conf").exists())

    def test_verify_names_the_earlier_dropin_that_wins(self):
        d = Path(tempfile.mkdtemp())
        (d / "00-cloud.conf").write_text("PasswordAuthentication yes\n")
        (d / "99-late.conf").write_text("")
        with mock.patch.object(pve, "SSH_DROPIN", str(d / "10-hardening.conf")), mock.patch.object(pve, "sshd_effective", lambda: {"passwordauthentication": "yes"}), \
                mock.patch.object(pve, "fail") as f:
            self.assertFalse(pve.verify_effective())
        self.assertIn("00-cloud.conf", f.call_args[0][0])
        self.assertNotIn("99-late.conf", f.call_args[0][0])

    def test_keep_cancels_the_revert(self):
        with self.env(soft={"systemctl is-active pve-setup-ssh-revert.timer": "active\n"}) as env:
            env.run(["ssh-harden-keep"], "proven\ny\n")
        self.assertEqual(env.cmds(), ["systemctl stop pve-setup-ssh-revert.timer"])
        with self.env() as env:
            r = env.run(["ssh-harden-keep"], "")
        self.assertIn("No automatic revert is armed", r.output)

    def test_remove_undoes_the_dropin(self):
        with self.env(texts={pve.SSH_DROPIN: pve.SSH_DROPIN_TEXT}, soft={"systemctl is-active pve-setup-ssh-revert.timer": "active\n"}) as env:
            env.run(["ssh-harden-remove"], "ssh\ny\n")
        self.assertEqual([s.path for s in env.steps if isinstance(s, pve.Remove)], [pve.SSH_DROPIN])
        self.assertEqual(env.cmds(), ["systemctl reload ssh", "systemctl stop pve-setup-ssh-revert.timer"])
        with self.env() as env:
            self.assertIn("not installed", env.run(["ssh-harden-remove"], "").output)

    def test_service_name_falls_back_to_sshd(self):
        with Env(soft={}):
            self.assertEqual(pve.ssh_service(), "sshd")

    def test_status_reports_ssh(self):
        with self.env(eff=HARD) as env:
            self.assertIn("SSH: password logins off", env.run(["status"]).output)
        with self.env() as env:
            self.assertIn("password logins NOT disabled", env.run(["status"]).output)
        with Env() as env:
            self.assertIn("unknown", env.run(["status"]).output)
        with self.env(eff=HARD, soft={"systemctl is-active pve-setup-ssh-revert.timer": "active\n"}) as env:
            self.assertIn("ssh-harden-keep", env.run(["status"]).output)


SS = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 4096 *:8006 *:*\nLISTEN 0 4096 [::]:3128 [::]:*\n"
TS_PVE = {**TS_UP, "hostname": "pve", "tags": ["tag:pve"]}
BOOT = "proxmox-boot-tool kernel list"


class Gate(Isolated):
    def setUp(self):
        super().setUp()
        d = Path(tempfile.mkdtemp())
        pve.LOG = d / "pve-setup.log"
        self.boot = "boot-1"
        self.patches = [mock.patch.object(pve, "pinned_kernel", lambda: "6.17.2-1-pve"), mock.patch.object(pve, "running_kernel", lambda: "6.17.2-1-pve"),
                        mock.patch.object(pve, "zfs_arc_max", lambda: pve.ARC_MAX_BYTES), mock.patch.object(pve, "boot_id", lambda: self.boot)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        pve.LOG = Path(tempfile.mkdtemp()) / "pve-setup.log"

    def env(self, **kw):
        soft = {SSHD_T: HARD, "ss -ltnH": SS, **NET, **kw.pop("soft", {})}
        return Env(texts={pve.AUTHORIZED_KEYS: kw.pop("keys", KEY + "\n" + KEY2 + "\n")}, soft=soft, ts=kw.pop("ts", TS_PVE))

    def test_all_host_checks_pass_and_are_recorded(self):
        with self.env() as env:
            r = env.run(["remote-gate"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(r.output.count("PASS"), 7)
        self.assertNotIn("FAIL", r.output)
        self.assertIn("Passing boots recorded: 1", r.output)
        self.assertIn("off the LAN", r.output)
        self.assertEqual(json.loads(pve.gate_path().read_text())["passing_boots"][0]["boot_id"], "boot-1")
        self.assertEqual(oct(pve.gate_path().stat().st_mode & 0o777), "0o600")

    def test_a_second_boot_proves_a_clean_reboot(self):
        with self.env() as env:
            env.run(["remote-gate"])
            env.run(["remote-gate"])
            self.assertIn("Passing boots recorded: 1", env.run(["remote-gate"]).output)
            self.boot = "boot-2"
            r = env.run(["remote-gate"])
        self.assertIn("Passing boots recorded: 2. Clean reboot with a passing gate afterwards: yes", r.output)

    def test_each_failure_is_reported_and_exits_nonzero(self):
        cases = {
            "password SSH": dict(soft={SSHD_T: SOFT}),
            "web UI listening": dict(soft={"ss -ltnH": "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"}),
            "tailscale up as pve": dict(ts={**TS_PVE, "hostname": "pve-1"}),
            "at least two authorized keys": dict(keys=KEY + "\n"),
        }
        for name, kw in cases.items():
            with self.subTest(name), self.env(**kw) as env:
                r = env.run(["remote-gate"])
            self.assertEqual(r.exit_code, 1, r.output)
            self.assertRegex(r.output, rf"FAIL  .*{name}")
        for attr, value, label in (("running_kernel", lambda: "6.8.0", "running kernel"), ("zfs_arc_max", lambda: 0, "ARC cap"), ("pinned_kernel", lambda: None, "running kernel")):
            with self.subTest(label), mock.patch.object(pve, attr, value), self.env() as env:
                r = env.run(["remote-gate"])
            self.assertRegex(r.output, rf"FAIL  .*{label}")

    def test_forwarding_is_part_of_the_ssh_check(self):
        with self.env(soft={SSHD_T: HARD.replace("allowagentforwarding no", "allowagentforwarding yes")}) as env:
            r = env.run(["remote-gate"])
        self.assertRegex(r.output, r"FAIL  .*password SSH")
        self.assertIn("allowagentforwarding=yes", r.output)

    def test_the_manual_checklist_covers_the_agent_and_the_phone(self):
        with self.env() as env:
            r = env.run(["remote-gate"])
        self.assertIn("1Password SSH agent exists on desktops only", r.output)
        self.assertIn("hotspot", r.output)

    def test_failing_gate_records_nothing(self):
        with self.env(soft={SSHD_T: SOFT}) as env:
            env.run(["remote-gate"])
        self.assertFalse(pve.gate_path().exists())

    def test_dry_run_records_nothing(self):
        with self.env() as env:
            env.run(["remote-gate"], dry=True)
        self.assertFalse(pve.gate_path().exists())

    def test_soak_in_progress(self):
        with self.env() as env:
            r = env.run(["remote-gate"])
        self.assertIn("0 h of the 48 h soak so far; run this again after it.", r.output)

    def test_soak_completion(self):
        old = (pve.datetime.now(pve.timezone.utc) - timedelta(hours=50)).isoformat(timespec="seconds")
        pve.gate_path().write_text(json.dumps({"passing_boots": [{"boot_id": "boot-0", "at": old}], "first_pass_at": old}))
        with self.env() as env:
            r = env.run(["remote-gate"])
        self.assertIn("The 48 h soak is complete and the gate passes now.", r.output)

    def test_reboot_is_refused_while_the_gate_fails(self):
        with self.env(soft={SSHD_T: SOFT}) as env:
            r = env.run(["remote-gate", "--reboot"], "reboot\ny\n")
        self.assertEqual(r.exit_code, 1)
        self.assertEqual(env.steps, [])
        self.assertIn("must pass before a reboot test", r.output)

    def test_reboot_needs_the_typed_word(self):
        with self.env() as env:
            env.run(["remote-gate", "--reboot"], "nope\n")
        self.assertEqual(env.steps, [])
        with self.env() as env:
            env.run(["remote-gate", "--reboot"], "reboot\ny\n")
        self.assertEqual(env.cmds(), ["systemctl reboot"])

    def test_pinned_kernel_parsing(self):
        out = "Manually selected kernels:\nNone.\n\nAutomatically selected kernels:\n6.17.2-1-pve\n\nPinned kernel:\n6.17.2-1-pve\n"
        none = "Manually selected kernels:\nNone.\n\nPinned kernel:\nNone.\n"
        for text, want in ((out, "6.17.2-1-pve"), (none, None), ("", None)):
            with mock.patch.object(pve, "soft", lambda argv, t=text: t or None):
                self.assertEqual(REAL_PINNED_KERNEL(), want)

    def test_listening_ports(self):
        with mock.patch.object(pve, "soft", lambda argv: SS):
            self.assertEqual(pve.listening_ports(), {22, 8006, 3128})


class ConfigArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        (t / "etc/pve/priv").mkdir(parents=True)
        (t / "etc/pve/nodes/pve").mkdir(parents=True)
        (t / "etc/pve/.version").write_text("x")
        (t / "etc/pve/storage.cfg").write_text("dir: bk\n")
        (t / "etc/pve/priv/token.cfg").write_text("SECRET-TOKEN")
        (t / "etc/pve/nodes/pve/pve-ssl.key").write_text("SECRET-KEY")
        (t / "etc/pve/nodes/pve/pve-ssl.pem").write_text("cert")
        os.symlink(t / "etc/pve/storage.cfg", t / "etc/pve/link.cfg")
        (t / "hosts").write_text("127.0.0.1 localhost\n")
        (t / "hb.timer").write_text("[Timer]\n")
        (t / "hb.curl").write_text("header = x\n")
        self.patches = [mock.patch.object(pve, "OWNED_FILES", [str(t / "hb.timer")]), mock.patch.object(pve, "HOST_FILES", [str(t / "hosts")]),
                        mock.patch.object(pve, "HOST_DIRS", [(str(t / "etc/pve"), True)]),
                        mock.patch.object(pve, "SECRET_FILES", [str(t / "hb.curl")]), mock.patch.object(pve, "SECRET_DIRS", []),
                        mock.patch.object(pve, "pvesh_soft", lambda path, *a: {"/cluster/resources": RES}.get(path)),
                        mock.patch.object(pve, "soft", lambda argv: None), mock.patch.object(pve, "node", lambda: "pve"),
                        mock.patch.object(pve, "api_users", lambda: [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def names(self, secrets_):
        return sorted(os.path.relpath(p, self.tmp) for p, _ in pve.collect_backup_files(secrets_))

    def test_secrets_are_excluded_by_default(self):
        self.assertEqual(self.names(False), ["etc/pve/nodes/pve/pve-ssl.pem", "etc/pve/storage.cfg", "hb.timer", "hosts"])

    def test_secrets_are_included_on_request(self):
        got = self.names(True)
        self.assertIn("etc/pve/priv/token.cfg", got)
        self.assertIn("etc/pve/nodes/pve/pve-ssl.key", got)
        self.assertIn("hb.curl", got)
        self.assertNotIn("etc/pve/.version", got)
        self.assertNotIn("etc/pve/link.cfg", got)

    def test_round_trip_is_verified_and_private(self):
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, False)
        self.assertEqual(oct(os.stat(dest).st_mode & 0o777), "0o600")
        manifest, files, inv = pve.read_config_archive(dest)
        self.assertFalse(manifest["includes_secrets"])
        self.assertEqual(files[str(self.tmp / "hosts")], b"127.0.0.1 localhost\n")
        self.assertEqual({r["vmid"] for r in inv["resources"]}, {100, 101, 110})
        self.assertNotIn(b"SECRET", b"".join(files.values()))

    def _rewrite(self, dest, mutate):
        out = dest + ".new"
        with tarfile.open(dest) as src, tarfile.open(out, "w:gz") as dst:
            for m in src:
                data = src.extractfile(m).read()
                m2, data2 = mutate(m, data)
                m2.size = len(data2)
                dst.addfile(m2, io.BytesIO(data2))
        return out

    def test_tampered_file_is_rejected(self):
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, False)
        bad = self._rewrite(dest, lambda m, d: (m, b"evil" if m.name.endswith("/hosts") else d))
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            pve.read_config_archive(bad)

    def test_path_traversal_member_is_rejected(self):
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, False)

        def mutate(m, d):
            if m.name.endswith("/hosts"):
                m.name = "files/../../etc/cron.d/evil"
            return m, d
        with self.assertRaisesRegex(ValueError, "unsafe"):
            pve.read_config_archive(self._rewrite(dest, mutate))

    def test_symlink_member_is_rejected(self):
        dest = str(self.tmp / "out.tar.gz")
        with tarfile.open(dest, "w:gz") as t:
            info = tarfile.TarInfo("files/etc/x")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/shadow"
            t.addfile(info)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            pve.read_config_archive(dest)

    def test_backup_command_writes_the_archive(self):
        folder = self.tmp / "bk"
        steps = []
        with mock.patch.object(pve, "run_step", lambda s: steps.append(s) or s.fn()), mock.patch.object(pve.os, "geteuid", lambda: 0):
            r = runner.invoke(pve.app, ["config-backup"], input=f"{folder}\n\ny\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("without config.db or secrets", r.output)
        written = list(folder.glob("pve-config-pve-*.tar.gz")) if folder.exists() else []
        self.assertEqual(len(written), 1, r.output)

    def restore(self, dest, extra="", stage=None, dry=True):
        args = (["--dry-run"] if dry else []) + ["config-restore", dest] + (["--stage", stage] if stage else []) + ([extra] if extra else [])
        with mock.patch.object(pve.os, "geteuid", lambda: 0):
            return runner.invoke(pve.app, args, input="y\n")

    def test_restore_plan_reports_state_and_stages_without_touching_live_files(self):
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, False)
        (self.tmp / "hosts").write_text("changed\n")
        (self.tmp / "hb.timer").unlink()
        stage = str(self.tmp / "stage")
        r = self.restore(dest, stage=stage, dry=False)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("2 files unchanged, 1 differ, 1 missing", r.output)
        self.assertIn("[owned ] missing", r.output)
        self.assertIn("install -D -m 0644", r.output)
        self.assertEqual((self.tmp / "hosts").read_text(), "changed\n")
        self.assertFalse((self.tmp / "hb.timer").exists())
        self.assertEqual(oct(os.stat(stage).st_mode & 0o777), "0o700")
        self.assertEqual((Path(stage) / ("files" + str(self.tmp / "hosts"))).read_text(), "127.0.0.1 localhost\n")

    def test_dry_run_stages_nothing(self):
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, False)
        stage = str(self.tmp / "stage")
        r = self.restore(dest, stage=stage, dry=True)
        self.assertFalse(Path(stage).exists())

    def test_apply_owned_restores_only_the_toolkit_files(self):
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, False)
        (self.tmp / "hosts").write_text("changed\n")
        (self.tmp / "hb.timer").write_text("tampered\n")
        written = []
        with mock.patch.object(pve, "run_step", lambda s: written.append(s) or True), mock.patch.object(pve.os, "geteuid", lambda: 0):
            runner.invoke(pve.app, ["config-restore", dest, "--apply-owned"], input="y\n")
        self.assertEqual([s.path for s in written if isinstance(s, pve.Write)], [str(self.tmp / "hb.timer")])
        self.assertIn(["systemctl", "daemon-reload"], [s.argv for s in written if isinstance(s, pve.Cmd)])

    def test_corrupt_archive_is_refused(self):
        bad = self.tmp / "bad.tar.gz"
        bad.write_bytes(b"not a tar")
        r = self.restore(str(bad))
        self.assertEqual(r.exit_code, 1)


ROOT_DS = "rpool/ROOT/pve-1"
ZFS_SOFT = {"findmnt -no SOURCE /": ROOT_DS + "\n", f"zfs list -H -o name {ROOT_DS}": ROOT_DS + "\n"}
SIM = """Inst base-files [13.8] (13.8+1 Debian:13/stable [amd64])
Inst proxmox-kernel-6.14.11-2-pve-signed (6.14.11-2 Proxmox:8/stable [amd64])
Inst htop (3.4.1-1 Debian:13/stable [amd64])
Conf base-files (13.8+1 Debian:13/stable [amd64])
"""


class Snapshots(Isolated):
    def test_snapshot_names_the_root_dataset_and_carries_its_own_undo(self):
        with Env(soft=ZFS_SOFT) as env:
            r = env.run(["snapshot"], "upgrade\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertRegex(r.output, rf"zfs snapshot {ROOT_DS}@pve-setup-upgrade-\d{{8}}-\d{{6}}")
        self.assertIn("rescue environment", r.output)

    def test_snapshot_refuses_a_non_zfs_root(self):
        with Env() as env:
            r = env.run(["snapshot"], "x\n")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("not ZFS", r.output)

    def test_only_toolkit_snapshots_are_listed_oldest_first(self):
        listing = f"{ROOT_DS}@pve-setup-a-1\t100\n{ROOT_DS}@manual\t150\n{ROOT_DS}@pve-setup-b-2\t200\n"
        with Env(soft={**ZFS_SOFT, f"zfs list -H -p -t snapshot -o name,creation -s creation -d 1 {ROOT_DS}": listing}):
            self.assertEqual(pve.toolkit_snapshots(), [(f"{ROOT_DS}@pve-setup-a-1", 100), (f"{ROOT_DS}@pve-setup-b-2", 200)])

    def test_prune_keeps_the_newest_and_needs_the_typed_word(self):
        listing = "".join(f"{ROOT_DS}@pve-setup-x-{i}\t{i}\n" for i in range(1, 5))
        soft = {**ZFS_SOFT, f"zfs list -H -p -t snapshot -o name,creation -s creation -d 1 {ROOT_DS}": listing}
        with Env(soft=soft) as env:
            r = env.run(["snapshot-prune"], "2\nprune\ny\n")
            self.assertEqual(env.cmds(), [f"zfs destroy {ROOT_DS}@pve-setup-x-1", f"zfs destroy {ROOT_DS}@pve-setup-x-2"])
        with Env(soft=soft) as env:
            env.run(["snapshot-prune"], "2\nno\n")
            self.assertEqual(env.cmds(), [])


class PoolReserve(Isolated):
    def plan(self, soft, cron):
        with Env(soft={**ZFS_SOFT, **soft}) as env, mock.patch.object(pve.os.path, "exists", lambda p: cron if p == "/etc/cron.d/zfsutils-linux" else os.path.lexists(p)):
            return env.run(["zfs-reserve"], "40G\n", dry=True)

    def test_fresh_pool_gets_autotrim_a_reserve_and_a_scrub_timer(self):
        r = self.plan({"zpool get -H -o value autotrim rpool": "off\n"}, cron=False)
        self.assertIn("zpool set autotrim=on rpool", r.output)
        self.assertIn("zfs create -o refreservation=40G -o mountpoint=none rpool/reserved", r.output)
        self.assertIn("zfs-scrub-monthly@rpool.timer", r.output)

    def test_configured_pool_is_a_no_op(self):
        soft = {"zpool get -H -o value autotrim rpool": "on\n", "zfs list -H -o name rpool/reserved": "rpool/reserved\n",
                f"zfs get -Hp -o value refreservation rpool/reserved": str(40 * 2**30) + "\n"}
        self.assertIn("Nothing to do", self.plan(soft, cron=True).output)

    def test_a_different_size_is_resized_in_place(self):
        soft = {"zpool get -H -o value autotrim rpool": "on\n", "zfs list -H -o name rpool/reserved": "rpool/reserved\n",
                "zfs get -Hp -o value refreservation rpool/reserved": str(10 * 2**30) + "\n"}
        r = self.plan(soft, cron=True)
        self.assertIn("zfs set refreservation=40G rpool/reserved", r.output)
        self.assertNotIn("zfs create", r.output)

    def test_release_needs_the_typed_word_and_says_how_much(self):
        soft = {**ZFS_SOFT, "zfs get -Hp -o value refreservation rpool/reserved": str(40 * 2**30) + "\n"}
        with Env(soft=soft) as env:
            r = env.run(["zfs-reserve-release"], "release\ny\n")
            self.assertEqual(env.cmds(), ["zfs set refreservation=none rpool/reserved"])
        self.assertIn("40 GiB", r.output)


class Firewall(Isolated):
    def setUp(self):
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        self.patches = [mock.patch.object(pve, "FW_FILE", str(t / "cluster.fw")), mock.patch.object(pve, "FW_SAVE_DIR", str(t / "save")),
                        mock.patch.object(pve, "FW_BEFORE", str(t / "save/before")), mock.patch.object(pve, "FW_ABSENT", str(t / "save/absent")),
                        mock.patch.object(pve, "FW_WRITTEN", str(t / "save/written")),
                        mock.patch.dict(os.environ, {"SSH_CONNECTION": "100.64.0.9 5555 100.64.0.2 22"})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_rules_text_allows_tailnet_and_lan_and_drops_the_rest(self):
        text = pve.firewall_text("192.168.1.0/24", "22,8006")
        self.assertIn("policy_in: DROP", text)
        self.assertIn("IN ACCEPT -source 100.64.0.0/10 -p tcp -dport 22,8006", text)
        self.assertIn("IN ACCEPT -source 192.168.1.0/24 -p tcp -dport 22,8006", text)
        self.assertTrue(pve.fw_rules_ok(text))
        self.assertFalse(pve.fw_rules_ok(text.replace("enable: 1", "enable: 0")))
        self.assertFalse(pve.fw_rules_ok(None))

    def test_lan_default_skips_the_tailnet_and_docker_links(self):
        routes = ("100.64.0.0/10 dev tailscale0 scope link\n172.17.0.0/16 dev docker0 scope link\n192.168.7.0/24 dev vmbr0 proto kernel scope link src 192.168.7.2\n")
        with Env(soft={"ip -4 -o route show scope link": routes}):
            self.assertEqual(pve.lan_cidr_default(), "192.168.7.0/24")

    def test_session_check(self):
        for conn, expected in (("100.64.0.9 1 2 22", True), ("192.168.1.50 1 2 22", True), ("203.0.113.5 1 2 22", False), ("", True)):
            with mock.patch.dict(os.environ, {"SSH_CONNECTION": conn}):
                self.assertEqual(pve.session_allowed("192.168.1.0/24"), expected, conn)

    def test_plan_snapshots_arms_the_timer_writes_in_place_and_validates(self):
        with Env(soft=ZFS_SOFT) as env:
            r = env.run(["firewall"], "192.168.1.0/24\n\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        out = r.output
        order = [out.index(x) for x in ("zfs snapshot", "save the current rules", "systemd-run --unit pve-setup-fw-revert --on-active=10m", "write ", "pve-firewall compile",
                                        "$ pve-firewall restart", "enabled/running")]
        self.assertEqual(order, sorted(order), out)

    def test_foreign_session_needs_the_lockout_word(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "203.0.113.5 1 2 22"}), Env(soft=ZFS_SOFT) as env:
            r = env.run(["firewall"], "192.168.1.0/24\n\nnope\n")
            self.assertEqual(env.steps, [])
        self.assertIn("would cut it", r.output)
        self.assertIn("Cancelled", r.output)
        self.assertNotIn("== Enable the firewall", r.output)

    def test_already_enforced_is_a_no_op(self):
        text = pve.firewall_text("192.168.1.0/24", "22,8006")
        with Env(soft={**ZFS_SOFT, "pve-firewall status": "Status: enabled/running\n"}, texts={pve.FW_FILE: text}) as env:
            r = env.run(["firewall"], "192.168.1.0/24\n\n", dry=True)
        self.assertIn("already has exactly these rules", r.output)

    def test_ports_are_validated(self):
        with Env() as env:
            r = env.run(["firewall"], "192.168.1.0/24\n22;reboot\n70000\n22,8006\n", dry=True)
        self.assertEqual(r.output.count("comma-separated port numbers"), 2)

    def test_the_first_saved_copy_survives_later_runs(self):
        Path(pve.FW_FILE).write_text("original\n")
        pve.save_firewall("new-1")
        pve.remember_written("new-1")
        Path(pve.FW_FILE).write_text("new-1")
        pve.save_firewall("new-2")  # our own earlier write: the saved original must stay
        self.assertEqual(Path(pve.FW_BEFORE).read_text(), "original\n")
        Path(pve.FW_FILE).write_text("edited by hand\n")
        pve.save_firewall("new-3")  # a human changed it since: that is the new original
        self.assertEqual(Path(pve.FW_BEFORE).read_text(), "edited by hand\n")

    def test_restore_puts_back_the_text_in_place_or_removes_a_file_that_was_absent(self):
        Path(pve.FW_FILE).write_text("original\n")
        pve.save_firewall("x")
        Path(pve.FW_FILE).write_text("toolkit\n")
        inode = os.stat(pve.FW_FILE).st_ino
        self.assertTrue(pve.restore_firewall(restart=False))
        self.assertEqual(Path(pve.FW_FILE).read_text(), "original\n")
        self.assertEqual(os.stat(pve.FW_FILE).st_ino, inode)  # pmxcfs: truncate and write, never rename
        os.unlink(pve.FW_FILE)
        os.unlink(pve.FW_BEFORE)
        Path(pve.FW_ABSENT).write_text("")
        Path(pve.FW_FILE).write_text("toolkit\n")
        self.assertTrue(pve.restore_firewall(restart=False))
        self.assertFalse(os.path.exists(pve.FW_FILE))

    def test_restore_without_a_saved_copy_fails_loudly(self):
        self.assertFalse(pve.restore_firewall(restart=False))

    def test_keep_and_remove_need_their_words(self):
        with Env(soft={"systemctl is-active pve-setup-fw-revert.timer": "active\n"}) as env:
            env.run(["firewall-keep"], "nope\n")
            self.assertEqual(env.cmds(), [])
            env.run(["firewall-keep"], "proven\ny\n")
            self.assertEqual(env.cmds(), ["systemctl stop pve-setup-fw-revert.timer"])
        with Env() as env:
            self.assertIn("No automatic firewall revert", env.run(["firewall-keep"], "").output)
            self.assertIn("nothing this toolkit can put back", env.run(["firewall-remove"], "").output)


class Update(Isolated):
    def test_parse_and_classify(self):
        rows = pve.parse_simulation(SIM)
        self.assertEqual([r["name"] for r in rows], ["base-files", "proxmox-kernel-6.14.11-2-pve-signed", "htop"])
        self.assertEqual(rows[0]["old"], "13.8")
        self.assertIsNone(rows[2]["old"])
        self.assertTrue(pve.is_kernel_package(rows[1]["name"]))
        self.assertFalse(pve.is_kernel_package("proxmox-kernel-helper"))
        self.assertEqual(pve.major_of("1:8.4.1"), 8)
        bump = [{"name": "pve-manager", "old": "8.4.1", "new": "9.0.2"}, {"name": "pve-manager", "old": None, "new": "9.0.2"},
                {"name": "proxmox-ve", "old": "8.4.0", "new": "8.4.1"}]
        self.assertEqual(pve.major_bumps(bump), bump[:1])

    def run_update(self, sim, text="", held="", dry=True):
        soft = {**ZFS_SOFT, "apt -s full-upgrade": sim, "apt-mark showhold": held}
        with Env(soft=soft) as env:
            r = env.run(["update"], text, dry=dry)
            self.last_cmds = env.cmds()
        return r

    def test_nothing_to_upgrade(self):
        r = self.run_update("", held="tailscale\n")
        self.assertIn("Nothing to upgrade", r.output)
        self.assertIn("Held back on purpose: tailscale", r.output)

    def test_a_major_proxmox_jump_is_refused(self):
        r = self.run_update("Inst pve-manager [8.4.1] (9.0.2 Proxmox:9/stable [amd64])\n")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("do not attempt that remotely", r.output)

    def test_a_kernel_needs_the_typed_word_and_cancelling_changes_nothing(self):
        r = self.run_update(SIM, "y\nno\n", dry=False)
        self.assertIn("Cancelled. Nothing was changed.", r.output)
        self.assertEqual(self.last_cmds, ["apt update"])

    def test_plan_snapshots_first_then_upgrades(self):
        r = self.run_update(SIM, "kernel\n")
        self.assertLess(r.output.index("zfs snapshot"), r.output.index("apt full-upgrade"))
        self.assertIn("3 package(s) would change", r.output)
        self.assertNotIn("\\n", r.output)

    def test_kernel_pin_steps_include_the_refresh_the_proxmox_docs_require(self):
        self.assertEqual(pve.UPDATE_NOTE.count("proxmox-boot-tool refresh"), 2)

    def test_a_failed_simulation_stops(self):
        with Env(soft=ZFS_SOFT) as env:
            r = env.run(["update"], "", dry=True)
        self.assertEqual(r.exit_code, 1)


class HeartbeatScript(unittest.TestCase):
    """Runs the generated script under bash with fake zpool, smartctl and curl."""

    def run_script(self, zpool_x="all pools are healthy", smart="PASSED", cap="40%"):
        d = Path(tempfile.mkdtemp())
        bindir = d / "bin"
        bindir.mkdir()
        log = d / "curl.log"
        (bindir / "zpool").write_text(f'#!/bin/sh\nif [ "$1" = status ]; then echo "{zpool_x}"; else echo "{cap}"; fi\n')
        (bindir / "smartctl").write_text(f'#!/bin/sh\necho "SMART overall-health self-assessment test result: {smart}"\n')
        (bindir / "curl").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > {log}\n')
        for f in bindir.iterdir():
            f.chmod(0o755)
        env_file, curl_file = d / "hb.env", d / "hb.curl"
        env_file.write_text("STATUS_HOST=status.example.test\nENDPOINT=home_proxmox-heartbeat\nPOOL=rpool\nNVME=/dev/nvme0\n")
        script = d / "hb.sh"
        script.write_text(pve.HEARTBEAT_SCRIPT_TEXT.replace("/etc/pve-heartbeat.env", str(env_file)).replace("/etc/pve-heartbeat.curl", str(curl_file)))
        r = subprocess.run(["bash", str(script)], env={"PATH": f"{bindir}:/usr/bin:/bin"}, capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return log.read_text().split("\n")

    def test_healthy_host_pushes_success(self):
        argv = self.run_script()
        self.assertIn("success=true", argv)
        self.assertNotIn("error=", " ".join(argv))
        self.assertIn("https://status.example.test/api/v1/endpoints/home_proxmox-heartbeat/external", argv)
        self.assertNotIn("Bearer", " ".join(argv))  # the token only lives in the -K file

    def test_each_failed_check_is_named(self):
        self.assertIn("error=zpool", self.run_script(zpool_x="pool 'rpool' is DEGRADED"))
        self.assertIn("error=smart", self.run_script(smart="FAILED!"))
        self.assertIn("error=pool-full", self.run_script(cap="91%"))
        self.assertIn("success=false", self.run_script(cap="85%"))

    def test_several_failures_are_listed_together(self):
        self.assertIn("error=zpool smart", self.run_script(zpool_x="bad", smart="FAILED!"))

    def test_an_unreadable_pool_is_a_failure_not_a_pass(self):
        self.assertIn("error=pool-unreadable", self.run_script(cap=""))


class HeartbeatMigration(Isolated):
    def test_the_old_edge_heartbeat_is_removed_after_the_new_one_is_planned(self):
        old = [p for p in pve.OLD_HEARTBEAT_FILES]
        with Env(soft={"systemctl is-active edge-heartbeat.timer": "active\n"}) as env, mock.patch.object(pve.os.path, "exists", lambda p: p in old):
            r = env.run(["heartbeat"], Heartbeat.ANSWERS, dry=True)
        self.assertIn("earlier edge-heartbeat timer is replaced", r.output)
        self.assertIn("systemctl disable --now edge-heartbeat.timer", r.output)
        self.assertLess(r.output.index("systemctl start pve-heartbeat.service"), r.output.index("systemctl disable --now edge-heartbeat.timer"))
        for p in old:
            self.assertIn(f"remove {p}", r.output)

    def test_nothing_legacy_means_nothing_removed(self):
        with Env() as env:
            r = env.run(["heartbeat"], Heartbeat.ANSWERS, dry=True)
        self.assertNotIn("edge-heartbeat", r.output)


class HostConfigCheck(Isolated):
    GOOD = {**ZFS_SOFT, "zpool get -H -o value autotrim rpool": "on\n", "zfs get -Hp -o value refreservation rpool/reserved": str(40 * 2**30) + "\n",
            "pve-firewall status": "Status: enabled/running\n", "pveum user tfa list root@pam": "totp\n",
            "systemctl is-enabled pve-heartbeat.timer": "enabled\n", "systemctl is-active pve-heartbeat.timer": "active\n",
            f"zfs list -H -p -t snapshot -o name,creation -s creation -d 1 {ROOT_DS}": f"{ROOT_DS}@pve-setup-a-1\t100\n"}

    def run_check(self, soft=None, texts=None, backup=("pve-config-pve-x.tar.gz.age", 0.0), cron=True):
        text = {pve.FW_FILE: pve.firewall_text("192.168.1.0/24", "22,8006"), **(texts or {})}
        recent = (pve.datetime.now(pve.timezone.utc).timestamp() - 86400) if backup else None
        with Env(soft={**self.GOOD, **(soft or {})}, texts=text) as env, \
                mock.patch.object(pve, "latest_backup", lambda f: (backup[0], recent) if backup else None), \
                mock.patch.object(pve, "revert_armed", lambda: False), mock.patch.object(pve, "fw_revert_armed", lambda: False), \
                mock.patch.object(pve, "scrub_scheduled", lambda pool: cron), mock.patch.object(pve.os.path, "exists", lambda p: p == pve.HB_SCRIPT or os.path.lexists(p)):
            return env.run(["host-config-check"])

    def test_all_good_exits_zero(self):
        r = self.run_check()
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(r.output.count("FAIL"), 0, r.output)

    def test_each_gap_is_reported_and_the_exit_code_is_nonzero(self):
        r = self.run_check(soft={"pveum user tfa list root@pam": "", "pve-firewall status": "Status: disabled/stopped\n",
                                 "zpool get -H -o value autotrim rpool": "off\n"}, backup=None, cron=False)
        self.assertEqual(r.exit_code, 1)
        for name in ("two-factor", "firewall is enabled", "autotrim", "monthly scrub", "config backup"):
            self.assertRegex(r.output, rf"FAIL  .*{name}", r.output)

    def test_a_pending_revert_is_a_failure(self):
        with mock.patch.object(pve, "fw_revert_armed", lambda: True):
            checks = dict((n, ok) for n, ok, _ in self._checks())
        self.assertFalse(checks["no unconfirmed automatic revert is armed"])

    def _checks(self):
        with Env(soft=self.GOOD, texts={pve.FW_FILE: pve.firewall_text("192.168.1.0/24", "22,8006")}), \
                mock.patch.object(pve, "revert_armed", lambda: False), mock.patch.object(pve, "latest_backup", lambda f: None):
            return pve.config_checks()

    def test_unknown_two_factor_is_not_a_pass(self):
        soft = {k: v for k, v in self.GOOD.items() if k != "pveum user tfa list root@pam"}
        with Env(soft=soft, texts={pve.FW_FILE: ""}), mock.patch.object(pve, "latest_backup", lambda f: None):
            checks = {n: (ok, d) for n, ok, d in pve.config_checks()}
        self.assertEqual(checks["two-factor is enrolled for root@pam"], (False, "unknown (pveum unavailable)"))


HAVE_AGE = bool(shutil.which("age") and shutil.which("age-keygen"))


class AgeRequired(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("PVE_SETUP_REQUIRE_AGE"), "only enforced in CI")
    def test_ci_has_age_so_the_encryption_tests_cannot_skip(self):
        self.assertTrue(HAVE_AGE, "age and age-keygen must be installed")


class EncryptedBackup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        t = self.tmp
        (t / "etc").mkdir()
        (t / "etc/hosts").write_text("127.0.0.1 localhost\n")
        (t / "hb.curl").write_text('header = "Authorization: Bearer SECRET-VALUE"\n')
        db = sqlite3.connect(t / "config.db")
        db.execute("create table tree (k text)")
        db.execute("insert into tree values ('storage.cfg')")
        db.commit()
        db.close()
        self.patches = [mock.patch.object(pve, "OWNED_FILES", []), mock.patch.object(pve, "HOST_FILES", [str(t / "etc/hosts")]),
                        mock.patch.object(pve, "HOST_DIRS", []), mock.patch.object(pve, "SECRET_FILES", [str(t / "hb.curl")]),
                        mock.patch.object(pve, "SECRET_DIRS", []), mock.patch.object(pve, "CLUSTER_DB", str(t / "config.db")),
                        mock.patch.object(pve, "pvesh_soft", lambda path, *a: {"/cluster/resources": RES}.get(path)),
                        mock.patch.object(pve, "soft", lambda argv: None), mock.patch.object(pve, "node", lambda: "pve"),
                        mock.patch.object(pve, "api_users", lambda: [])]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_recipient_validation(self):
        self.assertTrue(pve.valid_recipient("age1" + "q" * 58))
        self.assertFalse(pve.valid_recipient("age1short"))
        self.assertFalse(pve.valid_recipient("AGE-SECRET-KEY-1ABC"))
        self.assertFalse(pve.valid_recipient("not a key"))

    def test_recipients_file_ignores_comments_and_junk(self):
        good = "age1" + "q" * 58
        with mock.patch.object(pve, "read_text", lambda p: f"# mine\n{good}\njunk\n\n"):
            self.assertEqual(pve.read_recipients(pve.BACKUP_RECIPIENTS), [good])

    def test_without_age_the_encrypted_archive_is_refused_and_nothing_is_written(self):
        dest = str(self.tmp / "a.tar.gz")
        with mock.patch.object(pve.shutil, "which", lambda n: None):
            self.assertFalse(pve.write_config_archive(dest, ["age1" + "q" * 58]))
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith("a.")], [])

    def test_prune_and_latest_only_touch_archives_of_this_host(self):
        d = self.tmp / "bk"
        d.mkdir()
        names = [f"pve-config-pve-2026010{i}T000000Z.tar.gz.age" for i in range(1, 5)] + ["pve-config-other-20260101T000000Z.tar.gz", "notes.txt"]
        for n in names:
            (d / n).write_text("x")
        self.assertEqual(pve.prune_backups(str(d), 2), names[:2])
        self.assertTrue((d / "notes.txt").exists() and (d / names[4]).exists())
        self.assertEqual(pve.latest_backup(str(d))[0], "pve-config-pve-20260104T000000Z.tar.gz.age")
        self.assertIsNone(pve.latest_backup(str(d / "missing")))

    def test_auto_mode_without_recipients_fails_instead_of_writing_plaintext(self):
        with mock.patch.object(pve, "read_text", lambda p: None), mock.patch.object(pve.os, "geteuid", lambda: 0):
            r = runner.invoke(pve.app, ["config-backup", "--auto", "--folder", str(self.tmp / "bk")])
        self.assertEqual(r.exit_code, 1)
        self.assertIn("no valid recipients", r.output)
        self.assertFalse((self.tmp / "bk").exists())

    @unittest.skipUnless(HAVE_AGE, "age is not installed")
    def test_round_trip_holds_config_db_and_secrets_and_only_the_key_holder_can_read_it(self):
        key = self.tmp / "key.txt"
        pub = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True, check=True).stderr.split("Public key: ")[1].strip()
        other = self.tmp / "other.txt"
        subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
        dest = str(self.tmp / "out.tar.gz")
        self.assertTrue(pve.write_config_archive(dest, [pub]))
        self.assertFalse(os.path.exists(dest))
        raw = Path(dest + ".age").read_bytes()
        self.assertTrue(raw.startswith(b"age-encryption.org/v1"))
        self.assertNotIn(b"SECRET-VALUE", raw)
        self.assertEqual(oct(os.stat(dest + ".age").st_mode & 0o777), "0o600")
        with tempfile.TemporaryDirectory() as d:
            manifest, files, _ = pve.read_config_archive(pve.decrypt_archive(dest + ".age", str(key), d))
            self.assertTrue(manifest["includes_secrets"])
            self.assertIn(b"SECRET-VALUE", files[str(self.tmp / "hb.curl")])
            db_copy = Path(d) / "restored.db"
            db_copy.write_bytes(files[str(self.tmp / "config.db")])
            self.assertEqual(sqlite3.connect(db_copy).execute("select k from tree").fetchall(), [("storage.cfg",)])
            with self.assertRaisesRegex(ValueError, "could not decrypt"):
                pve.decrypt_archive(dest + ".age", str(other), d)
        self.assertEqual([f for f in os.listdir(self.tmp) if f.startswith(".pve-setup-")], [])

    @unittest.skipUnless(HAVE_AGE, "age is not installed")
    def test_restore_reads_an_encrypted_archive_and_marks_config_db_rebuild_only(self):
        key = self.tmp / "key.txt"
        pub = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True, check=True).stderr.split("Public key: ")[1].strip()
        dest = str(self.tmp / "out.tar.gz")
        pve.write_config_archive(dest, [pub])
        with mock.patch.object(pve.os, "geteuid", lambda: 0):
            r = runner.invoke(pve.app, ["config-restore", dest + ".age", "--identity", str(key)])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("secrets included: True", r.output)
        self.assertIn("config.db is for a rebuild only", r.output)
        self.assertNotIn("SECRET-VALUE", r.output)

    @unittest.skipUnless(HAVE_AGE, "age is not installed")
    def test_the_backup_command_encrypts_when_given_a_recipient(self):
        key = self.tmp / "key.txt"
        pub = subprocess.run(["age-keygen", "-o", str(key)], capture_output=True, text=True, check=True).stderr.split("Public key: ")[1].strip()
        folder = self.tmp / "bk"
        with mock.patch.object(pve, "run_step", lambda s: s.fn()), mock.patch.object(pve.os, "geteuid", lambda: 0):
            r = runner.invoke(pve.app, ["config-backup"], input=f"{folder}\n{pub}\n\ny\n")
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(len(list(folder.glob("pve-config-pve-*.tar.gz.age"))), 1, r.output)
        self.assertEqual(list(folder.glob("*.tar.gz")), [])


class BackupTimer(Isolated):
    RECIPIENT = "age1" + "q" * 58

    def test_a_recipient_is_required(self):
        with Env() as env:
            r = env.run(["config-backup-timer"], "\n")
        self.assertEqual(r.exit_code, 1)
        self.assertIn("a recipient is required", r.output)

    def test_plan_installs_script_units_and_enables_the_timer(self):
        with Env() as env:
            r = env.run(["config-backup-timer"], f"{self.RECIPIENT}\n\n\n\n\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("config-backup --auto --folder", r.output)
        self.assertIn("OnCalendar=Sun *-*-* 03:30:00", r.output)
        self.assertIn("systemctl enable --now pve-config-backup.timer", r.output)
        self.assertIn(self.RECIPIENT, r.output)

    def test_remove_needs_the_typed_word(self):
        with Env() as env, mock.patch.object(pve.os.path, "exists", lambda p: p in (pve.BACKUP_UNIT, pve.BACKUP_TIMER)):
            env.run(["config-backup-timer-remove"], "no\n")
            self.assertEqual(env.cmds(), [])
            env.run(["config-backup-timer-remove"], "backup\ny\n")
            self.assertIn("systemctl disable --now pve-config-backup.timer", env.cmds())

    def test_status_reports_the_new_sections(self):
        with Env(soft={**ZFS_SOFT, "pve-firewall status": "Status: enabled/running\n", "systemctl is-enabled pve-config-backup.timer": "enabled\n"}) as env:
            r = env.run(["status"])
        self.assertIn("Firewall: enabled/running", r.output)
        self.assertIn("ZFS: pool rpool", r.output)
        self.assertIn("weekly timer enabled", r.output)



def show(load="loaded", active="active", sub="running", result="success", restarts="0", status="0", since="Sat 2026-10-24 01:00:00 PDT"):
    return (f"LoadState={load}\nActiveState={active}\nSubState={sub}\nResult={result}\nNRestarts={restarts}\nExecMainStatus={status}\n"
            f"ActiveEnterTimestamp={since}\n")


SHOW_CMD = f"systemctl show --property={pve.SHOW_PROPS} "
HEALTHY = {SHOW_CMD + u: show(sub="running" if k == "service" else "waiting" if k == "timer" else "dead", active="active" if k != "oneshot" else "inactive")
           for u, k, _ in pve.SERVICE_UNITS}
HEALTHY.update({SHOW_CMD + u: show(load="not-found", active="inactive", sub="dead", result="") for u in
                ("pve-heartbeat.timer", "pve-heartbeat.service", "pve-config-backup.timer", "pve-config-backup.service")})


class Redaction(unittest.TestCase):
    def test_secrets_are_masked_and_everything_else_survives(self):
        cases = {
            "curl: (22) HTTP 401 Authorization: Bearer abc123.DEF-456_x": ("abc123", "HTTP 401"),
            "header = Authorization: Basic dXNlcjpwYXNz": ("dXNlcjpwYXNz", "Authorization"),
            "tailscale up --auth-key=tskey-auth-kABC123": ("tskey-auth", "tailscale up"),
            "preauth hskey-authreq-1234567890abcdef used": ("hskey-authreq", "preauth"),
            "AGE-SECRET-KEY-1QQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQQ": ("AGE-SECRET-KEY", ""),
            'login failed password="hunter2", retry': ("hunter2", "login failed"),
            "api token: s3cr3t-value rejected": ("s3cr3t-value", "rejected"),
            "Authorization: PVEAPIToken=root@pam!x=aaaa-bbbb": ("aaaa-bbbb", "Authorization"),
        }
        for line, (secret, kept) in cases.items():
            out = pve.redact(line)
            self.assertNotIn(secret, out, line)
            self.assertIn("<redacted>", out, line)
            self.assertIn(kept, out, line)

    def test_private_key_blocks_are_masked_across_lines(self):
        out = pve.redact("before\n-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\nBBBB\n-----END OPENSSH PRIVATE KEY-----\nafter")
        self.assertNotIn("AAAA", out)
        self.assertTrue(out.startswith("before") and out.endswith("after"))

    def test_ordinary_text_is_untouched(self):
        for line in ("pve-cluster: unable to open database /var/lib/pve-cluster/config.db", "basic configuration loaded for 192.168.1.2 (pve.ts.example.test)",
                     "tokens are not mentioned here"):
            self.assertEqual(pve.redact(line), line)


class JudgeUnit(unittest.TestCase):
    def test_required_service(self):
        self.assertEqual(pve.judge_unit("service", True, pve.parse_show(show()))[0], "PASS")
        self.assertEqual(pve.judge_unit("service", True, pve.parse_show(show(active="failed", sub="failed", result="exit-code")))[0], "FAIL")
        self.assertEqual(pve.judge_unit("service", True, pve.parse_show(show(active="activating", sub="auto-restart")))[0], "FAIL")
        self.assertEqual(pve.judge_unit("service", True, {}), ("FAIL", "not installed"))
        self.assertEqual(pve.judge_unit("service", True, pve.parse_show(show(load="not-found")))[0], "FAIL")

    def test_restart_storm_is_a_warning_not_a_failure(self):
        self.assertEqual(pve.judge_unit("service", True, pve.parse_show(show(restarts="2")))[0], "PASS")
        level, detail = pve.judge_unit("service", True, pve.parse_show(show(restarts="3")))
        self.assertEqual(level, "WARN")
        self.assertIn("restarted 3 times", detail)

    def test_optional_units_are_info_when_absent(self):
        self.assertEqual(pve.judge_unit("timer", False, {}), ("INFO", "not installed"))
        self.assertEqual(pve.judge_unit("oneshot", False, pve.parse_show(show(load="not-found")))[0], "INFO")

    def test_installed_but_inactive_timer_fails(self):
        self.assertEqual(pve.judge_unit("timer", False, pve.parse_show(show(active="inactive", sub="dead")))[0], "FAIL")

    def test_oneshot_is_judged_by_its_last_result(self):
        ok = pve.judge_unit("oneshot", False, pve.parse_show(show(active="inactive", sub="dead")))
        self.assertEqual(ok[0], "PASS")
        bad = pve.judge_unit("oneshot", False, pve.parse_show(show(active="failed", sub="failed", result="exit-code", status="22")))
        self.assertEqual(bad[0], "FAIL")
        self.assertIn("exit 22", bad[1])


class ServiceCheck(Isolated):
    def run_check(self, soft=None, args=()):
        with Env(soft={**HEALTHY, "systemctl cat ssh.service": "x", **(soft or {})}) as env:
            return env.run(["service-check", *args])

    def test_a_healthy_host_exits_zero_and_prints_no_journal(self):
        r = self.run_check()
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(r.output.count("FAIL"), 0, r.output)
        self.assertIn("PASS  pve-cluster.service", r.output)
        self.assertIn("INFO  pve-heartbeat.timer  (not installed)", r.output)
        self.assertNotIn("|", r.output)

    def test_a_dead_service_fails_and_shows_its_masked_journal(self):
        journal = "journalctl -u tailscaled.service -p err --since 24 hours ago -n 5 --no-pager -o short-iso -q"
        r = self.run_check({SHOW_CMD + "tailscaled.service": show(active="failed", sub="failed", result="exit-code"),
                            journal: "2026-10-24T01:00:00-0700 pve tailscaled[1]: login with --auth-key=tskey-auth-kSECRET failed\n"})
        self.assertEqual(r.exit_code, 1)
        self.assertIn("FAIL  tailscaled.service  (failed/failed, result exit-code)", r.output)
        self.assertIn("pve tailscaled[1]", r.output)
        self.assertNotIn("tskey-auth-kSECRET", r.output)
        self.assertIn("1 failing", r.output)

    def test_journal_is_not_read_for_passing_units_unless_asked(self):
        asked = []
        real = pve.unit_journal
        with mock.patch.object(pve, "unit_journal", lambda u, n: asked.append(u) or real(u, n)):
            self.run_check()
            self.assertEqual(asked, [])
            self.run_check(args=["--all"])
        self.assertIn("pve-cluster.service", asked)

    def test_zero_lines_suppresses_the_journal(self):
        r = self.run_check({SHOW_CMD + "pveproxy.service": show(active="failed", sub="failed", result="exit-code")}, args=["--lines", "0"])
        self.assertEqual(r.exit_code, 1)
        self.assertNotIn("|", r.output)

    def test_warnings_do_not_fail_the_command(self):
        r = self.run_check({SHOW_CMD + "pvestatd.service": show(restarts="5")})
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("WARN  pvestatd.service", r.output)

    def test_other_failed_units_are_listed_once(self):
        r = self.run_check({"systemctl --failed --no-legend --plain --no-pager": "foo.service loaded failed failed Foo\npveproxy.service loaded failed failed X\n"})
        self.assertEqual(r.exit_code, 1)
        self.assertEqual(r.output.count("foo.service"), 1)
        self.assertIn("FAIL  foo.service  (failed (systemctl --failed))", r.output)

    def test_armed_reverts_are_warned_about(self):
        r = self.run_check({"systemctl is-active pve-setup-ssh-revert.timer": "active\n", "systemctl is-active pve-setup-fw-revert.timer": "active\n"})
        self.assertEqual(r.exit_code, 0)
        self.assertIn("run ssh-harden-keep", r.output)
        self.assertIn("run firewall-keep", r.output)

    def test_a_failed_heartbeat_run_is_a_failure(self):
        r = self.run_check({SHOW_CMD + "pve-heartbeat.service": show(active="failed", sub="failed", result="exit-code", status="22"),
                            SHOW_CMD + "pve-heartbeat.timer": show(sub="waiting")})
        self.assertEqual(r.exit_code, 1)
        self.assertIn("FAIL  pve-heartbeat.service  (last run exit-code (exit 22))", r.output)

    def test_unusable_systemctl_is_an_error_not_a_wall_of_not_installed(self):
        with Env() as env:
            r = env.run(["service-check"])
        self.assertEqual(r.exit_code, 1)
        self.assertIn("systemctl cannot query systemd", r.output)
        self.assertNotIn("not installed", r.output)

    def test_one_unit_that_cannot_be_queried_fails_alone(self):
        soft = {k: v for k, v in HEALTHY.items() if k != SHOW_CMD + "pvestatd.service"}
        with Env(soft={**soft, "systemctl cat ssh.service": "x"}) as env:
            r = env.run(["service-check"])
        self.assertIn("FAIL  pvestatd.service  (systemctl show failed)", r.output)
        self.assertIn("PASS  pveproxy.service", r.output)

    def test_sshd_named_hosts_are_covered(self):
        with Env(soft={**HEALTHY, SHOW_CMD + "sshd.service": show(active="failed", sub="failed", result="exit-code")}) as env:
            r = env.run(["service-check"])
        self.assertIn("FAIL  sshd.service", r.output)


if __name__ == "__main__":
    unittest.main()
