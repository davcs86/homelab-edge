# Proxmox setup toolkit

`tools/pve-setup.py` is an interactive helper you download and run by hand on the PVE host, as root. Nothing runs from CI.
Every step prints the exact commands and files first and waits for a yes; `--dry-run` prints them and runs nothing.

## Run it

```sh
python3 -m venv ~/pve-setup-venv
~/pve-setup-venv/bin/pip install -r pve-setup.requirements.txt      # pinned; python >= 3.10
~/pve-setup-venv/bin/python pve-setup.py                            # menu
~/pve-setup-venv/bin/python pve-setup.py --dry-run vm-create        # preview one task
```

Download the two files from the repository (`tools/pve-setup.py`, `tools/pve-setup.requirements.txt`).
The log of commands (secrets are never in it) is `~/.pve-setup.log`.

## Tasks

| Command | What it does |
|---|---|
| `tailscale-install` | adds Tailscale's apt repo, installs the version you type, `apt-mark hold` |
| `tailscale-join` | `tailscale up` against your headscale with a pre-auth key (hidden input, passed as a 0600 file, deleted after); asks whether Tailscale may manage DNS |
| `heartbeat` | systemd timer pushing to Gatus; the token sits in a 0600 curl config, not on the command line |
| `vm-template` | checksum-verified cloud image → cloud-init template (needs PVE 8.1+) |
| `vm-create` | full clone from a template with cores, memory, disk, IP and tags |
| `lxc-create` | unprivileged container from a downloaded template; Docker nesting is an explicit question |
| `storage-add` | directory or NFS storage |
| `backup-job` | scheduled `vzdump` job with retention |
| `api-token` | PVE user plus a privilege-separated API token and ACL; the secret is printed once by `pveum` and not logged |

## Before you start

- Key for `tailscale-join`: `preauthkeys create --tags tag:pve --expiration 1h` on the edge (`docs/runbooks/tailnet-devices.md`).
- Token for `heartbeat`: `gatus_heartbeat_pve_token` from `secrets/edge.sops.yaml`; if DNS management stays off, `status.ts.<domain>` will not resolve and the timer fails.
- Cloud image: pick the image and its published SHA-256 yourself; the script refuses to import without a matching checksum. Use an image with `qemu-guest-agent` and `cloud-init`.
- Guests join the tailnet themselves; give each the hostname its tag requires (`docs/runbooks/onboard-service.md`).

## Tests

`python3 -m unittest tools/test_pve_setup.py` (needs the pinned requirements): dry-run only, no host, no root.
It cannot prove the Proxmox commands themselves; preview with `--dry-run` on the host before the first real run.
