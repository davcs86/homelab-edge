"""Offline tests for pve-setup.py: probes and commands are faked, nothing touches a Proxmox host or needs root."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from typer.testing import CliRunner

spec = importlib.util.spec_from_file_location("pve_setup", Path(__file__).with_name("pve-setup.py"))
pve = importlib.util.module_from_spec(spec)
sys.modules["pve_setup"] = pve
spec.loader.exec_module(pve)
runner = CliRunner()

RES = [
    {"vmid": 100, "name": "tpl", "type": "qemu", "status": "stopped", "template": 1, "tags": "pve-setup"},
    {"vmid": 101, "name": "old", "type": "qemu", "status": "running", "tags": "prod"},
    {"vmid": 110, "name": "mine", "type": "lxc", "status": "running", "tags": "pve-setup;x"},
]
JOB = {"id": "pve-setup-nightly", "schedule": "03:30", "storage": "bk", "mode": "snapshot", "all": 1,
       "prune-backups": "keep-weekly=4,keep-daily=7", "enabled": 1}
TS_UP = {"package": "1.98.10", "held": True, "backend": "Running", "hostname": "pve", "tags": ["tag:pve"],
         "control": "https://hs.example.test", "dns": True}
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
    ANSWERS = "status.ts.example.test\n\n60\nTOKEN-VALUE\n"

    def desired(self):
        return pve.heartbeat_files("status.ts.example.test", "home_proxmox-heartbeat", 60, "TOKEN-VALUE")

    def test_fresh_install_plans_everything_and_hides_the_token(self):
        with Env() as env:
            r = env.run(["heartbeat"], self.ANSWERS, dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("TOKEN-VALUE", r.output)
        self.assertIn("-K /etc/edge-heartbeat.curl https://status.ts.example.test/api/v1/endpoints/home_proxmox-heartbeat/external", r.output)
        self.assertIn("mode 0600", r.output)

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


class Tailscale(unittest.TestCase):
    def test_install_is_a_no_op_at_the_pinned_version(self):
        with Env(ts=TS_UP, texts={pve.TAILSCALE_LIST: LIST}) as env, mock.patch.object(pve.os.path, "exists", return_value=True), \
                mock.patch.object(pve.Path, "read_text", return_value='VERSION_CODENAME="bookworm"\n'):
            r = env.run(["tailscale-install"], "1.98.10\n", dry=True)
        self.assertIn("Nothing to do", r.output, r.output)

    def test_install_upgrades_with_held_package_flags(self):
        with Env(ts={**TS_UP, "package": "1.90.0"}, texts={pve.TAILSCALE_LIST: LIST}) as env, \
                mock.patch.object(pve.os.path, "exists", return_value=True), \
                mock.patch.object(pve.Path, "read_text", return_value='VERSION_CODENAME="bookworm"\n'):
            r = env.run(["tailscale-install"], "1.98.10\n", dry=True)
        self.assertIn("--allow-change-held-packages --allow-downgrades tailscale=1.98.10", r.output, r.output)
        self.assertNotIn("apt-get update", r.output)

    def test_join_hides_the_key(self):
        with Env() as env:
            r = env.run(["tailscale-join"], "https://hs.example.test\nxstock\ny\nSECRET-KEY-VALUE\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertNotIn("SECRET-KEY-VALUE", r.output)
        self.assertIn("--auth-key file:/run/pve-setup-authkey", r.output)
        self.assertNotIn("--reset", r.output)

    def test_join_already_joined_asks_for_no_key(self):
        with Env(ts=TS_UP) as env:
            r = env.run(["tailscale-join"], "https://hs.example.test\npve\ny\n", dry=True)
        self.assertEqual(r.exit_code, 0, r.output)
        self.assertIn("Already joined", r.output)
        self.assertIn("Nothing to do", r.output)

    def test_join_with_other_settings_reauthenticates(self):
        with Env(ts=TS_UP) as env:
            r = env.run(["tailscale-join"], "https://hs.example.test\nnew-name\ny\nKEY\n", dry=True)
        self.assertIn("--reset --force-reauth", r.output)

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
