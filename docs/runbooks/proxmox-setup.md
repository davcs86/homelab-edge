# Proxmox setup toolkit

`tools/pve-setup.py` is an interactive helper you download and run by hand on the PVE host, as root. Nothing runs from CI.
Every step prints the exact commands and files first and waits for a yes; `--dry-run` prints them and runs nothing
(it still reads the host, so it shows what would be skipped).

## Run it

```sh
python3 -m venv ~/pve-setup-venv
~/pve-setup-venv/bin/pip install -r pve-setup.requirements.txt      # pinned; python >= 3.10
~/pve-setup-venv/bin/python pve-setup.py                            # menu: set up, inspect, undo, config backup
~/pve-setup-venv/bin/python pve-setup.py --dry-run vm-create        # preview one task
~/pve-setup-venv/bin/python pve-setup.py status                     # or: status --json
```

Download `tools/pve-setup.py` and `tools/pve-setup.requirements.txt`. The command log is `~/.pve-setup.log`
(secrets are never in it).

## Set up

| Command | What it does |
|---|---|
| `tailscale-install` | repo + pinned package + `apt-mark hold`; skips what is already there, and can move a held package to another version |
| `tailscale-join` | `tailscale up` with a pre-auth key (hidden input, 0600 file, deleted after); already-joined hosts are left alone, a changed hostname/server/DNS setting re-authenticates with `--reset --force-reauth` |
| `heartbeat` | systemd timer pushing to Gatus; rewrites only the files that differ (the token file is compared but never printed) |
| `vm-template` | checksum-verified cloud image → cloud-init template (PVE 8.1+) |
| `vm-create` | full clone: cores, memory, disk, IP |
| `lxc-create` | unprivileged container; Docker nesting is an explicit question |
| `storage-add` | directory or NFS; identical existing storage is skipped, a different one is an error |
| `backup-job` | vzdump job with id `pve-setup-<name>`; created, left alone when identical, or updated in place |
| `api-token` | user + privilege-separated token + ACL; each missing piece is created, so a half-finished run resumes |

Re-running a task does the missing part and nothing else. `vm-template`, `vm-create` and `lxc-create` always make a new guest
(IDs cannot collide, and a same-named guest needs a yes). A plan that fails part-way offers the exact commands that undo what
it already did, and you decide; declining leaves things as they are and a re-run resumes.

## Inspect

`status` lists the host, Tailscale, the heartbeat, every guest, storage, backup jobs and toolkit users. It is read-only and marks
what the toolkit created with `*`.

## Undo

The toolkit marks what it creates: Proxmox tag `pve-setup` on guests, job ids `pve-setup-*`, user comment `created by pve-setup`.
Undo touches only marked things, and each one needs you to type the target's ID or name.

| Command | Effect |
|---|---|
| `tailscale-leave` | `tailscale logout`; optionally unholds and removes the package and apt repo. The node stays in headscale: `node-admin → remove-device` |
| `heartbeat-remove` | stops the timer, removes its units and token file |
| `vm-destroy` | stops (if running) and destroys the guest **and its disks**; backups already taken are kept |
| `storage-remove` | drops the definition; the data is not touched, but guests with disks there lose access until it is re-added |
| `backup-job-remove` | drops the job; existing backups stay |
| `api-token-revoke` | revokes one token or deletes the user |

## Config backup and restore

`config-backup` writes a `0600` `.tar.gz` of `/etc/pve` (storage, users, jobs, guest configs, firewall), network, hosts, fstab, apt
sources, sshd drop-ins and the files the toolkit wrote, plus an inventory of guests, storage and jobs, then re-reads it and checks every
checksum. It never includes guest disks: use `backup-job` for those. **Secrets are off by default** (`/etc/pve/priv`, `*.key`, the
heartbeat token, Tailscale's node key); if you include them, keep the archive off the host and encrypted.

`config-restore ARCHIVE` verifies the archive (checksums, no absolute or `..` paths, no links, size cap) and prints a plan: which files
are unchanged, differ or are missing, which guests existed then and are missing now, and warnings for the risky ones. With `--stage DIR`
it extracts the differing files into a new `0700` directory and prints an `install -D -m …` line per file; **those commands are for you
to review and run**. The only thing it applies itself is `--apply-owned`: the files this toolkit wrote (heartbeat units, the Tailscale
apt list), after one confirmation. Cluster config, network and fstab are never applied automatically.

## Before you start

- Key for `tailscale-join`: `preauthkeys create --tags tag:pve --expiration 1h` on the edge (`docs/runbooks/tailnet-devices.md`).
- Token for `heartbeat`: `gatus_heartbeat_pve_token` from `secrets/edge.sops.yaml`; if DNS management stays off, `status.ts.<domain>` will not resolve and the timer fails.
- Cloud image: pick the image and its published SHA-256 yourself; the script refuses to import without a matching checksum.
  Use an image with `qemu-guest-agent` and `cloud-init`.
- Guests join the tailnet themselves; give each the hostname its tag requires (`docs/runbooks/onboard-service.md`).

## Tests

`python3 -m unittest tools/test_pve_setup.py` (needs the pinned requirements): every probe and command is faked, so no host or root.
It cannot prove the Proxmox commands themselves; preview with `--dry-run` on the host before the first real run.
