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
The edge's Caddy and Gatus upstreams resolve `xstockstrat.<ts domain>` (and `xstockstrat-staging` for staging): the VM
must be registered under that hostname before the deploy. Gatus probes each machine directly, by its tag name plus `-vm` in group `machines` (`machines_xstockstrat-ui-vm`,
`machines_xstockstrat-agent-vm`, `machines_xstockstrat-staging-ui-vm`, `machines_xstockstrat-staging-agent-vm`), and alerts through ntfy. Each environment's public name is also probed from the internet
(`public_xstockstrat-ui-public`, `public_xstockstrat-staging-ui-public`: `/trader/api/health`, 200 + `status == ok`); a probe for a
VM that does not exist yet alerts after 3 failed minutes, so register the node first. Renaming a probe starts a new history;
the old key stays in Gatus's database and nowhere on the status page.

Deploying it from GitHub Actions: `deploy-compose-app.md`. CI reaches a workload only through a `tag:ci` grant on its
tag; add it (and flip the `tag:ci` test for that tag from `deny` to `accept`) in the same PR as the tag.

## 2. Shared Postgres (database-level isolation)

The DB server is shared, so the tailnet cannot separate databases. `playbooks/db.yml` applies every item below from `db_workloads`
(`docs/runbooks/db-host.md`), except the per-role limits you have not given values for (`settings:`). Per workload, on the DB host:

- Own database and own login role(s); never share a role across environments.
- `REVOKE CONNECT ON DATABASE <db> FROM PUBLIC;` then `GRANT CONNECT` to that workload's role only; `REVOKE CREATE ON SCHEMA public FROM PUBLIC`.
- `pg_hba.conf`: one `hostssl <db> <role> <app tailnet IP>/32 scram-sha-256` line per workload, before any broader line.
- `CREATE EXTENSION timescaledb` in that database (extensions are per database).
- Limits: `ALTER ROLE <role> CONNECTION LIMIT n`, `statement_timeout`, `idle_in_transaction_session_timeout`.
- PgBouncer: its own `[databases]` entry pinned to `dbname` (a forced `user=` cannot use the SCRAM verifiers in `userlist.txt`; the role is kept out by `REVOKE CONNECT` and `pg_hba`), its own `userlist.txt` entry and `pool_size`; keep the sum of pools below Postgres `max_connections`.

## 3. Secrets and recovery

- Separate encryption keys, DB credentials and mTLS identities per workload. Never restore prod rows holding broker or
  config secrets into staging unless staging holds prod's keys on purpose.
- pgBackRest is cluster-wide: one retention policy and one restore point for every database. Restoring one workload
  means restoring to a scratch instance and dumping that database out.

## 4. Edge exposure (only if it needs a public or tailnet name)

**New portals go under the two wildcard zones; nothing is registered in Cloudflare or deSEC for them.** `*.ts` is for
machines only: a new service is never added there, and a machine never gets a `*.private` or `*.public` name
(`CLAUDE.md` rule 8; CI enforces it).

- `<service>.public.<domain>` (internet): an `edge_sites` entry with `zone: public`. Its Caddy handle inside the `*.public` site,
  and `tls-check` are generated from it (it gets no split-horizon record); add a Gatus probe. Any other name under the zone answers 404.
- `<service>.private.<domain>` (tailnet-only): four places (EDGE-07) — a hostname var, a `host` handle in the
  `*.private` site of `Caddyfile.j2`, the name in `_names` (`roles/tailscale/tasks/main.yml`), and a Gatus probe. The zone has no
  public DNS records; tailnet nodes resolve it through headscale's DNS route for the zone, so a node joined with
  `--accept-dns=false` cannot resolve it.

An app that holds a single origin (commsgun's `ORIGIN`, xstockstrat's `APP_URL`) works on one name at a time: serving a second
name proves DNS, certificate, routing and health only, and login or OAuth move with the origin.

Every public hostname on 443 is an entry in `edge_sites` (`inventories/prod/group_vars/all/main.yml`): its Caddy site, `tls-check`
and the tailscale split-horizon records are generated from it. `kind: app` is an xstockstrat-style app (MCP and OAuth
routes; `ui_public` also serves the whole UI; no tailnet bypass), `kind: bearer_mcp` is one path to a local container behind a static key. Gatus probes
are still written per service. The webhooks ingest is `webhooks.private.<domain>` (`webhooks_ingest_private_hostname`) on 443, one URL for every
environment: it is the one `*.private` name the staging VM may reach (see `docs/decisions.md`).
A workload that must send email or SMS needs no route of its own: it POSTs to commsgun's tailnet-only enqueue name
(`commsgun_enqueue_private_hostname`, `comms.private.<domain>`) with its own app key, over the `tag:edge:443` grant its tag already needs;
register the app as in `docs/runbooks/commsgun.md` § 2.

A new public hostname with its own certificate also needs three things outside this repo, **all before the deploy**
that introduces it:

1. Cloudflare, DNS-only: `<label>` A → the reserved IPv4.
2. Cloudflare, DNS-only: `_acme-challenge.<label>` CNAME → `<label>.acme.<domain>`.
3. deSEC: a `TXT` write policy for `<label>` on the existing ACME token —
   [`acme-token.md` § Add a name](acme-token.md#add-a-name-required-before-the-first-deploy-of-a-new-public-hostname).

Skip step 3 and the deploy fails at the certificate check; a `*.public` or `*.private` name needs none of the three.

Staging is `xstockstrat-staging.public.<domain>`, a `zone: public` entry: no DNS or token step, one view for every client, and the
staging VM's `APP_URL` is set to it. The VM's tailnet address is denied on every 443 site, its own included, by `deny_staging_vm` (grants are per port,
so the grant alone cannot do it); Caddy learns the address from headscale on each deploy, so a re-registered VM keeps the old address
in the rule until the next deploy.

### Which staging name goes where

| Name | Is | Use for |
|---|---|---|
| `xstockstrat-staging.ts.<domain>` | the VM's MagicDNS node name (`staging_tailnet_host`; inventory host, `known_hosts`, tag name) | `DEPLOY_HOST`, SSH, `--limit`, and the upstream Caddy and Gatus connect to |
| `xstockstrat-staging.public.<domain>` | the edge's public name (`staging_public_hostname`) | `APP_URL`, `AGENT_PUBLIC_URL`, the OAuth `resource`, and what browsers and clients open |
| `xstockstrat-staging.<domain>` | the former public name | nothing: it is no longer served. Point MCP clients and bookmarks at the name above |

The Gatus endpoint named `xstockstrat-staging-ui-vm` is a probe label; it probes the node name, not the public name.

## 5. SSO (only if people log in; design, not deployed)

The design is `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md`; § 3 there is the contract the app implements
(`AUTH_MODE`, the `OIDC_*` names, users and roles in the app, explicit "Link SSO", a "create if empty" admin CLI).

1. **App side.** It speaks plain OIDC (or, for an MCP server, validates JWTs against JWKS with an audience) and defaults to
   `AUTH_MODE=local`, so its repo runs without Authelia; the homelab sets `oidc`. Third-party software that has no OIDC may use Caddy `forward_auth`
   instead; our own apps never do.
2. **Authelia.** One client entry (confidential for a web app, public PKCE for MCP clients, with both Claude.ai callback
   hosts and `access_token_signed_response_alg`/`audience` for MCP), one group with an `authorization_policy`, the client
   secret in SOPS via `init-secrets` `fill_missing`.
3. **Edge.** A `*.public` name, `extra_hosts: auth.public.<domain>:host-gateway` if it runs on the edge, `authelia_session`
   stripped before it, Caddy login limits on its local-login POST (local login is off in steady state: `AUTH_MODE=oidc`),
   query/`Referer`/token-path redaction if its URLs carry tokens, a Gatus probe, and its database in the edge backup if it
   holds identities.
4. **People.** Add the group to each person in SOPS, deploy, and each person links in the app (`sso.md` § 5), with the app
   in `both` for that window.
