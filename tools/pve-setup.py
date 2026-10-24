#!/usr/bin/env python3
"""Interactive Proxmox VE setup toolkit. Run as root on the PVE host; see docs/runbooks/proxmox-setup.md.

Every step prints the exact commands and files it will touch and waits for a yes. Setup tasks probe first and build
only the missing steps. Secrets are read with hidden input, are never written to the log, and reach commands through
0600 files, not argv.
"""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import typer

app = typer.Typer(add_completion=False, help="Interactive Proxmox VE setup toolkit (run on the PVE host, as root).")
STATE = {"dry_run": False}
LOG = Path.home() / ".pve-setup.log"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SIZE_RE = re.compile(r"^[1-9][0-9]*[MG]$")

# Everything the toolkit creates carries one of these, and status/undo touch nothing else.
TAG = "pve-setup"
JOB_PREFIX = "pve-setup-"
USER_COMMENT = "created by pve-setup"

NODE_NAME = "pve"  # Gatus probes tcp://pve.ts.<domain>:8006; the heartbeat endpoint is named for it
TAILSCALE_VERSION = "1.98.10"  # keep in step with tailscale_package_version in inventories/prod/group_vars/all/versions.yml
ARC_MAX_BYTES = 3 * 2**30  # the guide's ZFS ARC cap (section 5.2)
AUTHORIZED_KEYS = "/root/.ssh/authorized_keys"  # a symlink into the cluster filesystem on Proxmox
SSHD_CONFIG = "/etc/ssh/sshd_config"
SSH_DROPIN = "/etc/ssh/sshd_config.d/10-hardening.conf"
SSH_DROPIN_TEXT = "PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin prohibit-password\nAllowAgentForwarding no\n"
SSH_REVERT_UNIT = "pve-setup-ssh-revert"
HB_CURL, HB_ENV, HB_SCRIPT = "/etc/pve-heartbeat.curl", "/etc/pve-heartbeat.env", "/usr/local/sbin/pve-heartbeat"
HB_UNIT, HB_TIMER = "/etc/systemd/system/pve-heartbeat.service", "/etc/systemd/system/pve-heartbeat.timer"
HEARTBEAT_FILES = (HB_CURL, HB_ENV, HB_SCRIPT, HB_UNIT, HB_TIMER)
OLD_HEARTBEAT_FILES = ("/etc/edge-heartbeat.curl", "/etc/systemd/system/edge-heartbeat.service", "/etc/systemd/system/edge-heartbeat.timer")
SNAP_PREFIX = "pve-setup-"
RESERVE_DATASET_NAME = "reserved"
TAILSCALE_LIST = "/etc/apt/sources.list.d/tailscale.list"
TAILSCALE_KEYRING = "/usr/share/keyrings/tailscale-archive-keyring.gpg"

BACKUP_RECIPIENTS = "/etc/pve-setup-backup.recipients"
BACKUP_UNIT, BACKUP_TIMER = "/etc/systemd/system/pve-config-backup.service", "/etc/systemd/system/pve-config-backup.timer"
# Config backup: (path, is pmxcfs). Files the toolkit writes itself are "owned"; keys and tokens are "secret".
OWNED_FILES = [HB_UNIT, HB_TIMER, HB_SCRIPT, HB_ENV, TAILSCALE_LIST, BACKUP_UNIT, BACKUP_TIMER, BACKUP_RECIPIENTS]
HOST_FILES = ["/etc/hostname", "/etc/hosts", "/etc/fstab", "/etc/network/interfaces", "/etc/apt/sources.list"]
HOST_DIRS = [("/etc/network/interfaces.d", False), ("/etc/apt/sources.list.d", False), ("/etc/ssh/sshd_config.d", False),
             ("/etc/modprobe.d", False), ("/etc/pve", True)]
SECRET_FILES = [HB_CURL]
SECRET_DIRS: list[tuple[str, bool]] = []  # the guide: never back up /var/lib/tailscale; a rebuilt host re-registers
CLUSTER_DB = "/var/lib/pve-cluster/config.db"
BACKUP_INSTALL_DIR = "/usr/local/lib/pve-setup"
BACKUP_KEEP = 8
MAX_FILE = 10 * 2**20
MAX_ARCHIVE = 200 * 2**20


@dataclass
class Cmd:
    argv: list[str]
    shown: str = ""
    undo: Optional["Cmd"] = None
    undo_on_fail: bool = False


@dataclass
class Write:
    path: str
    content: str
    mode: int = 0o644
    secret: bool = False
    undo: Optional["Step"] = None
    in_place: bool = False  # pmxcfs (/etc/pve) cannot chmod or rename-create safely


@dataclass
class Fetch:
    url: str
    dest: str
    sha256: str


@dataclass
class Verify:
    path: str
    sha256: str


@dataclass
class Remove:
    path: str


@dataclass
class Append:
    path: str
    lines: list[str]


@dataclass
class Do:
    label: str
    fn: Callable[[], bool]


Step = Cmd | Write | Fetch | Verify | Remove | Append | Do


@dataclass
class Plan:
    title: str
    steps: list[Step]
    cleanup: list[str] = field(default_factory=list)


def fail(msg: str) -> int:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    return 1


def warn(msg: str) -> None:
    typer.secho(f"warning: {msg}", fg=typer.colors.YELLOW)


def log(line: str) -> None:
    LOG.touch(mode=0o600, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")


def describe(step: Step) -> str:
    if isinstance(step, Cmd):
        return "$ " + (step.shown or shlex.join(step.argv))
    if isinstance(step, Write):
        body = f"<hidden, {len(step.content)} bytes>" if step.secret else "\n" + step.content.rstrip("\n")
        return f"write {step.path} (mode {step.mode:04o}) {body}"
    if isinstance(step, Fetch):
        return f"download {step.url} -> {step.dest}, then require sha256 {step.sha256}"
    if isinstance(step, Verify):
        return f"require sha256 {step.sha256} for {step.path}"
    if isinstance(step, Remove):
        return f"remove {step.path}"
    if isinstance(step, Append):
        return f"append {len(step.lines)} line(s) to {step.path} in place (a symlink is kept)" + "".join(f"\n  {l[:100]}" for l in step.lines)
    return step.label


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_file(step: Write) -> None:
    if step.in_place:
        Path(step.path).parent.mkdir(parents=True, exist_ok=True)
        with open(step.path, "w") as f:
            f.write(step.content)
        return
    Path(step.path).parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(Path(step.path).parent), prefix=".pve-setup-")
    try:
        os.fchmod(fd, step.mode)
        with os.fdopen(fd, "w") as f:
            f.write(step.content)
        os.replace(tmp, step.path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def append_lines(path: str, lines: list[str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        existing = Path(path).read_text()
    except FileNotFoundError:
        existing = ""
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)  # follows a symlink, never replaces it
    with os.fdopen(fd, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("".join(l.rstrip("\n") + "\n" for l in lines))


def run_step(step: Step) -> bool:
    log(describe(step).splitlines()[0])
    if isinstance(step, Write):
        write_file(step)
        return True
    if isinstance(step, Append):
        append_lines(step.path, step.lines)
        return True
    if isinstance(step, Remove):
        if os.path.lexists(step.path):
            os.unlink(step.path)
        return True
    if isinstance(step, Do):
        return step.fn()
    if isinstance(step, Fetch):
        Path(step.dest).parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(step.url, step.dest)  # noqa: S310  # https URL supplied by the operator
        step = Verify(step.dest, step.sha256)
    if isinstance(step, Verify):
        got = sha256_of(step.path)
        if got != step.sha256.lower():
            fail(f"sha256 mismatch for {step.path}: expected {step.sha256}, got {got}")
            return False
        typer.echo(f"sha256 ok: {step.path}")
        return True
    rc = subprocess.run(step.argv, check=False).returncode
    log(f"  exit {rc}")
    if rc != 0:
        fail(f"exit {rc}: {step.shown or shlex.join(step.argv)}")
    return rc == 0


def offer_rollback(done: list[Step], failed: Step) -> None:
    undos = [u for u in (getattr(s, "undo", None) for s in reversed(done)) if u]
    if isinstance(failed, Cmd) and failed.undo and failed.undo_on_fail:
        undos.insert(0, failed.undo)
    if not undos:
        typer.echo("Nothing was changed that needs rolling back.")
        return
    typer.secho("\nThe plan stopped part-way. These commands undo what it already did:", bold=True)
    for u in undos:
        typer.echo("  " + describe(u).splitlines()[0])
    if not typer.confirm("Run the rollback now?", default=False):
        typer.echo("Left as is. Re-running the task resumes from what exists.")
        return
    for u in undos:
        run_step(u)


def execute(plan: Plan) -> bool:
    typer.secho(f"\n== {plan.title}", bold=True)
    if not plan.steps:
        typer.echo("Nothing to do: already in the desired state.")
        return True
    for i, step in enumerate(plan.steps, 1):
        typer.echo(f"{i}. {describe(step)}")
    if STATE["dry_run"]:
        typer.secho("(dry run: nothing executed)", fg=typer.colors.YELLOW)
        return True
    if not typer.confirm("Run these now?", default=False):
        typer.echo("Skipped.")
        return False
    done: list[Step] = []
    failed: Step | None = None
    try:
        for s in plan.steps:
            if not run_step(s):
                failed = s
                break
            done.append(s)
    finally:
        for p in plan.cleanup:
            if os.path.exists(p):
                os.unlink(p)
    if failed is not None:
        offer_rollback(done, failed)
        return False
    return True


def confirm_typed(expected: str, what: str) -> bool:
    if STATE["dry_run"]:
        return True
    typer.secho(what, fg=typer.colors.RED)
    return typer.prompt(f'Type "{expected}" to continue', default="", show_default=False).strip() == expected


# ---------- probes (read-only) ----------
def capture(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise typer.Exit(fail(f"{argv[0]} not found; run this on the Proxmox host")) from None
    except subprocess.CalledProcessError as e:
        raise typer.Exit(fail(f"{shlex.join(argv)}: {e.stderr.strip()}")) from None


def soft(argv: list[str]) -> str | None:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return r.stdout if r.returncode == 0 else None


def soft_json(argv: list[str]) -> object | None:
    out = soft(argv)
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return None


def pvesh_soft(path: str, *args: str) -> object | None:
    return soft_json(["pvesh", "get", path, "--output-format", "json", *args])


def pvesh(path: str, *args: str) -> object:
    return json.loads(capture(["pvesh", "get", path, "--output-format", "json", *args]))


def node() -> str:
    return socket.gethostname().split(".")[0]


def pve_version() -> tuple[int, int]:
    m = re.search(r"pve-manager/(\d+)\.(\d+)", capture(["pveversion"]))
    if not m:
        raise typer.Exit(fail("cannot parse `pveversion`"))
    return int(m[1]), int(m[2])


def read_text(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


def guests() -> list[dict]:
    rows = pvesh_soft("/cluster/resources", "--type", "vm") or []
    out = []
    for r in rows:  # type: ignore[union-attr]
        tags = [t for t in str(r.get("tags", "")).replace(",", ";").split(";") if t]
        out.append({"vmid": int(r["vmid"]), "name": r.get("name", ""), "type": r.get("type", ""), "status": r.get("status", ""),
                    "template": r.get("template") == 1, "tags": tags, "managed": TAG in tags})
    return out


def with_tag(tags: str) -> str:
    parts = [t for t in tags.replace(",", ";").split(";") if t and t != TAG]
    return ";".join([TAG, *parts])


def tailscale_state() -> dict | None:
    if not shutil.which("tailscale"):
        return None
    st = soft_json(["tailscale", "status", "--json"]) or {}
    prefs = soft_json(["tailscale", "debug", "prefs"]) or {}
    me = st.get("Self") or {}  # type: ignore[union-attr]
    return {"package": (soft(["dpkg-query", "-W", "-f=${Version}", "tailscale"]) or "").strip(),
            "held": "tailscale" in (soft(["apt-mark", "showhold"]) or "").split(),
            "backend": st.get("BackendState"),  # type: ignore[union-attr]
            "hostname": (me.get("DNSName") or "").split(".")[0], "tags": me.get("Tags") or [],
            "control": (prefs.get("ControlURL") or "").rstrip("/"), "dns": prefs.get("CorpDNS")}  # type: ignore[union-attr]


def heartbeat_state() -> dict:
    return {"files": {p: os.path.exists(p) for p in HEARTBEAT_FILES},
            "enabled": (soft(["systemctl", "is-enabled", "pve-heartbeat.timer"]) or "").strip(),
            "active": (soft(["systemctl", "is-active", "pve-heartbeat.timer"]) or "").strip(),
            "legacy": [p for p in OLD_HEARTBEAT_FILES if os.path.exists(p)],
            "legacy_active": (soft(["systemctl", "is-active", "edge-heartbeat.timer"]) or "").strip() == "active"}


def backup_jobs() -> list[dict]:
    return list(pvesh_soft("/cluster/backup") or [])  # type: ignore[call-overload]


def storage_config(sid: str) -> dict | None:
    cfg = pvesh_soft(f"/storage/{sid}")
    return cfg if isinstance(cfg, dict) else None


def api_users() -> list[dict]:
    return list(pvesh_soft("/access/users") or [])  # type: ignore[call-overload]


# ---------- prompts ----------
def choose(prompt: str, options: list[tuple[str, str]]) -> str:
    if not options:
        raise typer.Exit(fail(f"nothing to choose for: {prompt}"))
    for i, (_, label) in enumerate(options, 1):
        typer.echo(f"  {i}) {label}")
    return options[ask_int(prompt, 1, len(options), 1 if len(options) == 1 else None) - 1][0]


def ask_int(prompt: str, lo: int, hi: int, default: int | None = None) -> int:
    while True:
        v = typer.prompt(prompt, type=int, default=default)
        if lo <= v <= hi:
            return v
        typer.echo(f"  enter a number from {lo} to {hi}")


def ask_name(prompt: str, default: str | None = None) -> str:
    while True:
        v = typer.prompt(prompt, default=default)
        if NAME_RE.match(v):
            return v
        typer.echo("  lowercase letters, digits and '-', starting with a letter or digit")


def ask_size(prompt: str, default: str) -> str:
    while True:
        v = typer.prompt(prompt, default=default)
        if SIZE_RE.match(v):
            return v
        typer.echo("  use a number with M or G, e.g. 32G")


def ask_sha256() -> str:
    while True:
        v = typer.prompt("Expected sha256 (from the publisher's checksum file)").strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", v):
            return v
        typer.echo("  64 hex characters")


def ask_pubkey_file() -> str:
    while True:
        v = typer.prompt("SSH public key file for the guest user", default=str(Path.home() / ".ssh" / "id_ed25519.pub"))
        p = Path(v).expanduser()
        if p.is_file() and p.read_text().startswith(("ssh-", "ecdsa-", "sk-")):
            return str(p)
        typer.echo("  not a readable public key file")


def free_id(kind: str) -> str:
    used = {int(r["vmid"]) for r in pvesh("/cluster/resources", "--type", "vm")}  # type: ignore[union-attr]
    suggested = int(pvesh("/cluster/nextid"))  # type: ignore[arg-type]
    while True:
        v = typer.prompt(f"{kind} ID", type=int, default=suggested)
        if v < 100:
            typer.echo("  IDs start at 100")
        elif v in used:
            typer.echo(f"  {v} is in use")
        else:
            return str(v)


def storages(content: str) -> list[tuple[str, str]]:
    rows = pvesh(f"/nodes/{node()}/storage", "--content", content, "--enabled", "1")
    return [(r["storage"], f"{r['storage']} ({r['type']}, {int(r.get('avail', 0)) // 2**30} GiB free)") for r in rows]  # type: ignore[union-attr]


def bridges() -> list[tuple[str, str]]:
    rows = pvesh(f"/nodes/{node()}/network", "--type", "bridge")
    return [(r["iface"], f"{r['iface']} ({r.get('cidr') or 'no address'})") for r in rows]  # type: ignore[union-attr]


def ip_config() -> str:
    if typer.confirm("Use DHCP?", default=True):
        return "ip=dhcp"
    addr = typer.prompt("Address with prefix, e.g. 192.168.1.50/24")
    gw = typer.prompt("Gateway")
    return f"ip={addr},gw={gw}"


def warn_same_name(name: str, kind: str) -> bool:
    same = [g for g in guests() if g["name"] == name]
    if not same:
        return True
    for g in same:
        warn(f"{kind} named '{name}' already exists: ID {g['vmid']} ({g['type']}, {g['status']}{', template' if g['template'] else ''})")
    return typer.confirm("Create another one with the same name?", default=False)


# ---------- tailnet ----------
def apt_has_version(version: str) -> bool:
    out = soft(["apt-cache", "madison", "tailscale"]) or ""
    if f"| {version} |" in out:
        return True
    fail(f"tailscale {version} is not in the apt repository; stop and ask which version the edge supports before installing another")
    return False


def tailscale_install() -> None:
    """Add Tailscale's apt repo, install a pinned version, hold it and start tailscaled."""
    st = tailscale_state()
    if st:
        typer.echo(f"tailscale {st['package'] or '?'} is installed{' and held' if st['held'] else ''}.")
    codename = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines() if "=" in l)["VERSION_CODENAME"].strip('"')
    version = typer.prompt("Tailscale package version (the edge's pin; headscale rejects clients outside its range)", default=TAILSCALE_VERSION)
    if version != TAILSCALE_VERSION:
        warn(f"the edge pins {TAILSCALE_VERSION} (versions.yml); only continue if you changed that pin")
    base = f"https://pkgs.tailscale.com/stable/debian/{codename}"
    listing = f"deb [signed-by={TAILSCALE_KEYRING}] https://pkgs.tailscale.com/stable/debian {codename} main\n"
    steps: list[Step] = []
    repo_ok = os.path.exists(TAILSCALE_KEYRING) and read_text(TAILSCALE_LIST) == listing
    if not repo_ok:
        steps += [Cmd(["curl", "-fsSL", "-o", TAILSCALE_KEYRING, f"{base}.noarmor.gpg"]), Write(TAILSCALE_LIST, listing),
                  Cmd(["apt-get", "update"])]
    installing = not st or st["package"] != version
    if installing:
        steps += [Do(f"check that tailscale {version} exists in apt (stops here if not)", lambda: apt_has_version(version)),
                  Cmd(["apt-get", "install", "-y", "--allow-change-held-packages", "--allow-downgrades", f"tailscale={version}"])]
    if not st or not st["held"] or installing:
        steps.append(Cmd(["apt-mark", "hold", "tailscale"]))
    if installing or (soft(["systemctl", "is-active", "tailscaled"]) or "").strip() != "active":
        steps.append(Cmd(["systemctl", "enable", "--now", "tailscaled"]))
    execute(Plan("Install Tailscale", steps))


def ssh_over_tailnet() -> bool:
    try:
        return ipaddress.ip_address(os.environ.get("SSH_CONNECTION", "").split()[0]) in ipaddress.ip_network("100.64.0.0/10")
    except (IndexError, ValueError):
        return False


def node_registered_as(host: str) -> bool:
    st = tailscale_state() or {}
    got = st.get("hostname")
    if got == host:
        typer.echo(f"Registered as '{got}'.")
        return True
    fail(f"registered as '{got}', not '{host}': a stale node named '{host}' exists. Remove it (node-admin -> list-devices, then remove-device), "
         "run `tailscale logout` here and join again")
    return False


JOIN_CHECKLIST = """Next, from an admin device:
  - node-admin -> list-devices: the node is online, tagged tag:pve, no key expiry
  - https://status.ts.<domain>: the proxmox probe turns green within a few minutes
  - ssh root@<tailscale ip> works (after SSH hardening)"""


def tailscale_join() -> None:
    """Join the tailnet as the node the edge expects (name pve, DNS and routes off)."""
    st = tailscale_state()
    if st is None and not STATE["dry_run"]:
        raise typer.Exit(fail("tailscale is not installed; run tailscale-install first"))
    login = typer.prompt("Control server URL, e.g. https://hs.<domain>")
    host = ask_name("Node name on the tailnet", NODE_NAME)
    if host != NODE_NAME:
        warn(f"the edge's Gatus probe (tcp://{NODE_NAME}.ts.<domain>:8006) and heartbeat expect the name '{NODE_NAME}'")
    dns = typer.confirm("Let Tailscale manage DNS? Leave off: it rewrites /etc/resolv.conf, and apt and the guests depend on plain LAN DNS", default=False)
    running = bool(st and st["backend"] == "Running")
    if running and st["hostname"] == host and st["control"] == login.rstrip("/") and st["dns"] == dns:  # type: ignore[index]
        typer.echo(f"Already joined as '{host}' to {login} (tags: {', '.join(st['tags']) or 'none'}).")  # type: ignore[index]
        execute(Plan("Join the tailnet", []))
        return
    if running:
        warn(f"already joined as '{st['hostname']}' to {st['control']}; continuing re-authenticates it with the settings above")  # type: ignore[index]
        if ssh_over_tailnet() and not STATE["dry_run"] and not typer.confirm(
                "This SSH session runs over the tailnet and re-authenticating can cut it. Continue?", default=False):
            typer.echo("Cancelled. Run this at the console or over the LAN.")
            return
    key = typer.prompt("Pre-auth key carrying the node's tag (leave empty to log in with a URL instead)", hide_input=True, default="",
                       show_default=False)
    keyfile = "/run/pve-setup-authkey"
    flags = [*(["--reset", "--force-reauth"] if running else []), "--login-server", login, "--hostname", host,
             f"--accept-dns={str(dns).lower()}", "--accept-routes=false"]
    steps: list[Step] = []
    if key:
        steps.append(Write(keyfile, key + "\n", 0o600, secret=True))
        argv = ["tailscale", "up", *flags, "--auth-key", f"file:{keyfile}"]
    else:
        argv = ["tailscale", "up", *flags, "--timeout", "15m"]
        typer.echo("tailscale up will print a login URL; approve it with node-admin -> approve-device (user david_admin, tags pve).")
    steps += [Cmd(argv, shlex.join(argv)), Do(f"check that the node registered as '{host}', not '{host}-1'", lambda: node_registered_as(host)),
              Cmd(["tailscale", "ip", "-4"]), Cmd(["tailscale", "ping", "--c", "3", "--timeout", "10s", "edge"]), Cmd(["tailscale", "status"])]
    if execute(Plan("Join the tailnet", steps, cleanup=[keyfile])) and not STATE["dry_run"]:
        typer.echo(JOIN_CHECKLIST)


def ask_edge_ip() -> str:
    while True:
        v = typer.prompt("Edge tailnet IPv4, from `ssh david_admin@edge tailscale ip -4` (needed while Tailscale DNS is off; empty = use DNS)",
                         default="", show_default=False).strip()
        try:
            if not v or ipaddress.ip_address(v) in ipaddress.ip_network("100.64.0.0/10"):
                return v
        except ValueError:
            pass
        typer.echo("  a tailnet address in 100.64.0.0/10")


HEARTBEAT_SCRIPT_TEXT = """#!/usr/bin/env bash
# Pushes Proxmox health to Gatus; written by pve-setup.
set -u
. /etc/pve-heartbeat.env
ok=true
why=""
zpool status -x 2>/dev/null | grep -q 'all pools are healthy' || { ok=false; why="zpool"; }
smartctl -H "$NVME" 2>/dev/null | grep -q PASSED || { ok=false; why="$why smart"; }
cap=$(zpool list -H -o capacity "$POOL" 2>/dev/null | tr -dc 0-9)
if [ -z "$cap" ]; then ok=false; why="$why pool-unreadable"; elif [ "$cap" -ge 85 ]; then ok=false; why="$why pool-full"; fi
args=(--url-query "success=$ok")
[ "$ok" = true ] || args+=(--url-query "error=${why# }")
exec curl -fsS --max-time 15 -X POST -K /etc/pve-heartbeat.curl "${args[@]}" "https://$STATUS_HOST/api/v1/endpoints/$ENDPOINT/external" >/dev/null
"""


def heartbeat_files(host: str, endpoint: str, seconds: int, token: str, edge_ip: str = "", pool: str = "rpool",
                    nvme: str = "/dev/nvme0") -> dict[str, tuple[str, int, bool]]:
    unit = """[Unit]
Description=Push Proxmox health to Gatus
After=tailscaled.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/pve-heartbeat
"""
    timer = f"""[Unit]
Description=Proxmox heartbeat every {seconds}s

[Timer]
OnBootSec={seconds}
OnUnitActiveSec={seconds}
AccuracySec=5s

[Install]
WantedBy=timers.target
"""
    env = f"STATUS_HOST={shlex.quote(host)}\nENDPOINT={shlex.quote(endpoint)}\nPOOL={shlex.quote(pool)}\nNVME={shlex.quote(nvme)}\n"
    curl = f'header = "Authorization: Bearer {token}"\n' + (f'resolve = "{host}:443:{edge_ip}"\n' if edge_ip else "")
    return {HB_CURL: (curl, 0o600, True), HB_ENV: (env, 0o644, False), HB_SCRIPT: (HEARTBEAT_SCRIPT_TEXT, 0o750, False),
            HB_UNIT: (unit, 0o644, False), HB_TIMER: (timer, 0o644, False)}


def heartbeat() -> None:
    """Install the guide's Proxmox heartbeat: health checks pushed to Gatus every minute."""
    host = typer.prompt("Gatus host, e.g. status.ts.<domain>")
    endpoint = typer.prompt("Gatus external endpoint key (<group>_<name>)", default="home_proxmox-heartbeat")
    seconds = ask_int("Push every N seconds", 10, 3600, 60)
    pool = ask_name("ZFS pool to watch", root_pool() or "rpool")
    nvme = typer.prompt("NVMe device for the SMART check", default="/dev/nvme0")
    if not re.fullmatch(r"/dev/[A-Za-z0-9/_-]+", nvme):
        raise typer.Exit(fail("the device must look like /dev/nvme0"))
    token = typer.prompt("Heartbeat token", hide_input=True)
    edge_ip = ask_edge_ip()
    if not STATE["dry_run"]:
        if not shutil.which("smartctl"):
            warn("smartctl is not installed (apt install smartmontools); every push would report 'smart'")
        if not edge_ip and subprocess.run(["getent", "hosts", host], capture_output=True, check=False).returncode != 0:
            warn(f"{host} does not resolve here and no edge IP was given; the timer will fail")
    wanted = heartbeat_files(host, endpoint, seconds, token, edge_ip, pool, nvme)
    differing = [p for p, (content, _, _) in wanted.items() if read_text(p) != content]
    state = heartbeat_state()
    steps: list[Step] = [Write(p, wanted[p][0], wanted[p][1], wanted[p][2]) for p in differing]
    if any(p.endswith((".service", ".timer")) for p in differing):
        steps.append(Cmd(["systemctl", "daemon-reload"]))
    if differing or state["enabled"] != "enabled" or state["active"] != "active":
        steps += [Cmd(["systemctl", "enable", "--now", "pve-heartbeat.timer"]), Cmd(["systemctl", "start", "pve-heartbeat.service"]),
                  Cmd(["systemctl", "status", "--no-pager", "pve-heartbeat.service"])]
    if state["legacy"] or state["legacy_active"]:
        typer.echo("The earlier edge-heartbeat timer is replaced; it is removed after the new one has pushed once.")
        steps += legacy_heartbeat_removal(state)
    if differing:
        typer.echo("Changed files: " + ", ".join(differing) + " (secret contents are never shown)")
    execute(Plan("Install the heartbeat timer", steps))


def legacy_heartbeat_removal(state: dict) -> list[Step]:
    steps: list[Step] = [Cmd(["systemctl", "disable", "--now", "edge-heartbeat.timer"])] if state["legacy_active"] else []
    steps += [Remove(p) for p in state["legacy"]]
    return [*steps, Cmd(["systemctl", "daemon-reload"])]


# ---------- guests ----------
def vm_template() -> None:
    """Turn a checksum-verified cloud image into a cloud-init VM template."""
    if pve_version() < (8, 1):
        raise typer.Exit(fail("import-from needs Proxmox VE 8.1 or newer"))
    vmid = free_id("Template VM")
    name = ask_name("Template name", "tpl-cloud")
    if not warn_same_name(name, "template/guest"):
        return
    storage = choose("Disk storage", storages("images"))
    bridge = choose("Bridge", bridges())
    src = typer.prompt("Cloud image: https URL or local path")
    sha = ask_sha256()
    cores = ask_int("Cores", 1, 128, 2)
    mem = ask_int("Memory (MiB)", 256, 1 << 20, 2048)
    size = ask_size("Disk size after resize", "32G")
    user = ask_name("Cloud-init user", "ops")
    key = ask_pubkey_file()
    if src.startswith("https://"):
        image = f"/var/tmp/pve-setup-{vmid}.img"
        get: Fetch | Verify = Fetch(src, image, sha)
    else:
        image = str(Path(src).expanduser())
        get = Verify(image, sha)
    destroy = Cmd(["qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1"])
    execute(Plan(f"Create template {vmid} ({name})", [
        get,
        Cmd(["qm", "create", vmid, "--name", name, "--memory", str(mem), "--cores", str(cores), "--net0", f"virtio,bridge={bridge}",
             "--scsihw", "virtio-scsi-single", "--ostype", "l26", "--agent", "enabled=1", "--serial0", "socket", "--vga", "serial0",
             "--tags", TAG], undo=destroy, undo_on_fail=True),
        Cmd(["qm", "set", vmid, "--scsi0", f"{storage}:0,import-from={image},discard=on,ssd=1"]),
        Cmd(["qm", "set", vmid, "--ide2", f"{storage}:cloudinit", "--boot", "order=scsi0", "--ciuser", user, "--sshkeys", key,
             "--ipconfig0", "ip=dhcp"]),
        Cmd(["qm", "resize", vmid, "scsi0", size]),
        Cmd(["qm", "template", vmid]),
    ], cleanup=[image] if src.startswith("https://") else []))


def vm_create() -> None:
    """Clone a VM from a template."""
    tpls = [(str(g["vmid"]), f"{g['vmid']} {g['name']}") for g in guests() if g["template"] and g["type"] == "qemu"]
    tpl = choose("Template", tpls)
    vmid = free_id("New VM")
    name = ask_name("VM name (use the tailnet hostname you want)")
    if not warn_same_name(name, "guest"):
        return
    storage = choose("Target storage", storages("images"))
    cores = ask_int("Cores", 1, 128, 2)
    mem = ask_int("Memory (MiB)", 256, 1 << 20, 4096)
    size = ask_size("Disk size", "40G")
    ipcfg = ip_config()
    tags = typer.prompt("Extra Proxmox tags (semicolon-separated)", default="")
    start = typer.confirm("Start at boot and start now?", default=True)
    setcmd = ["qm", "set", vmid, "--cores", str(cores), "--memory", str(mem), "--ipconfig0", ipcfg, "--tags", with_tag(tags)]
    if start:
        setcmd += ["--onboot", "1"]
    destroy = Cmd(["qm", "destroy", vmid, "--purge", "1", "--destroy-unreferenced-disks", "1"])
    steps: list[Step] = [
        Cmd(["qm", "clone", tpl, vmid, "--name", name, "--full", "1", "--storage", storage], undo=destroy, undo_on_fail=True),
        Cmd(setcmd), Cmd(["qm", "resize", vmid, "scsi0", size])]
    if start:
        steps.append(Cmd(["qm", "start", vmid], undo=Cmd(["qm", "stop", vmid])))
    execute(Plan(f"Create VM {vmid} ({name})", steps))


def lxc_create() -> None:
    """Create an unprivileged LXC container from a downloaded template."""
    tpl_storage = choose("Template storage", storages("vztmpl"))
    out = capture(["pveam", "list", tpl_storage]).splitlines()[1:]
    tpls = [(l.split()[0], l.split()[0]) for l in out if l.strip()]
    if not tpls:
        typer.echo("No templates downloaded yet. Run `pveam update` and `pveam available --section system`, then `pveam download`.")
        raise typer.Exit(1)
    tpl = choose("Template", tpls)
    ctid = free_id("Container")
    host = ask_name("Hostname (use the tailnet hostname you want)")
    if not warn_same_name(host, "guest"):
        return
    storage = choose("Root disk storage", storages("rootdir"))
    size = ask_size("Root disk size", "16G")
    cores = ask_int("Cores", 1, 128, 2)
    mem = ask_int("Memory (MiB)", 128, 1 << 20, 2048)
    bridge = choose("Bridge", bridges())
    ipcfg = ip_config()
    key = ask_pubkey_file()
    docker = typer.confirm("Enable nesting+keyctl (needed to run Docker inside; weakens isolation)?", default=False)
    cmd = ["pct", "create", ctid, tpl, "--hostname", host, "--cores", str(cores), "--memory", str(mem),
           "--rootfs", f"{storage}:{size.rstrip('G')}", "--net0", f"name=eth0,bridge={bridge},{ipcfg}", "--unprivileged", "1",
           "--ssh-public-keys", key, "--onboot", "1", "--tags", TAG]
    if docker:
        cmd += ["--features", "nesting=1,keyctl=1"]
    destroy = Cmd(["pct", "destroy", ctid, "--purge", "1", "--destroy-unreferenced-disks", "1"])
    execute(Plan(f"Create container {ctid} ({host})", [Cmd(cmd, undo=destroy, undo_on_fail=True),
                                                       Cmd(["pct", "start", ctid], undo=Cmd(["pct", "stop", ctid]))]))


# ---------- storage, backups, access ----------
def storage_add() -> None:
    """Add directory or NFS storage."""
    kind = choose("Type", [("dir", "Directory on this host"), ("nfs", "NFS export")])
    sid = ask_name("Storage ID")
    content = typer.prompt("Content types", default="backup" if kind == "nfs" else "backup,iso,vztmpl,snippets")
    if kind == "dir":
        path = typer.prompt("Absolute path (must exist and be mounted)")
        want = {"type": "dir", "path": path}
        cmd = ["pvesm", "add", "dir", sid, "--path", path, "--content", content]
    else:
        server = typer.prompt("NFS server")
        export = typer.prompt("Export path")
        want = {"type": "nfs", "server": server, "export": export}
        cmd = ["pvesm", "add", "nfs", sid, "--server", server, "--export", export, "--content", content]
    have = storage_config(sid)
    if have:
        same = all(str(have.get(k)) == v for k, v in want.items()) and set(str(have.get("content", "")).split(",")) == set(content.split(","))
        if not same:
            raise typer.Exit(fail(f"storage '{sid}' exists with different settings ({json.dumps(have)}); run storage-remove first"))
        typer.echo(f"Storage '{sid}' already exists with these settings.")
        execute(Plan(f"Add storage {sid}", []))
        return
    execute(Plan(f"Add storage {sid}", [Cmd(cmd, undo=Cmd(["pvesm", "remove", sid]), undo_on_fail=True), Cmd(["pvesm", "status"])]))


def _norm_prune(value: object) -> list[str]:
    return sorted(str(value or "").split(","))


def backup_job() -> None:
    """Create or update a scheduled vzdump job."""
    name = ask_name("Job name (the job id becomes pve-setup-<name>)")
    jid = JOB_PREFIX + name
    storage = choose("Backup storage", storages("backup"))
    schedule = typer.prompt("Schedule (systemd calendar, e.g. 'sat 02:30' or '03:30')")
    scope = choose("Guests", [("all", "All guests"), ("list", "Specific IDs")])
    ids = typer.prompt("Comma-separated IDs") if scope == "list" else ""
    mode = choose("Mode", [("snapshot", "snapshot"), ("suspend", "suspend"), ("stop", "stop")])
    keep = typer.prompt("Retention (prune-backups)", default="keep-daily=7,keep-weekly=4")
    common = ["--schedule", schedule, "--storage", storage, "--mode", mode, "--compress", "zstd", "--prune-backups", keep,
              "--enabled", "1", "--notes-template", "{{guestname}}", "--comment", TAG]
    scope_args = ["--all", "1"] if scope == "all" else ["--vmid", ids]
    have = next((j for j in backup_jobs() if j.get("id") == jid), None)
    if have is None:
        steps: list[Step] = [Cmd(["pvesh", "create", "/cluster/backup", "--id", jid, *common, *scope_args],
                                 undo=Cmd(["pvesh", "delete", f"/cluster/backup/{jid}"]), undo_on_fail=True)]
    else:
        same = (have.get("schedule") == schedule and have.get("storage") == storage and have.get("mode") == mode
                and _norm_prune(have.get("prune-backups")) == _norm_prune(keep) and str(have.get("enabled", 1)) == "1"
                and ((scope == "all" and str(have.get("all")) == "1") or (scope == "list" and sorted(str(have.get("vmid", "")).split(",")) == sorted(ids.split(",")))))
        if same:
            typer.echo(f"Job {jid} already exists with these settings.")
            steps = []
        else:
            drop = ["--delete", "vmid"] if scope == "all" and have.get("vmid") else ["--delete", "all"] if scope == "list" and have.get("all") else []
            warn(f"job {jid} exists with other settings; this updates it in place")
            steps = [Cmd(["pvesh", "set", f"/cluster/backup/{jid}", *common, *scope_args, *drop])]
    if steps:
        steps.append(Cmd(["pvesh", "get", "/cluster/backup"]))
    execute(Plan(f"Create or update backup job {jid}", steps))


def api_token() -> None:
    """Create a PVE user and a privilege-separated API token with a role you pick."""
    user = typer.prompt("User ID, e.g. automation@pve")
    token = ask_name("Token name")
    role = choose("Role", [("PVEAuditor", "PVEAuditor: read-only"), ("PVEVMAdmin", "PVEVMAdmin: manage guests"),
                           ("PVEDatastoreUser", "PVEDatastoreUser: use storage")])
    path = typer.prompt("ACL path", default="/vms" if role == "PVEVMAdmin" else "/")
    users = {u.get("userid"): u for u in api_users()}
    tokens = {t.get("tokenid") for t in (pvesh_soft(f"/access/users/{user}/token") or [])} if user in users else set()  # type: ignore[union-attr]
    acls = pvesh_soft("/access/acl") or []
    has_acl = any(a.get("ugid") == f"{user}!{token}" and a.get("path") == path and a.get("roleid") == role for a in acls)  # type: ignore[union-attr]
    steps: list[Step] = []
    if user not in users:
        steps.append(Cmd(["pveum", "user", "add", user, "--comment", USER_COMMENT], undo=Cmd(["pveum", "user", "delete", user])))
    else:
        typer.echo(f"User {user} already exists.")
    if token in tokens:
        warn(f"token {user}!{token} already exists; its secret cannot be shown again. Run api-token-revoke first to rotate it")
    else:
        typer.secho("The token secret is printed once, by pveum, straight to this terminal. It is not logged.", fg=typer.colors.YELLOW)
        steps.append(Cmd(["pveum", "user", "token", "add", user, token, "--privsep", "1"],
                         undo=Cmd(["pveum", "user", "token", "remove", user, token]), undo_on_fail=True))
    if not has_acl:
        steps.append(Cmd(["pveum", "acl", "modify", path, "--tokens", f"{user}!{token}", "--roles", role]))
    execute(Plan(f"Create {user}!{token}", steps))


# ---------- ssh hardening and the remote-readiness gate ----------
KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)"
                    r" ([A-Za-z0-9+/]+={0,3})(?: (.*))?$")


def key_ident(line: str) -> tuple[str, str] | None:
    m = KEY_RE.match(line.strip())
    return (m[1], m[2]) if m else None


def key_fingerprint(line: str) -> str | None:
    try:
        r = subprocess.run(["ssh-keygen", "-lf", "-"], input=line.strip() + "\n", capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def fetch_key_text(url: str) -> str:
    if not url.startswith("https://"):
        raise ValueError("only https:// URLs are fetched")
    with urllib.request.urlopen(url, timeout=15) as r:  # noqa: S310  # https only, checked above
        data = r.read(65537)
    if len(data) > 65536:
        raise ValueError("the response is larger than 64 KiB")
    return data.decode()


def collect_keys() -> list[dict]:
    how = choose("Where are your public keys?", [("url", "A URL, e.g. https://github.com/<you>.keys"), ("file", "A file"), ("paste", "Paste lines")])
    try:
        if how == "url":
            text = fetch_key_text(typer.prompt("URL"))
        elif how == "file":
            text = Path(typer.prompt("File")).expanduser().read_text()
        else:
            lines = []
            while (l := typer.prompt("Public key line (empty to finish)", default="", show_default=False).strip()):
                lines.append(l)
            text = "\n".join(lines)
    except (OSError, ValueError) as e:
        raise typer.Exit(fail(f"cannot read the keys: {e}")) from None
    keys: dict[tuple[str, str], dict] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        ident = key_ident(line)
        fp = key_fingerprint(line) if ident else None
        if not ident or not fp:
            warn(f"skipping a line that is not a plain public key: {line[:40]}")
            continue
        keys.setdefault(ident, {"line": line, "ident": ident, "fp": fp})
    return list(keys.values())


def existing_key_idents() -> set[tuple[str, str]]:
    return {i for i in (key_ident(l) for l in (read_text(AUTHORIZED_KEYS) or "").splitlines()) if i}


def sshd_effective() -> dict[str, str] | None:
    out = soft(["sshd", "-T", "-f", SSHD_CONFIG])
    if not out:
        return None
    eff: dict[str, str] = {}
    for line in out.splitlines():
        k, _, v = line.partition(" ")
        eff.setdefault(k.lower(), v.strip().lower())
    return eff


def ssh_is_hardened(eff: dict[str, str] | None) -> bool:
    # sshd -T prints the old spelling "without-password" for prohibit-password
    return bool(eff) and eff.get("passwordauthentication") == "no" and eff.get("kbdinteractiveauthentication") == "no" \
        and eff.get("permitrootlogin") in ("prohibit-password", "without-password") and eff.get("allowagentforwarding") == "no"


def ssh_service() -> str:
    return "ssh" if soft(["systemctl", "cat", "ssh.service"]) is not None else "sshd"


def revert_armed() -> bool:
    return soft(["systemctl", "is-active", f"{SSH_REVERT_UNIT}.timer"]) is not None


def lan_address() -> str:
    for tok in (soft(["hostname", "-I"]) or "").split():
        try:
            ip = ipaddress.ip_address(tok)
        except ValueError:
            continue
        if ip.version == 4 and not ip.is_loopback and ip not in ipaddress.ip_network("100.64.0.0/10"):
            return tok
    return ""


def tailnet_address() -> str:
    return ((soft(["tailscale", "ip", "-4"]) or "").split() or [""])[0]


def validate_or_remove() -> bool:
    if subprocess.run(["sshd", "-t", "-f", SSHD_CONFIG], check=False).returncode == 0:
        return True
    if os.path.lexists(SSH_DROPIN):
        os.unlink(SSH_DROPIN)
    fail("sshd -t rejected the configuration; the drop-in was removed again so the next sshd start cannot fail")
    return False


def verify_effective() -> bool:
    eff = sshd_effective()
    if ssh_is_hardened(eff):
        typer.echo("sshd -T confirms: password and keyboard-interactive logins off, root by key only, agent forwarding off.")
        return True
    earlier = sorted(f for f in Path(SSH_DROPIN).parent.glob("*.conf") if f.name < Path(SSH_DROPIN).name) if Path(SSH_DROPIN).parent.is_dir() else []
    fail("sshd -T does not show the hardened settings" + (f"; the first value wins, so check these earlier drop-ins: {', '.join(map(str, earlier))}" if earlier
                                                           else "; check `sshd -T | grep -i -e passwordauth -e permitroot`"))
    return False


def remove_dropin_and_reload(svc: str) -> bool:
    if os.path.lexists(SSH_DROPIN):
        os.unlink(SSH_DROPIN)
    return subprocess.run(["systemctl", "reload", svc], check=False).returncode == 0


PROOF_SSH = "ssh -o IdentitiesOnly=yes -i <public key file>"


def ssh_harden() -> None:
    """Install your SSH key, then disable password SSH (drop-in, automatic revert, effective-config check)."""
    have = existing_key_idents()
    typer.echo(f"{AUTHORIZED_KEYS}: {len(have)} key(s).")
    if not have or typer.confirm("Add more keys?", default=False):
        found = collect_keys()
        new = [k for k in found if k["ident"] not in have]
        for k in found:
            typer.echo(f"  {'new    ' if k in new else 'present'} {k['fp']}")
        if found:
            typer.echo("Compare each fingerprint with the one 1Password (or `ssh-keygen -lf <key>.pub`) shows for that key.")
        if len(new) > 1:
            typer.echo("Several keys are new (`ssh-add -L` lists every key the agent holds). Choose which to install:")
            new = [k for k in new if typer.confirm(f"  Install {k['fp']}?", default=False)]
        if not execute(Plan("Add authorized keys", [Append(AUTHORIZED_KEYS, [k["line"] for k in new])] if new else [])):
            return
        have |= {k["ident"] for k in new}
    if not have and not STATE["dry_run"]:
        raise typer.Exit(fail("no key in authorized_keys; refusing to disable passwords"))
    lan, ts = lan_address(), tailnet_address()
    if not ts and not STATE["dry_run"]:
        raise typer.Exit(fail("the host is not on the tailnet yet (run tailscale-join); the guide requires proving login over both paths"))
    typer.echo(f"\nBefore passwords are disabled, open NEW terminals on your laptop:\n  {PROOF_SSH} root@{lan or '<LAN IP>'}\n  {PROOF_SSH} root@{ts or '<TAILNET IP>'}\n"
               "Both must log in without a password. Keep this console logged in.\n"
               "With an SSH agent (1Password) that holds many keys, ssh offers them all and sshd stops after about 6 tries ('Too many authentication\n"
               "failures'); IdentitiesOnly with the public key file makes ssh offer only that key, and the agent still signs.")
    if not confirm_typed("proven", "Password logins are disabled next."):
        typer.echo("Cancelled.")
        return
    eff = sshd_effective()
    if read_text(SSH_DROPIN) == SSH_DROPIN_TEXT and ssh_is_hardened(eff):
        typer.echo("Password SSH is already disabled by the drop-in.")
        execute(Plan("Disable password SSH", []))
        return
    svc = ssh_service()
    steps: list[Step] = [
        Write(SSH_DROPIN, SSH_DROPIN_TEXT, undo=Do("remove the drop-in and reload sshd", lambda: remove_dropin_and_reload(svc))),
        Do("validate with sshd -t; the drop-in is removed again if it fails", validate_or_remove)]
    if not revert_armed() and typer.confirm("Arm an automatic revert in 10 minutes, cancelled by ssh-harden-keep once both logins are proven?", default=True):
        steps.append(Cmd(["systemd-run", "--unit", SSH_REVERT_UNIT, "--on-active=10m", "/bin/sh", "-c", f"rm -f {SSH_DROPIN} && systemctl reload {svc}"]))
    steps += [Cmd(["systemctl", "reload", svc]), Do("check the effective settings with sshd -T", verify_effective)]
    if execute(Plan("Disable password SSH", steps)) and not STATE["dry_run"]:
        typer.echo(f"\nNow, in fresh terminals: {PROOF_SSH} root@{lan or '<LAN IP>'} and the same for {ts or '<TAILNET IP>'} must still log in, and\n"
                   f"  ssh -o PubkeyAuthentication=no root@{ts or '<TAILNET IP>'}   must fail with 'Permission denied (publickey)'.\n"
                   "Then run ssh-harden-keep to cancel the automatic revert. Keep the root password: the web UI and the console use it.")


def ssh_harden_keep() -> None:
    """Cancel the automatic SSH revert once both logins are proven."""
    if not revert_armed():
        typer.echo("No automatic revert is armed.")
        return
    if not confirm_typed("proven", "This keeps password SSH disabled for good."):
        typer.echo("Cancelled.")
        return
    execute(Plan("Keep the SSH hardening", [Cmd(["systemctl", "stop", f"{SSH_REVERT_UNIT}.timer"])]))


def ssh_harden_remove() -> None:
    """Remove the SSH hardening drop-in and reload sshd (keys are left alone)."""
    if read_text(SSH_DROPIN) is None:
        typer.echo("The hardening drop-in is not installed.")
        return
    if not confirm_typed("ssh", "Password logins become possible again if nothing else in sshd_config forbids them."):
        typer.echo("Cancelled.")
        return
    steps: list[Step] = [Remove(SSH_DROPIN), Cmd(["systemctl", "reload", ssh_service()])]
    if revert_armed():
        steps.append(Cmd(["systemctl", "stop", f"{SSH_REVERT_UNIT}.timer"]))
    execute(Plan("Remove the SSH hardening", steps))


def pinned_kernel() -> str | None:
    lines = [l.strip() for l in (soft(["proxmox-boot-tool", "kernel", "list"]) or "").splitlines()]
    for i, l in enumerate(lines):
        if l.lower().startswith("pinned kernel"):
            for nxt in lines[i + 1:]:
                if nxt:
                    return None if nxt.lower().startswith("none") else nxt
    return None


def running_kernel() -> str:
    return os.uname().release


def listening_ports() -> set[int]:
    ports = set()
    for line in (soft(["ss", "-ltnH"]) or "").splitlines():
        cols = line.split()
        if len(cols) >= 4 and cols[3].rsplit(":", 1)[-1].isdigit():
            ports.add(int(cols[3].rsplit(":", 1)[-1]))
    return ports


def zfs_arc_max() -> int | None:
    try:
        return int(Path("/sys/module/zfs/parameters/zfs_arc_max").read_text())
    except (OSError, ValueError):
        return None


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def gate_path() -> Path:
    return LOG.with_name(".pve-setup-gate.json")


def gate_checks() -> list[tuple[str, bool, str]]:
    eff = sshd_effective()
    keys = len(existing_key_idents())
    ts = tailscale_state() or {}
    pin, run = pinned_kernel(), running_kernel()
    arc = zfs_arc_max()
    ports = listening_ports()
    return [
        ("password SSH disabled (sshd -T)", ssh_is_hardened(eff), ", ".join(f"{k}={v}" for k, v in (eff or {}).items() if k in (
            "passwordauthentication", "kbdinteractiveauthentication", "permitrootlogin", "allowagentforwarding")) or "sshd -T unavailable"),
        ("at least two authorized keys (one independent of 1Password)", keys >= 2, f"{keys} key(s)"),
        ("SSH listening on 22", 22 in ports, ""),
        ("web UI listening on 8006", 8006 in ports, ""),
        (f"tailscale up as {NODE_NAME} with tag:pve", ts.get("backend") == "Running" and ts.get("hostname") == NODE_NAME and "tag:pve" in ts.get("tags", []),
         f"{ts.get('backend')}, {ts.get('hostname')}, {','.join(ts.get('tags', []))}"),
        ("running kernel is the pinned one", bool(pin) and pin == run, f"pinned {pin}, running {run}"),
        ("ZFS ARC cap is 3 GiB", arc == ARC_MAX_BYTES, f"zfs_arc_max={arc}"),
    ]


def record_gate(passing: bool) -> dict:
    try:
        data = json.loads(gate_path().read_text())
    except (OSError, json.JSONDecodeError):
        data = {"passing_boots": [], "first_pass_at": None}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if passing and not STATE["dry_run"]:
        if boot_id() not in [b["boot_id"] for b in data["passing_boots"]]:
            data["passing_boots"].append({"boot_id": boot_id(), "at": now})
        data["first_pass_at"] = data["first_pass_at"] or now
        write_file(Write(str(gate_path()), json.dumps(data, indent=1), 0o600))
    return data


GATE_MANUAL = """Still yours to do (the host cannot check these):
  - off the LAN (laptop on the phone's hotspot with Wi-Fi off and Tailscale connected, or the phone's own SSH app with its own key):
    ssh root@{ts} logs in with your key. The 1Password SSH agent exists on desktops only, so a phone needs a separate key.
  - the same device: https://{ts}:8006 loads and accepts root@pam
  - after a reboot: SSH and the web UI are back within about 3 minutes, and node-admin -> list-devices shows pve online
  - pull the power cable, wait 30 s, plug it back in without pressing the button: the node boots and passes the SSH check
  - record in your password manager: static LAN IP, tailnet IP, hostname, pinned kernel, root password, BIOS photos"""


def remote_gate_impl(reboot: bool) -> bool:
    checks = gate_checks()
    ok = all(c[1] for c in checks)
    typer.secho("\n== Remote-readiness gate (host side)", bold=True)
    for name, passed, detail in checks:
        typer.echo(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    data = record_gate(ok)
    boots = len(data["passing_boots"])
    typer.echo(f"\nPassing boots recorded: {boots}. Clean reboot with a passing gate afterwards: {'yes' if boots >= 2 else 'not yet'}.")
    if data["first_pass_at"]:
        hours = (datetime.now(timezone.utc) - datetime.fromisoformat(data["first_pass_at"])).total_seconds() / 3600
        typer.echo(f"First pass {data['first_pass_at']}: " + (f"{hours:.0f} h ago. The 48 h soak is complete and the gate passes now." if hours >= 48 and ok
                                                              else f"{hours:.0f} h of the 48 h soak so far; run this again after it."))
    typer.echo(GATE_MANUAL.format(ts=tailnet_address() or "<tailnet IP>"))
    if reboot:
        if not ok:
            fail("the gate must pass before a reboot test; fix the FAIL lines first")
            return False
        typer.echo("\nFrom your phone (Wi-Fi off), SSH and the web UI must be back within about 3 minutes. Then run remote-gate again.")
        if not confirm_typed("reboot", "The host reboots now."):
            typer.echo("Cancelled.")
            return ok
        execute(Plan("Reboot for the remote-readiness test", [Cmd(["systemctl", "reboot"])]))
    return ok


def remote_gate_menu() -> None:
    remote_gate_impl(False)


def remote_gate(reboot: bool = typer.Option(False, "--reboot", help="After a passing gate, record it and reboot for the clean-reboot test")) -> None:
    """Run the host-side remote-readiness checks and keep a baseline across reboots."""
    if not remote_gate_impl(reboot):
        raise typer.Exit(1)


# ---------- host configuration (guide section 8) ----------
def root_dataset() -> str | None:
    src = (soft(["findmnt", "-no", "SOURCE", "/"]) or "").strip()
    if "/" in src and soft(["zfs", "list", "-H", "-o", "name", src]) is not None:
        return src
    return None


def root_pool() -> str | None:
    ds = root_dataset()
    return ds.split("/")[0] if ds else None


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def snapshot_step(label: str) -> Step:
    """A pre-change snapshot of the root dataset, with its own undo."""
    ds = root_dataset() or "<root dataset>"
    name = f"{ds}@{SNAP_PREFIX}{label}-{utc_stamp()}"
    return Cmd(["zfs", "snapshot", name], undo=Cmd(["zfs", "destroy", name]))


def toolkit_snapshots() -> list[tuple[str, int]]:
    ds = root_dataset()
    if not ds:
        return []
    out = soft(["zfs", "list", "-H", "-p", "-t", "snapshot", "-o", "name,creation", "-s", "creation", "-d", "1", ds]) or ""
    rows = []
    for line in out.splitlines():
        name, _, created = line.partition("\t")
        if f"@{SNAP_PREFIX}" in name and created.strip().isdigit():
            rows.append((name, int(created)))
    return rows


ROLLBACK_NOTE = ("A snapshot of the running root dataset cannot be rolled back on the live system: boot a rescue environment "
                 "(this needs the KVM or the console) and run `zfs rollback <snapshot>`.")


def snapshot() -> None:
    """Take a pre-change snapshot of the root dataset (the guide's 8.1 habit)."""
    if not root_dataset() and not STATE["dry_run"]:
        raise typer.Exit(fail("the root filesystem is not ZFS (findmnt/zfs found no dataset)"))
    label = ask_name("What is about to change? (a short label)", "change")
    typer.echo(ROLLBACK_NOTE)
    execute(Plan(f"Snapshot the root dataset before '{label}'", [snapshot_step(label)]))


def snapshot_prune() -> None:
    """Delete this toolkit's old snapshots, keeping the newest N."""
    rows = toolkit_snapshots()
    if not rows:
        typer.echo(f"No snapshots named @{SNAP_PREFIX}* on the root dataset.")
        return
    keep = ask_int("How many of the newest to keep", 1, 1000, 10)
    old = [n for n, _ in rows[:-keep]]
    if not old:
        typer.echo(f"{len(rows)} snapshot(s); nothing older than the newest {keep}.")
        return
    for n in old:
        typer.echo(f"  would destroy {n}")
    if not confirm_typed("prune", f"{len(old)} snapshot(s) are destroyed; the changes they protect can no longer be rolled back."):
        typer.echo("Cancelled.")
        return
    execute(Plan("Prune old snapshots", [Cmd(["zfs", "destroy", n]) for n in old]))


def zfs_value(args: list[str]) -> str | None:
    out = (soft(["zfs", "get", "-Hp", "-o", "value", *args]) or "").strip()
    return out or None


def size_bytes(size: str) -> int:
    return int(size[:-1]) * (2**30 if size[-1] == "G" else 2**20)


def scrub_scheduled(pool: str) -> bool:
    return os.path.exists("/etc/cron.d/zfsutils-linux") or (soft(["systemctl", "is-enabled", f"zfs-scrub-monthly@{pool}.timer"]) or "").strip() == "enabled"


def zfs_reserve() -> None:
    """Enable autotrim, hold back a reserve so a full pool never wedges the host, and check scrubbing."""
    pool = root_pool()
    if not pool and not STATE["dry_run"]:
        raise typer.Exit(fail("the root filesystem is not ZFS (findmnt/zfs found no dataset)"))
    pool = pool or "rpool"
    size = ask_size("Reserve size (the guide: 40G keeps free space above 10% of the 1 TB drive)", "40G")
    dataset = f"{pool}/{RESERVE_DATASET_NAME}"
    steps: list[Step] = []
    if (soft(["zpool", "get", "-H", "-o", "value", "autotrim", pool]) or "").strip() != "on":
        steps.append(Cmd(["zpool", "set", "autotrim=on", pool], undo=Cmd(["zpool", "set", "autotrim=off", pool])))
    have = zfs_value(["refreservation", dataset])
    if soft(["zfs", "list", "-H", "-o", "name", dataset]) is None:
        steps.append(Cmd(["zfs", "create", "-o", f"refreservation={size}", "-o", "mountpoint=none", dataset], undo=Cmd(["zfs", "destroy", dataset])))
    elif have != str(size_bytes(size)):
        steps.append(Cmd(["zfs", "set", f"refreservation={size}", dataset]))
    if not scrub_scheduled(pool):
        warn("no monthly scrub is scheduled (/etc/cron.d/zfsutils-linux is missing)")
        steps.append(Cmd(["systemctl", "enable", "--now", f"zfs-scrub-monthly@{pool}.timer"]))
    if steps:
        steps += [Cmd(["zpool", "status", "-x"]), Cmd(["zfs", "list", "-o", "name,used,avail", pool])]
    typer.echo("Release the reserve only to recover from a full pool: zfs-reserve-release.")
    execute(Plan("Pool health and a reserve", steps))


def zfs_reserve_release() -> None:
    """Release the pool reserve (only to recover from a full pool)."""
    pool = root_pool() or "rpool"
    dataset = f"{pool}/{RESERVE_DATASET_NAME}"
    have = zfs_value(["refreservation", dataset])
    if have in (None, "0", "none", "-"):
        typer.echo("No reserve is held.")
        return
    if not confirm_typed("release", f"{dataset} stops holding {int(have) // 2**30} GiB; run zfs-reserve again afterwards to hold it back."):
        typer.echo("Cancelled.")
        return
    execute(Plan("Release the reserve", [Cmd(["zfs", "set", "refreservation=none", dataset])]))


FW_FILE = "/etc/pve/firewall/cluster.fw"
FW_SAVE_DIR = "/root/.pve-setup"
FW_BEFORE, FW_ABSENT, FW_WRITTEN = (f"{FW_SAVE_DIR}/cluster.fw.{x}" for x in ("before", "absent", "written"))
FW_REVERT_UNIT = "pve-setup-fw-revert"
TAILNET_CIDR = "100.64.0.0/10"


def firewall_text(lan_cidr: str, ports: str) -> str:
    return f"""[OPTIONS]
enable: 1
policy_in: DROP
policy_out: ACCEPT

[RULES]
IN ACCEPT -source {TAILNET_CIDR} -p tcp -dport {ports}
IN ACCEPT -source {lan_cidr} -p tcp -dport {ports}
IN ACCEPT -p icmp
"""


def lan_cidr_default() -> str:
    for line in (soft(["ip", "-4", "-o", "route", "show", "scope", "link"]) or "").splitlines():
        cols = line.split()
        dev = cols[cols.index("dev") + 1] if "dev" in cols else ""
        try:
            net = ipaddress.ip_network(cols[0])
        except (ValueError, IndexError):
            continue
        if net.subnet_of(ipaddress.ip_network(TAILNET_CIDR)) or dev.startswith(("tailscale", "docker", "veth", "fw", "tap")):
            continue
        return str(net)
    return ""


def ask_lan_cidr() -> str:
    while True:
        v = typer.prompt("Your LAN subnet (CIDR) to allow", default=lan_cidr_default() or None).strip()
        try:
            net = ipaddress.ip_network(v)
            if net.version == 4 and net.prefixlen >= 8:
                return str(net)
        except ValueError:
            pass
        typer.echo("  an IPv4 network such as 192.168.1.0/24 (not wider than a /8)")


def ask_ports() -> str:
    while True:
        v = typer.prompt("TCP ports to allow from the tailnet and the LAN", default="22,8006").strip()
        if re.fullmatch(r"\d{1,5}(,\d{1,5})*", v) and all(1 <= int(x) <= 65535 for x in v.split(",")):
            return v
        typer.echo("  comma-separated port numbers, e.g. 22,8006")


def ssh_client_ip() -> str:
    return (os.environ.get("SSH_CONNECTION", "").split() or [""])[0]


def session_allowed(lan_cidr: str) -> bool:
    ip = ssh_client_ip()
    if not ip:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return addr in ipaddress.ip_network(TAILNET_CIDR) or addr in ipaddress.ip_network(lan_cidr)


def fw_revert_armed() -> bool:
    return soft(["systemctl", "is-active", f"{FW_REVERT_UNIT}.timer"]) is not None


def fw_running() -> bool:
    out = soft(["pve-firewall", "status"]) or ""
    if "enabled/running" in out:
        typer.echo("pve-firewall reports: enabled/running.")
        return True
    fail(f"pve-firewall does not report enabled/running (it said: {out.strip() or 'nothing'})")
    return False


def save_firewall(new_text: str) -> bool:
    """Keep the rules from before the toolkit's first change; later runs keep that first copy."""
    d = Path(FW_SAVE_DIR)
    d.mkdir(mode=0o700, exist_ok=True)
    current = read_text(FW_FILE)
    written = read_text(FW_WRITTEN)
    ours = current is not None and written is not None and hashlib.sha256(current.encode()).hexdigest() == written.strip()
    if ours and (os.path.exists(FW_BEFORE) or os.path.exists(FW_ABSENT)):
        return True
    for f in (FW_BEFORE, FW_ABSENT):
        if os.path.lexists(f):
            os.unlink(f)
    if current is None:
        Path(FW_ABSENT).write_text("")
    else:
        fd = os.open(FW_BEFORE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(current)
    return True


def remember_written(text: str) -> bool:
    fd = os.open(FW_WRITTEN, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(hashlib.sha256(text.encode()).hexdigest())
    return True


def restore_firewall(restart: bool = True) -> bool:
    before = read_text(FW_BEFORE)
    if before is not None:
        write_file(Write(FW_FILE, before, in_place=True))
    elif os.path.exists(FW_ABSENT):
        if os.path.lexists(FW_FILE):
            os.unlink(FW_FILE)
    else:
        fail(f"no saved copy of the earlier rules in {FW_SAVE_DIR}; restore {FW_FILE} by hand")
        return False
    return not restart or subprocess.run(["pve-firewall", "restart"], check=False).returncode == 0


def compile_or_restore() -> bool:
    r = subprocess.run(["pve-firewall", "compile"], capture_output=True, text=True, check=False)
    for line in (r.stdout or r.stderr).splitlines()[:12]:
        typer.echo("  " + line)
    if r.returncode == 0:
        return True
    restore_firewall()
    fail("pve-firewall compile rejected the rules; the earlier rules were restored")
    return False


def firewall() -> None:
    """Enable the Proxmox firewall for the tailnet and the LAN (guide 8.3), with an automatic revert."""
    lan = ask_lan_cidr()
    ports = ask_ports()
    text = firewall_text(lan, ports)
    if read_text(FW_FILE) == text and (soft(["pve-firewall", "status"]) or "").count("enabled/running"):
        typer.echo("The firewall already has exactly these rules and is running.")
        execute(Plan("Enable the firewall", []))
        return
    if not session_allowed(lan):
        warn(f"your SSH session comes from {ssh_client_ip()}, which is neither the tailnet nor {lan}: enabling the firewall would cut it")
        if not confirm_typed("lockout", "Continue anyway only if you are at the console."):
            typer.echo("Cancelled.")
            return
    typer.echo("Open an SSH session to the host first and keep it (and the console) open until the tests below pass.")
    restore_cmd = f"if [ -f {FW_BEFORE} ]; then cat {FW_BEFORE} > {FW_FILE}; elif [ -f {FW_ABSENT} ]; then rm -f {FW_FILE}; fi; pve-firewall restart"
    steps: list[Step] = [snapshot_step("firewall"), Do(f"save the current rules under {FW_SAVE_DIR}", lambda: save_firewall(text))]
    if not fw_revert_armed():
        steps.append(Cmd(["systemd-run", "--unit", FW_REVERT_UNIT, "--on-active=10m", "/bin/sh", "-c", restore_cmd]))
    steps += [Write(FW_FILE, text, in_place=True, undo=Do("restore the earlier firewall rules and restart the firewall", restore_firewall)),
              Do("remember what was written", lambda: remember_written(text)),
              Do("validate with pve-firewall compile; the earlier rules come back if it fails", compile_or_restore),
              Cmd(["pve-firewall", "restart"]), Do("check that pve-firewall reports enabled/running", fw_running)]
    if execute(Plan("Enable the firewall", steps)) and not STATE["dry_run"]:
        ts, lan_ip = tailnet_address(), lan_address()
        typer.echo(f"\nTest in fresh sessions, while the automatic revert is still armed:\n  - SSH and https://{ts or '<tailnet IP>'}:8006 over the tailnet from a device off the LAN\n"
                   f"  - SSH over the LAN ({lan_ip or '<LAN IP>'})\n  - the edge's proxmox probe turns green on the status page (it comes from a 100.64.0.0/10 address)\n"
                   "Then run firewall-keep to cancel the revert. Guest traffic on the bridge is not filtered until you enable a guest firewall.")


def firewall_keep() -> None:
    """Cancel the automatic firewall revert once the tests pass."""
    if not fw_revert_armed():
        typer.echo("No automatic firewall revert is armed.")
        return
    if not confirm_typed("proven", "This keeps the firewall rules for good."):
        typer.echo("Cancelled.")
        return
    execute(Plan("Keep the firewall rules", [Cmd(["systemctl", "stop", f"{FW_REVERT_UNIT}.timer"])]))


def firewall_remove() -> None:
    """Put back the firewall rules that were there before the toolkit's first change."""
    if read_text(FW_BEFORE) is None and not os.path.exists(FW_ABSENT):
        typer.echo(f"No saved earlier rules in {FW_SAVE_DIR}; nothing this toolkit can put back.")
        return
    if not confirm_typed("firewall", "The host stops being protected by these rules (the earlier ones come back)."):
        typer.echo("Cancelled.")
        return
    steps: list[Step] = [Do("restore the earlier firewall rules and restart the firewall", restore_firewall)]
    if fw_revert_armed():
        steps.append(Cmd(["systemctl", "stop", f"{FW_REVERT_UNIT}.timer"]))
    execute(Plan("Remove the toolkit's firewall rules", steps))


INST_RE = re.compile(r"^Inst (\S+)(?: \[([^\]]+)\])? \(([^ ]+) ")


def parse_simulation(text: str) -> list[dict]:
    """The packages `apt-get -s full-upgrade` would install or upgrade: name, old version (None if new), new version."""
    rows = []
    for line in text.splitlines():
        m = INST_RE.match(line)
        if m:
            rows.append({"name": m[1], "old": m[2], "new": m[3]})
    return rows


def is_kernel_package(name: str) -> bool:
    return bool(re.match(r"^(proxmox-kernel|pve-kernel)-\d", name))


def major_of(version: str | None) -> int | None:
    m = re.match(r"^(?:\d+:)?(\d+)", version or "")
    return int(m[1]) if m else None


def major_bumps(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["name"] in ("pve-manager", "proxmox-ve") and r["old"]
            and major_of(r["old"]) is not None and major_of(r["new"]) != major_of(r["old"])]


UPDATE_NOTE = """If a new kernel was installed:
  1. proxmox-boot-tool kernel pin <new version> --next-boot, then reboot (it boots once; a hang plus a power cycle falls back to the pinned kernel)
  2. pin it for good only after a clean boot: proxmox-boot-tool kernel pin <new version>
Tailscale stays held until the edge's headscale pin moves. Do not attempt the next Proxmox major version remotely."""


def reboot_hint() -> bool:
    if os.path.exists("/var/run/reboot-required"):
        typer.echo("The host asks for a reboot (/var/run/reboot-required).")
    return True


def update() -> None:
    """Upgrade packages the careful way (guide 8.7): snapshot, show what changes, refuse a major jump, warn about kernels."""
    if not root_dataset() and not STATE["dry_run"]:
        raise typer.Exit(fail("the root filesystem is not ZFS (findmnt/zfs found no dataset)"))
    if not execute(Plan("Refresh the package lists", [Cmd(["apt-get", "update"])])):
        return
    sim = soft(["apt-get", "-s", "full-upgrade"])
    if sim is None:
        raise typer.Exit(fail("`apt-get -s full-upgrade` failed; run it by hand to see why"))
    rows = parse_simulation(sim)
    held = (soft(["apt-mark", "showhold"]) or "").split()
    if not rows:
        typer.echo("Nothing to upgrade.")
        if held:
            typer.echo("Held back on purpose: " + ", ".join(held))
        return
    kernels = [r for r in rows if is_kernel_package(r["name"])]
    typer.echo(f"\n{len(rows)} package(s) would change:")
    for r in rows[:40]:
        typer.echo(f"  {r['name']}  {r['old'] or '(new)'} -> {r['new']}")
    if len(rows) > 40:
        typer.echo(f"  ... and {len(rows) - 40} more")
    if held:
        typer.echo("Held back on purpose: " + ", ".join(held))
    bumps = major_bumps(rows)
    if bumps:
        raise typer.Exit(fail("this upgrade moves " + ", ".join(f"{b['name']} {b['old']} -> {b['new']}" for b in bumps)
                              + " to a new major version; do not attempt that remotely"))
    if kernels:
        typer.echo("\nA new kernel is part of this upgrade: " + ", ".join(k["name"] for k in kernels))
        if not confirm_typed("kernel", "With no KVM and nobody on site a kernel that fails to boot cannot be fixed remotely; skip kernel updates until a KVM is installed."):
            typer.echo("Cancelled. Nothing was changed.")
            return
    steps: list[Step] = [snapshot_step("upgrade"), Cmd(["apt-get", "full-upgrade"]), Do("check whether a reboot is asked for", reboot_hint)]
    if execute(Plan("Upgrade the packages", steps)) and not STATE["dry_run"]:
        typer.echo("\n" + UPDATE_NOTE)


def tfa_enrolled(user: str = "root@pam") -> bool | None:
    out = soft(["pveum", "user", "tfa", "list", user])
    return None if out is None else bool(out.strip())


def fw_rules_ok(text: str | None) -> bool:
    if not text:
        return False
    lines = [l.strip() for l in text.splitlines()]
    ports_ok = any(l.startswith("IN ACCEPT") and f"-source {TAILNET_CIDR}" in l and "22" in l and "8006" in l for l in lines)
    return "enable: 1" in lines and "policy_in: DROP" in lines and ports_ok


def config_checks() -> list[tuple[str, bool, str]]:
    pool = root_pool()
    snaps = toolkit_snapshots()
    newest = datetime.fromtimestamp(snaps[-1][1], timezone.utc).strftime("%Y-%m-%d") if snaps else "-"
    reserve = zfs_value(["refreservation", f"{pool}/{RESERVE_DATASET_NAME}"]) if pool else None
    autotrim = (soft(["zpool", "get", "-H", "-o", "value", "autotrim", pool]) or "").strip() if pool else ""
    fw_text = read_text(FW_FILE)
    tfa = tfa_enrolled()
    hb = heartbeat_state()
    last = latest_backup(default_backup_folder())
    age_days = (datetime.now(timezone.utc).timestamp() - last[1]) / 86400 if last else None
    return [
        ("root is on ZFS", bool(pool), pool or "no ZFS root found"),
        (f"a pre-change snapshot exists (@{SNAP_PREFIX}*)", bool(snaps), f"{len(snaps)} snapshot(s), newest {newest}"),
        ("autotrim is on", autotrim == "on", autotrim or "unknown"),
        ("a pool reserve is held", bool(reserve) and reserve not in ("0", "none", "-"), f"refreservation={reserve}"),
        ("a monthly scrub is scheduled", bool(pool) and scrub_scheduled(pool), ""),
        ("the firewall is enabled and running", "enabled/running" in (soft(["pve-firewall", "status"]) or ""), ""),
        ("firewall rules: default DROP, tailnet allowed on 22 and 8006", fw_rules_ok(fw_text), FW_FILE),
        ("two-factor is enrolled for root@pam", tfa is True, "unknown (pveum unavailable)" if tfa is None else "enrolled" if tfa else "none enrolled"),
        ("the heartbeat timer is active", hb["enabled"] == "enabled" and hb["active"] == "active" and os.path.exists(HB_SCRIPT), f"{hb['enabled'] or 'not installed'} / {hb['active'] or 'inactive'}"),
        ("a config backup is at most 8 days old", age_days is not None and age_days <= 8, f"{last[0]}, {age_days:.0f} d old" if last and age_days is not None else "none in " + default_backup_folder()),
        ("no unconfirmed automatic revert is armed", not revert_armed() and not fw_revert_armed(),
         " ".join(x for x, on in (("ssh (ssh-harden-keep)", revert_armed()), ("firewall (firewall-keep)", fw_revert_armed())) if on) or "none"),
    ]


def host_config_check_impl() -> bool:
    checks = config_checks()
    typer.secho("\n== Host configuration checks (guide section 8)", bold=True)
    for name, passed, detail in checks:
        typer.echo(f"  {'PASS' if passed else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    typer.echo("\nNot checked here: the web UI two-factor login itself (log in once from a fresh browser), and that a config backup copy is off this host.")
    return all(c[1] for c in checks)


def host_config_check_menu() -> None:
    host_config_check_impl()


def host_config_check() -> None:
    """Check section 8 end to end: snapshots, pool reserve, firewall, 2FA, heartbeat, config backup (read-only)."""
    if not host_config_check_impl():
        raise typer.Exit(1)


# ---------- status ----------
def collect_status() -> dict:
    ts = tailscale_state()
    jobs = backup_jobs()
    storage = pvesh_soft("/storage") or []
    users = [u for u in api_users() if u.get("comment") == USER_COMMENT]
    return {
        "host": {"node": node(), "pve": (soft(["pveversion"]) or "").strip() or None},
        "tailscale": ts,
        "heartbeat": heartbeat_state(),
        "ssh": {"hardened": (ssh_is_hardened(eff) if (eff := sshd_effective()) else None), "revert_armed": revert_armed()},
        "firewall": {"running": ("enabled/running" in out) if (out := soft(["pve-firewall", "status"])) is not None else None, "revert_armed": fw_revert_armed()},
        "zfs": {"pool": root_pool(), "snapshots": len(toolkit_snapshots())},
        "config_backup": {"timer": (soft(["systemctl", "is-enabled", "pve-config-backup.timer"]) or "").strip() or None,
                          "latest": (lb[0] if (lb := latest_backup(default_backup_folder())) else None)},
        "guests": guests(),
        "storage": [{"id": s.get("storage"), "type": s.get("type"), "content": s.get("content")} for s in storage],  # type: ignore[union-attr]
        "backup_jobs": [{"id": j.get("id"), "schedule": j.get("schedule"), "storage": j.get("storage"), "enabled": j.get("enabled", 1),
                         "scope": "all" if str(j.get("all")) == "1" else j.get("vmid"), "managed": str(j.get("id", "")).startswith(JOB_PREFIX)}
                        for j in jobs],
        "api_users": [{"userid": u.get("userid"), "tokens": [t.get("tokenid") for t in (pvesh_soft(f"/access/users/{u.get('userid')}/token") or [])]}  # type: ignore[union-attr]
                      for u in users],
        "log": str(LOG),
    }


def render_status(d: dict) -> list[str]:
    out = [f"Host: {d['host']['node']}   {d['host']['pve'] or 'pveversion unavailable'}"]
    ts = d["tailscale"]
    out.append("Tailscale: not installed" if ts is None else
               f"Tailscale: {ts['package'] or '?'}{' (held)' if ts['held'] else ' (NOT held)'}, {ts['backend'] or 'unknown'}"
               + (f", node '{ts['hostname']}' on {ts['control']}, tags {', '.join(ts['tags']) or 'none'}" if ts["backend"] == "Running" else ""))
    hb = d["heartbeat"]
    out.append(f"Heartbeat: timer {hb['enabled'] or 'not installed'} / {hb['active'] or 'inactive'}; files "
               + ", ".join(f"{Path(p).name}={'yes' if ok else 'no'}" for p, ok in hb["files"].items()))
    ssh = d["ssh"]
    out.append("SSH: " + ("password logins off" if ssh["hardened"] else "password logins NOT disabled" if ssh["hardened"] is False else "unknown (sshd -T unavailable)")
               + ("; automatic revert armed (run ssh-harden-keep once both logins are proven)" if ssh["revert_armed"] else ""))
    fw = d["firewall"]
    out.append("Firewall: " + ("unknown (pve-firewall unavailable)" if fw["running"] is None else "enabled/running" if fw["running"] else "NOT running")
               + ("; automatic revert armed (run firewall-keep once the tests pass)" if fw["revert_armed"] else ""))
    z = d["zfs"]
    out.append(f"ZFS: pool {z['pool'] or 'none found'}, {z['snapshots']} pve-setup snapshot(s)")
    cb = d["config_backup"]
    out.append(f"Config backup: weekly timer {cb['timer'] or 'not installed'}; latest archive {cb['latest'] or 'none'}")
    out.append("Guests (* = created by this toolkit):")
    for g in sorted(d["guests"], key=lambda g: g["vmid"]):
        out.append(f"  {'*' if g['managed'] else ' '} {g['vmid']:>5} {g['type']:<5} {g['status']:<8} {g['name']}{' [template]' if g['template'] else ''}")
    if not d["guests"]:
        out.append("    none")
    out.append("Storage: " + ", ".join(f"{s['id']} ({s['type']})" for s in d["storage"]) if d["storage"] else "Storage: none")
    out.append("Backup jobs (* = created by this toolkit):")
    for j in d["backup_jobs"]:
        out.append(f"  {'*' if j['managed'] else ' '} {j['id']}  {j['schedule']}  -> {j['storage']}  scope {j['scope']}  enabled {j['enabled']}")
    if not d["backup_jobs"]:
        out.append("    none")
    out.append("API users created by this toolkit: " + (", ".join(f"{u['userid']} [{', '.join(u['tokens']) or 'no tokens'}]" for u in d["api_users"]) or "none"))
    out.append(f"Command log: {d['log']}")
    return out


def status(as_json: bool = False) -> None:
    """Show what is installed and which objects this toolkit created (read-only)."""
    data = collect_status()
    if as_json:
        typer.echo(json.dumps(data, indent=2, sort_keys=True))
    else:
        for line in render_status(data):
            typer.echo(line)


# ---------- undo ----------
def tailscale_leave() -> None:
    """Log this host out of the tailnet; optionally uninstall Tailscale."""
    st = tailscale_state()
    if st is None:
        typer.echo("Tailscale is not installed.")
        return
    uninstall = typer.confirm("Also uninstall the package and remove its apt repo?", default=False)
    if not confirm_typed(st["hostname"] or node(), f"This logs '{st['hostname'] or node()}' out of {st['control'] or 'its control server'}."):
        typer.echo("Cancelled.")
        return
    steps: list[Step] = [Cmd(["tailscale", "logout"])] if st["backend"] == "Running" else []
    if uninstall:
        steps += [Cmd(["apt-mark", "unhold", "tailscale"]), Cmd(["apt-get", "remove", "-y", "tailscale"]), Remove(TAILSCALE_LIST)]
    typer.echo("The node record stays in headscale: remove it with node-admin -> remove-device.")
    execute(Plan("Leave the tailnet", steps))


def heartbeat_remove() -> None:
    """Remove the heartbeat timer, script, units and token file (and the earlier edge-heartbeat one)."""
    state = heartbeat_state()
    present = [p for p, ok in state["files"].items() if ok]
    if not present and not state["enabled"] and not state["legacy"]:
        typer.echo("The heartbeat is not installed.")
        return
    if not confirm_typed("heartbeat", "Gatus will alert once the heartbeat has been silent for its interval."):
        typer.echo("Cancelled.")
        return
    steps: list[Step] = []
    if state["enabled"] or state["active"] == "active":
        steps.append(Cmd(["systemctl", "disable", "--now", "pve-heartbeat.timer"]))
    steps += [Remove(p) for p in present]
    steps += legacy_heartbeat_removal(state) if (state["legacy"] or state["legacy_active"]) else [Cmd(["systemctl", "daemon-reload"])]
    execute(Plan("Remove the heartbeat", steps))


def vm_destroy() -> None:
    """Destroy a VM, template or container this toolkit created (disks included)."""
    mine = [g for g in guests() if g["managed"]]
    if not mine:
        typer.echo(f"No guests carry the '{TAG}' tag; the toolkit only destroys what it created.")
        return
    gid = choose("Guest to destroy", [(str(g["vmid"]), f"{g['vmid']} {g['name']} ({g['type']}, {g['status']}{', template' if g['template'] else ''})") for g in mine])
    g = next(x for x in mine if str(x["vmid"]) == gid)
    if not confirm_typed(gid, f"This permanently deletes {g['type']} {gid} '{g['name']}' and its disks. Backups already taken are kept."):
        typer.echo("Cancelled.")
        return
    tool = "qm" if g["type"] == "qemu" else "pct"
    steps: list[Step] = []
    if g["status"] == "running":
        steps.append(Cmd([tool, "stop", gid]))
    steps.append(Cmd([tool, "destroy", gid, "--purge", "1", "--destroy-unreferenced-disks", "1"]))
    execute(Plan(f"Destroy {g['type']} {gid} ({g['name']})", steps))


def storage_remove() -> None:
    """Remove a storage definition (the data on it is not touched)."""
    rows = [s for s in (pvesh_soft("/storage") or []) if s.get("storage") not in ("local", "local-lvm")]  # type: ignore[union-attr]
    if not rows:
        typer.echo("No removable storage definitions (local and local-lvm are never offered).")
        return
    sid = choose("Storage to remove", [(s["storage"], f"{s['storage']} ({s.get('type')}, {s.get('content')})") for s in rows])
    if not confirm_typed(sid, f"Guests with disks on '{sid}' lose access to them until it is added back. The data itself is not deleted."):
        typer.echo("Cancelled.")
        return
    execute(Plan(f"Remove storage {sid}", [Cmd(["pvesm", "remove", sid])]))


def backup_job_remove() -> None:
    """Remove a backup job this toolkit created (existing backups are kept)."""
    mine = [j for j in backup_jobs() if str(j.get("id", "")).startswith(JOB_PREFIX)]
    if not mine:
        typer.echo(f"No backup jobs with the '{JOB_PREFIX}' id prefix; the toolkit only removes what it created.")
        return
    jid = choose("Job to remove", [(j["id"], f"{j['id']}  {j.get('schedule')} -> {j.get('storage')}") for j in mine])
    if not confirm_typed(jid, "Scheduled backups stop. Backups already taken stay on the storage."):
        typer.echo("Cancelled.")
        return
    execute(Plan(f"Remove backup job {jid}", [Cmd(["pvesh", "delete", f"/cluster/backup/{jid}"])]))


def api_token_revoke() -> None:
    """Revoke a token or delete a user this toolkit created."""
    mine = [u for u in api_users() if u.get("comment") == USER_COMMENT]
    if not mine:
        typer.echo(f"No users with the comment '{USER_COMMENT}'; the toolkit only revokes what it created.")
        return
    user = choose("User", [(u["userid"], u["userid"]) for u in mine])
    toks = [t.get("tokenid") for t in (pvesh_soft(f"/access/users/{user}/token") or [])]  # type: ignore[union-attr]
    what = choose("Action", [("user", "Delete the user, with its tokens and permissions")] + [(t, f"Revoke token {t}") for t in toks])
    expected = user if what == "user" else f"{user}!{what}"
    if not confirm_typed(expected, f"{'The user and every token' if what == 'user' else 'This token'} stops working immediately."):
        typer.echo("Cancelled.")
        return
    cmd = ["pveum", "user", "delete", user] if what == "user" else ["pveum", "user", "token", "remove", user, what]
    execute(Plan(f"Revoke {expected}", [Cmd(cmd)]))


# ---------- config backup and restore ----------
def _is_secret(path: str) -> bool:
    p = Path(path)
    return "priv" in p.parts or p.name.endswith(".key") or path in SECRET_FILES or any(path.startswith(d + "/") for d, _ in SECRET_DIRS)


def _walk(root: str, pmxcfs: bool) -> list[str]:
    found = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if pmxcfs and dirpath == root:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            filenames = [f for f in filenames if not f.startswith(".")]
        for f in filenames:
            p = os.path.join(dirpath, f)
            if not os.path.islink(p) and os.path.isfile(p):
                found.append(p)
    return found


def collect_backup_files(include_secrets: bool) -> list[tuple[str, str]]:
    files: dict[str, str] = {}
    for p in OWNED_FILES:
        if os.path.isfile(p):
            files[p] = "owned"
    for p in HOST_FILES:
        if os.path.isfile(p):
            files.setdefault(p, "host")
    roots = list(HOST_DIRS) + (list(SECRET_DIRS) if include_secrets else [])
    for root, pmx in roots:
        if os.path.isdir(root):
            for p in _walk(root, pmx):
                files.setdefault(p, "host")
    if include_secrets:
        for p in SECRET_FILES:
            if os.path.isfile(p):
                files[p] = "secret"
    out = []
    for p, grp in sorted(files.items()):
        secret = _is_secret(p)
        if secret and not include_secrets:
            continue
        out.append((p, "secret" if secret else grp))
    return out


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes, mode: int = 0o600) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mode = mode
    info.mtime = int(datetime.now(timezone.utc).timestamp())
    tar.addfile(info, io.BytesIO(data))


AGE_RECIPIENT_RE = re.compile(r"^age1[02-9ac-hj-np-z]{58}$")


def valid_recipient(r: str) -> bool:
    return bool(AGE_RECIPIENT_RE.match(r)) or key_ident(r) is not None


def read_recipients(path: str) -> list[str]:
    return [l.strip() for l in (read_text(path) or "").splitlines() if l.strip() and not l.lstrip().startswith("#") and valid_recipient(l.strip())]


def ask_recipients() -> list[str]:
    found = read_recipients(BACKUP_RECIPIENTS)
    if found and typer.confirm(f"Encrypt to the {len(found)} recipient(s) in {BACKUP_RECIPIENTS}?", default=True):
        return found
    typer.echo("age recipients (age1... or an ssh public key). Empty line to finish; none = no encryption, and then no config.db and no secrets.")
    out: list[str] = []
    while (r := typer.prompt("Recipient", default="", show_default=False).strip()):
        if valid_recipient(r):
            out.append(r)
        else:
            typer.echo("  not an age recipient or ssh public key")
    return out


def cluster_db_bytes() -> bytes:
    """A consistent copy of the pmxcfs database while pve-cluster keeps running (SQLite online backup)."""
    src = sqlite3.connect(CLUSTER_DB)
    try:
        with tempfile.TemporaryDirectory() as d:
            dst_path = os.path.join(d, "config.db")
            dst = sqlite3.connect(dst_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
            return Path(dst_path).read_bytes()
    finally:
        src.close()


def write_config_archive(dest: str, recipients: list[str] | None = None) -> bool:
    """Write the verified archive; with recipients it also holds config.db and the secret files, and the result is dest + '.age'."""
    recipients = recipients or []
    if recipients and not shutil.which("age"):
        fail("age is not installed (apt install age); without it config.db and secrets cannot be protected")
        return False
    manifest = {"version": 1, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": node(),
                "pve": (soft(["pveversion"]) or "").strip(), "includes_secrets": bool(recipients), "files": []}
    inventory = {"resources": pvesh_soft("/cluster/resources", "--type", "vm"), "storage": pvesh_soft("/storage"),
                 "backup": pvesh_soft("/cluster/backup"),
                 "users": [{"userid": u.get("userid"), "comment": u.get("comment")} for u in api_users()]}
    old_umask = os.umask(0o077)
    work = tempfile.mkdtemp(prefix=".pve-setup-", dir=os.path.dirname(dest) or ".")
    plain = os.path.join(work, os.path.basename(dest))
    try:
        with tarfile.open(plain, "w:gz") as tar:
            for path, grp in collect_backup_files(bool(recipients)):
                if os.path.getsize(path) > MAX_FILE:
                    warn(f"skipping {path}: larger than {MAX_FILE // 2**20} MiB")
                    continue
                data = Path(path).read_bytes()
                manifest["files"].append({"path": path, "group": grp, "sha256": hashlib.sha256(data).hexdigest(),
                                          "mode": os.stat(path).st_mode & 0o777, "size": len(data)})
                _add_bytes(tar, "files" + path, data)
            if recipients and os.path.exists(CLUSTER_DB):
                db = cluster_db_bytes()
                manifest["files"].append({"path": CLUSTER_DB, "group": "cluster-db", "sha256": hashlib.sha256(db).hexdigest(), "mode": 0o600, "size": len(db)})
                _add_bytes(tar, "files" + CLUSTER_DB, db)
            for k, v in inventory.items():
                _add_bytes(tar, f"inventory/{k}.json", json.dumps(v, indent=1, sort_keys=True).encode())
            _add_bytes(tar, "MANIFEST.json", json.dumps(manifest, indent=1, sort_keys=True).encode())
        read_config_archive(plain)  # re-read and re-hash what was just written
        final = dest
        if recipients:
            final = dest + ".age"
            argv = ["age", *[a for r in recipients for a in ("-r", r)], "-o", final, plain]
            if subprocess.run(argv, check=False).returncode != 0:
                fail("age failed; no archive was written")
                return False
            with open(final, "rb") as f:
                if not f.read(32).startswith(b"age-encryption.org/v1"):
                    fail("the encrypted file does not start with an age header; removed")
                    os.unlink(final)
                    return False
        else:
            os.replace(plain, final)
        os.chmod(final, 0o600)
    finally:
        os.umask(old_umask)
        if os.path.exists(plain):
            os.unlink(plain)
        os.rmdir(work)
    typer.echo(f"Wrote {final} ({len(manifest['files'])} files{', encrypted with config.db and secrets' if recipients else ', no config.db and no secrets'}).")
    return True


def prune_backups(folder: str, keep: int) -> list[str]:
    names = sorted(f for f in os.listdir(folder) if re.fullmatch(rf"pve-config-{re.escape(node())}-\d{{8}}T\d{{6}}Z\.tar\.gz(\.age)?", f))
    old = names[:-keep] if keep > 0 else []
    for f in old:
        os.unlink(os.path.join(folder, f))
    return old


def latest_backup(folder: str) -> tuple[str, float] | None:
    try:
        names = sorted(f for f in os.listdir(folder) if re.fullmatch(r"pve-config-.*\.tar\.gz(\.age)?", f))
    except OSError:
        return None
    return (names[-1], os.stat(os.path.join(folder, names[-1])).st_mtime) if names else None


def default_backup_folder() -> str:
    return str(Path.home() / "pve-setup-backups")


def config_backup_impl(auto: bool, folder: str | None, keep: int) -> bool:
    folder = folder or (default_backup_folder() if auto else typer.prompt("Folder for the archive", default=default_backup_folder()))
    if auto:
        recipients = read_recipients(BACKUP_RECIPIENTS)
        if not recipients:
            fail(f"no valid recipients in {BACKUP_RECIPIENTS}; run config-backup-timer to set them")
            return False
    else:
        recipients = ask_recipients()
    if not recipients:
        warn("no recipient: the archive will hold the host files only, without config.db or secrets")
    dest = os.path.join(folder, f"pve-config-{node()}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.tar.gz")
    files = collect_backup_files(bool(recipients))
    typer.echo(f"{len(files)} files, groups: {', '.join(sorted({g for _, g in files}))}"
               f"{' + config.db' if recipients else ''}. Guest disks are not included; use backup-job for those.")

    def make() -> bool:
        Path(folder).mkdir(parents=True, exist_ok=True, mode=0o700)
        if not write_config_archive(dest, recipients):
            return False
        for f in prune_backups(folder, keep):
            typer.echo(f"pruned {f}")
        return True

    return execute(Plan("Back up host and Proxmox config", [Do(f"create {dest}{'.age' if recipients else ''} (0600), verify it, keep the newest {keep}", make)]))


def config_backup(auto: bool = typer.Option(False, "--auto", help="No prompts: recipients from the recipients file (used by the timer)"),
                  folder: Optional[str] = typer.Option(None, help="Where the archives go"),
                  keep: int = typer.Option(BACKUP_KEEP, help="How many archives to keep")) -> None:
    """Archive host and Proxmox config (not guest disks); with an age recipient also config.db and secrets, encrypted."""
    if not config_backup_impl(auto, folder, keep):
        raise typer.Exit(1)


def config_backup_menu() -> None:
    config_backup_impl(False, None, BACKUP_KEEP)


def config_backup_timer() -> None:
    """Install a weekly local config backup (encrypted, newest few kept)."""
    recipients = ask_recipients()
    if not recipients:
        raise typer.Exit(fail("a recipient is required: the weekly archive includes config.db and secrets, so it must be encrypted"))
    when = typer.prompt("Weekly schedule (systemd calendar)", default="Sun *-*-* 03:30:00")
    keep = ask_int("Archives to keep", 1, 200, BACKUP_KEEP)
    folder = typer.prompt("Folder", default=default_backup_folder())
    script = f"{BACKUP_INSTALL_DIR}/pve-setup.py"
    unit = f"""[Unit]
Description=Weekly encrypted Proxmox config backup (pve-setup)

[Service]
Type=oneshot
ExecStart={shlex.quote(sys.executable)} {script} config-backup --auto --folder {shlex.quote(folder)} --keep {keep}
"""
    timer = f"""[Unit]
Description=Weekly Proxmox config backup

[Timer]
OnCalendar={when}
Persistent=true
RandomizedDelaySec=15m

[Install]
WantedBy=timers.target
"""
    me = Path(__file__).resolve()
    steps: list[Step] = []
    if read_text(script) != me.read_text():
        steps.append(Write(script, me.read_text(), 0o755))
    wanted = {BACKUP_RECIPIENTS: ("\n".join(recipients) + "\n", 0o644), BACKUP_UNIT: (unit, 0o644), BACKUP_TIMER: (timer, 0o644)}
    changed = [path for path, (content, _) in wanted.items() if read_text(path) != content]
    steps += [Write(path, wanted[path][0], wanted[path][1]) for path in changed]
    if BACKUP_UNIT in changed or BACKUP_TIMER in changed:
        steps.append(Cmd(["systemctl", "daemon-reload"]))
    if changed or (soft(["systemctl", "is-enabled", "pve-config-backup.timer"]) or "").strip() != "enabled":
        steps.append(Cmd(["systemctl", "enable", "--now", "pve-config-backup.timer"]))
    typer.echo(f"The timer runs {sys.executable} (keep that Python environment) and a copy of this script in {BACKUP_INSTALL_DIR}. "
               "Archives stay on this host, so they protect against bad edits, not against losing the host: copy them off.")
    execute(Plan("Install the weekly config backup", steps))


def config_backup_timer_remove() -> None:
    """Remove the weekly config backup timer (archives already written are kept)."""
    present = [p for p in (BACKUP_UNIT, BACKUP_TIMER) if os.path.exists(p)]
    if not present:
        typer.echo("The weekly config backup is not installed.")
        return
    if not confirm_typed("backup", "No more weekly archives are written; the ones already there stay."):
        typer.echo("Cancelled.")
        return
    execute(Plan("Remove the weekly config backup", [Cmd(["systemctl", "disable", "--now", "pve-config-backup.timer"]),
                                                     *[Remove(p) for p in present], Cmd(["systemctl", "daemon-reload"])]))


def decrypt_archive(path: str, identity: str | None, into: str) -> str:
    if not shutil.which("age"):
        raise ValueError("age is not installed (apt install age)")
    identity = identity or typer.prompt("age identity (private key) file")
    out = os.path.join(into, "archive.tar.gz")
    r = subprocess.run(["age", "-d", "-i", identity, "-o", out, path], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise ValueError(f"age could not decrypt it: {r.stderr.strip() or 'wrong identity?'}")
    return out


def read_config_archive(path: str) -> tuple[dict, dict[str, bytes], dict[str, object]]:
    """Return (manifest, {live path: bytes}, {inventory name: json}); raise ValueError if anything is off."""
    total = 0
    blobs: dict[str, bytes] = {}
    with tarfile.open(path, "r:gz") as tar:
        for m in tar:
            parts = Path(m.name).parts
            if m.name.startswith("/") or ".." in parts or not (m.isreg() or m.isdir()):
                raise ValueError(f"unsafe or unsupported archive member: {m.name!r}")
            if m.isdir():
                continue
            if not (m.name == "MANIFEST.json" or m.name.startswith(("files/", "inventory/"))):
                raise ValueError(f"unexpected archive member: {m.name!r}")
            total += m.size
            if total > MAX_ARCHIVE:
                raise ValueError("archive is larger than the safety limit")
            if m.name in blobs:
                raise ValueError(f"duplicate archive member: {m.name!r}")
            blobs[m.name] = tar.extractfile(m).read()  # type: ignore[union-attr]
    if "MANIFEST.json" not in blobs:
        raise ValueError("MANIFEST.json is missing")
    manifest = json.loads(blobs.pop("MANIFEST.json"))
    files: dict[str, bytes] = {}
    for entry in manifest["files"]:
        data = blobs.pop("files" + entry["path"], None)
        if data is None:
            raise ValueError(f"{entry['path']} is listed in the manifest but missing")
        if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["size"]:
            raise ValueError(f"checksum mismatch for {entry['path']}")
        files[entry["path"]] = data
    inventory = {k[len("inventory/"):-len(".json")]: json.loads(v) for k, v in blobs.items() if k.startswith("inventory/")}
    extra = [k for k in blobs if not k.startswith("inventory/")]
    if extra:
        raise ValueError(f"files not in the manifest: {extra[:3]}")
    return manifest, files, inventory


RESTORE_NOTES = {
    "/etc/pve": "pmxcfs: copy single files only, while the node is quorate; a wrong storage.cfg or user.cfg applies at once",
    "/etc/network": "review first; a wrong file cuts off the host. Apply with `ifreload -a` from the console, not over SSH",
    "/etc/fstab": "review first; a wrong entry can stop the host booting",
    "/var/lib/pve-cluster": "config.db replaces the whole cluster configuration: a rebuild-only step (stop pve-cluster, copy with mode 0600, adapt /etc/hostname and /etc/hosts, reboot)",
}


def config_restore_impl(path: str | None, stage: str | None, apply_owned: bool, identity: str | None = None) -> None:
    path = path or typer.prompt("Archive to read")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            os.chmod(tmp, 0o700)
            manifest, files, inventory = read_config_archive(decrypt_archive(path, identity, tmp) if path.endswith(".age") else path)
    except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError, KeyError) as e:
        raise typer.Exit(fail(f"cannot use {path}: {e}")) from None
    typer.secho(f"\n== Restore plan from {path}", bold=True)
    typer.echo(f"Created {manifest['created']} on {manifest['host']} ({manifest['pve']}); secrets included: {manifest['includes_secrets']}")
    counts = {"same": 0, "differs": 0, "missing": 0, "rebuild-only": 0}
    todo: list[dict] = []
    for e in manifest["files"]:
        live = Path(e["path"]).read_bytes() if os.path.isfile(e["path"]) else None
        if e["group"] == "cluster-db":  # a backup copy never equals the live database byte for byte
            counts["rebuild-only"] += 1
            todo.append({**e, "state": "rebuild"})
            continue
        state = "missing" if live is None else "same" if live == files[e["path"]] else "differs"
        counts[state] += 1
        if state != "same":
            todo.append({**e, "state": state})
    typer.echo(f"{counts['same']} files unchanged, {counts['differs']} differ, {counts['missing']} missing"
               + (f", config.db is for a rebuild only (not compared)." if counts["rebuild-only"] else "."))
    if stage and STATE["dry_run"]:
        typer.echo(f"(dry run: would stage {len(todo)} files under {stage})")
    elif stage:
        sp = Path(stage)
        sp.mkdir(parents=True, exist_ok=False, mode=0o700)
        for e in todo:
            dest = sp / ("files" + e["path"])
            dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            dest.write_bytes(files[e["path"]])
            os.chmod(dest, 0o600)
        typer.echo(f"Staged {len(todo)} files under {stage} (0700). Nothing outside it was changed.")
    for e in todo:
        typer.echo(f"  [{e['group']:<6}] {e['state']:<7} {e['path']}")
    if stage and todo:
        typer.echo("\nCommands to restore a file after you have reviewed it (nothing here ran):")
        for e in todo:
            typer.echo(f"  install -D -m {e['mode']:04o} {shlex.quote(str(Path(stage) / ('files' + e['path'])))} {shlex.quote(e['path'])}")
    elif todo:
        typer.echo("\nRe-run with --stage DIR to extract the differing files there and get exact install commands.")
    notes = sorted({n for e in todo for pre, n in RESTORE_NOTES.items() if e["path"].startswith(pre)})
    for n in notes:
        warn(n)
    live_ids = {g["vmid"] for g in guests()}
    gone = [r for r in (inventory.get("resources") or []) if int(r["vmid"]) not in live_ids]  # type: ignore[union-attr]
    if gone:
        typer.echo("\nGuests that existed at backup time and are missing now (restore them from vzdump backups, not from this archive): "
                   + ", ".join(f"{r['vmid']} {r.get('name', '')}" for r in gone))
    owned = [e for e in todo if e["group"] == "owned"]
    if apply_owned:
        steps: list[Step] = [Write(e["path"], files[e["path"]].decode(), e["mode"]) for e in owned]
        if any(e["path"].endswith((".service", ".timer")) for e in owned):
            steps.append(Cmd(["systemctl", "daemon-reload"]))
        execute(Plan("Restore the files this toolkit wrote", steps))
    elif owned:
        typer.echo(f"\n{len(owned)} of these are files the toolkit wrote itself; --apply-owned restores just those after one confirmation.")


def config_restore(path: Optional[str] = typer.Argument(None, help="Archive from config-backup"),
                   stage: Optional[str] = typer.Option(None, help="Extract differing files here (new directory, 0700)"),
                   apply_owned: bool = typer.Option(False, "--apply-owned", help="Restore only the files this toolkit wrote"),
                   identity: Optional[str] = typer.Option(None, help="age identity file, to read a .age archive")) -> None:
    """Verify an archive (decrypting a .age one) and print a restore plan; only the toolkit's own files can be applied."""
    config_restore_impl(path, stage, apply_owned, identity)


def status_command(as_json: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
    """Show what exists and which objects this toolkit created (read-only)."""
    status(as_json)


SECTIONS: list[tuple[str, list[tuple[str, str, Callable[[], None]]]]] = [
    ("Set up", [("tailscale-install", "Install Tailscale (pinned)", tailscale_install), ("tailscale-join", "Join the tailnet", tailscale_join),
                ("heartbeat", "Install the Gatus heartbeat timer", heartbeat), ("vm-template", "Build a cloud-init VM template", vm_template),
                ("vm-create", "Clone a VM", vm_create), ("lxc-create", "Create an LXC container", lxc_create),
                ("storage-add", "Add storage", storage_add), ("backup-job", "Create or update a backup job", backup_job),
                ("api-token", "Create an API user and token", api_token)]),
    ("Host configuration", [("host-config-check", "Check section 8 end to end", host_config_check_menu), ("snapshot", "Snapshot the root dataset before a change", snapshot),
                            ("zfs-reserve", "Autotrim, pool reserve and scrub check", zfs_reserve),
                            ("firewall", "Enable the Proxmox firewall for tailnet and LAN", firewall),
                            ("firewall-keep", "Cancel the automatic firewall revert", firewall_keep),
                            ("update", "Upgrade packages with a snapshot and a kernel guard", update)]),
    ("Host hardening", [("ssh-harden", "Install your SSH key and disable password SSH", ssh_harden),
                        ("ssh-harden-keep", "Cancel the automatic SSH revert", ssh_harden_keep),
                        ("remote-gate", "Run the remote-readiness checks", remote_gate_menu)]),
    ("Inspect", [("status", "Show what exists and what this toolkit created", status)]),
    ("Undo (only things this toolkit created)", [
        ("tailscale-leave", "Leave the tailnet / uninstall Tailscale", tailscale_leave), ("heartbeat-remove", "Remove the heartbeat", heartbeat_remove),
        ("vm-destroy", "Destroy a VM, template or container", vm_destroy), ("storage-remove", "Remove a storage definition", storage_remove),
        ("backup-job-remove", "Remove a backup job", backup_job_remove), ("api-token-revoke", "Revoke a token or delete a user", api_token_revoke),
        ("ssh-harden-remove", "Remove the SSH hardening drop-in", ssh_harden_remove),
        ("config-backup-timer-remove", "Remove the weekly config backup", config_backup_timer_remove),
        ("firewall-remove", "Put back the earlier firewall rules", firewall_remove),
        ("snapshot-prune", "Destroy old pve-setup snapshots", snapshot_prune), ("zfs-reserve-release", "Release the pool reserve", zfs_reserve_release)]),
    ("Config backup", [("config-backup", "Archive host and Proxmox config (encrypted with config.db)", config_backup_menu),
                       ("config-restore", "Verify an archive and print a restore plan", lambda: config_restore_impl(None, None, False)),
                       ("config-backup-timer", "Install a weekly encrypted local config backup", config_backup_timer)]),
]
for _section, _items in SECTIONS:
    for _name, _label, _fn in _items:
        if _name not in ("status", "config-restore", "remote-gate", "config-backup", "host-config-check"):
            app.command(_name, help=_fn.__doc__)(_fn)
app.command("status")(status_command)
app.command("config-restore")(config_restore)
app.command("config-backup")(config_backup)
app.command("host-config-check")(host_config_check)
app.command("remote-gate")(remote_gate)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, dry_run: bool = typer.Option(False, "--dry-run", help="Show every step, run nothing.")) -> None:
    STATE["dry_run"] = dry_run
    if not dry_run and os.geteuid() != 0:
        raise typer.Exit(fail("run as root on the Proxmox host (or use --dry-run to preview)"))
    if ctx.invoked_subcommand is not None:
        return
    flat = [item for _, items in SECTIONS for item in items]
    while True:
        n = 0
        typer.secho("\nProxmox setup", bold=True)
        for title, items in SECTIONS:
            typer.secho(f" {title}", dim=True)
            for _, label, _ in items:
                n += 1
                typer.echo(f"  {n:>2}) {label}")
        typer.echo("   q) Quit")
        pick = typer.prompt("Choose", default="q").strip().lower()
        if pick == "q":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(flat):
            flat[int(pick) - 1][2]()


if __name__ == "__main__":
    sys.exit(app())
