# Authelia SSO — re-spec against the repo as of 2026-11-23 (DRAFT for approval)

Status: **design re-approved in part, not deployed.** It re-reads the approved 2026-11-15 design (`docs/decisions.md` § Design
decisions, "SSO is Authelia"; `docs/runbooks/sso.md`) against `main` at `c06667e`, and against the app repos at: xstockstrat
`6ce536f`, webhooks-mcp `c9be704`, commsgun `017db3e`, happybabytracker `d3820ab`. `sso.md` and `decisions.md` carry the
settled parts; what is still open is § 7.

## 1. Owner rulings (2026-11-23)

| Q | Ruling |
|---|---|
| Q1 commsgun dashboard | **Public, behind `forward_auth`** (reverses commit `20b52fc`'s private name) |
| Q2 happybabytracker | **Joins SSO** |
| Q3 webhooks-mcp | **Switches to Authelia** (the static bearer key goes) |
| Q4 MCP clients | **Both Claude.ai and Claude Code** clients, per environment |
| Q5 DigitalOcean | DO xstockstrat deployments exist and **stay legacy**: SSO is homelab only |
| Q6 VM back-channel | **Option A**: the VMs use `auth.public.<domain>` over the home WAN; no `auth.private` (§ 4) |
| Q7 config delivery | **Spike C** (compose `configs:` content); **fall back to A** (directory mount + hash-compare restart) (§ 5) |
| Q8 app repos | Read; findings in § 2 |
| Q9 people | Template: `docs/runbooks/sso-people.template.yaml` |
| Q11 notifier | Owner asks for **commsgun** → § 5a |
| Q12 commsgun password login | **Kept as a fallback**, not mapped to an Authelia group, **switchable off by configuration** (§ 5b) |
| Q13 happybabytracker admin | **Same as Q12**: local env admin kept as fallback, not mapped, switchable off by configuration |
| Q14 home WAN IP | **Not static** → no IP exemption; path-scoped zones instead (§ 4) |
| Q15 app-repo amendments | Owner runs them in a new session with every repo; this one hands off (artifact + § 9) |

## 2. What still holds, and what changed

**Unchanged:** Authelia (not a custom IdP / Authentik / Keycloak / Zitadel / Pocket ID); release **v4.39.28** (still the
latest, 2026-09-17; no v4.40); the person/group/`sub` model, immutable usernames, unique admin-owned emails; `default_policy:
deny` + `two_factor` everywhere; headscale, SSH, the tailnet and break-glass never depend on Authelia; public surface stays
80/443/3478 (Rule 2).

| # | Old design / assumption | State now | Evidence | Consequence |
|---|---|---|---|---|
| D1 | Portal `auth.<domain>`, new deSEC policy + ACME label | Rule 8: internet services live under `*.public`, one wildcard cert | `CLAUDE.md` Rule 8, EDGE-14, `Caddyfile.j2` `*.{{ public_zone }}` | **`auth.public.<domain>`** under the existing wildcard: no deSEC or ACME change. xstockstrat's issuer is a config row (`ui/oidc.issuer`, `agent/oidc.issuer`), so only its docs say `auth.<domain>` |
| D2 | commsgun dashboard public | Now `commsgun.private.<domain>`; commsgun's own spec lists "the dashboard is reachable from the internet" as an **anti-goal** | `main.yml:86`; commsgun `docs/superpowers/specs/2026-11-13-commsgun-design.md:56` | Q1 ruling needs a commsgun spec amendment and code (no `Remote-User` support today; fixed user `admin`, stateless HMAC cookie U4, `ui/src/lib/server/session.ts`) |
| D3 | webhooks-mcp OIDC | Static `MCP_AUTH_KEY`, `hmac.compare_digest` in `mcp_app.py#build_mcp_app`; MCP SDK 2.3.0 has `AuthSettings` / `TokenVerifier` / PRM routes but they are bypassed; no JWKS verifier ships | webhooks-mcp `src/webhooks_mcp/mcp_app.py:176-197`, `uv.lock` (`mcp` 2.3.0, `pyjwt` 2.15.1) | Amends contracts **C1** (auth scheme, PRM route, `WWW-Authenticate resource_metadata`) and **C4** (`MCP_AUTH_KEY` → issuer/audience/resource env); its spec listed OIDC as a non-goal "until shared OIDC" |
| D4 | — | happybabytracker: env-only admin + invited parents with local passwords; `identities(provider, subject)` table is the **designed Authelia seam**; internet exposure is an anti-goal "until the Authelia work decides the network path" | happybabytracker spec `2026-11-20-happybabytracker-design.md` § 5.4 (184-193), § 15 (601-604), `store/parents.go:15-18` | SSO via OIDC maps cleanly onto `identities(provider='authelia', subject=<sub>)`; network exposure is Q10 |
| D5 | "DigitalOcean or homelab" switch | DO stays legacy | xstockstrat FR-14 (`product-spec.md:159-201`): empty issuers = legacy | Nothing to do on DO; its Claude.ai connectors keep the legacy OAuth server |
| D6 | Memory | Declared limits 1024m on a 2 GB droplet; 1237 MiB worst-case available measured *before* happybabytracker (+288m) | `compose.yml.j2`; plan `2026-11-21-happybabytracker-edge.md` row 11 | Re-measure before P1 (P1.0) |
| D7 | SQLite in the backup | The uid-10001 private-bind-mount snapshot shape exists | `edge-backup.sh.j2:27-33` | Reuse it for `authelia-data` |
| D8 | Strip the session cookie from other upstreams | xstockstrat 345 requires the cookie domain to **cover each `APP_URL` host** (its logout `rd=${APP_URL}/auth/login` must be a safe redirect) | xstockstrat `345-authelia-sso/design.md:373-375` | Cookie domain is **`public.<domain>`**: `authelia_session` reaches every `*.public` upstream, so Caddy strips it (`header_up Cookie` regex) for xstockstrat, staging and webhooks-mcp; commsgun keeps it only through `forward_auth`. Routing test pins it |
| D9 | Access log drops OAuth query values | `Location` is only dropped in the `*.private` logger | `Caddyfile.j2` `(access_log)`, `log private_zone` | Authorize/consent redirects carry `?code=` in `Location`: drop it on the auth host |
| D10 | xstockstrat side ready | Feature 345 is **`implementation-ready`, 0 of 52 steps executed**, no branch; its config migration number `053` is stale (next free is `056`) | xstockstrat `345-authelia-sso/status.md`, `implementation-spec.md` header, `docs/roadmap/features/merge-order.md:274` | P2/P3 are gated on 345 shipping; edge P1 can run before it |
| D11 | — | 345 needs the **app VM's single source IP exempt from any Authelia rate-limit zone**, and a cutover pre-check that the VM reaches the issuer for discovery, token and JWKS | xstockstrat `debate.md` D-74 | Drives § 4 |
| D12 | Claude Code client | Not planned in 345 (only a spike line) | xstockstrat `product-spec.md:364` | Q4 ruling needs a 345 amendment: `xstockstrat[-staging]-mcp-claude-code[-ro]` with a loopback redirect (S9) |
| D13 | Notifier | Authelia sends a one-time code by email before a person can enrol a passkey/TOTP or reset a password; it supports only SMTP or a file notifier. commsgun exposes an HTTP enqueue API, not SMTP | `commsgun-config.json.j2`; commsgun K1–K6 | Q11 |

345 facts the edge must honour: issuer byte-exact, no trailing slash; `jwks_uri` same origin as the issuer; web client
confidential `client_secret_basic` + PKCE, redirect `${APP_URL}/api/auth/sso/callback`, post-logout `${APP_URL}/auth/login`;
MCP clients public PKCE, audience `${APP_URL}/agent`, `requested_audience_mode: implicit`, `claims_policy` (`id_token:
[email, email_verified]`, `access_token: [email]`); scopes `xstockstrat.read|write|admin|trade`; access tokens 1 h; auth code
≥ 30 s (default ≈ 60 s); no userinfo calls; all back-channel calls are server-side from the VM.

## 3. Design (deltas)

### 3.1 Edge, Authelia itself
- **Names:** `authelia_public_hostname: "auth.{{ public_zone }}"`. Optional `authelia_private_hostname` per § 4.
- **Caddy:** a dedicated `@authelia host` handle in `*.{{ public_zone }}` before the `edge_sites` loop. Zones
  `authelia_login`, `authelia_backchannel`, `authelia_general` (EDGE-12; path-scoped, § 4), `body_limits.default`,
  `reverse_proxy 127.0.0.1:{{ authelia_port }}`, `Location` dropped from its log lines. No `trusted_proxies`.
- **Compose:** `authelia` with `<<: *hardening`, `127.0.0.1:{{ authelia_port }}:9091`, named volume `authelia-data`
  (SQLite), uid 10001, `read_only`, `mem_limit` from S8 (start 128m) (EDGE-05). Digest pin + `docs/versions.md` row
  (Rule 4, EDGE-01).
- **Config delivery:** § 5.
- **Secrets** (SOPS, `init_secrets.py fill_missing`, EDGE-08 shapes, `no_log`): session secret, storage encryption key,
  reset-password JWT secret, OIDC HMAC secret, OIDC JWKS RSA key, one client secret per confidential client (stored as a
  hash in config), the people list. Losing the storage encryption key makes the SQLite unreadable, so a restore needs SOPS too.
- **Backup:** D7 shape for `authelia-data`; drill `integrity_check`; `scripts/edge-backup-test.py`; `recovery.md` step.
- **Monitoring:** Gatus `authelia-public` (`/api/health`, through 1.1.1.1).

### 3.2 Clients and access rules (per environment `prod` / `staging`, homelab only)

| Client | Type | Group (`authorization_policy`) | Scopes | Redirect |
|---|---|---|---|---|
| `xstockstrat[-staging]-web` | confidential | `xstockstrat[-staging]` | `openid email` | `${APP_URL}/api/auth/sso/callback` |
| `xstockstrat[-staging]-mcp-claude` | public PKCE | same | all `xstockstrat.*`, `offline_access` | `https://claude.ai/api/mcp/auth_callback` |
| `xstockstrat[-staging]-mcp-claude-ro` | public PKCE | same | `xstockstrat.read`, `offline_access` | same |
| `xstockstrat[-staging]-mcp-claude-code[-ro]` | public PKCE | same | as above | loopback (S9) |
| `webhooks-mcp-claude`, `webhooks-mcp-claude-code` | public PKCE | `webhooks-mcp` | `webhooks-mcp` (custom) | Claude.ai / loopback |
| `happybabytracker` | confidential | `happybabytracker` | `openid profile email groups` | `${ORIGIN}/auth/callback` (app-defined) |
| commsgun dashboard | no client: `forward_auth` rule on its host | `commsgun` | — | — |

### 3.3 Per app

- **xstockstrat (prod, staging):** 345 as specified plus D12. Edge keeps the legacy `/agent/oauth/*`, register, login page
  and login limits while password login exists (DO stays legacy; the homelab flag is per deployment). Removing them later is
  its own change.
- **webhooks-mcp:** a JWKS `TokenVerifier` (pyjwt `PyJWKClient`) and the SDK's `AuthSettings(issuer_url,
  resource_server_url, required_scopes)`; the PRM route `/.well-known/oauth-protected-resource/mcp`; amend C1/C4. Edge: the
  `bearer_mcp` macro also proxies the PRM path (or a new `oauth_mcp` kind); the Gatus probe still expects 401; JWKS is
  fetched server-side (§ 4, S11). Single-owner service: the `webhooks-mcp` group is the authorization.
- **commsgun (Q1 = public):** new `commsgun_ui_public_hostname` under `*.public` with `forward_auth 127.0.0.1:{{ authelia_port
  }} { uri /api/authz/forward-auth; copy_headers Remote-User Remote-Groups Remote-Email Remote-Name }`. Caddy deletes
  client-sent `Remote-*` first. Port 9202 stays loopback. EDGE-07 four-place wiring is undone for the private name, EDGE-11
  `ORIGIN` follows the public name, EDGE-02 holds (no XFF trust), and the zone's query/Referer redaction moves with it.
  commsgun side: trust `Remote-User` on the public origin only; keep the `ADMIN_PASSWORD` login as the switchable fallback
  (§ 5b, reach per Q16); an SMTP door for Authelia's notifier (§ 5a); amend its anti-goal.
- **happybabytracker (Q2):** OIDC client recommended over `forward_auth`: its spec's seam is exactly
  `identities(provider='authelia', subject=<sub>)`, and OIDC needs no trusted-header boundary. App side: an OIDC login route,
  link-or-provision by `sub`; the env-only admin stays a local login behind a switch (§ 5b). Exposure: Q10.

## 4. Q6 — `auth.private.<domain>` for the VMs

**Finding:** in v4.39 Authelia derives the OIDC issuer and every discovery URL from the request's host, and the host must be
a configured session-cookie domain. No fixed issuer setting exists yet (multi-issuer is on Authelia's roadmap). xstockstrat
345 checks the discovery `issuer` byte for byte, requires a same-origin `jwks_uri`, and calls the `token_endpoint` that
discovery returns.

So a plain `auth.private.<domain>` would make Authelia answer with `issuer: https://auth.private.<domain>`. The UI then
rejects it, and if it were accepted, the browser would be sent to a tailnet-only authorize URL. Routing and certificates are
not the obstacle (the VMs already hold `tag:edge:443` grants, `policy/policy.hujson:39,46`); the issuer identity is.

| Option | How | Cost | Breaks |
|---|---|---|---|
| **A. Public path only** (345 as designed) | VMs call `auth.public.<domain>` over the home WAN; Caddy exempts the home WAN IP from the `authelia_*` zones | One exemption var; if the WAN IP is dynamic, the exemption drifts (Q14) | Nothing; matches D-74 |
| **B. `auth.private` with the host rewritten** | `*.private` handle for `auth.private.<domain>` (between `tailnet_only` and `deny_staging_vm`, like the ingest), restricted to discovery, JWKS and token paths, `header_up Host`/`X-Forwarded-Host` set to the public name so Authelia answers as `auth.public` | EDGE-07 four places; an explicit EDGE-02 exception (`header_up X-Forwarded-Host` in the `*.private` site, which also fronts commsgun); **and a 345 amendment**: an `oidc.internal_base_url` that rewrites the origin of discovered back-channel URLs, because discovery still returns public URLs | 345's byte-exact discovery design (needs a new D-n there) |
| **C. Split-horizon `auth.public` for tailnet nodes** | headscale extra record → edge tailnet IP | Contradicts the "one view for every client" decision (`decisions.md:110`), and Linux nodes only resolve edge names through the `*.private` DNS route (EDGE-14) | Probably does not resolve on the VMs at all |

**Chosen: A.** webhooks-mcp needs no name at all: it runs on the edge and reaches Authelia over the compose network if S11
shows JWKS answers with the public `Host`.

**No IP exemption (Q14: the WAN IP is dynamic).** The rate-limit exemption 345 D-74 asks for is not possible by address.
Instead the zones are scoped by path, so the VM's calls never share a counter with brute-force targets:

| Zone | Paths | Who hits it | Limit |
|---|---|---|---|
| `authelia_login` | `POST /api/firstfactor`, `/api/secondfactor/*`, `/api/reset-password/*` | browsers only; never the VMs | tight (like `ratelimit.login`) |
| `authelia_backchannel` | `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server`, `/jwks.json`, `/api/oidc/token`, `/api/oidc/revocation` | the VMs (discovery cached 600 s, JWKS on rotation, one token call per login), MCP clients refreshing | generous; sized from S15 |
| `authelia_general` | everything else | browsers | default |

Authelia's own regulation (per-username lockout) is unaffected by the shared IP. The 345 cutover runbook's D-74 line becomes
"no zone on a back-channel path is tight" (a 345 amendment, § 9).

## 5. Q7 — how Authelia's configuration reaches the container

Authelia reads one YAML configuration plus, for the file backend, `users_database.yml`. Scalars and secrets can also come
from `AUTHELIA_*` env vars; lists (clients, access rules, cookies) cannot. With `X_AUTHELIA_CONFIG_FILTERS=template` the
YAML may read env vars (`{{ env "X" }}`), so secrets can stay in an `env_file` even with a file config. Sessions are
**in memory** (no Redis on the edge), so **every restart signs everyone out of the portal** and every `forward_auth`
session (commsgun). OIDC refresh tokens and consents live in SQLite and survive.

| | **A. Directory bind mount + hash-compare restart** | **B. Everything in `env_file`** | **C. Compose `configs:` with inline `content`** |
|---|---|---|---|
| Mechanism | `./authelia/:/config:ro` (directory, not a file: the Caddy inode trap, `decisions.md:225`); secrets in `authelia.env` read by the template filter; an unconditional task compares the container's file hash with the host's and restarts on mismatch (Caddy's precedent), so **no handler** | The whole YAML in one env var, written to a `tmpfs` file by an overridden entrypoint (`sh -c 'printf … > /run/authelia/config.yml; exec authelia --config …'`), the commsgun `COMMSGUN_CONFIG` precedent | Compose materialises the rendered content as a read-only file; secrets via `env_file` + template filter |
| Change detection | The hash task; survives a failed play (it re-runs each deploy) | Compose's config hash; native (EDGE-04's preferred shape) | Only if compose 5.5.1 hashes `configs.content` into the service (**S12**); otherwise as A |
| People changes | `authentication_backend.file.watch: true` reloads users **without a restart** (**S13**: the watch must survive Ansible's rename-replace inside a directory mount) | Recreate = everyone signed out on every people change | Recreate, as B |
| Image seam | None; stock entrypoint | Overrides the entrypoint; needs a shell in the image and a `tmpfs` under `read_only` (**S14**) | None |
| Secret exposure | Config file holds no secrets; `users_database.yml` (hashes, emails) mode 0400, uid 10001 | Secrets and hashes in the container env (`docker inspect`), as for the other services today | Content sits in `compose.yml`; keep secrets out via env |
| New code | One hash-compare task + one restart task (EDGE-03 shape) | Entrypoint override + tmpfs; no task | Compose top-level `configs:`, a first in this repo |

**Chosen: spike C, fall back to A.** C is taken only if **S12** (compose 5.5.1 recreates on a `configs.content` change)
passes. A people change under C recreates the container and signs everyone out; the owner accepts that, or **S13** decides
whether `users_database.yml` alone moves to A's watched directory mount. If S12 fails, A is built as specified above. B is
dropped.

## 5a. Q11 — commsgun as Authelia's notifier

Authelia v4.39 notifies through **SMTP or a file only**; commsgun accepts mail only through its HTTP enqueue API
(`POST /v1/messages` with `X-Commsgun-App`, `Authorization: Bearer`, `Idempotency-Key`; contracts K1–K6). So "use commsgun"
needs an SMTP door into it.

| Option | How | Cost |
|---|---|---|
| **A. SMTP listener inside commsgun-api** *(recommended)* | A small SMTP submission listener on the compose network only (never published, like the admin listener), `AUTH PLAIN` with app id `authelia` + its app key; each accepted message is enqueued as that app, reusing its limits, bounce tracking and dashboard | commsgun code + a contract row (new container port, K-item); edge: `commsgun_apps` gains `authelia`, compose network `commsgun` gains Authelia |
| B. Bridge sidecar on the edge | A tiny SMTP→HTTP relay container | A second custom-code service on the edge to pin, harden and back up; duplicates commsgun's validation |
| C. File notifier + a watcher | Authelia writes `notification.txt`; a systemd path unit posts it to commsgun | Parses Authelia's file format (not a contract); fragile |

Authelia side for A: `notifier.smtp.address: submission://commsgun-api:<port>`, plain on the internal network
(`disable_require_tls: true`, `disable_starttls: true`; `TODO(verify)` in S16 that v4.39 permits this with a non-loopback
host), `disable_startup_check: true` so an Authelia start never waits on commsgun. Failure mode: commsgun down → no
enrolment or reset codes; existing logins unaffected. Until A ships, the **file notifier** is the bootstrap (an admin reads
the code on the edge), so P1 does not wait for commsgun.

## 5b. Q12/Q13 — local logins kept as a switchable fallback

Both apps keep their own login as a break-glass path, **not mapped to any Authelia group**, with an app config flag that
turns it off (default on until each cutover proves SSO; the edge sets it from inventory, e.g. `commsgun_password_login_enabled`,
`happybabytracker_admin_login_enabled`).

- **happybabytracker:** easy. OIDC is app-level, so the login page offers "Sign in with SSO" plus, when the flag is on, the
  env admin's local form on the same host. Reachable while Authelia is down.
- **commsgun: the fallback is unreachable as specified.** On the public name, Caddy's `forward_auth` runs before the app, so
  with Authelia down the password form is never served. And the dashboard enforces a **single** `ORIGIN`, so it cannot also be
  served on `commsgun.private.<domain>`. Options (Q16):
  - **(a)** commsgun accepts a list of origins; the public name runs `forward_auth` + `Remote-User`; the private name
    (tailnet-only, no `forward_auth`, `Remote-*` deleted) serves the password login when the flag is on. EDGE-07 four-place
    wiring stays for the private name. *(recommended)*
  - **(b)** one origin; break-glass = flip an edge variable that drops `forward_auth` and puts the name back under
    `*.private`, then deploy. Simpler code, but the fallback costs a deploy and the tailnet path.
  - **(c)** commsgun becomes an OIDC client like happybabytracker instead of `forward_auth` (reverses the Q1 mechanism).

## 6. Phases and pass conditions (Rule 6: shown, not claimed)

| Phase | Work | Pass condition |
|---|---|---|
| P0 spike | Scratch v4.39.28, off the droplet | Each S-item (§ 6.1) recorded here with its observed output |
| P1 edge infra | § 3.1, one test person, no app wired | P1.0 droplet `free -m` + `docker stats --no-stream` ≥ 400 MiB left with the new limit; `ci` green incl. new routing cases (auth host → Authelia only, no XFF trust, `Location`/`code` absent from logs, unique zones, `authelia_session` stripped before xstockstrat/webhooks upstreams); **deploy**; **verify**; Gatus `authelia-public` green; one `edge-backup.service` run `backup ok` + drill with the Authelia DB; test person logs in with password + passkey/TOTP |
| P2 xstockstrat-staging | 345 shipped; staging config rows set; `sso.md` § 6 cutover | Claude.ai **and** Claude Code complete OAuth against Authelia and list tools; web login links the seeded row by email (ledger audit event seen); rollback (clear issuers, flag true) rehearsed |
| P3 xstockstrat prod | Same | Same checks on prod |
| P4 webhooks-mcp | § 3.3; C1/C4 amended | Unauthenticated `POST /mcp` 401 with `resource_metadata`; Claude.ai + Claude Code connect; a token for another audience gets 401 |
| P5 commsgun | § 3.3 | Dashboard via the public name redirects to Authelia; a forged `Remote-User` from the client is ignored (routing test); group-less person gets 403; the fallback password login works on its Q16 path with Authelia stopped, and is refused with the flag off; an enrolment code sent through commsgun arrives (if § 5a A) |
| P6 happybabytracker | § 3.3 | A parent logs in via Authelia and lands on their kids; an `identities` row with `provider='authelia'` exists; group-less person refused; the env admin logs in locally with Authelia stopped, and cannot with the flag off |

### 6.1 Spike items
S1–S5 as in `decisions.md:51-53` (345's 2026-11-16 local spike already passed most of them; re-run against the edge shape).
- **S7** regulation keys on Caddy's single overwritten XFF entry with no `trusted_proxies`.
- **S8** idle and login-burst RSS, to size `mem_limit`.
- **S9** a loopback redirect for Claude Code: does v4.39 accept `http://127.0.0.1/callback` with any port (RFC 8252 § 7.3) or need a fixed port that Claude Code can be told to use?
- **S10** issuer follows `Host` (§ 4 finding): observe discovery on a second host name.
- **S11** JWKS fetched over the compose network with `Host: auth.public.<domain>` returns the keys.
- **S12** compose 5.5.1 recreates the container when `configs.content` changes.
- **S13** `file.watch` reloads after an Ansible rename-replace inside a read-only directory mount.
- ~~**S14**~~ dropped with option B.
- **S15** back-channel request rate per VM (discovery, JWKS, token, refresh) over one day of staging, to size `authelia_backchannel`.
- **S16** the SMTP notifier accepts a plain, unauthenticated-TLS submission to a non-loopback compose host (`disable_require_tls`).

## 7. Still open

- **Q10** happybabytracker exposure (blocks P6): public under `*.public` *(recommended: parents off the tailnet can reach it;
  its spec § 15 leaves the path to this work)*, or private with a second cookie domain `private.<domain>` and a browser
  portal `auth.private.<domain>` (a second login per person).
- **Q11′** confirm option A of § 5a (blocks commsgun-as-notifier; P1 starts on the file notifier).
- **Q16** commsgun fallback reach, § 5b (a)/(b)/(c) (blocks P5).
- **Q9** fill `docs/runbooks/sso-people.template.yaml` (blocks P2; P1 needs one test person).

## 9. Handoff: work per repo

The next session has every repo. Order follows § 6. Each item cites what it must honour.

| Repo | Work | Must honour |
|---|---|---|
| homelab-edge | P0 spike (S1–S16); P1 § 3.1 (C-or-A config, path-scoped zones, file notifier, backup, Gatus, routing tests); later the per-app edge wiring | Rules 1–8, EDGE-01…14; the spec's pass conditions |
| xstockstrat | Amend 345 before `/sdd-execute`: issuer value `https://auth.public.<domain>`; Claude Code clients `…-mcp-claude-code[-ro]` (D12, S9); D-74 rewritten to path-scoped zones (§ 4); config migration renumbered to the next free number (056 at `6ce536f`); then execute | 345 `design.md`, `debate.md` D-74/D-96/D-97, `merge-order.md:274` |
| webhooks-mcp | Spec § 6 amendment for C1/C4; JWKS `TokenVerifier`, `AuthSettings(issuer_url, resource_server_url, required_scopes)`, PRM route; drop `MCP_AUTH_KEY` | `docs/contracts.md` (C1, C4, C8), rule WM-08 revisited |
| commsgun | Amend the "dashboard reachable from the internet" anti-goal; `Remote-User` trust on the public origin; switchable password fallback; reach per Q16; SMTP submission door for Authelia (§ 5a) | K1–K6, U1–U4, CG-01…09 (CG-03 XFF, CG-08 three-place env) |
| happybabytracker | Amend non-goal (line 54) and anti-goal (line 69); OIDC login → `identities(provider='authelia', subject=<sub>)`; switchable env-admin fallback; exposure per Q10 | spec § 5.4, § 13, § 15; `CLAUDE.md` Rule 1 contracts |

## 8. Docs to change when implemented

`docs/runbooks/sso.md` (status, hostname, scope, groups, SOPS keys, hash command, notifier), `docs/decisions.md` (SSO entry
rewritten from § 1/§ 4/§ 5 outcomes), `README.md`, `docs/versions.md`, `docs/runbooks/recovery.md`, `docs/runbooks/alerts.md`,
`docs/runbooks/commsgun.md` and `happybabytracker.md`, `secrets/README.md`, `docs/context-constitution.md` (new seams: auth
host XFF, session-cookie stripping, `Remote-*` deletion, EDGE-07 for commsgun).
