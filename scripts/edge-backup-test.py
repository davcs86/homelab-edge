#!/usr/bin/env python3
"""Renders the backup scripts and units with the prod group_vars; runs the happybabytracker snapshot step against a tree
that mimics Docker's modes while a writer commits (root only; EDGE_BACKUP_TEST_REQUIRE_ROOT=1 turns a skip into a failure)."""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parent.parent
VARS_DIR = ROOT / "inventories/prod/group_vars/all"
APP_UID = 10001
AS_APP = f"setpriv --reuid={APP_UID} --regid={APP_UID} --clear-groups"
AS_APP_NO_GROUPS = AS_APP.split()
AS_APP_GROUP_ZERO = ["setpriv", f"--reuid={APP_UID}", f"--regid={APP_UID}", "--groups=0"]
SRC_DIR = "$vol_root/edge_happybabytracker-data/_data"


def load_vars():
    raw = {}
    for f in sorted(VARS_DIR.glob("*.yml")):
        raw.update(yaml.safe_load(f.read_text()) or {})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    resolved = dict(raw)
    for _ in range(10):  # values reference each other ("backup.{{ magicdns_base_domain }}")
        nxt = {k: env.from_string(v).render(**resolved) if isinstance(v, str) else v for k, v in resolved.items()}
        if nxt == resolved:
            break
        resolved = nxt
    return resolved


V = load_vars()
J = jinja2.Environment(undefined=jinja2.StrictUndefined)


def render(text):
    return J.from_string(text).render(**V, ansible_managed="test")


BACKUP_RAW = (ROOT / "roles/backup/templates/edge-backup.sh.j2").read_text()
BACKUP = render(BACKUP_RAW)
VERIFY = render((ROOT / "roles/backup/templates/edge-backup-verify.sh.j2").read_text())
TASKS = yaml.safe_load((ROOT / "roles/backup/tasks/main.yml").read_text())
UNITS_TASK = next(t for t in TASKS if t.get("name") == "Systemd units")
UNITS = {i["name"]: render(i["content"]) for i in UNITS_TASK["loop"]}
GATUS = yaml.safe_load(render((ROOT / "roles/stack/templates/gatus-config.yaml.j2").read_text()))


def lines(text):
    return [ln.strip() for ln in text.splitlines()]


def snapshot_sections():
    """The rendered script's own lines: the temp-dir set-up (work=… through the chmod of $mnt) and the happybabytracker
    snapshot (src_dir=… through the move into $work)."""
    ls = BACKUP.splitlines()
    a0 = next(i for i, ln in enumerate(ls) if ln.startswith("work="))
    a1 = next(i for i, ln in enumerate(ls) if ln == 'chmod 0755 "$mnt"')
    b0 = next(i for i, ln in enumerate(ls) if ln == f'src_dir="{SRC_DIR}"')
    b1 = next(i for i, ln in enumerate(ls) if ln.startswith("mv ") and "$work/happybabytracker/" in ln)
    return "\n".join(ls[a0:a1 + 1]), "\n".join(ls[b0:b1 + 1])


def hours(interval):
    m = re.fullmatch(r"(\d+)([hm])", str(interval))
    return int(m.group(1)) / (1 if m.group(2) == "h" else 60)


class Structure(unittest.TestCase):
    def test_the_database_is_snapshotted_as_the_apps_uid(self):
        ls = lines(BACKUP)
        self.assertIn(f'src_dir="{SRC_DIR}"', ls)
        backup = [ln for ln in ls if ".backup" in ln and "happybabytracker" in ln]
        self.assertEqual(backup, [
            "unshare --mount --propagation private -- sh -c 'mount --bind \"$1\" \"$2\" && exec " + AS_APP
            + " sqlite3 \"$2/happybabytracker.db\" \".backup '\\''$3/happybabytracker.db'\\''\"' sh \"$src_dir\" \"$mnt\" \"$snap\""])
        guard = [i for i, ln in enumerate(ls) if ln.startswith('test -f "$src_dir/happybabytracker.db" ||') and "exit 1" in ln]
        self.assertTrue(guard and guard[0] < ls.index(backup[0]), "test -f guard before the namespace")
        for ln in ls:
            if "sqlite3" in ln and ("$src_dir" in ln or "edge_happybabytracker-data" in ln):
                self.assertIn(AS_APP + " sqlite3 ", ln)
        for gone in ("--groups=0", "dac_read_search", "caps"):
            self.assertNotIn(gone, BACKUP)
        self.assertIn('mnt="$(mktemp -d /run/edge-backup.XXXXXX)"', ls)
        self.assertIn('chmod 0755 "$mnt"', ls)
        self.assertTrue([ln for ln in ls if ln.startswith("trap ") and 'rmdir "$mnt"' in ln], "trap removes $mnt")
        self.assertIn('snap="$(mktemp -d)"', ls)  # mktemp -d makes it 0700
        self.assertIn(f'chown {APP_UID}:{APP_UID} "$snap"', ls)
        self.assertFalse([ln for ln in ls if ln.startswith("snap=") and "$work" in ln])
        self.assertTrue([ln for ln in ls if ln.startswith("trap ") and '"$work"' in ln and "snap" in ln], "trap removes $snap")
        self.assertNotIn("docker exec", BACKUP)
        self.assertNotIn("docker", " ".join(ln for ln in ls if "happybabytracker" in ln and "vol_root" not in ln))

    def test_the_copy_is_integrity_checked_and_manifested(self):
        ls = lines(BACKUP)
        check = "sqlite3 \"$snap/happybabytracker.db\" 'PRAGMA integrity_check;' | grep -qx ok"
        self.assertIn(check, ls)
        move = next(ln for ln in ls if ln.startswith("mv ") and "$work/happybabytracker/" in ln)
        self.assertLess(ls.index(check), ls.index(move))
        manifest = [ln for ln in ls if "MANIFEST.sha256" in ln]
        self.assertEqual(len(manifest), 1)
        self.assertIn("find headscale happybabytracker -type f", manifest[0])
        self.assertIn("happybabytracker", next(ln for ln in BACKUP_RAW.splitlines()[:4] if ln.startswith("# ") and "->" in ln))
        self.assertNotIn("Nightly", BACKUP_RAW)

    def test_the_drill_checks_the_restored_copy_and_prints_no_rows(self):
        ls = lines(VERIFY)
        sql = [ln for ln in ls if "sqlite3" in ln]
        self.assertEqual(len(sql), 1, sql)
        self.assertTrue(sql[0].startswith("sqlite3 \"$work/happybabytracker/happybabytracker.db\" 'PRAGMA integrity_check;' "
                                          "| grep -qx ok || {"), sql[0])
        self.assertIn("exit 1", sql[0])
        manifest = next(i for i, ln in enumerate(ls) if "sha256sum -c MANIFEST.sha256" in ln)
        self.assertLess(manifest, ls.index(sql[0]))
        for ln in (ln for ln in ls if "happybabytracker" in ln or "sqlite3" in ln):
            self.assertIsNone(re.search(r"select|\.dump|count|\.tables|\.schema", ln, re.I), ln)
        for needle in ("FAIL noise key differs", "persistent nodes missing", "restore drill OK"):
            self.assertIn(needle, VERIFY)

    def test_rendered_scripts_pass_shellcheck(self):
        if not shutil.which("shellcheck"):
            self.skipTest("shellcheck not installed")
        with tempfile.TemporaryDirectory() as d:
            # The verify script's existing `ls -1t` raises SC2012 (info); it is held to warnings.
            for name, text, severity in (("edge-backup", BACKUP, "style"), ("edge-backup-verify", VERIFY, "warning")):
                p = Path(d) / name
                p.write_text(text)
                r = subprocess.run(["shellcheck", "-s", "bash", "-S", severity, str(p)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_timer_follows_the_schedule(self):
        self.assertEqual(V["backup_schedule"], "*-*-* 00,12:00:00 UTC")
        timer = lines(UNITS["edge-backup.timer"])
        self.assertIn(f"OnCalendar={V['backup_schedule']}", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("RandomizedDelaySec=10m", timer)
        for unit in UNITS.values():
            self.assertNotIn("nightly", unit.split("[Unit]")[1].split("\n\n")[0].lower())
        if not shutil.which("systemd-analyze"):
            print("systemd-analyze not installed: calendar expansion not checked")
            return
        r = subprocess.run(["systemd-analyze", "calendar", "--iterations=4", V["backup_schedule"]],
                           capture_output=True, text=True, check=True)
        times = re.findall(r"(?:Next elapse|Iteration #\d+): \w+ \S+ (\d\d:\d\d:\d\d) UTC", r.stdout)
        self.assertEqual(sorted(set(times)), ["00:00:00", "12:00:00"], r.stdout)
        self.assertEqual(len(times), 4, r.stdout)

    def test_changing_the_unit_restarts_the_timer(self):
        reg = UNITS_TASK.get("register")
        self.assertTrue(reg, "the units task registers its result")
        restart = [t for t in TASKS for mod in ("ansible.builtin.systemd_service", "ansible.builtin.systemd")
                   if isinstance(t.get(mod), dict) and t[mod].get("name") == "edge-backup.timer"
                   and t[mod].get("state") == "restarted"]
        self.assertEqual(len(restart), 1, "one restart task for the timer")
        when = " ".join(restart[0]["when"]) if isinstance(restart[0].get("when"), list) else restart[0].get("when", "")
        self.assertIn(reg, when)
        self.assertIn("edge-backup.timer", when)
        self.assertIn("changed", when)
        self.assertGreater(TASKS.index(restart[0]), TASKS.index(UNITS_TASK))

    def test_the_backup_heartbeat_keeps_its_name_and_covers_the_schedule(self):
        ext = [e for e in GATUS["external-endpoints"] if e["name"] == "nightly-backup"]
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0]["group"], "edge")
        self.assertEqual(V["heartbeat_backup_interval"], "14h")
        self.assertTrue(13 <= hours(ext[0]["heartbeat"]["interval"]) <= 24, ext[0]["heartbeat"])

    def test_known_hosts_is_left_as_deferred(self):
        main = (VARS_DIR / "main.yml").read_text()
        self.assertIn('backup_host_known_hosts: ""                             # TODO(provide): `ssh-keyscan backup.<tailnet>` '
                      "output; empty = trust on first use", main)
        self.assertIn("-o StrictHostKeyChecking={{ 'yes' if backup_host_known_hosts | default('') else 'accept-new' }}",
                      BACKUP_RAW)


CREATE = """
import sqlite3
c = sqlite3.connect("happybabytracker.db")
c.execute("PRAGMA journal_mode=WAL")
c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
c.commit()
c.close()
"""
WRITER = """
import sqlite3, time
c = sqlite3.connect("happybabytracker.db", timeout=10)
i = 0
while True:
    i += 1
    c.execute("INSERT INTO t (id, v) VALUES (?, ?)", (i, "x" * 200))
    c.commit()
    print(i, flush=True)
    time.sleep(0.002)
"""
CLOSE = """
import sqlite3
c = sqlite3.connect("happybabytracker.db")
c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
c.close()
"""


SHIM = """#!/bin/sh
case "$*" in *.backup*)
  while read -r k v; do [ "$k" = CapEff: ] && cap=$v; done < /proc/self/status
  echo "uid=$(id -u) CapEff=$cap" >> {log};;
esac
exec {real} "$@"
"""


def mounted(path):
    return any(f" {path} " in ln for ln in Path("/proc/self/mountinfo").read_text().splitlines())


class Snapshot(unittest.TestCase):
    def setUp(self):
        missing = [t for t in ("setpriv", "sqlite3", "unshare", "mount") if not shutil.which(t)]
        if not missing and os.geteuid() == 0 and subprocess.run(["unshare", "--mount", "--propagation", "private", "true"]).returncode:
            missing = ["a private mount namespace"]
        if os.geteuid() != 0 or missing:
            msg = "SKIPPED (" + ("not root" if os.geteuid() != 0 else f"missing {', '.join(missing)}") + ")"
            print(msg)
            if os.environ.get("EDGE_BACKUP_TEST_REQUIRE_ROOT") == "1":
                self.fail(msg + " but EDGE_BACKUP_TEST_REQUIRE_ROOT=1")
            self.skipTest(msg)
        self.base = Path(tempfile.mkdtemp(prefix="edge-backup-test-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.base, True)
        os.chmod(self.base, 0o755)
        self.volumes = self.base / "docker/volumes"
        self.data = self.volumes / "edge_happybabytracker-data/_data"
        self.data.mkdir(parents=True)
        os.chmod(self.base / "docker", 0o710)  # as /var/lib/docker: 0710 root:root
        os.chmod(self.volumes, 0o701)
        os.chmod(self.data.parent, 0o701)
        os.chown(self.data, APP_UID, APP_UID)
        os.chmod(self.data, 0o700)
        self.live = self.data / "happybabytracker.db"
        self.appdata = self.base / "appdata"  # the stand-in app's own /data, as a container mount gives it
        self.appdata.mkdir(mode=0o755)
        self.app([sys.executable, "-I", "-c", CREATE])
        self.tmp = self.base / "tmp"
        self.out = self.base / "out"
        self.tmp.mkdir(mode=0o711)
        os.chmod(self.tmp, 0o711)
        self.out.mkdir(mode=0o755)
        os.chmod(self.out, 0o755)
        self.caplog = self.out / "caps.log"
        self.caplog.touch(mode=0o666)
        os.chmod(self.caplog, 0o666)
        shim = self.base / "shim"
        shim.mkdir(mode=0o755)
        (shim / "sqlite3").write_text(SHIM.format(log=self.caplog, real=shutil.which("sqlite3")))
        os.chmod(shim / "sqlite3", 0o755)
        self.path = f"{shim}:{os.environ['PATH']}"

    def app(self, cmd, popen=False, **kw):
        """Runs cmd as uid 10001 with no capabilities, in /data bind-mounted from the volume (the container's view)."""
        argv = ["unshare", "--mount", "--propagation", "private", "--", "sh", "-c",
                'mount --bind "$1" "$2" && cd "$2" && shift 2 && exec ' + AS_APP + ' "$@"', "sh",
                str(self.data), str(self.appdata)] + cmd
        return subprocess.Popen(argv, **kw) if popen else subprocess.run(argv, check=True, **kw)

    def owners(self):
        return {p.name: (p.stat().st_uid, p.stat().st_gid) for p in self.data.iterdir()}

    def run_snapshot(self):
        a, b = snapshot_sections()
        self.assertNotIn("/var/lib/docker", a + b)
        script = "\n".join([
            "#!/usr/bin/env bash", "set -euo pipefail", "umask 077", f"vol_root={self.volumes}", a,
            f'stat -c "%u %g %a" "$snap" > {self.out}/snapdir', f'echo "$mnt" > {self.out}/mnt', b,
            f'cp -p "$work/happybabytracker/happybabytracker.db" {self.out}/', ""])
        (self.base / "snapshot.sh").write_text(script)
        r = subprocess.run(["bash", str(self.base / "snapshot.sh")], capture_output=True, text=True,
                           env={**os.environ, "TMPDIR": str(self.tmp), "PATH": self.path})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        mnt = (self.out / "mnt").read_text().strip()
        self.assertTrue(mnt.startswith("/run/edge-backup."), mnt)
        self.assertFalse(mounted(mnt), "the bind mount never shows outside the namespace")
        self.assertFalse(Path(mnt).exists(), "the mount point is removed on exit")
        self.assertFalse(mounted(self.data), "nothing stays mounted over the volume")
        cap = self.caplog.read_text().splitlines()
        self.caplog.write_text("")
        print(f"sqlite3 .backup ran as: {cap}")
        self.assertEqual(cap, [f"uid={APP_UID} CapEff=0000000000000000"])
        self.assertEqual((self.out / "snapdir").read_text().split(), [str(APP_UID), str(APP_UID), "700"])
        self.assertEqual(list(self.tmp.iterdir()), [], "both temp dirs are removed on exit")
        copy = self.out / "happybabytracker.db"
        ok = subprocess.run(["sqlite3", str(copy), "PRAGMA integrity_check;"], capture_output=True, text=True, check=True)
        self.assertEqual(ok.stdout.strip(), "ok")
        top = subprocess.run(["sqlite3", str(copy), "SELECT max(id) FROM t;"], capture_output=True, text=True, check=True)
        copy.unlink()
        return int(top.stdout.strip() or 0)

    def cannot_reach(self, as_uid, label):
        probe = self.base / "probe"
        probe.mkdir(exist_ok=True)
        os.chown(probe, APP_UID, APP_UID)
        r = subprocess.run(as_uid + ["sqlite3", str(self.live), f".backup '{probe}/x.db'"], capture_output=True, text=True)
        print(f"{label} sqlite3: exit {r.returncode}: {r.stderr.strip()}")
        self.assertNotEqual(r.returncode, 0)
        s = subprocess.run(as_uid + ["stat", str(self.live)], capture_output=True, text=True)
        print(f"{label} stat: exit {s.returncode}: {s.stderr.strip()}")
        self.assertIn("Permission denied", s.stderr)

    def test_plain_clear_groups_cannot_reach_the_database(self):
        self.cannot_reach(AS_APP_NO_GROUPS, "--clear-groups")

    def test_group_zero_alone_cannot_traverse_the_volume_dir(self):
        # Group root matches the 0701 root:root volume dirs, so their group bits (none) apply, not o+x (R9 fails).
        self.cannot_reach(AS_APP_GROUP_ZERO, "--groups=0")

    def test_a_snapshot_during_writes_is_valid_and_leaves_nothing_owned_by_root(self):
        writer = self.app([sys.executable, "-I", "-u", "-c", WRITER], popen=True, stdout=subprocess.PIPE, text=True)
        self.addCleanup(writer.wait)
        self.addCleanup(writer.kill)
        self.addCleanup(writer.stdout.close)
        seen = 0
        while seen < 300:
            line = writer.stdout.readline()
            self.assertTrue(line, "writer died")
            seen = int(line)

        top = self.run_snapshot()
        print(f"writer had committed {seen} rows before the snapshot; the copy holds up to row {top}")
        self.assertGreaterEqual(top, seen)
        after = seen
        while after < seen + 50:
            line = writer.stdout.readline()
            self.assertTrue(line, "writer died after the snapshot")
            after = int(line)
        self.assertIsNone(writer.poll())
        owners = self.owners()
        print(f"source files after the snapshot (writer running): {owners}")
        self.assertTrue({"happybabytracker.db", "happybabytracker.db-wal", "happybabytracker.db-shm"} <= set(owners))
        self.assertEqual(set(owners.values()), {(APP_UID, APP_UID)})

        writer.kill()
        writer.wait()
        self.app([sys.executable, "-I", "-c", CLOSE])
        self.assertEqual(set(self.owners()), {"happybabytracker.db"}, "no -wal/-shm before the idle snapshot")
        self.assertGreaterEqual(self.run_snapshot(), after)
        owners = self.owners()
        print(f"source files after the snapshot (app stopped, no -wal/-shm before): {owners}")
        self.assertEqual(set(owners.values()), {(APP_UID, APP_UID)})


if __name__ == "__main__":
    unittest.main()
