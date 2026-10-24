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
HEARTBEAT_FILES = ("/etc/edge-heartbeat.curl", "/etc/systemd/system/edge-heartbeat.service", "/etc/systemd/system/edge-heartbeat.timer")
TAILSCALE_LIST = "/etc/apt/sources.list.d/tailscale.list"
TAILSCALE_KEYRING = "/usr/share/keyrings/tailscale-archive-keyring.gpg"

# Config backup: (path, is pmxcfs). Files the toolkit writes itself are "owned"; keys and tokens are "secret".
OWNED_FILES = ["/etc/systemd/system/edge-heartbeat.service", "/etc/systemd/system/edge-heartbeat.timer", TAILSCALE_LIST]
HOST_FILES = ["/etc/hostname", "/etc/hosts", "/etc/fstab", "/etc/network/interfaces", "/etc/apt/sources.list"]
HOST_DIRS = [("/etc/network/interfaces.d", False), ("/etc/apt/sources.list.d", False), ("/etc/ssh/sshd_config.d", False),
             ("/etc/pve", True)]
SECRET_FILES = ["/etc/edge-heartbeat.curl"]
SECRET_DIRS = [("/var/lib/tailscale", False)]
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
class Do:
    label: str
    fn: Callable[[], bool]


Step = Cmd | Write | Fetch | Verify | Remove | Do


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
    return step.label


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_file(step: Write) -> None:
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


def run_step(step: Step) -> bool:
    log(describe(step).splitlines()[0])
    if isinstance(step, Write):
        write_file(step)
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
    undos = [s.undo for s in reversed(done) if isinstance(s, Cmd) and s.undo]
    if isinstance(failed, Cmd) and failed.undo and failed.undo_on_fail:
        undos.insert(0, failed.undo)
    if not undos:
        typer.echo("Nothing was changed that needs rolling back.")
        return
    typer.secho("\nThe plan stopped part-way. These commands undo what it already did:", bold=True)
    for u in undos:
        typer.echo("  $ " + (u.shown or shlex.join(u.argv)))
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
            "enabled": (soft(["systemctl", "is-enabled", "edge-heartbeat.timer"]) or "").strip(),
            "active": (soft(["systemctl", "is-active", "edge-heartbeat.timer"]) or "").strip()}


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


def heartbeat_files(host: str, endpoint: str, seconds: int, token: str, edge_ip: str = "") -> dict[str, tuple[str, int, bool]]:
    url = f"https://{host}/api/v1/endpoints/{endpoint}/external?success=true"
    unit = f"""[Unit]
Description=Push a heartbeat to Gatus
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/bin/curl -fsS --max-time 10 -X POST -K /etc/edge-heartbeat.curl {url}
"""
    timer = f"""[Unit]
Description=Gatus heartbeat every {seconds}s

[Timer]
OnBootSec=1min
OnUnitActiveSec={seconds}s
AccuracySec=5s

[Install]
WantedBy=timers.target
"""
    curl = f'header = "Authorization: Bearer {token}"\n' + (f'resolve = "{host}:443:{edge_ip}"\n' if edge_ip else "")
    return {HEARTBEAT_FILES[0]: (curl, 0o600, True),
            HEARTBEAT_FILES[1]: (unit, 0o644, False), HEARTBEAT_FILES[2]: (timer, 0o644, False)}


def heartbeat() -> None:
    """Install the Gatus dead-man heartbeat as a systemd timer."""
    host = typer.prompt("Gatus host, e.g. status.ts.<domain>")
    endpoint = typer.prompt("Gatus external endpoint key (<group>_<name>)", default="home_proxmox-heartbeat")
    seconds = ask_int("Push every N seconds", 10, 3600, 60)
    token = typer.prompt("Heartbeat token", hide_input=True)
    edge_ip = ask_edge_ip()
    if not edge_ip and not STATE["dry_run"] and subprocess.run(["getent", "hosts", host], capture_output=True, check=False).returncode != 0:
        warn(f"{host} does not resolve here and no edge IP was given; the timer will fail")
    wanted = heartbeat_files(host, endpoint, seconds, token, edge_ip)
    differing = [p for p, (content, _, _) in wanted.items() if read_text(p) != content]
    state = heartbeat_state()
    steps: list[Step] = [Write(p, wanted[p][0], wanted[p][1], wanted[p][2]) for p in differing]
    if any(p.endswith((".service", ".timer")) for p in differing):
        steps.append(Cmd(["systemctl", "daemon-reload"]))
    if differing or state["enabled"] != "enabled" or state["active"] != "active":
        steps += [Cmd(["systemctl", "enable", "--now", "edge-heartbeat.timer"]), Cmd(["systemctl", "start", "edge-heartbeat.service"]),
                  Cmd(["systemctl", "status", "--no-pager", "edge-heartbeat.service"])]
    if differing:
        typer.echo("Changed files: " + ", ".join(differing) + " (secret contents are never shown)")
    execute(Plan("Install the heartbeat timer", steps))


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
    """Remove the heartbeat timer, its units and its token file."""
    state = heartbeat_state()
    present = [p for p, ok in state["files"].items() if ok]
    if not present and not state["enabled"]:
        typer.echo("The heartbeat is not installed.")
        return
    if not confirm_typed("heartbeat", "Gatus will alert once the heartbeat has been silent for its interval."):
        typer.echo("Cancelled.")
        return
    steps: list[Step] = []
    if state["enabled"] or state["active"] == "active":
        steps.append(Cmd(["systemctl", "disable", "--now", "edge-heartbeat.timer"]))
    steps += [Remove(p) for p in present]
    steps.append(Cmd(["systemctl", "daemon-reload"]))
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


def write_config_archive(dest: str, include_secrets: bool) -> bool:
    manifest = {"version": 1, "created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "host": node(),
                "pve": (soft(["pveversion"]) or "").strip(), "includes_secrets": include_secrets, "files": []}
    inventory = {"resources": pvesh_soft("/cluster/resources", "--type", "vm"), "storage": pvesh_soft("/storage"),
                 "backup": pvesh_soft("/cluster/backup"),
                 "users": [{"userid": u.get("userid"), "comment": u.get("comment")} for u in api_users()]}
    old_umask = os.umask(0o077)
    tmp = dest + ".part"
    try:
        with tarfile.open(tmp, "w:gz") as tar:
            for path, grp in collect_backup_files(include_secrets):
                if os.path.getsize(path) > MAX_FILE:
                    warn(f"skipping {path}: larger than {MAX_FILE // 2**20} MiB")
                    continue
                data = Path(path).read_bytes()
                manifest["files"].append({"path": path, "group": grp, "sha256": hashlib.sha256(data).hexdigest(),
                                          "mode": os.stat(path).st_mode & 0o777, "size": len(data)})
                _add_bytes(tar, "files" + path, data)
            for k, v in inventory.items():
                _add_bytes(tar, f"inventory/{k}.json", json.dumps(v, indent=1, sort_keys=True).encode())
            _add_bytes(tar, "MANIFEST.json", json.dumps(manifest, indent=1, sort_keys=True).encode())
        os.replace(tmp, dest)
        os.chmod(dest, 0o600)
    finally:
        os.umask(old_umask)
        if os.path.exists(tmp):
            os.unlink(tmp)
    read_config_archive(dest)  # re-read and re-hash what was just written
    typer.echo(f"Wrote and verified {dest} ({len(manifest['files'])} files{', including secrets' if include_secrets else ''}).")
    return True


def config_backup() -> None:
    """Archive host and Proxmox config (not guest disks) with a verified manifest."""
    default = str(Path.home() / "pve-setup-backups")
    folder = typer.prompt("Folder for the archive", default=default)
    secrets_ = typer.confirm("Include secrets (/etc/pve/priv, TLS keys, the heartbeat token, Tailscale node key)?", default=False)
    if secrets_:
        warn("the archive then holds private keys; keep it off the host and encrypted")
    dest = os.path.join(folder, f"pve-config-{node()}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.tar.gz")
    files = collect_backup_files(secrets_)
    typer.echo(f"{len(files)} files: " + ", ".join(sorted({g for _, g in files})) + ". Guest disks are not included; use backup-job for those.")
    def make() -> bool:
        Path(folder).mkdir(parents=True, exist_ok=True, mode=0o700)
        return write_config_archive(dest, secrets_)

    execute(Plan("Back up host and Proxmox config", [Do(f"create {dest} (0600) and verify it by re-reading", make)]))


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
    "/var/lib/tailscale": "stop tailscaled before copying, then start it",
}


def config_restore_impl(path: str | None, stage: str | None, apply_owned: bool) -> None:
    path = path or typer.prompt("Archive to read")
    try:
        manifest, files, inventory = read_config_archive(path)
    except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError, KeyError) as e:
        raise typer.Exit(fail(f"cannot use {path}: {e}")) from None
    typer.secho(f"\n== Restore plan from {path}", bold=True)
    typer.echo(f"Created {manifest['created']} on {manifest['host']} ({manifest['pve']}); secrets included: {manifest['includes_secrets']}")
    counts = {"same": 0, "differs": 0, "missing": 0}
    todo: list[dict] = []
    for e in manifest["files"]:
        live = Path(e["path"]).read_bytes() if os.path.isfile(e["path"]) else None
        state = "missing" if live is None else "same" if live == files[e["path"]] else "differs"
        counts[state] += 1
        if state != "same":
            todo.append({**e, "state": state})
    typer.echo(f"{counts['same']} files unchanged, {counts['differs']} differ, {counts['missing']} missing.")
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
                   apply_owned: bool = typer.Option(False, "--apply-owned", help="Restore only the files this toolkit wrote")) -> None:
    """Verify an archive and print a restore plan; only the toolkit's own files can be applied."""
    config_restore_impl(path, stage, apply_owned)


def status_command(as_json: bool = typer.Option(False, "--json", help="Machine-readable output")) -> None:
    """Show what exists and which objects this toolkit created (read-only)."""
    status(as_json)


SECTIONS: list[tuple[str, list[tuple[str, str, Callable[[], None]]]]] = [
    ("Set up", [("tailscale-install", "Install Tailscale (pinned)", tailscale_install), ("tailscale-join", "Join the tailnet", tailscale_join),
                ("heartbeat", "Install the Gatus heartbeat timer", heartbeat), ("vm-template", "Build a cloud-init VM template", vm_template),
                ("vm-create", "Clone a VM", vm_create), ("lxc-create", "Create an LXC container", lxc_create),
                ("storage-add", "Add storage", storage_add), ("backup-job", "Create or update a backup job", backup_job),
                ("api-token", "Create an API user and token", api_token)]),
    ("Inspect", [("status", "Show what exists and what this toolkit created", status)]),
    ("Undo (only things this toolkit created)", [
        ("tailscale-leave", "Leave the tailnet / uninstall Tailscale", tailscale_leave), ("heartbeat-remove", "Remove the heartbeat", heartbeat_remove),
        ("vm-destroy", "Destroy a VM, template or container", vm_destroy), ("storage-remove", "Remove a storage definition", storage_remove),
        ("backup-job-remove", "Remove a backup job", backup_job_remove), ("api-token-revoke", "Revoke a token or delete a user", api_token_revoke)]),
    ("Config backup", [("config-backup", "Archive host and Proxmox config", config_backup),
                       ("config-restore", "Verify an archive and print a restore plan", lambda: config_restore_impl(None, None, False))]),
]
for _section, _items in SECTIONS:
    for _name, _label, _fn in _items:
        if _name not in ("status", "config-restore"):
            app.command(_name, help=_fn.__doc__)(_fn)
app.command("status")(status_command)
app.command("config-restore")(config_restore)


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
