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
| `tailscale-install` | repo + the edge's pinned package (default 1.98.10, checked to exist in apt first) + `apt-mark hold` + `systemctl enable --now tailscaled`; skips what is already there, and can move a held package to another version |
| `tailscale-join` | `tailscale up` as node `pve` with `--accept-dns=false --accept-routes=false` (a different name is warned about; DNS stays off unless you say yes). The pre-auth key goes through a hidden prompt and a 0600 file; an empty key prints a login URL to approve with `node-admin -> approve-device`. Afterwards it checks the node registered as `pve` (not `pve-1`), prints the tailnet IP, pings the edge and lists the admin-side checks. Already-joined hosts are left alone; a changed setting re-authenticates with `--reset --force-reauth` and asks first when your SSH session runs over the tailnet |
| `heartbeat` | systemd timer pushing to Gatus; the edge's tailnet IP is pinned in the curl config (`resolve =`) so it works with Tailscale DNS off; rewrites only the files that differ (the token file is compared but never printed) |
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

- Key for `tailscale-join`: `preauthkeys create --tags tag:pve --expiration 1h` on the edge (`docs/runbooks/tailnet-devices.md`), or skip it and approve the login URL. If an old `pve` or `pve01` node exists, remove it first (`node-admin -> list-devices`, `remove-device`), or the new one registers as `pve-1`.
- Token for `heartbeat`: `gatus_heartbeat_pve_token` from `secrets/edge.sops.yaml`; the edge's tailnet IP from `ssh david_admin@edge tailscale ip -4`.
- Cloud image: pick the image and its published SHA-256 yourself; the script refuses to import without a matching checksum.
  Use an image with `qemu-guest-agent` and `cloud-init`.
- Guests join the tailnet themselves; give each the hostname its tag requires (`docs/runbooks/onboard-service.md`).

## Tests

`python3 -m unittest tools/test_pve_setup.py` (needs the pinned requirements): every probe and command is faked, so no host or root.
It cannot prove the Proxmox commands themselves; preview with `--dry-run` on the host before the first real run.
