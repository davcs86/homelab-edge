# Backups: agreements and next steps (2026-11-22)

Status log for the home-side backup work. "Decided" means the owner said so; "recommended" means Claude proposed it and the owner has
not ruled on it. A check is marked **seen** only when its output was read in this work; everything else is open.
The Postgres home-side copy (repo2) is designed in `docs/superpowers/specs/2026-11-22-pgbackrest-home-repo-design.md` (PR #105);
this file does not repeat it.

## Where things stand

| Area | State | Evidence |
|---|---|---|
| Headscale + happybabytracker archive, edge to home VM | Working | **seen:** `backup ok: …`, `restore drill OK`, `Result=success`; both archives and `.sha256` on the VM |
| Gatus `edge_nightly-backup` | Reported green by the owner | owner's report |
| Backup host key pin (`backup_host_known_hosts`) | Merged (#107), **not yet deployed** | needs **deploy**, then one more `edge-backup.service` run |
| pgBackRest repo1 on the edge (stanza `main`) | Working since 2026-10-27 | **seen:** `status: ok`, `check` exits 0 twice, WAL `…04` to `…C9`, Gatus `edge_pgbackrest-backup` green 7 days (owner) |
| repo1 size | 1.3 GB of the 15 GB budget (archive 952 MB, backups 347 MB); droplet disk 9.3 GB used of 48 GB | **seen** |
| Postgres cluster growth | 40 MB (10-27), 64 MB (11-15), 374 MB (11-21); about 24 WAL segments a day, about 113 MB a day compressed | **seen**, one week of staging backfill |
| Home-side Postgres copy (repo2) | Designed, not decided | PR #105, open questions Q1–Q11 and spikes S1–S4 |
| Restore proof for Postgres (full and PITR, timed) | Not done | |
| PVE host | firewall not running, password SSH not disabled, no vzdump jobs, no config-backup timer | **seen:** `pve-setup.py status` |

## Agreements (decided by the owner)

1. The home backup host is a **Proxmox VM** (`backup`, ID 105, Debian 13, 1 core, 1 GiB, system disk on `local-zfs`, DHCP), cloned from `tpl-debian13`.
2. A **stopgap** until a dedicated disk exists: a 16G vdisk on storage `vm-105-backup-stopgap` (dataset `rpool/data/backup-stopgap`, quota 20G), ext4 at `/home/edge-backup/headscale`.
3. VM-side retention: **14 days, never fewer than the newest 14 archives** (`edge-backup-prune.timer`, daily 05:00 UTC), because the edge's rrsync is write-only and cannot prune.
4. The Postgres design runs as its own session and is reviewed in PR #105 before any code.
5. The pgBackRest backup runs from the edge (already built); this work did not change it.

## Recommended, not yet decided

- The dedicated disk is an **M.2 NVMe in the free slot** (OCuLink only if the slot is needed elsewhere), added as its own Proxmox storage, not passed through, then `qm disk move 105 scsi1 <storage>`.
- Extend `pve-setup.py heartbeat` from one hard-coded NVMe to a list, so the new disk is monitored (a PR with its own pass condition: the second device's failing `smartctl -H` reports a distinct cause).
- Add a vzdump job for the DB LXC (design option B in #105; the owner chooses whether it belongs in the repo2 change, Q10).

## Next steps, in order

| # | Step | Pass condition |
|---|---|---|
| 1 | Run **deploy** (tailnet) | the edge's `/etc/edge-backup/known_hosts` holds the pinned line |
| 2 | On the edge: `sudo systemctl start edge-backup.service` | journal ends `backup ok: …`, `Result=success`, run now uses `StrictHostKeyChecking=yes` |
| 3 | After 11:00 UTC today: `sudo -u pgbackrest pgbackrest info` and `… verify` | a third full appears and `verify` is clean |
| 4 | Decrypt one archive with the offline key (`recovery.md`) | `age -d -i backup edge-….tar.age \| tar -x` yields `headscale/db.sqlite` |
| 5 | Re-read repo size after two more fulls (about 2026-12-06) | archive under about 4 GB; larger means re-check the WAL rate before sizing repo2 |
| 6 | Answer PR #105 questions Q1–Q11, in particular repo encryption (Q11), who holds the restore identity (Q5, Q6), and whether repo2 may ever block WAL archiving (Q3) | the design doc loses its "DRAFT" status |
| 7 | Run spikes S1–S4 on a scratch PG18 pair | each spike's observed behaviour recorded in the design doc |
| 8 | Prove a Postgres restore: one full and one PITR from repo1, timed (`db-host.md` step 9) | restored DB starts, `pg_isready` answers, a canary query matches the source |
| 9 | Buy and fit the M.2 disk; `storage-add`; `qm disk move 105 scsi1 <storage>`; extend the PVE heartbeat | `findmnt` on the VM unchanged, next `edge-backup.service` run succeeds, the heartbeat covers both devices |
| 10 | PVE host hardening: `firewall`, `ssh-harden`, `remote-gate`, `config-backup-timer`, `zfs-reserve` (`proxmox-setup.md`) | `host-config-check` and `service-check` exit 0 |

## Calendar and housekeeping

- pgBackRest leaf certs last 3 years from the day `init-secrets` issued them and the CA 10 years (`scripts/init_secrets.py`). Read the real date with
  `openssl x509 -noout -enddate -in /etc/pgbackrest/tls/server.crt` on the edge and put a reminder a month before it; backups began in October 2026, so expect about October 2029. No rotation runbook exists yet (Q6 in #105).
- The design doc in #105 states the backup host key is still `TODO(provide)` and the host a "SATA backup host"; both are out of date after #107. Fix when #105 is reviewed, and the "SATA backup host" comment in `policy/policy.hujson`.
- Until the dedicated disk is in, the home archives share the Proxmox pool's failure domain; the droplet keeps 7 days independently.
