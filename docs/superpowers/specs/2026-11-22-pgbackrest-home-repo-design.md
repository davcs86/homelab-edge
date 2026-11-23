# pgBackRest home-side repository (repo2) — design

Status: DRAFT for owner review, 2026-11-22. Design only: no code, no implementation plan. Stops before `impl-plan`.
Nothing below is claimed as met; every check in § Pass condition is still to be run.

Method note: the `design-buddy:design-debate` skill was not installed in this session (not in the skill list, not in the
repo). The same debate was run by hand: options, strongest case for and against each, kill criteria, then a
recommendation. Rerun the skill if you want its exact format.

## Intent

PostgreSQL has one backup copy today: repo1 on the edge droplet (`/var/lib/pgbackrest`, 2 full / 6 diff, 15 GB budget on a
50 GB disk). Add a second copy in the house, so a restore does not depend on the droplet, the tailnet's edge hop or the
internet.

What this does **not** buy, stated up front so it is not mistaken for more:

- Same Proxmox host as the DB guest. A host loss (PSU, fire, theft, ransomware on PVE) takes the DB and repo2 together; only repo1 survives.
- Until the M.2 arrives the disk is a vdisk on the NVMe pool that also holds the DB guest. One device failure takes both.
- So repo2 is a **fast, droplet-independent local copy**. Whether it counts as an independent copy is an owner call (Q2).

## Verified facts (re-checked against `e8bbe69`, 2026-11-22)

| Fact | Where |
|---|---|
| repo1 only, retention 2 full / 6 diff, path `/var/lib/pgbackrest`, port 8432, stanza `main` | `inventories/prod/group_vars/all/main.yml:136-145`, `roles/pgbackrest_repo/tasks/main.yml` |
| The edge is the repository host: it runs `backup` (systemd timers, Sun full / other days diff, 03:00 LA), pulls from the DB host's TLS server, heartbeats Gatus after each success | `roles/pgbackrest_repo/tasks/main.yml`, `docs/decisions.md` § heartbeat |
| DB host pushes WAL with `archive_command = pgbackrest --stanza=main archive-push %p`, **synchronous**: no `archive-async`, no `spool-path`, no `archive-push-queue-max` anywhere in `roles/db_host` | `roles/db_host/templates/10-xs.conf.j2:13`, `pgbackrest.conf.j2` |
| pgBackRest 2.59.2 on both sides (edge `2.59.2-1.pgdg26.04+1`, DB `2.59.2-1.pgdg13+1`); a Debian 13 backup VM can reuse the DB host's pgdg13 pin | `versions.yml:42,47` |
| Policy: `tag:db`<->`tag:edge` 8432 (+443 heartbeat); `tag:edge`->`tag:backup` tcp:22 only; nothing else touches `tag:backup` except `group:admins` | `policy/policy.hujson:36,51-53` |
| Backup account today is `rrsync -wo ~/headscale`; `backup_host_known_hosts` is still `TODO(provide)` (host not yet a tailnet node) | `docs/runbooks/home-side.md:5`, `main.yml:135` |
| pgBackRest PKI: one private CA (10y), 3y leaves, from `scripts/init_secrets.py`; the edge gets `ca.crt` and its own leaf, not the CA key | `init_secrets.py:59-174` |
| pve heartbeat watches one pool (`rpool`) and one device (`/dev/nvme0`) | `tools/pve-setup.py` `heartbeat`, `HEARTBEAT_SCRIPT_TEXT` |
| **Drift:** the DB host is documented as a Debian 13 **LXC** (`docs/runbooks/db-host.md:3`); the brief says "DB guest". The policy comment calls the backup host a "SATA backup host" | `db-host.md`, `policy.hujson:18` |

Upstream behaviour relied on (pgBackRest docs and source, read today; not yet reproduced on our versions, hence spike S1-S4):

- `archive-push` always pushes to **all** configured repos; `--repo` is not valid for it. Every repo is reachable-checked; an unreachable one is skipped for the check, but any per-repo I/O error is thrown back to PostgreSQL, so the segment stays in `pg_wal` and is retried.
- Docs: with `archive-async=n` the healthy repos can only get one segment ahead of the unreachable one; `archive-async=y` is needed to keep them advancing. If WAL cannot be pushed anywhere, `pg_wal` grows and can fill the volume.
- `archive-push-queue-max` bounds `pg_wal` growth by **dropping** WAL (reports success to PostgreSQL), which breaks PITR past that point **in every repo**.
- With a separate repo host the `backup` command runs on the repo host, not on the DB host.

## Assumptions (each with its basis; none is a decision)

| # | Assumption | Basis | If wrong |
|---|---|---|---|
| A1 | The DB host is an unprivileged LXC with a separate data volume (`mp0`, `backup=1`) | `db-host.md:3,50`, `proxmox-setup.md:30` | Option B changes: VM vzdump differs from LXC |
| A2 | The backup VM runs its own `pgbackrest server` and its own `backup`/`expire`, exactly like the edge | upstream: `backup` runs on the repo host; the pattern is already in `roles/pgbackrest_repo` | Topology in § Recommended design changes |
| A3 | The backup VM's local repo is `repo1` in its own config, while the DB host calls it `repo2` (numbering is per config file) | pgBackRest config model; **unverified** | Restore and `--repo=` examples change (S2) |
| A4 | No backup bandwidth concern: ~750 MB of data on the LAN | brief | none material |
| A5 | WAL volume per day is unknown | not in repo | sizing and Q1/Q3 numbers wait on a measurement |
| A6 | pgBackRest on the backup VM can be the same pin as the DB host (`2.59.2-1.pgdg13+1`); no new package | `versions.yml:47` | a new pin row in both files (rule 4) |
| A7 | The backup VM will join the tailnet as `backup` with `tag:backup` before any of this is deployed | `backup_tailnet_hostname`, `home-side.md:5` | blocks everything; it is a prerequisite, not part of this change |
| A8 | The deploy path to configure the backup VM is Ansible from CI over the tailnet (rule 5) | `CLAUDE.md` rule 5; `playbooks/db.yml` precedent | Q8 |

## Pass condition (defined now; to be proven, not asserted)

1. `pgbackrest --stanza=main check` exits 0 **on the DB host** and its output shows archiving verified for both repos (it forces a WAL switch and checks each repo). Same command exits 0 on each repo host.
2. `pgbackrest --stanza=main info` shows, for each repo, a full backup and a WAL range that advances; `verify` is clean on repo2.
3. **Restore from repo2 alone** on a scratch host whose `pgbackrest.conf` contains no repo1 settings and no repo1 key material: restore to latest, and PITR to a chosen timestamp; PostgreSQL starts, `pg_isready` answers, and a row-count / canary query on the xstockstrat-staging database matches the source.
4. Failure injection (S3): with repo2 down for the Q3-agreed duration, the observed `pg_wal` growth and PostgreSQL availability match what the owner chose in Q3.
5. Public surface unchanged: 80/tcp, 443/tcp, 3478/udp only. Evidence: `scripts/policy-test.sh` (new grants have accept and deny cases) and the `verify` playbook; the backup VM exposes nothing public.
6. No secret in repo, logs or commit message: new key material only through `init_secrets.py` into SOPS; tasks handling it use `no_log: true`; `scripts/secrets-keys-test.py` and `scripts/init-secrets-test.py` updated and green.
7. Every new pin in `versions.yml` and `docs/versions.md` in the same change (likely none, A6).
8. `ci` green, plus the updated `docs/runbooks/db-host.md`, `home-side.md`, `proxmox-setup.md` (Teardown rule) and one `docs/decisions.md` entry.

The scratch host for item 3 does not exist yet (Q5).

## Options

### A. pgBackRest multi-repo, repo2 on the backup VM (recommended)

DB host config gains `repo2-host=backup…`, `repo2-host-type=tls`, `repo2-host-ca-file/cert/key`. The backup VM runs
`pgbackrest server` (tailnet address only, mTLS, `tls-server-auth=db-pgbackrest=main`), and its own systemd timers run
`backup` pulling from the DB host's server on 8432, followed by `expire`, then a heartbeat. WAL arrives by the DB host's
`archive-push` to both repos.

For: native, tested pgBackRest path; PITR in repo2; same operational shape as repo1, so one runbook pattern, one
`check`; restore tooling is the tool you already know; backups are checksummed and `verify`-able.

Against: couples PostgreSQL's WAL pipeline to a **second** remote that shares a Proxmox host with it (VM reboots,
`qm disk move`, PVE patching all become archive failures); one more mTLS identity and server to run; the DB host's
conf and the backup VM's conf number the same repo differently (A3); new grants.

Kill criteria: S1/S3 show WAL stalls whenever repo2 is down and Q3 cannot accept the bounded options; or S2 shows the
asymmetric repo numbering cannot be made to work with one stanza.

### B. Proxmox vzdump of the DB guest onto the new storage (complementary, not a substitute)

For: restores the whole container (config, packages, `pg_hba`, certs) in minutes; no coupling to the WAL path; uses
`pve-setup.py backup-job` that already exists.

Against: crash-consistent snapshot only, no PITR and no consistency proof (a ZFS or LVM snapshot of a running
PostgreSQL recovers via WAL replay, but nothing in vzdump checks that); the target must be Proxmox **storage**, not a
path inside the backup VM, so it lands on the same M.2 or pool as repo2 (the same failure domain, Q2); for an LXC the data volume is only included with `backup=1`
(`proxmox-setup.md:30`); size scales with the whole disk, not 750 MB of data.

Verdict: not a replacement for A. Worth doing for "rebuild the LXC quickly", but its restore proof is a different drill. Keep out of this change unless Q10 says otherwise.

### C. rsync of the repo directory

Rejected. A live repo (backup in progress, `expire`, WAL arriving) copied file by file can be internally inconsistent;
pgBackRest gives no guarantee about a torn copy and `verify` only finds out later. Also forces an rsync trust path from
`tag:edge` into a repo it should not write. Do not revisit unless the edge is quiesced (`stop` then copy), which loses the point of a live copy.

### D. repo2 as S3-compatible object store (MinIO or similar) on the backup VM

For: pgBackRest writes with object-store semantics, no repo-host protocol, no `tls-server-auth`.
Against: a new service to patch, pin and monitor (more attack surface than a second `pgbackrest server`, which is the same binary already pinned); S3 credentials are a second secret type; still the same WAL coupling as A.
Rejected on "minimum that solves the problem" (CLAUDE.md § How to Act 2).

### E. Replicate repo1 to the home VM (the edge pushes or the VM pulls the repo)

Same torn-copy problem as C unless pgBackRest is the transport; pgBackRest has no repo-to-repo copy command. Rejected.

### F. Decouple the WAL path: `pg_receivewal` or a hot standby on the backup VM

For: removes WAL coupling entirely (a streaming consumer is not on PostgreSQL's commit path).
Against: introduces a replication slot (which itself can fill the DB disk), a second Postgres 18 install, a restore procedure outside pgBackRest, and a different toolchain to drill. Out of scope; recorded as the escape hatch if S3 shows the coupling is unacceptable.

## Recommended design (A, with B deferred)

```
DB host (tag:db) ── archive-push (sync today) ──► edge repo1   (tcp 8432, mTLS)
        │                                         ▲  pull full/diff (edge runs backup)
        └─────── archive-push ───────────────► backup VM repo   (tcp 8432, mTLS)  = "repo2" on the DB host
                                                  ▲  pull full/diff (backup VM runs backup + expire)
                  backup VM ── tcp 8432 ────────► DB host pgbackrest server
```

Delta, by area (names are proposals, not decisions):

- **pgBackRest**: DB host `repo2-*` settings; a new role (working name `pgbackrest_home_repo`) on the backup VM mirroring `roles/pgbackrest_repo`, with its own `pgbackrest` system user and its own filesystem, separate from the `~/headscale` rrsync account. Retention set on the backup VM (Q1).
- **Archive coupling**: policy choice in Q3. Whatever is chosen, make the failure visible: a `pg_wal` / `archive_status` size check in the DB heartbeat (extends `db-heartbeat`, `proxmox-setup.md:27`) with a distinct error cause, so a stuck repo2 pages before the data disk does.
- **Policy** (`policy/policy.hujson`): `tag:db`->`tag:backup` tcp:8432; `tag:backup`->`tag:db` tcp:8432; `tag:backup`->`tag:edge` tcp:443 (heartbeat push). Add accept and deny cases to the `tests` block, including that `tag:ci`, `tag:agent` and `tag:xstockstrat*` still cannot reach `tag:backup:8432`, and that the existing `tag:edge`->`tag:backup` stays port 22 only. Nothing public changes.
- **Secrets** (`init_secrets.py`, `secret_keys.py`, SOPS): new leaves from the existing pgBackRest CA: a server cert for the backup VM and a client cert for its pulls (new CN, distinct from `db-pgbackrest` and `edge-pgbackrest`); the DB host's existing client cert is reused to push. `--fill-missing` must create the set whole and refuse a partial one, like the existing groups. The CA key never goes to the backup VM.
- **Monitoring**: a Gatus external endpoint for the backup VM's backups (EDGE-10 is a URL contract: key, token, and the pusher must change together), alert on silence like the edge's 26 h endpoint; a disk-full / disk-unreadable check for the repo filesystem; the pve heartbeat extended to the new M.2 (its SMART check is hard-coded to one device; Q4).
- **Docs**: `db-host.md`, `home-side.md`, `proxmox-setup.md`, `docs/versions.md` row, `docs/decisions.md` entry, and fix the "SATA backup host" policy comment.

## Failure-mode review (the part most likely to cause rework)

| Failure | Today (repo1 only) | With repo2 | Mitigation to decide |
|---|---|---|---|
| repo2 VM down for hours (reboot, `qm disk move`, PVE patching) | n/a | sync archive-push errors; segments pile up in `pg_wal`; healthy repo1 also lags (docs: one segment ahead) | Q3 |
| edge down | same coupling already exists, unbounded | same | the bound in Q3 also fixes repo1 |
| repo2 disk fills | n/a | same as above, plus `backup` fails | retention sizing, disk alert |
| Proxmox host down | DB down too; repo1 survives | repo2 down too; repo1 survives | accepted, Q2 |
| `qm disk move` to M.2 | n/a | VM stalls or reboots mid-copy | schedule inside a window; Q3 numbers must cover it |
| backup VM compromised | n/a | it holds the repo and a client cert that can pull from the DB host's 8432; `tls-server-auth` limits it to stanza `main` | repo-level encryption is **not** configured today (`repo1-cipher-*` absent): a stolen repo is plaintext. Q11 |
| CA/leaf expiry (3y leaves, 10y CA) | silent TLS failure | two more leaves | Q6 and an expiry check |
| pgBackRest version skew between three hosts | pinned to one | three | one pin variable family, bumped together (`db-host.md:126`) |

## Spikes (cheap, before any implementation plan)

Run on a scratch PG18 + pgBackRest 2.59.2 pair, not on prod:

- **S1** Confirm sync `archive-push` behaviour with one of two repos unreachable: what the log says, how far each repo advances, what PostgreSQL does.
- **S2** Confirm one stanza can have the DB host's `repo2` be the backup VM's local `repo1`: `stanza-create`, `check`, `info`, `restore --repo`. (A3.)
- **S3** With `archive-async=y`, `spool-path` and `archive-push-queue-max` set at the Q3 numbers: kill repo2, observe `pg_wal`, observe what is lost when the max is hit and that `check` still behaves.
- **S4** Whether a restore with only repo2 configured, on a host with no repo1 material, completes (pass item 3 on a small data set).

## Questions for the owner (not guessed; the design waits on these)

1. **Retention for repo2.** Same 2 full / 6 diff, or longer? Current data is ~750 MB, so disk is cheap; the constraint is how far back you want PITR. I will not set a number without an answer; I need the WAL/day figure first (`pgbackrest info` on repo1 can give it).
2. **Failure domain until the M.2 arrives.** Confirm that the DB guest's disk and repo2's vdisk share the same NVMe pool and fate, and that you accept repo2 being labelled "fast local copy, not independent" until the M.2 is in and holds its own storage. Is the M.2 going to be its own Proxmox storage (a pool or LVM-thin) or passed through to the VM?
3. **May repo2 ever block WAL archiving, and so PostgreSQL?** Pick one, with numbers: (a) block and alert (sync, today's semantics; PostgreSQL stops at disk-full, which also fixes nothing for repo1); (b) `archive-async=y` + `spool-path` + `archive-push-queue-max=<size>`: bounded, but at the limit WAL is dropped **for both repos** and PITR has a gap until the next full; (c) async with a large spool and an alert at N% of `pg_wal`, no drop. Also: how long an outage must the choice survive (a `qm disk move` window? a weekend?), and how much `pg_wal` headroom does the DB LXC have?
4. **Monitoring.** Is a new Gatus endpoint for repo2's backups plus a repo filesystem check on the VM acceptable, and may `pve-setup.py heartbeat` be extended from one hard-coded NVMe to a list so the M.2 is covered? (Today it would not be.)
5. **Restore-drill cadence, and where is the scratch host?** Monthly, quarterly, after each change? It needs no prod secrets beyond what restore needs; do you want a throwaway LXC for it, and who holds the repo2 client cert it needs?
6. **Who owns repo2 TLS CA rotation**, and one CA for both repos (recommended: simplest, the CA key stays in SOPS only) or a separate CA per repo (rotation independence; pgBackRest allows a CA per repo)? Today there is no documented rotation procedure for the existing CA either.
7. **Confirm the DB host is the LXC `db` in `db-host.md`**, not a VM. It decides what Option B would do.
8. **How is the backup VM configured?** A new `playbooks/backup.yml` run by CI means a new grant (`tag:ci` or `tag:deploy` -> `tag:backup:22`) next to the forced-command rsync account; by hand means breaking rule 5. Which?
9. **Disk sharing.** Must headscale age archives and the pgBackRest repo share a filesystem? I propose separate filesystems or a quota so a repo growth spike cannot starve the headscale archive. OK?
10. **Is Option B (vzdump) in this change or a follow-up?** I recommend a follow-up with its own pass condition.
11. **Repo encryption.** Neither repo has `cipher-*` today. Add `repo2-cipher` (key in SOPS), or accept a plaintext repo on a VM in your house as the same trust level as the DB?

## Out of scope

Replication or standby, changing repo1, moving the backup VM's M.2 over to it, vzdump (Q10), any code.

## Next step

Answer Q1-Q11, run S1-S4, then `impl-plan`. Until the answers land, no value in this document is a decision.
