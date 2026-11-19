# Wildcard portal zones (`*.public`, `*.private`) — design

Status: awaiting owner approval. Phase 1 only; phase 2 (migration) is listed, not designed.

## Intent

Adding a web portal today costs three manual steps outside this repo per public hostname: a Cloudflare A record, a
Cloudflare `_acme-challenge.<label>` CNAME, and a deSEC TXT write policy on the ACME token
(`docs/runbooks/onboard-service.md` § 4). The owner wants that registration to be a one-time cost, with names shaped

- `<service>.public.<domain>` — reachable from the internet;
- `<service>.private.<domain>` — reachable only inside the tailnet.

## Goals

1. After a one-time setup, a new portal under either zone needs no change in Cloudflare or deSEC.
2. Both zones run in parallel with every existing name. Nothing existing is renamed, removed or re-issued.
3. Prove the zones with two real tenants: the commsgun dashboard on `.private`, xstockstrat-staging on `.public`.

## Non-goals (phase 1)

- Migrating `status`, `ntfy`, `webhooks`, `comms`, prod `xstockstrat`, `webhooks-mcp` (phase 2).
- Moving `hs` or `auth` — they stay as they are, permanently.
- The commsgun enqueue API (`comms.ts.<domain>`): dashboard only.
- One service with different exposure on both names (nice-to-have; see Phase 2).
- The staging `:8443` listener.
- A generic "portal" inventory schema. Per-service routing stays written in this repo.

## Anti-goals

- Any new secret, API token or IaC tool. The deSEC token keeps its label-scoped, subnet-locked shape.
- Any moment where an old name stops working before the owner declares the new way tested.
- A `.private` name that is resolvable or routable from outside the tailnet.

## Constraints

- Public surface stays 80/tcp, 443/tcp, 3478/udp (CLAUDE.md rule 2). Both zones are served on 443.
- Cloudflare stays DNS-only; ACME stays DNS-01 through the deSEC-delegated `acme.<domain>` zone, with the existing
  `propagation_delay` (`docs/decisions.md` § Known traps).
- A wildcard certificate covers one label only: `svc.public.<domain>`, never `a.b.public.<domain>`.
- Rate-limit zone names are global in caddy-ratelimit (EDGE-12): a second route for the same app needs its own prefix.
- The commsgun dashboard's query strings carry recipient addresses; every route to it must use the redacting logger
  (`Caddyfile.j2`, `log commsgun_ui`).
- The dashboard's `ORIGIN` and staging's `APP_URL` each hold exactly one URL (EDGE-11; staging's is set in the app's
  own deploy). The dashboard answers `400 Bad origin` on every path except `/healthz` when the host differs from
  `ORIGIN` (`davcs86/commsgun`, `ui/src/hooks.server.ts`); staging's OAuth resource is bound to `APP_URL`. Each app is
  therefore usable on only one name at a time.
- A headscale extra record outside a DNS route is not used by Linux nodes (systemd-resolved): they ask their normal
  resolver. Measured 2026-11-18 on the staging VM: `xstockstrat.<domain>` returned the reserved IPv4, not the edge's
  tailnet address. Apple clients do use such records. So `policy/policy.hujson`'s "App -> edge … (split-horizon)"
  comment does not describe what the app VMs do today: their self-calls arrive over the internet.
- A headscale config change restarts headscale (existing handler).

## Approaches

| | Approach | Trade-off |
|---|---|---|
| **A (chosen)** | Two wildcard zones on the existing chain: `*.public` A record, two `_acme-challenge` CNAMEs, two deSEC policies — once. Wildcard certificate per zone, as `*.ts` already does. | No new secret or component. All services in a zone share one certificate; any `*.public` name reaches Caddy (unknown hosts get 404). Service names leave certificate-transparency logs. |
| B | Keep per-host certificates; Ansible drives the Cloudflare and deSEC APIs on deploy. | Per-service certificates, but needs a Cloudflare DNS-edit token and a deSEC token that can manage policies, both in CI — a much larger blast radius than today's token. |
| C | Wildcard A record plus Caddy on-demand TLS (TLS-ALPN). | No registration and per-host certificates, but does not fit `.private`, adds a second issuance mechanism and an `ask` endpoint, and is exposed to Let's Encrypt rate limits. |

The owner picked A. Per-host certificates isolate little here: one Caddy process holds every key.

## Design

### One-time setup outside the repo (owner, before the first deploy)

| Where | Record |
|---|---|
| Cloudflare, DNS-only | `*.public` A → reserved IPv4 |
| Cloudflare, DNS-only | `_acme-challenge.public` CNAME → `public.acme.<domain>` |
| Cloudflare, DNS-only | `_acme-challenge.private` CNAME → `private.acme.<domain>` |
| deSEC, existing token | TXT write policy for `public` and for `private` (`docs/runbooks/acme-token.md` § Add a name) |

`*.private` gets no public DNS record of any kind.

### Inventory

- `public_zone: "public.{{ domain }}"`, `private_zone: "private.{{ domain }}"`.
- `commsgun_ui_private_hostname: "commsgun.{{ private_zone }}"`.
- `staging_portal_hostname: "xstockstrat-staging.{{ public_zone }}"`.
- A second `edge_sites` entry for staging on the new name, same `kind: app`, same upstream, `ui_public: true`,
  `isolate_staging_vm: false`, its own `name` (rate-limit and log prefix), and a marker that it lives in the public
  wildcard zone (so it gets no certificate of its own).

### Caddy

- New site `*.{{ public_zone }}`: `import acme public.{{ acme_zone }}`. Each `edge_sites` entry marked for the zone is
  rendered inside it as a host-matched handle with the same body its `kind` produces today; anything else answers 404.
  Entries not marked keep rendering as their own site, byte-for-byte as now.
- New site `*.{{ private_zone }}`: `import acme private.{{ acme_zone }}`, `deny_staging_vm`, `tailnet_only` first,
  a host-matched handle for the commsgun dashboard using the redacting logger, then 404. Same shape as `*.ts`.

### Resolution inside the tailnet

Owner's choice (over a public wildcard record pointing at the edge's tailnet address, which would publish that
address, leak looked-up service names to public resolvers, break behind DNS-rebinding filters and go stale on a rebuild):

- headscale gets a DNS route for `{{ private_zone }}` (`dns.nameservers.split`), so every client OS sends the zone to
  the Tailscale resolver. Upstream for names with no record: `1.1.1.1`. *My call* — the resolver Gatus already uses
  for public probes; the owner has not named one.
- `commsgun.private.<domain>` is added to the generated headscale extra records (`roles/tailscale/tasks/main.yml`,
  `_names`). No public DNS record exists for the zone, so that is the only way the name resolves.
- Proven by a throwaway test on 2026-11-18 (route + one record, since reverted): on the Debian staging VM
  `getent hosts dns-test.private.<domain>` returned the edge's tailnet address. macOS, iOS and an off-tailnet lookup
  were not reported; they are in the success condition below.
- Nodes joined with `--accept-dns=false` (the edge itself, the Proxmox host) cannot resolve `.private` names, as with
  `*.ts` today.
- `xstockstrat-staging.public.<domain>` **also** gets an extra record and keeps the tailnet bypass, exactly like the
  existing staging name. *My call:* parity — phase 1 changes only how the name is registered and certified, not what
  any client sees (Apple clients get the full app, Linux nodes the internet view, as today). "A `.public` name
  always shows the internet view" belongs with dual exposure in phase 2.

### Checks wired in

- `tls-check` in the deploy probes both new hostnames (one SNI per new certificate).
- `scripts/caddy-routing-test.sh`: the staging portal name behaves like the existing staging name; an unknown
  `*.public` host gets 404; the private name aborts from a non-tailnet source and proxies from a tailnet one; no
  rate-limit zone is shared.
- Gatus: a `/healthz` probe on the private dashboard name; a `/trader/api/health` probe on the staging portal name
  through the public resolver. New endpoint names only — no existing endpoint is renamed (EDGE-10).

### Testing in two steps per tenant

**Step 1 — parallel name (this change).** Proves DNS, certificate, routing, rate limits and health on the new names.
Expected and documented: on the new names only the health routes answer usefully — the dashboard returns 400 on
every other path, and Claude.ai's MCP/OAuth flow works only on the old staging name.

**Step 2 — cutover (separate change per tenant, on the owner's go).** Flip the one variable; the old route stays
served as rollback, and after the flip it is the old name that loses login/OAuth.

- commsgun: `ORIGIN` → the private name. Proof: dashboard login on the new name.
- staging: `APP_URL` in the xstockstrat repo's staging deploy → the portal name; Gatus `resource` assertions follow;
  the Claude.ai staging connector is re-pointed. Proof: the connector completes OAuth and lists tools.

Old DNS records, certificates and token policies are removed only in phase 2.

### Success condition for step 1

1. `ci` green, including the extended `caddy-routing-test.sh`, `gatus-config-test.py`, `commsgun-config-test.py`.
2. `deploy` (`tailnet`) green: "Every site presents a publicly trusted certificate" lists both new names.
3. `verify` green; both new Gatus endpoints healthy.
4. `https://commsgun.private.<domain>/healthz` → 200 from a Debian tailnet node, the owner's Mac and phone (OS
   resolver, not `tailscale dns query`). From a device off the tailnet: the name does not resolve.
5. From the internet: `https://xstockstrat-staging.public.<domain>/trader/api/health` → 200;
   `https://nonexistent.public.<domain>/` → 404 with a valid certificate.
6. Every pre-existing Gatus endpoint still healthy.

### Docs changed in the same PR

`README.md` (DNS table, § Rebuild), `docs/runbooks/onboard-service.md` § 4 (the zero-registration path),
`docs/runbooks/acme-token.md` (label list, rotate loop), `docs/runbooks/commsgun.md` (second name, login caveat),
`docs/decisions.md` (the decision; the one-URL-per-app trap), `docs/context-constitution.md` (EDGE-07 gains the zones),
`docs/context-constitution-findings.md` (Linux nodes ignore extra records outside a DNS route; the policy comment).

## Left to the builder

- How an `edge_sites` entry is marked for the public zone, and how the `kind` bodies are shared between a standalone
  site and a host-matched handle without duplicating them.
- Access-log file naming inside the wildcard sites, and the name of the second redacting logger (it must also be
  listed in the global `log` include/exclude lines).
- Exact Gatus endpoint names and probe conditions, following the existing ones.

## Phase 2 (listed, not designed)

Move prod `xstockstrat`, `webhooks-mcp` (cross-repo contracts C1–C9), `status`, `ntfy`, `webhooks`, `comms`; retire
the old names, their DNS records and token policies; decide whether app VMs' self-calls should stay on the internet
path (the Linux finding above); optionally dual exposure, where the `.private` name carries the
full app and the `.public` name only the internet route set, replacing same-name split-horizon.
