"""Offline tests for pve-setup.py: probes and commands are faked, nothing touches a Proxmox host or needs root."""
import contextlib
import importlib.util
import io
import json
import os
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
    ANSWERS = "status.ts.example.test\n\n60\nTOKEN-VALUE\n100.64.0.1\n"

    def desired(self):
        return pve.heartbeat_files("status.ts.example.test", "home_proxmox-heartbeat", 60, "TOKEN-VALUE", "100.64.0.1")

    def test_fresh_install_plans_everything_and_hides_the_token(self):
        with Env() as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("TOKEN-VALUE", r.output)
        self.assertIn("-K /etc/edge-heartbeat.curl https://status.ts.example.test/api/v1/endpoints/home_proxmox-heartbeat/external", r.output)
        self.assertIn("mode 0600", r.output)

    def test_curl_config_pins_the_edge_address_so_dns_can_stay_off(self):
        content = self.desired()[pve.HEARTBEAT_FILES[0]][0]
        self.assertIn('resolve = "status.ts.example.test:443:100.64.0.1"', content)
        self.assertIn('header = "Authorization: Bearer TOKEN-VALUE"', content)
        self.assertNotIn("resolve", pve.heartbeat_files("h", "e", 60, "T")[pve.HEARTBEAT_FILES[0]][0])

    def test_edge_address_must_be_a_tailnet_address(self):
        with Env() as env:
            r = env.run(["heartbeat"], "status.ts.example.test\n\n60\nT\n8.8.8.8\nnope\n100.64.0.1\n", dry=True)
        self.assertEqual(r.output.count("a tailnet address in 100.64.0.0/10"), 2)
        self.assertNotIn("8.8.8.8\n", r.output.split("== ")[-1])

    def test_unchanged_install_does_nothing(self):
        texts = {p: c for p, (c, _, _) in self.desired().items()}
        soft = {"systemctl is-enabled edge-heartbeat.timer": "enabled\n", "systemctl is-active edge-heartbeat.timer": "active\n"}
        with Env(texts=texts, soft=soft) as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertIn("Nothing to do", r.output)
        self.assertNotIn("write ", r.output)

    def test_only_the_changed_file_is_rewritten(self):
        texts = {p: c for p, (c, _, _) in self.desired().items()}
        texts["/etc/systemd/system/edge-heartbeat.timer"] = "old\n"
        soft = {"systemctl is-enabled edge-heartbeat.timer": "enabled\n", "systemctl is-active edge-heartbeat.timer": "active\n"}
        with Env(texts=texts, soft=soft) as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertIn("write /etc/systemd/system/edge-heartbeat.timer", r.output)
        self.assertNotIn("write /etc/systemd/system/edge-heartbeat.service", r.output)
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
        self.assertNotIn("apt-get update", r.output)

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
        soft = {"systemctl is-enabled edge-heartbeat.timer": "enabled\n", "systemctl is-active edge-heartbeat.timer": "active\n"}
        with Env(soft=soft) as env, mock.patch.object(pve.os.path, "exists", lambda p: files.get(p, False)):
            env.run(["heartbeat-remove"], "heartbeat\ny\n")
        self.assertEqual(env.cmds(), ["systemctl disable --now edge-heartbeat.timer", "systemctl daemon-reload"])
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
HARD = "passwordauthentication no\nkbdinteractiveauthentication no\npermitrootlogin without-password\n"
SOFT = "passwordauthentication yes\nkbdinteractiveauthentication no\npermitrootlogin without-password\n"
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
            r = env.run(["ssh-harden"], f"3\n{KEY}\n{KEY}\nnot a key\n{KEY2}\n\n", dry=True)
        self.assertIn("new     256 SHA256:FP-laptop", r.output)
        self.assertEqual(r.output.count("SHA256:FP-laptop"), 1)
        self.assertIn("skipping a line that is not a plain public key", r.output)
        self.assertIn("append 2 line(s) to /root/.ssh/authorized_keys in place (a symlink is kept)", r.output)

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
            eff = {"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "permitrootlogin": spelling}
            self.assertTrue(pve.ssh_is_hardened(eff))
        self.assertFalse(pve.ssh_is_hardened({"passwordauthentication": "yes", "kbdinteractiveauthentication": "no", "permitrootlogin": "without-password"}))
        self.assertFalse(pve.ssh_is_hardened({"passwordauthentication": "no", "kbdinteractiveauthentication": "no", "permitrootlogin": "yes"}))
        self.assertFalse(pve.ssh_is_hardened(None))

    def test_plan_shows_proof_commands_dropin_revert_and_checks(self):
        with self.env() as env:
            r = env.run(["ssh-harden"], "\n\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        for text in ("ssh root@192.168.1.10", "ssh root@100.64.0.9", "write /etc/ssh/sshd_config.d/10-hardening.conf",
                     "PermitRootLogin prohibit-password", "validate with sshd -t", "systemd-run --unit pve-setup-ssh-revert --on-active=10m",
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
        return Env(texts={pve.AUTHORIZED_KEYS: KEY + "\n"}, soft=soft, ts=kw.pop("ts", TS_PVE))

    def test_all_host_checks_pass_and_are_recorded(self):
        with self.env() as env:
            r = env.run(["remote-gate"])
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertEqual(r.output.count("PASS"), 7)
        self.assertNotIn("FAIL", r.output)
        self.assertIn("Passing boots recorded: 1", r.output)
        self.assertIn("phone, Wi-Fi off", r.output)
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
            r = runner.invoke(pve.app, ["config-backup"], input=f"{folder}\nn\ny\n")
        self.assertEqual(r.exit_code, 0, r.output)
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


if __name__ == "__main__":
    unittest.main()
