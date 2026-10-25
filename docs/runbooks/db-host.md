# Database host (`tag:db`)

One Debian 13 LXC (`db`) runs PostgreSQL 18 with TimescaleDB (Apache build), PgBouncer and pgBackRest for every workload.
`playbooks/db.yml` (role `db_host`) configures it; the container itself comes from `tools/pve-setup.py lxc-create`.

| Port | Service | Reached by |
|---|---|---|
| 5432 | PostgreSQL, TLS only | config, ledger, identity, notify, ui and the migrator (they need `LISTEN/NOTIFY` and advisory locks) |
| 6432 | PgBouncer, transaction mode, TLS | trading, portfolio, marketdata, indicators, ingest, analysis (`DB_PGBOUNCER=true`) |
| 8432 | pgBackRest TLS server | the edge, which pulls backups (`tag:edge`) |
| 22 | sshd | `tag:ci` as `deploy` (this playbook), admins as `david_admin` |

Per workload (`db_workloads` in `group_vars/all/main.yml`): database `xstockstrat-<env>`, owner role `xstockstrat_<env>`, a
`CONNECT` grant for that role and `admin` only, a `hostssl` line for that workload's tailnet IP, a PgBouncer pool named after the database.

| Role | Can do | Reaches the host from | Connections | SOPS key |
|---|---|---|---|---|
| `xstockstrat_production`, `xstockstrat_staging` | own their database (schemas, hypertables, data) | that workload's tailnet IP (TLS), loopback for PgBouncer | 25 each | `db_role_xstockstrat_<env>_password` |
| `admin` | read only: `pg_monitor`, `pg_read_all_data` | devices in `db_admin_tailnet_hosts` (empty), loopback | 3 | `db_role_admin_password` |
| `superadmin` | everything (PostgreSQL superuser), for manual work | devices in `db_superadmin_tailnet_hosts` (`macbook-pro`) over TLS, loopback; never PgBouncer | 3 | `db_role_superadmin_password` |
| `postgres` (OS user) | superuser over the local socket | `ssh david_admin@db…`, then `sudo -u postgres psql` (key only, passwordless sudo) | n/a | none (peer authentication) |

## Manual access

From `macbook-pro` (it must be on the tailnet under that name and owned by `david_admin`, and the **db-host** workflow must have run while
it was online, because that run writes its `hostssl` line):

```sh
export SOPS_AGE_SSH_PRIVATE_KEY_FILE=~/keys/sops_admin
PGPASSWORD="$(sops decrypt --extract '["db_role_superadmin_password"]' secrets/edge.sops.yaml)" \
  psql "host=db.ts.natanddavehouse.com user=superadmin dbname=postgres sslmode=require"
```

`sslmode=verify-full` with `sslrootcert=<file holding db_tls_ca_crt>` also verifies the server (the certificate's name is `db.ts.natanddavehouse.com`).
Use `superadmin` for repairs, restores and `ALTER` work; use the owner roles through the app, and `admin` for looking around. Without the
laptop on the tailnet: `ssh david_admin@db.ts.natanddavehouse.com` (your key from `admin_ssh_public_keys`), then `sudo -u postgres psql`. If a connection is refused with "no pg_hba.conf entry",
the device was not visible during the last run: confirm it with **node-admin → list-devices** and run **db-host** again.

Add a device to `db_superadmin_tailnet_hosts` (or `db_admin_tailnet_hosts`) in `group_vars/all/main.yml`, merge, run **db-host**. Remove it the same way; the next run
rewrites `pg_hba.conf` whole. Passwords never go on a command line: read them from SOPS as above.

## First build

1. **Secrets.** Run **Actions → init-secrets** with `fill_missing` on the branch. It adds the four `db_role_*_password` values (`admin`, `superadmin`, one per workload), the
   database TLS CA and server cert (`db_tls_*`, its own CA, not the pgBackRest one), and, signed by the pgBackRest CA, the
   database host's pgBackRest server cert and the edge's client cert (`pgbackrest_db_server_*`, `pgbackrest_edge_client_*`).
   Nothing existing changes. A set that is only partly present is refused.
2. **Edge.** Run **deploy** (tailnet). The edge now holds its client cert and the `[main]` stanza section; the backup timers stay
   off (`pgbackrest_backups_enabled: false`).
3. **Container.** On the PVE host: `python3 pve-setup.py lxc-create`. Answers: Debian 13 template, hostname `db`, root disk
   `8G`, 2 cores, memory `3072`, swap `0`, **no** Docker nesting, **yes** to the data volume (`64G`, mounted at
   `/var/lib/postgresql`, tuned: `recordsize=16K`, `compression=lz4`, `atime=off`), **yes** to `/dev/net/tun`. The data volume is a
   separate filesystem, so the heartbeat's 85% check measures the data and not the root disk.
4. **Host key.** The playbook pins it under the alias `db` in `inventories/prod/known_hosts`. On the PVE host:
   `pct exec <id> -- cat /etc/ssh/ssh_host_ed25519_key.pub`, compare its fingerprint with
   `pct exec <id> -- ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`, then add the line `db <that key>` to `known_hosts` in a PR.
5. **Bootstrap** (once, from an admin machine that has the SOPS key and the SSH key you gave `lxc-create` for root; over the LAN):
   mint a one-hour key with `--tags tag:db` (`tailnet-devices.md`), then
   ```sh
   export DB_TAILSCALE_AUTHKEY=<the key>
   ansible-playbook playbooks/db.yml -e db_stage=bootstrap -e ansible_user=root -e ansible_host=<LAN IP of the container>
   ```
   It creates the `deploy` user and your admin user (`david_admin`, keys from `admin_ssh_public_keys`), hardens sshd, installs the pinned Tailscale and joins as `db` with `tag:db`. Check
   **node-admin → list-devices**: `db` carries `tag:db`.
6. **Converge.** **node-admin → mint-ci-key** (24 h), then **Actions → db-host**. It installs the pinned packages, writes the
   configuration, creates roles, databases and the TimescaleDB extension, sets passwords, builds the PgBouncer user list from
   PostgreSQL's SCRAM verifiers, and ends with checks: all four listeners answer, `ssl=on`, `max_locks_per_transaction=1024`,
   UTF8, no `pg_hba` errors, the extension version equals `db_timescaledb_version`, and a pooled query succeeds as each workload role.
   A workload host that is not on the tailnet yet gets no `hostssl` line and a message; run again after it joins. Archiving is turned on
   only when the stanza exists (step 7); the same message says what to run.
7. **Backups.** The first **db-host** run finds no stanza on the edge, leaves `archive_mode` off and says so. On the edge, once
   (PostgreSQL must be running): `sudo -u pgbackrest pgbackrest --stanza=main stanza-create`. Run **db-host** again: it sees the stanza,
   turns archiving on (one PostgreSQL restart) and ends with `pgbackrest --stanza=main check`. The edge's timers (Sundays a full, Monday to
   Saturday a differential, 03:00 `America/Los_Angeles`) are enabled by **deploy** and skip silently until the stanza exists. Take the first
   full by hand (`sudo -u pgbackrest pgbackrest --stanza=main --type=full backup`) and read the repository size against the 15 GB budget.
8. **Heartbeat.** From the PVE host: `python3 pve-setup.py db-heartbeat`. The address to give is the container's **tailnet IP**
   (`tailscale ip -4` inside it): PostgreSQL and PgBouncer listen on loopback and that address, pgBackRest only on that address, so only
   it works for all three checks. The data directory is `/var/lib/postgresql/18/main`.
9. **Gate before cutover** (build guide, phase 3): restore a staging dump into a scratch database, compare row counts, one full restore
   and one point-in-time recovery from the edge repository, both timed.

## Sizing

The container has 3 GiB, 2 cores and no swap. Where each number lives and why (the arithmetic is in `decisions.md`):

| Setting | Value | Where |
|---|---|---|
| `max_connections` | 60 | `roles/db_host/defaults/main.yml` (`db_pg_settings`) |
| Role connection limits | 25 per workload, 3 `admin`, 3 `superadmin` | `db_workloads`, `db_admin_connection_limit`, `db_superadmin_connection_limit` |
| PgBouncer pool | 5 per database (server connections); `max_client_conn` 200 | `db_pgbouncer_pool_size`; `templates/pgbouncer.ini.j2` |
| `shared_buffers`, `work_mem`, `maintenance_work_mem`, `autovacuum_work_mem` | 768MB, 8MB, 256MB, 64MB | `db_pg_settings` |

The playbook refuses to run when the role limits plus 3 reserved slots exceed `max_connections`. Change the container size and these together.
Memory use is an estimate until measured: after the first restore read the container's memory with `pct exec <id> -- free -m`, check the
journal of the PVE host for OOM kills, and tune from there.

## Why it can be re-run

Every task is idempotent. A role password is set only when logging in with the SOPS value fails, so a rotated secret converges and a
steady state changes nothing. `pg_hba.conf` is rewritten whole from the template; a workload is added by adding it to `db_workloads`
(with a `password_key` that you also add to `GENERATED` in `scripts/init_secrets.py`, then `fill_missing`); see `onboard-service.md`.

## Constraints worth knowing

- **Compression is not enabled.** The package is TimescaleDB's Apache build, the same edition as DigitalOcean's. Compression needs the
  community build and waits for the DigitalOcean cutover; xstockstrat's `003_canonicalize_ohlcv_timeframe` also refuses to run
  against compressed `marketdata.ohlcv` chunks.
- **The extension version must equal the source's for a restore.** DigitalOcean production runs 2.30.2, the pinned version. Read
  `SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'` again before the cutover and after any bump of either side.
- **Locale is `C.UTF-8`** (`db_cluster_locale`, `db_workload_locale`), the owner's choice. The workload databases are created with
  `db_workload_locale`, so drop and recreate an empty one to change it. A dump restored from DigitalOcean keeps its data; compare
  `SELECT datcollate FROM pg_database` there once, because text ordering in indexes follows the collation.
- **Role settings** (`statement_timeout`, `idle_in_transaction_session_timeout`) are per workload `settings:`, empty until you give the values.
- **`tag:ci` reaches this host's sshd and the `deploy` user has passwordless sudo.** Anything that runs with a `tag:ci` key (including
  xstockstrat's deploy pipeline) can become root here. The owner accepted this; `tag:deploy` would narrow it with a one-line grant change plus the workflow's key.
- `tailscale up` runs with `--accept-dns=true` (the host resolves `edge.ts…` and the workload names through MagicDNS). If apt cannot resolve
  names after the join, look at headscale's DNS settings first.

## Upgrading a pin

Edit the version in `inventories/prod/group_vars/all/versions.yml` and the row in `docs/versions.md`, run **db-host**. The packages are held, so
nothing changes until you do. The PostgreSQL major, the TimescaleDB version (must match the restore source) and pgBackRest (must equal the
edge's upstream version, `pgbackrest_package_version`) move together; a PostgreSQL major upgrade is a new cluster, not a pin bump.

## Still open

Nothing below is guessed; each is a variable or an empty list waiting for your value. Settled: locale `C.UTF-8`, backups at 03:00
Los Angeles time (weekly full, daily differential), archiving that follows the stanza, compression deferred until the DigitalOcean cutover.

| What | Where | Now | Recommendation |
|---|---|---|---|
| Per-role `idle_in_transaction_session_timeout`, `statement_timeout` | `settings:` of each entry in `db_workloads` | `{}` | `idle_in_transaction_session_timeout: 10min`; no `statement_timeout`: the migrator shares the owner role and DigitalOcean sets neither |
| Devices allowed to log in as read-only `admin` | `db_admin_tailnet_hosts` | `[]` | leave empty: `macbook-pro` already has `superadmin`, and the edge must not hold database credentials |
| Compression (community build, per-hypertable policy) | not implemented | none | after the DigitalOcean cutover; start with append-only tables such as `ledger.events` |
| A heartbeat from the edge after each backup | not implemented | none | add: a Gatus external endpoint pushed on success, so a backup that never runs (timer off, edge down) alerts too |

## Break glass

`pct enter <id>` on the PVE host reaches the container without the tailnet. `pg_hba.conf` accepts only the tailnet IPs listed and
loopback, so a lost workload address is fixed by the next **db-host** run. To restore PostgreSQL from the edge repository, run `pgbackrest --stanza=main restore` on the database host after
stopping `postgresql@18-main`; `recovery.md` covers the edge, not this host.
