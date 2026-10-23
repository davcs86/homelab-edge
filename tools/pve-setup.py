#!/usr/bin/env python3
"""Interactive Proxmox VE setup toolkit. Run as root on the PVE host; see docs/runbooks/proxmox-setup.md.

Every step prints the exact commands and files it will touch and waits for a yes. Secrets are read with hidden
input, are never written to the log, and reach commands through 0600 files, not argv.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import typer

app = typer.Typer(add_completion=False, help="Interactive Proxmox VE setup toolkit (run on the PVE host, as root).")
STATE = {"dry_run": False}
LOG = Path.home() / ".pve-setup.log"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SIZE_RE = re.compile(r"^[1-9][0-9]*[MG]$")


@dataclass
class Cmd:
    argv: list[str]
    shown: str = ""


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
class Plan:
    title: str
    steps: list[Cmd | Write | Fetch | Verify]
    cleanup: list[str] = field(default_factory=list)


def fail(msg: str) -> int:
    typer.secho(f"error: {msg}", fg=typer.colors.RED, err=True)
    return 1


def log(line: str) -> None:
    LOG.touch(mode=0o600, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {line}\n")


def describe(step: Cmd | Write | Fetch | Verify) -> str:
    if isinstance(step, Cmd):
        return "$ " + (step.shown or shlex.join(step.argv))
    if isinstance(step, Write):
        body = f"<hidden, {len(step.content)} bytes>" if step.secret else "\n" + step.content.rstrip("\n")
        return f"write {step.path} (mode {step.mode:04o}) {body}"
    if isinstance(step, Fetch):
        return f"download {step.url} -> {step.dest}, then require sha256 {step.sha256}"
    return f"require sha256 {step.sha256} for {step.path}"


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


def run_step(step: Cmd | Write | Fetch | Verify) -> bool:
    log(describe(step).splitlines()[0])
    if isinstance(step, Write):
        write_file(step)
        return True
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


def execute(plan: Plan) -> bool:
    typer.secho(f"\n== {plan.title}", bold=True)
    for i, step in enumerate(plan.steps, 1):
        typer.echo(f"{i}. {describe(step)}")
    if STATE["dry_run"]:
        typer.secho("(dry run: nothing executed)", fg=typer.colors.YELLOW)
        return True
    if not typer.confirm("Run these now?", default=False):
        typer.echo("Skipped.")
        return False
    try:
        return all(run_step(s) for s in plan.steps)
    finally:
        for p in plan.cleanup:
            if os.path.exists(p):
                os.unlink(p)


def capture(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        raise typer.Exit(fail(f"{argv[0]} not found; run this on the Proxmox host")) from None
    except subprocess.CalledProcessError as e:
        raise typer.Exit(fail(f"{shlex.join(argv)}: {e.stderr.strip()}")) from None


def pvesh(path: str, *args: str) -> object:
    return json.loads(capture(["pvesh", "get", path, "--output-format", "json", *args]))


def node() -> str:
    return socket.gethostname().split(".")[0]


def pve_version() -> tuple[int, int]:
    m = re.search(r"pve-manager/(\d+)\.(\d+)", capture(["pveversion"]))
    if not m:
        raise typer.Exit(fail("cannot parse `pveversion`"))
    return int(m[1]), int(m[2])


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


# ---------- tailnet ----------
@app.command("tailscale-install")
def tailscale_install() -> None:
    """Add Tailscale's apt repo, install a pinned version, and hold it."""
    if shutil.which("tailscale"):
        typer.echo("tailscale is already installed: " + capture(["tailscale", "version"]).splitlines()[0])
        if not typer.confirm("Continue anyway?", default=False):
            return
    codename = dict(l.split("=", 1) for l in Path("/etc/os-release").read_text().splitlines() if "=" in l)["VERSION_CODENAME"].strip('"')
    version = typer.prompt("Tailscale package version to pin (must be one your headscale supports)")
    base = f"https://pkgs.tailscale.com/stable/debian/{codename}"
    keyring = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
    execute(Plan("Install Tailscale", [
        Cmd(["curl", "-fsSL", "-o", keyring, f"{base}.noarmor.gpg"]),
        Write("/etc/apt/sources.list.d/tailscale.list", f"deb [signed-by={keyring}] https://pkgs.tailscale.com/stable/debian {codename} main\n"),
        Cmd(["apt-get", "update"]),
        Cmd(["apt-get", "install", "-y", f"tailscale={version}"]),
        Cmd(["apt-mark", "hold", "tailscale"]),
    ]))


@app.command("tailscale-join")
def tailscale_join() -> None:
    """Join the tailnet with a pre-auth key (read with hidden input, passed by file)."""
    login = typer.prompt("Control server URL, e.g. https://hs.<domain>")
    host = ask_name("Node hostname (hostnames must be unique on the tailnet)", node().lower())
    dns = typer.confirm("Let Tailscale manage DNS? (needed to resolve MagicDNS names; rewrites /etc/resolv.conf)", default=True)
    key = typer.prompt("Pre-auth key carrying the node's tag", hide_input=True)
    keyfile = "/run/pve-setup-authkey"
    shown = f"tailscale up --login-server {login} --auth-key file:{keyfile} --hostname {host} --accept-dns={str(dns).lower()}"
    execute(Plan("Join the tailnet", [
        Write(keyfile, key + "\n", 0o600, secret=True),
        Cmd(["tailscale", "up", "--login-server", login, "--auth-key", f"file:{keyfile}", "--hostname", host,
             f"--accept-dns={str(dns).lower()}"], shown),
        Cmd(["tailscale", "status"]),
    ], cleanup=[keyfile]))


@app.command("heartbeat")
def heartbeat() -> None:
    """Install the Gatus dead-man heartbeat as a systemd timer."""
    host = typer.prompt("Gatus host, e.g. status.ts.<domain>")
    endpoint = typer.prompt("Gatus external endpoint key (<group>_<name>)", default="home_proxmox-heartbeat")
    seconds = ask_int("Push every N seconds", 10, 3600, 60)
    token = typer.prompt("Heartbeat token", hide_input=True)
    if not STATE["dry_run"] and subprocess.run(["getent", "hosts", host], capture_output=True, check=False).returncode != 0:
        typer.secho(f"warning: {host} does not resolve here; the timer will fail until DNS works", fg=typer.colors.YELLOW)
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
    execute(Plan("Install the heartbeat timer", [
        Write("/etc/edge-heartbeat.curl", f'header = "Authorization: Bearer {token}"\n', 0o600, secret=True),
        Write("/etc/systemd/system/edge-heartbeat.service", unit),
        Write("/etc/systemd/system/edge-heartbeat.timer", timer),
        Cmd(["systemctl", "daemon-reload"]),
        Cmd(["systemctl", "enable", "--now", "edge-heartbeat.timer"]),
        Cmd(["systemctl", "start", "edge-heartbeat.service"]),
        Cmd(["systemctl", "status", "--no-pager", "edge-heartbeat.service"]),
    ]))


# ---------- guests ----------
@app.command("vm-template")
def vm_template() -> None:
    """Turn a checksum-verified cloud image into a cloud-init VM template."""
    if pve_version() < (8, 1):
        raise typer.Exit(fail("import-from needs Proxmox VE 8.1 or newer"))
    vmid = free_id("Template VM")
    name = ask_name("Template name", "tpl-cloud")
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
    execute(Plan(f"Create template {vmid} ({name})", [
        get,
        Cmd(["qm", "create", vmid, "--name", name, "--memory", str(mem), "--cores", str(cores), "--net0", f"virtio,bridge={bridge}",
             "--scsihw", "virtio-scsi-single", "--ostype", "l26", "--agent", "enabled=1", "--serial0", "socket", "--vga", "serial0"]),
        Cmd(["qm", "set", vmid, "--scsi0", f"{storage}:0,import-from={image},discard=on,ssd=1"]),
        Cmd(["qm", "set", vmid, "--ide2", f"{storage}:cloudinit", "--boot", "order=scsi0", "--ciuser", user, "--sshkeys", key,
             "--ipconfig0", "ip=dhcp"]),
        Cmd(["qm", "resize", vmid, "scsi0", size]),
        Cmd(["qm", "template", vmid]),
    ], cleanup=[image] if src.startswith("https://") else []))


@app.command("vm-create")
def vm_create() -> None:
    """Clone a VM from a template."""
    rows = pvesh("/cluster/resources", "--type", "vm")
    tpls = [(str(r["vmid"]), f"{r['vmid']} {r.get('name', '')}") for r in rows if r.get("template") == 1]  # type: ignore[union-attr]
    tpl = choose("Template", tpls)
    vmid = free_id("New VM")
    name = ask_name("VM name (use the tailnet hostname you want)")
    storage = choose("Target storage", storages("images"))
    cores = ask_int("Cores", 1, 128, 2)
    mem = ask_int("Memory (MiB)", 256, 1 << 20, 4096)
    size = ask_size("Disk size", "40G")
    ipcfg = ip_config()
    tags = typer.prompt("Proxmox tags (semicolon-separated)", default="")
    start = typer.confirm("Start at boot and start now?", default=True)
    setcmd = ["qm", "set", vmid, "--cores", str(cores), "--memory", str(mem), "--ipconfig0", ipcfg]
    if tags:
        setcmd += ["--tags", tags]
    if start:
        setcmd += ["--onboot", "1"]
    steps: list[Cmd | Write | Fetch | Verify] = [
        Cmd(["qm", "clone", tpl, vmid, "--name", name, "--full", "1", "--storage", storage]),
        Cmd(setcmd), Cmd(["qm", "resize", vmid, "scsi0", size])]
    if start:
        steps.append(Cmd(["qm", "start", vmid]))
    execute(Plan(f"Create VM {vmid} ({name})", steps))


@app.command("lxc-create")
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
           "--ssh-public-keys", key, "--onboot", "1"]
    if docker:
        cmd += ["--features", "nesting=1,keyctl=1"]
    execute(Plan(f"Create container {ctid} ({host})", [Cmd(cmd), Cmd(["pct", "start", ctid])]))


# ---------- storage, backups, access ----------
@app.command("storage-add")
def storage_add() -> None:
    """Add directory or NFS storage."""
    kind = choose("Type", [("dir", "Directory on this host"), ("nfs", "NFS export")])
    sid = ask_name("Storage ID")
    content = typer.prompt("Content types", default="backup" if kind == "nfs" else "backup,iso,vztmpl,snippets")
    if kind == "dir":
        path = typer.prompt("Absolute path (must exist and be mounted)")
        cmd = ["pvesm", "add", "dir", sid, "--path", path, "--content", content]
    else:
        server = typer.prompt("NFS server")
        export = typer.prompt("Export path")
        cmd = ["pvesm", "add", "nfs", sid, "--server", server, "--export", export, "--content", content]
    execute(Plan(f"Add storage {sid}", [Cmd(cmd), Cmd(["pvesm", "status"])]))


@app.command("backup-job")
def backup_job() -> None:
    """Create a scheduled vzdump job."""
    storage = choose("Backup storage", storages("backup"))
    schedule = typer.prompt("Schedule (systemd calendar, e.g. 'sat 02:30' or '03:30')")
    scope = choose("Guests", [("all", "All guests"), ("list", "Specific IDs")])
    ids = typer.prompt("Comma-separated IDs") if scope == "list" else ""
    mode = choose("Mode", [("snapshot", "snapshot"), ("suspend", "suspend"), ("stop", "stop")])
    keep = typer.prompt("Retention (prune-backups)", default="keep-daily=7,keep-weekly=4")
    cmd = ["pvesh", "create", "/cluster/backup", "--schedule", schedule, "--storage", storage, "--mode", mode, "--compress", "zstd",
           "--prune-backups", keep, "--enabled", "1", "--notes-template", "{{guestname}}"]
    cmd += ["--all", "1"] if scope == "all" else ["--vmid", ids]
    execute(Plan("Create the backup job", [Cmd(cmd), Cmd(["pvesh", "get", "/cluster/backup"])]))


@app.command("api-token")
def api_token() -> None:
    """Create a PVE user and a privilege-separated API token with a role you pick."""
    user = typer.prompt("User ID, e.g. automation@pve")
    token = ask_name("Token name")
    role = choose("Role", [("PVEAuditor", "PVEAuditor: read-only"), ("PVEVMAdmin", "PVEVMAdmin: manage guests"),
                           ("PVEDatastoreUser", "PVEDatastoreUser: use storage")])
    path = typer.prompt("ACL path", default="/vms" if role == "PVEVMAdmin" else "/")
    typer.secho("The token secret is printed once, by pveum, straight to this terminal. It is not logged.", fg=typer.colors.YELLOW)
    execute(Plan(f"Create {user}!{token}", [
        Cmd(["pveum", "user", "add", user, "--comment", "created by pve-setup"]),
        Cmd(["pveum", "user", "token", "add", user, token, "--privsep", "1"]),
        Cmd(["pveum", "acl", "modify", path, "--tokens", f"{user}!{token}", "--roles", role]),
    ]))


MENU = [("tailscale-install", "Install Tailscale (pinned)"), ("tailscale-join", "Join the tailnet"),
        ("heartbeat", "Install the Gatus heartbeat timer"), ("vm-template", "Build a cloud-init VM template"),
        ("vm-create", "Clone a VM"), ("lxc-create", "Create an LXC container"), ("storage-add", "Add storage"),
        ("backup-job", "Create a backup job"), ("api-token", "Create an API user and token")]


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, dry_run: bool = typer.Option(False, "--dry-run", help="Show every step, run nothing.")) -> None:
    STATE["dry_run"] = dry_run
    if not dry_run and os.geteuid() != 0:
        raise typer.Exit(fail("run as root on the Proxmox host (or use --dry-run to preview)"))
    if ctx.invoked_subcommand is not None:
        return
    commands = {c.name or c.callback.__name__.replace("_", "-"): c.callback for c in app.registered_commands}
    while True:
        typer.secho("\nProxmox setup", bold=True)
        for i, (_, label) in enumerate(MENU, 1):
            typer.echo(f"  {i}) {label}")
        typer.echo("  q) Quit")
        pick = typer.prompt("Choose", default="q").strip().lower()
        if pick == "q":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(MENU):
            commands[MENU[int(pick) - 1][0]]()  # type: ignore[misc]


if __name__ == "__main__":
    sys.exit(app())
