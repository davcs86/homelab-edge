# Versions

All pins were at least two weeks old on 2026-10-14; container images are pinned by digest, binaries by checksum.

| Component | Pin | Why this one |
|---|---|---|
| Ubuntu | 26.04 LTS (`ubuntu-26-04-x64`) | your choice; Docker, Tailscale and PGDG all publish `resolute` packages |
| Droplet | `s-1vcpu-2gb-amd`, sfo3 | 2 GB RAM headroom, 50 GB disk (30 GB budget incl. ~15 GB pgBackRest); sfo3 is closest to Hayward |
| headscale | v0.29.4 | v0.29.5 was one day old; 0.29.5's fixes only touch `via` grants, which this policy does not use |
| tailscale (edge + runners) | 1.98.10 | newest client inside headscale 0.29.4's generated capability table (≤1.98); 0.29.4 rejects < 1.80 |
| Caddy | 2.11.6 | 2.11.7 was 11 days old |
| caddy-dns/desec | v1.1.0 | latest release |
| mholt/caddy-ratelimit | commit `5625512f` (2026-06-12) | only tag (v0.1.0) is from 2024; HEAD has been quiet for 4 months |
| Gatus | v5.37.0 | newest ≥ 2 weeks; checks, heartbeats and ntfy alerts are declared in the repo |
| ntfy | v2.28.0 | v2.29/2.30 under two weeks; 2.28 already has declarative users/ACLs/tokens |
| Docker CE / CLI | 29.8.2 | 29.9.x under two weeks |
| containerd.io / compose / buildx | 2.3.6 / 5.5.1 / 0.37.1 | newest ≥ 2 weeks for 26.04 |
| pgBackRest | 2.59.2 (PGDG) | 2.59.3 under two weeks; **must equal the DB host's version** |
| ansible-core | 2.21.4 | 2.21.5 under two weeks |
| collections | general 13.4.0, docker 5.3.0, posix 2.2.2, sops 2.4.0, crypto 3.4.0 | newest ≥ 2 weeks |
| sops | 3.13.3 | SSH-key age recipients need ≥ 3.10 |
| age (on edge) | Ubuntu archive 1.2.1 | supports ssh-ed25519 recipients |
| typer (`tools/pve-setup.py`) | 0.27.3 | released 2026-10-06, newest ≥ 2 weeks; transitive pins in `tools/pve-setup.requirements.txt` |
| tailscale in `templates/app-deploy/deploy.yml` | 1.98.10 | same pin and checksum as the edge; update both together |
| 1password/load-secrets-action (`templates/app-deploy`) | v5.0.1, commit `70062d7a876d3eb6334754fa26efd2fbd90c32f2` | released 2026-08-18; v5 is newer than the v4 in 1Password's docs examples. Its Workload Identity mode is public preview and needs a GitHub secret too, so service accounts are used. |
| 1Password CLI (`templates/app-deploy`) | 2.40.0, zip sha256 `74277219e8da60958c00f9aee9d2023225e98fdda8bfd2156a5d9e85e0edaab3` | released 2026-10-02; 2.41.x under two weeks. The zip's `op.sig` verified against 1Password's code-signing key `3FEF 9748 469A DBE1 5DA7 CA80 AC2D 6274 2012 EA22` when pinned. The template installs it first, because the action otherwise downloads an unverified "latest". |
| actions/checkout, setup-python | v7.0.1, v7.0.0 by commit | newest ≥ 2 weeks |
| actions/create-github-app-token (`init-secrets`, `node-admin`, `deploy`) | v3.2.0, commit `bcd2ba49218906704ab6c1aa796996da409d3eb1` | released 2026-05-12; v3.3.0 (2026-10-12) is under two weeks. Uses `client-id`: `app-id` is deprecated in v3. |
| age and smartmontools (Proxmox host, used by `tools/pve-setup.py`) | Debian archive packages | not pinned: the toolkit calls only `age -r/-o/-d/-i`, `age-keygen` and `smartctl -H`, stable across releases, and the host gets security updates from its own apt. The `tools` CI job installs `age` from the Ubuntu archive for the same reason. |

Ubuntu-archive packages (fail2ban, chrony, sqlite3, age, …) are pinned by the release and receive security updates
through unattended-upgrades. Third-party packages (Docker, Tailscale, pgBackRest) are pinned and `apt-mark hold`, and
unattended-upgrades is told not to touch them.
