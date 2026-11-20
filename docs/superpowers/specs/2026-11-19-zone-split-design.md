# Zone split — phase 2 design and sequence

Status: approved by the owner 2026-11-19. Follows `2026-11-18-wildcard-portal-zones-design.md` (phase 1, merged in #83 and #84).

## Intent

Each DNS zone holds one kind of name, so a service can never collide with a machine and a new portal never needs a
DNS or token registration.

| Zone | Holds | Named by | Reachable from |
|---|---|---|---|
| `*.ts.<domain>` | machines (tailnet nodes) | headscale (MagicDNS base domain) | tailnet |
| `*.private.<domain>` | services, served by the edge's Caddy wherever they run | this repo | tailnet |
| `*.public.<domain>` | services, served by the edge's Caddy wherever they run | this repo | internet |

## Decisions (owner, 2026-11-19)

1. The machine zone keeps the name `ts`. No machine is renamed.
2. The repo rules are hardened so no service is added under `*.ts` and no machine under `*.private` or `*.public`.
3. The xstockstrat apps get a `.public` name only — no `.private` route.
4. Production xstockstrat mimics staging on its `.public` name: the whole UI is served to the internet behind the
   per-IP rate limits (`ui_public: true`). Accepted with login protected by rate limits alone; the production node is
   not set up yet.
5. After an app's `APP_URL` moves to its `.public` name, the old name redirects there. This serves browsers only.
6. The apps' `.public` names show one view to everyone: no tailnet bypass, no staging-VM exemption, no split-horizon
   record. `/agent/api/tools` stays 404 for every client.
7. Order: rules first, then the edge services, then the apps, then retiring the `*.ts` wildcard.
8. `hs` and `auth` stay where they are (phase 1 decision).

## Constraints carried from phase 1

- An app with a single origin works on one name at a time (commsgun `ORIGIN`, xstockstrat `APP_URL`).
- A redirect does not move an MCP/OAuth client: the OAuth resource is bound to `APP_URL`, so the Claude.ai connector is
  re-pointed by hand at each app cutover.
- Linux nodes resolve `.private` only through the headscale DNS route; nodes joined with `--accept-dns=false` (the
  edge, the Proxmox host) cannot resolve it and need an address pin.
- Gatus `external-endpoints` keys are a URL contract with pushers installed by hand on other hosts (EDGE-10).
- webhooks-mcp `INGEST_BASE_URLS` and ports are cross-repo contracts (C1–C9, EDGE-11).
- The first deploy of a new certificate name may need a re-run (`docs/decisions.md` § Known traps). No PR below adds one:
  both wildcard certificates exist.

## Why the apps need no tailnet bypass

The xstockstrat UI's server fetches its tool catalog from `${APP_URL}/agent/api/tools`
(`davcs86/xstockstrat`, `services/xstockstrat-ui/src/app/accounts/api/mcp-tools/route.ts`), a path the edge serves to
tailnet sources only because the agent leaves it unauthenticated. Measured 2026-11-19 inside the staging UI container:
the app's public name resolves to the reserved IPv4, so that request arrives as an internet client and gets 404 — the
bypass and the staging-VM exemption never served it. Their only effect is on the owner's Apple and Android devices.
The fix belongs in xstockstrat (fetch the catalog from the agent inside the VM); the edge does not wait for it.

Corrected in `docs/decisions.md` with step 6.

## Calls that are mine (correct them if wrong)

- During each service migration the old `*.ts` name keeps being served until its last caller has moved.
- Production keeps `deny_staging_vm`: with no bypass it only matters as defence in depth, and it costs nothing.

## Sequence — one PR each, each deployed and proven before the next

| # | PR | Change | Outside this repo | Proof |
|---|---|---|---|---|
| 1 | Zone rules | Rule in `CLAUDE.md`, an `EDGE-*` entry, a CI guard, docs. No behaviour change. | — | CI green; the guard fails on a planted violation |
| 2 | `comms` | `comms.private` served beside `comms.ts`; Gatus probe | each app's enqueue URL, in its own repo | 202 on the new name from a tailnet node; old name unchanged |
| 3 | `status` | `status.private` beside `status.ts`; ntfy alert link moves | heartbeat pushers stay on the old name until re-installed with `tools/pve-setup.py` | page loads on the new name; heartbeats stay green |
| 4 | `ntfy` | `ntfy.private`; `base-url` moves | owner re-adds the server in the phone app | test notification arrives through the new name |
| 5 | `webhooks` ingest | `webhooks.private` on 443 and on a `*.private:8443` listener, beside `webhooks.ts` on both; `INGEST_BASE_URLS` names the new host, so newly minted URLs use it; two probes on the new name | contract C8 amended in `davcs86/webhooks-mcp` (owner approved 2026-11-20); xstockstrat's stored ingest URL; URLs handed out by hand | a URL minted through the MCP carries the new host; a POST to it from a tailnet node is stored and readable through the MCP |
| 5b | `webhooks-mcp` public name | `webhooks-mcp.public` served beside `webhooks-mcp.<domain>` (static key, no origin, so both work at once); the old name leaves once the connector has moved | second half of C8; owner re-points the Claude.ai connector | the connector lists tools on the new URL |
| 6 | Staging cutover | `staging_public_hostname` becomes the `.public` name, served without the tailnet bypass or a split-horizon record; the old name redirects; Gatus `resource` assertions follow | `APP_URL` in the xstockstrat repo; re-point the Claude.ai staging connector | connector completes OAuth and lists tools on the new name |
| 7 | Production | `xstockstrat.public` with `ui_public: true`, same form as staging; the old name redirects | `APP_URL`; re-point the production connector; production node online | as PR 6, on production |
| 8 | Move probes and pushers | `commsgun-backlog` moves to `comms.private`; the old-name duplicates `commsgun-api` and `commsgun-ui` are removed (their `-private` twins stay); a CI check that no Gatus probe targets a `*.ts` service name | owner re-installs the DB host's heartbeat pusher with `status.private` (`tools/pve-setup.py`); the Proxmox pusher, never installed, is installed with `status.private` from the start | `commsgun-backlog` green on the new name; `db-heartbeat` green after the re-install; no probe URL under `*.ts` except machines |
| 9 | Retire `*.ts` services | remove the legacy routes, the `*.ts` Caddy sites and wildcard certificate, the `ts` ACME label | delete `_acme-challenge.ts` in Cloudflare and the `ts` deSEC policy | CI guard's legacy list is empty; nothing under `*.ts` is served by Caddy |

Before PR 3 and PR 4: check that Gatus and ntfy answer correctly under a second hostname (Gatus: checked 2026-11-20 against
v5.37.0 — page, API and `external` pushes answer identically under either name; ntfy: checked against v2.28.0; the webhooks
ingest never reads the Host header — read in its source, not run).

## Probes and pushers on the old names

Each migration adds a probe on the new name and keeps the old one, because the old route is still served. Before a
`*.ts` service route is removed (PR 9), nothing may still depend on it:

- **Gatus probes in this repo.** `commsgun-backlog` exists only on `comms.ts` and must move, or backlog alerting is lost.
  `commsgun-api` and `commsgun-ui` are duplicates of their `-private` twins once the old routes go. `webhooks-ingest` and
  `webhooks-ingest-staging` got `-private` twins in PR 5 and are removed in PR 8; `webhooks-ingest` carries the only certificate-expiry
  check on the `*.ts` wildcard, so it stays until that certificate is retired (PR 9).
- **Heartbeat pushers on the home hosts** (`tools/pve-setup.py`, installed by hand): the DB host's pushes to `status.ts`
  and must be re-installed with `status.private`; the Proxmox one was never installed (2026-11-20) and starts on the new
  name. Endpoint names and groups do not change (EDGE-10) — only the hostname the pusher calls.
- **Not affected:** the `nightly-backup` and `pgbackrest-backup` heartbeats run on the edge and push to Gatus locally.

Precondition for PR 9: no Gatus probe and no pusher references a `*.ts` service name; the CI check from PR 8 enforces the
probe half.

## PR 1 — zone rules (design)

- **`CLAUDE.md` § Rules, new rule 8:** one kind of name per zone — `*.ts` machines, `*.private` tailnet-only services
  and `*.public` internet services, wherever they run (the edge's Caddy serves the name); never a service under `*.ts` nor a machine under a portal zone.
- **`docs/context-constitution.md`, `EDGE-14`:** the rule with its reason and evidence.
- **CI guard in `scripts/portal-zones-test.py`:**
  - every inventory variable whose value is under `*.ts` is either a machine (`*_tailnet_host`) or one of the legacy
    services `status_hostname`, `ntfy_hostname`, `webhooks_ingest_hostname`, `commsgun_enqueue_hostname`,
    `commsgun_ui_hostname`; the set must match exactly, so a new service fails and a migrated one must be removed
    from the list;
  - every `host` matcher inside the `*.ts` Caddy sites uses one of those legacy variables;
  - no `*_tailnet_host` variable and no inventory host address is under `*.private` or `*.public`;
  - headscale's base domain is the `ts` zone, and no zone is a suffix of another.
- **Docs:** `docs/runbooks/onboard-service.md` § 4 states the rule; one entry in `docs/decisions.md`.

## Out of scope

Renaming the machine zone; a `.private` route for the xstockstrat apps; SSO in front of xstockstrat; the port-80 and
odd-Host logging gap (pre-existing, separate change).
