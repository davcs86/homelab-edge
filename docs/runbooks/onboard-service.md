# Onboarding a workload (service or environment)

Tags name **workloads**, not tiers: `tag:xstockstrat`, `tag:xstockstrat-staging`, `tag:agent`, … A new workload gets its own
tag and reaches nothing until a grant says so (default deny). Prod and staging of the same product are separate tags.

## 1. Tailnet (this repo)

1. `policy/policy.hujson`: add the tag to `tagOwners` and only the grants it needs (src = the new tag).
2. Add a `tests` entry: every `accept` it needs and `deny` for the ports of every *other* workload.
3. PR → CI policy tests must pass → merge → run `deploy`.
4. Hostname equals the tag name without `tag:` (`xstockstrat`, `xstockstrat-staging`). Hostnames are unique; a duplicate registers as `name-1`.
5. Join it with a pre-auth key carrying only that tag (`tailnet-devices.md`).

Migrating from the old `tag:app`: no node may still carry `tag:app` when the policy that drops it is deployed. Check
`node-admin → list-devices`; if one does, re-tag it to `tag:xstockstrat` first.

## 2. Shared Postgres (database-level isolation)

The DB server is shared, so the tailnet cannot separate databases. Per workload, on the DB host:

- Own database and own login role(s); never share a role across environments.
- `REVOKE CONNECT ON DATABASE <db> FROM PUBLIC;` then `GRANT CONNECT` to that workload's role only; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.
- `pg_hba.conf`: one `hostssl <db> <role> <app tailnet IP>/32 scram-sha-256` line per workload, before any broader line.
- `CREATE EXTENSION timescaledb` in that database (extensions are per database).
- Limits: `ALTER ROLE <role> CONNECTION LIMIT n`, `statement_timeout`, `idle_in_transaction_session_timeout`.
- PgBouncer: its own `[databases]` entry pinned to `dbname`/`user`, its own `userlist.txt` entry and `pool_size`; keep the sum of pools below Postgres `max_connections`.

## 3. Secrets and recovery

- Separate encryption keys, DB credentials and mTLS identities per workload. Never restore prod rows holding broker or
  config secrets into staging unless staging holds prod's keys on purpose.
- pgBackRest is cluster-wide: one retention policy and one restore point for every database. Restoring one workload
  means restoring to a scratch instance and dumping that database out.

## 4. Edge exposure (only if it needs a public or `*.ts` name)

Today the edge hardcodes `public_app_hostname` in the Caddyfile, the ACME CNAME delegation, `tls-check`, the Gatus
config and the tailscale role's split-horizon records. Exposing another workload means editing each of those; generalising
them into a list variable is a separate change.
