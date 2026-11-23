# Authelia SSO — re-spec against the repo as of 2026-11-23 (DRAFT for approval)

Status: **draft, nothing decided here.** It re-reads the approved 2026-11-15 design (`docs/decisions.md` § Design decisions,
"SSO is Authelia"; `docs/runbooks/sso.md`) against `main` at `c06667e` and proposes the deltas. Where the old design and
the current repo disagree, the owner rules (Q-items in § 6); nothing in `sso.md` or `decisions.md` is edited until then.

## 1. What still holds unchanged

- Authelia, not a custom IdP / Authentik / Keycloak / Zitadel / Pocket ID: same reasons (`decisions.md`, lines 38–41).
- Release **v4.39.28**: still the newest release on GitHub (published 2026-09-17; no v4.40 exists), so the planned pin and
  every spike item (RFC 8707 `resource`, `scope` + `email` in JWT access tokens, custom scopes, group re-check on refresh,
  pre-registered public client for Claude.ai, `sub` bound to username) carry over as-is.
- The person/group/`sub` model, the immutable-username and unique-email rules, the xstockstrat linking order (`sso.md` § 1–7),
  client IDs from xstockstrat feature 345 FR-13, `default_policy: deny` + `two_factor` everywhere.
- headscale, SSH, the tailnet and the break-glass path never depend on Authelia.
- Public surface stays 80/443/3478 (Rule 2): Authelia is one more 443 name, no new port.

## 2. Drift since 2026-11-15

| # | 2026-11-15 design says | Repo now | Evidence | Effect on SSO |
|---|---|---|---|---|
| D1 | Portal at `auth.<domain>` with a **new** deSEC token policy and ACME label | Three-zone rule: an internet service lives under `*.public`, served from one wildcard cert (ACME label `public`) | `CLAUDE.md` Rule 8; EDGE-14; `main.yml` `public_zone`; `Caddyfile.j2` `*.{{ public_zone }}`; `scripts/portal-zones-test.py` | `auth.<domain>` violates Rule 8. Re-spec: **`auth.public.<domain>`**, covered by the existing wildcard → no new deSEC policy, no new ACME label, no first-issuance re-run trap |
| D2 | commsgun dashboard "made public" behind `forward_auth`; undo EDGE-07 for `commsgun_ui_private_hostname` | The dashboard was deliberately moved to `commsgun.private.<domain>` with that name as its `ORIGIN` | commit `20b52fc`; `main.yml:86`; `Caddyfile.j2` `@commsgun_ui`; EDGE-07, EDGE-11 | Moving it public now reverses a later, merged decision. Needs a ruling (Q1) |
| D3 | webhooks-mcp `/mcp`: OIDC, "its static bearer key is removed" | Served as `webhooks-mcp.public.<domain>`, `kind: bearer_mcp`, static `webhooks_mcp_auth_key`; old name retired 2026-11-20 | `main.yml:122`; `decisions.md:84–85`; Gatus `webhooks-mcp-portal-requires-auth` | Still possible, but it is a webhooks-mcp code change under contracts C1–C9 plus a new edge site kind. Needs a ruling on phase (Q3) |
| D4 | — | **happybabytracker** is new on the edge: own admin + invite-based parent accounts, tailnet-only | `docs/runbooks/happybabytracker.md` § 2–3; `main.yml:112` | Not in the old scope. Needs a ruling (Q2) |
| D5 | "Each xstockstrat deployment (DigitalOcean or homelab)" | Prod is `xstockstrat.public.<domain>` → home VM; no App Platform reference left in this repo | `main.yml:10,118`; commit `5cf2345`; `grep -ri 'App Platform'` empty | If no DO deployment remains, the per-deployment switch is two deployments (prod, staging), both homelab. Confirm (Q5) |
| D6 | Memory: "a handful of users", one Go binary | Declared limits grew to 1024m (happybabytracker added 288m); worst-case available measured 1237 MiB *before* that | `compose.yml.j2` mem_limits; plan `2026-11-21-happybabytracker-edge.md` row 11 | Authelia at a 128m limit leaves ≈ 820 MiB on paper. Re-measure before Phase 1 (pass condition P1.0) |
| D7 | SQLite "in the edge backup" | The backup now snapshots a uid-10001 SQLite through a private bind mount (`unshare` + `setpriv`) and the drill checks `integrity_check` | `roles/backup/templates/edge-backup.sh.j2:27–33`, `edge-backup-verify.sh.j2:16` | Authelia's DB uses exactly this shape; reuse it, do not invent a new one |
| D8 | Caddy strips the session cookie from other upstreams | Every `*.public` name is in one site; a cookie scoped to `public.<domain>` reaches xstockstrat, staging and webhooks-mcp | `Caddyfile.j2` `*.{{ public_zone }}` loop | If no `forward_auth` app is in scope (Q1 = keep private), scope the cookie to `auth.public.<domain>` itself and the leak disappears (S6) |
| D9 | Access log drops `code`/`state`/`txn`/`agent_cb`/`code_verifier` from queries | Same filter; responses' `Location` is only dropped in the `*.private` logger | `Caddyfile.j2` `(access_log)`, `log private_zone` | Authelia's authorize/consent responses redirect with `?code=…` in `Location`. The auth host needs `resp_headers>Location delete` (or the whole zone does) |

## 3. Proposed design (deltas only)

### 3.1 Edge
- **Name:** `authelia_public_hostname: "auth.{{ public_zone }}"` in `main.yml`. Served by a dedicated `@authelia host` handle
  inside `*.{{ public_zone }}`, before the `edge_sites` loop (it is neither `app` nor `bearer_mcp`; no third site kind
  for one entry — Rule "minimum").
- **Handle:** rate limits `authelia_general`, `authelia_login` (first-factor POST), `authelia_token` (EDGE-12 prefix); a
  body cap from `body_limits.default`; `reverse_proxy 127.0.0.1:{{ authelia_port }}`. No `trusted_proxies`; Authelia reads
  Caddy's single overwritten XFF entry for regulation (same reasoning as EDGE-02 — pin it in the routing test).
  Whether it imports `deny_staging_vm` is Q6.
- **Compose:** `authelia` service with `<<: *hardening`, `127.0.0.1:{{ authelia_port }}:9091`, named volume `authelia-data`,
  uid 10001, `read_only`, `mem_limit: 128m` (EDGE-05). Image pinned by digest in `versions.yml` with a `docs/versions.md`
  row (Rule 4, EDGE-01 quoting).
- **Config shape (EDGE-04):** secrets and scalar settings through `env_file` (`AUTHELIA_*`, `*_FILE` not needed); the
  `configuration.yml` (clients, access rules, cookies) and `users_database.yml` are files Authelia must read → a bind mount
  with a "Restart authelia" handler in the EDGE-03 shape. Alternative in Q7.
- **Secrets (SOPS, `init_secrets.py fill_missing`, read in EDGE-08 shapes, `no_log: true`):** session secret, storage
  encryption key, reset-password JWT secret, OIDC HMAC secret, OIDC JWKS private key (RS256), `xstockstrat-web` and
  `xstockstrat-staging-web` client secrets (stored as hashes in config), the people list. `secrets/README.md` gains a row
  each. Note: losing the storage encryption key makes the SQLite unreadable — it is in SOPS, so the backup restore needs
  both.
- **Backup:** `edge-backup.sh.j2` snapshots `authelia-data/_data/db.sqlite3` with the D7 shape; the drill adds an
  `integrity_check`; `scripts/edge-backup-test.py` covers it; `recovery.md` gains the restore step.
- **Monitoring:** Gatus `authelia-public` on `https://auth.public.<domain>/api/health` via 1.1.1.1 (the public-probe
  convention in `decisions.md:17`). Not an external endpoint, so EDGE-10 is not touched.
- **Logging:** D9 fix on the auth host; no new log file (it stays in `public.access.log`).

### 3.2 xstockstrat (prod + staging), unchanged in substance
- Per-deployment switch by its own config (legacy → Authelia), clearing the issuer keys rolls back.
- Edge side during transition: `public_routes` keeps `/agent/oauth/*`, `/agent/oauth/register`, `/auth/oauth-login` and the
  login limits while either deployment is legacy or the password flag exists. Removing them is a separate, later change
  with its own routing-test diff.
- Client redirect URIs: `https://claude.ai/api/mcp/auth_callback` for the Claude.ai MCP clients; a Claude Code client is Q4.

## 4. Phases and pass conditions

| Phase | Work | Pass condition (shown, Rule 6) |
|---|---|---|
| P0 spike | Scratch Authelia v4.39.28 + one MCP client, off the droplet | Each S-item in § 5 recorded with observed output in this doc |
| P1 edge infra | § 3.1, no app wired yet; one test person | P1.0 `free -m` + `docker stats --no-stream` on the droplet ≥ 400 MiB available with the new limit; `ci` green incl. `caddy-routing-test.sh` new cases (auth host routes only to Authelia, no XFF trust, `Location`/`code` absent from the log, rate-limit zones unique); **deploy** OK; `verify` OK; Gatus `authelia-public` green; one `edge-backup.service` run ends `backup ok`, drill passes with the Authelia DB; test person logs in with password + passkey/TOTP |
| P2 staging cutover | xstockstrat-staging switch; `sso.md` § 6 cutover | Claude.ai connector to staging completes OAuth against Authelia; `whoami`-class MCP call succeeds; web login links the pre-seeded row by email (ledger audit event seen) |
| P3 prod cutover | Same for prod | Same checks on prod; rollback (clear issuer keys) rehearsed once on staging first |
| P4 per Q1–Q3 | commsgun / happybabytracker / webhooks-mcp, as ruled | Defined when ruled |

## 5. Spike items (P0)

S1–S5 are the old `TODO(verify)` list (`decisions.md:51–53`). New:
- **S6** cookie domain = `auth.public.<domain>` (portal host itself) is accepted by v4.39.28 for an OIDC-only deployment.
- **S7** Authelia's regulation keys on the XFF Caddy sets with no `trusted_proxies` (not on Caddy's own address).
- **S8** idle RSS of Authelia with SQLite and the planned config, to size `mem_limit`.

## 6. Questions for the owner (block P1)

- **Q1 commsgun dashboard:** (a) keep it tailnet-only with its own password, out of SSO *(recommended: smallest change,
  no `forward_auth`, enables S6)*; (b) keep it private but add `forward_auth` (needs a second cookie domain and
  `auth.private.<domain>`, four-place wiring, EDGE-07); (c) make it public as originally planned (reverses `20b52fc`).
- **Q2 happybabytracker:** out of SSO *(recommended: it has its own invite/reset model and is tailnet-only)*, or a later phase?
- **Q3 webhooks-mcp:** keep the static bearer for now and do OIDC in P4 *(recommended)*, or in P1/P2?
- **Q4** Is a Claude Code (localhost redirect) client wanted alongside the Claude.ai ones, per environment?
- **Q5** Is there still any DigitalOcean xstockstrat deployment, or only homelab prod + staging?
- **Q6** Should `auth.public.<domain>` admit the staging VM? Its token/JWKS calls normally arrive from the home public IP
  (not tailnet), so `deny_staging_vm` would not match — but if they ever route over the tailnet, importing it breaks staging.
- **Q7** Config delivery: bind-mounted files + handler (EDGE-04's weaker shape, stranded after a failed play), or render them
  into an env var / templated entrypoint so compose's config hash recreates the container?
- **Q8** Status of xstockstrat feature 345 (`authelia-sso`): may this session attach `davcs86/xstockstrat` (and
  `davcs86/webhooks-mcp` for Q3) read-only to check what has shipped?
- **Q9** Who are the first people, usernames and groups (permanent, `sso.md` § 2.1)?

## 7. Docs to change when implemented

`docs/runbooks/sso.md` (status, hostname, scope, SOPS keys, hash command), `docs/decisions.md` (the SSO entry: D1–D3
outcomes), `README.md` index line, `docs/versions.md`, `docs/runbooks/recovery.md`, `docs/runbooks/alerts.md` (probe
list), `secrets/README.md`, `docs/context-constitution.md` if a new seam appears (auth host XFF, cookie scope).
