# Authelia SSO — re-spec (2026-11-23)

Status: **design approved by the owner, not deployed.** It re-specs the 2026-11-15 design against `main` at `c06667e` and
the app repos at xstockstrat `6ce536f`, webhooks-mcp `c9be704`, commsgun `017db3e`, happybabytracker `d3820ab`, after two
adversarial reviews. Summaries live in `docs/decisions.md` § "SSO is Authelia" and `docs/runbooks/sso.md`; this file is the
full record. Open: § 12.

## 1. Decisions

| # | Decision |
|---|---|
| 1 | Authelia **v4.39.28** (latest, 2026-09-17), digest-pinned. **lldap** as the user store (LDAP backend), SQLite storage, in-memory sessions (no Redis), `default_policy: deny`, `two_factor` everywhere |
| 2 | Scope: **homelab only**. xstockstrat prod and staging (web + MCP), webhooks-mcp, the commsgun dashboard, happybabytracker. DigitalOcean deployments stay `AUTH_MODE=local`. Gatus and Proxmox later; headscale never |
| 3 | Portal **`auth.public.<domain>`** under the existing `*.public` wildcard (Rule 8). No second name: v4.39 derives the issuer from the request host (§ 4.2) |
| 4 | Every app is a **provider-neutral OIDC client** (OSS: forks must not depend on Authelia). webhooks-mcp is an OAuth 2.1 resource server. **No `forward_auth` for our own apps**; allowed only for third-party software without OIDC |
| 5 | **`AUTH_MODE`** per app: `local` (default) \| `oidc` \| `both`; webhooks-mcp: `static` \| `static+oauth` (transition) \| `oauth`. The homelab's steady state is **`oidc`**; `both` is switched on only to link accounts or for break-glass (§ 3) |
| 6 | **Apps own users and roles** in their database. Authelia proves identity and gates which app a person may open (one group per app). No group→role mapping |
| 7 | **Explicit linking** everywhere: a signed-in local user presses "Link SSO"; or an invite is redeemed by SSO (happybabytracker). Unlinked SSO logins and MCP tokens are refused. Just-in-time provisioning only behind a flag, default off. *Amended 2026-11-24 (owner): no app implements the flag (§ 3); webhooks-mcp has no accounts and authorizes by scope (decision 16)* |
| 8 | **No passwordless accounts in xstockstrat**: every user has a local password (`create-user`, then "Link SSO"), which keeps DigitalOcean supported. 345's SSO-only end state, SSO-created users (D-6) and email linking are dropped |
| 9 | Env-seeded superusers become a **"create if empty" CLI bootstrap** plus a one-time import of today's env admin; `admin reset-password` CLI for break-glass |
| 10 | Identities keyed on **`(issuer, subject)`**. Permanent: the portal hostname (issuer and passkey RP ID), usernames (never reused), pinned `sub`s, lldap's `key_seed` |
| 11 | VMs reach `auth.public` over the home WAN; edge containers through `host-gateway` to Caddy (§ 4.2). Rate limits **path-scoped**, no IP exemption (the WAN IP is dynamic) |
| 12 | Regulation `modes: [user]`, 5 min ban; Caddy per-IP limits on top (§ 4.4) |
| 13 | Lifetimes: access token 1 h; MCP refresh 30 days, rotating; **password-reset link 1 h** (absorbs Resend's rate limiting); explicit portal session times (§ 4.5) |
| 14 | **Self-service password reset** by email for everyone; lldap is the only writer of passwords; SOPS never holds one (§ 4.10) |
| 15 | **Second factors:** `require_second_factor: true`, so another device needs an existing one. Recovery after losing every device is an **admin-present re-enrolment**; after losing Authelia's database, restore from backup first (§ 8) |
| 16 | webhooks-mcp authorizes by an app-side **`(iss, sub)` allowlist** plus scope; a **client-credentials** client for headless use. *Amended 2026-11-24 (owner): **scope only**: issuer, audience and the `webhooks-mcp` scope, which Authelia grants only to clients gated on group `webhooks-mcp`; no allowlist. Amended 2026-11-27 (owner): no client-credentials client: the access logs show no headless caller (§ 4.6)* |
| 17 | Authelia's, lldap's and commsgun's databases are in the **edge backup**; a deploy runs the backup before an Authelia or lldap digest change |
| 18 | Local logins, when switched on, are **public behind Caddy's login limits** (the pattern xstockstrat already uses); a tailnet-only rule cannot work on a `*.public` name (F9) |
| 19 | Each person's **`sub` is pinned** in two places that restore each other: Authelia's storage and an `sso-sub` attribute on their lldap user; reconciled before Authelia serves; a mismatch fails the deploy (§ 4.7) |
| 20 | Config delivery: spike compose `configs:` content (C), fall back to a mounted directory + hash-compare restart (A) (§ 4.8). *Settled 2026-11-27 by S12: **A** (Compose 5.5.1 does not recreate on a `configs.content` change)* |
| 21 | Claude.ai **and** Claude Code clients per environment; both Claude.ai callback hosts; `requested_audience_mode: implicit`. *Amended 2026-11-27 (owner): Claude.ai clients only; the edge access logs (30 days) show no local Claude Code MCP client. Adding one later is a new Authelia client, no app code* |
| 22 | People: § 7. Login is gated on lldap group **`sso_people`**; offboarding removes the person from it (lldap has no disable flag) |
| 23 | Notifier: an **SMTP submission listener inside commsgun-api, STARTTLS** with an edge-local CA, on a network shared only with Authelia; file notifier until it ships (§ 9) |
| 24 | One **browser profile per account** for people with several (`david`, `david_admin`, `david_claude`) |
| 25 | lldap admin UI at **`lldap.private.<domain>`** (tailnet-only) |
| 26 | **lldap owns people** (owner, 2026-11-23): admins add, change and offboard people in its UI; SOPS holds no people. The repo declares only the group names and the `sso-sub` attribute, which a deploy ensures exist. *Amended 2026-11-27 (owner): the deploy also creates and owns Authelia's bind account, the single exception (§ 4.10)* |

## 2. Findings that drove the changes

| # | Finding | Evidence |
|---|---|---|
| F1 | `auth.<domain>` broke Rule 8; `*.public` already has a wildcard cert | `CLAUDE.md` Rule 8; `Caddyfile.j2` `*.{{ public_zone }}` |
| F2 | commsgun: internet exposure an anti-goal; single fixed `admin`; stateless HMAC cookie (U4) with no principal | commsgun spec `2026-11-13-commsgun-design.md:48,56`; `ui/src/lib/server/session.ts:19` |
| F3 | webhooks-mcp: static `MCP_AUTH_KEY`; MCP SDK 2.3.0's `AuthSettings`/`TokenVerifier`/PRM unused; single owner | `src/webhooks_mcp/mcp_app.py:176-197`; `docs/contracts.md` C1, C4 |
| F4 | happybabytracker: env-only admin structurally not a parent; `identities` seam; internet exposure an anti-goal | its spec §5.1, §5.4 (`:137-148,184-193`), `:54,69` |
| F5 | xstockstrat 345: `implementation-ready`, 0/52 steps; email linking and auto-create; migration `053` stale; no Claude Code client | `345-authelia-sso/status.md`, `product-spec.md:54-67,364`, `merge-order.md:274` |
| F6 | Authelia v4.39 derives issuer and discovery URLs from the request host | [authelia#12250](https://github.com/authelia/authelia/discussions/12250) |
| F7 | 345 needs the cookie domain to cover each `APP_URL` → `public.<domain>` | `345-authelia-sso/design.md:373-375` |
| F8 | The edge never hairpins through its reserved IP; edge app containers are on bridge networks; no host firewall, only the cloud firewall | `docs/decisions.md:10`; `compose.yml.j2:111,138`; `roles/base` |
| F9 | Public names have no split-horizon: tailnet clients reach them over the internet, so Caddy sees their WAN IP | `headscale-config.yaml.j2:68-70`; `roles/tailscale/tasks/main.yml:84-86`; `group_vars/all/main.yml:115` |
| F10 | The file backend writes reset passwords into its file; lldap has no disable flag ([lldap#750](https://github.com/lldap/lldap/issues/750)); lldap's default image starts as root | Authelia passwords guide; lldap Docker Hub |
| F11 | `require_second_factor` applies only while a device is registered; `authelia storage` has no OAuth token revocation; plaintext SMTP is unsupported; Authelia checks NTP at start | Authelia docs (§ 14) |
| F12 | App digests reach the edge through bot PRs that change only the digest | `.github/workflows/bump-*.yml` |
| F13 | All commsgun apps share a 100/day provider cap | `group_vars/all/main.yml:91-92` |

## 3. The app auth contract (all four repos)

- **Config:** `AUTH_MODE`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_AUTO_PROVISION`
  (default `false`). Amended 2026-11-24 (owner, app-spec PRs): no app implements auto-provisioning; commsgun and
  happybabytracker refuse `OIDC_AUTO_PROVISION` if set (even `false`), and xstockstrat has no such key: the edge never sets it. xstockstrat keeps its config-service keys (`ui/oidc.*`, `agent/oidc.issuer`) plus `auth.mode`, in place
  of 345's issuer-presence switch and `password_login_enabled`.
- **Modes:** `local` = password login (xstockstrat MCP: its built-in OAuth server); `oidc` = SSO only (MCP: the external
  issuer); `both` = SSO and password on one login page. The homelab runs `oidc`; `both` is a deliberate window for linking or
  break-glass, and the login page must keep working in `both` while the issuer is unreachable. xstockstrat's MCP uses its
  built-in OAuth server in `local` and `both` and the external issuer only in `oidc` (owner, 2026-11-24), so a `both` window
  cuts every Authelia-connected xstockstrat MCP connector off until the mode is `oidc` again (`sso.md` § 6).
- **Data:** users with a role; `identities(user_id, issuer, subject)` unique on `(issuer, subject)`; local passwords stay
  with the user. happybabytracker keeps its admin in a separate principal table with its own identities.
- **Relying party:** the UI server does the code flow (PKCE; `state` and `nonce` bound to the browser session; an OIDC
  transaction cookie `SameSite=Lax`), validates the ID token, then calls an internal API route (e.g. `POST /v1/session/oidc
  {iss, sub, email, name}`); the API alone resolves the identity and issues the session.
- **Callback state (owner, 2026-11-27; every app, current and future):** the callback is a cross-site top-level navigation,
  so it relies only on the transaction cookie (`SameSite=Lax`, encrypted and integrity-protected, path-scoped, ≤ 10 min,
  bound to `state`). Whatever the callback must show or bind to (purpose, `next`, the display username, the hash of the
  session it binds to, the flow's start time) is sealed in that cookie, which stays well under 4 KB and holds no other
  secret. Authorization happens only at a same-origin POST (the confirmation), which re-checks the live session against the
  bound hash; the sealed cookie alone never authorizes. Mandatory where the session cookie is `SameSite=Strict`
  (commsgun, happybabytracker); xstockstrat's `Lax` session and session-gated confirm POST already comply.
- **Signing keys (owner, 2026-11-27; no exceptions):** a token or ID token is accepted only on a key from an issuer key
  set fetched within the last **3600 s**; past that, a failed refresh refuses every token (resource servers 503, relying
  parties fail the SSO attempt). No stale-key grace window anywhere. `openid-client` relying parties call
  `enableNonRepudiationChecks` so ID-token signatures are verified (its cache refetches at 300 s and fails closed).
- **Linking:** "Link SSO" only for a signed-in local user, after local re-authentication, with `prompt=login` (S17) and a
  "you are linking <IdP user> to <account>" confirmation. Every link and every SSO invite redemption sends `prompt=login`
  and `max_age=1` (not `0`: Authelia v4.39.28 then refuses consent even after re-authentication, spike S17; owner,
  2026-11-27), and the relying party refuses unless the ID token's `auth_time` is at least the flow's start time
  minus a 30 s skew (owner, 2026-11-27); a missing `auth_time` is refused. happybabytracker invites are redeemable by SSO in `oidc`/`both`
  and by password in `local`/`both`; its reset links work only in `local`/`both` (owner, 2026-11-24). Unlinked logins:
  `not linked`.
- **Bootstrap and break-glass:** `<app> admin create <username>` (if no admin exists) and `<app> admin reset-password
  <username>`, run as `docker compose run --rm` (EDGE-03 shape). The app imports today's env admin once, at start; the env
  password is then ignored with a warning and its SOPS key retired in a later edge change, once the import is proven.
- **Switching mode without a login:** an operator changes each app's mode without signing in (env for commsgun and
  happybabytracker; for xstockstrat a runbook step that sets `auth.mode` over the tailnet).
- **Sessions:** server-side or versioned, so disabling a user or removing a role ends their sessions (commsgun amends U4).
  xstockstrat's bounded deviation: a role change reaches a web session at the next access-JWT refresh (345 FR-10).
- **Upgrade safety:** a new image keeps today's behaviour by default (`AUTH_MODE=local`, the env admin still accepted) until
  the edge sets the new variables; the import is idempotent; migrations are forward-only, so a digest is never reverted past one.
- **Conformance:** one shared list of OIDC cases (bad `state`, wrong `iss`/`aud`, expired token, unlinked `sub`, replayed
  code, link while another IdP session is live, a missing or stale `auth_time` on a link or SSO invite, an ID token on a
  key set older than 3600 s with the refresh failing) run by commsgun and happybabytracker.
- **Threat-model pass:** before an app goes public, a checklist section in its own spec (exposed routes, auth on each,
  rate limits, secrets in URLs and logs, session lifetime, CSRF), reviewed by the owner.

## 4. Edge design

### 4.1 Authelia service
- `authelia_public_hostname: "auth.{{ public_zone }}"`; a dedicated `@authelia host` handle in `*.{{ public_zone }}` before
  the `edge_sites` loop; `reverse_proxy 127.0.0.1:{{ authelia_port }}`; no `trusted_proxies` (EDGE-02; S7).
- Compose: `<<: *hardening`, `127.0.0.1:{{ authelia_port }}:9091`, named volume `authelia-data`, uid 10001, `read_only`,
  `mem_limit` 128m (§ 6, S8; EDGE-05). Digest in `versions.yml` + `docs/versions.md` row (Rule 4, EDGE-01). Its networks keep
  outbound access for the startup NTP check (or `ntp.address` is set deliberately).
- Password reset on; `identity_validation.reset_password.jwt_lifespan: 1h`;
  `identity_validation.elevated_session.require_second_factor: true`.
- LDAP backend `implementation: lldap`; `users_filter` requires membership of `sso_people` and matches `uid` only (no login
  by email), so lldap's `admin` and Authelia's bind account can never log in on the portal.

### 4.2 Back-channel paths
- **VMs** (xstockstrat UI and agent): `auth.public.<domain>` over the home WAN; the VMs already hold `tag:edge:443`.
- **Edge containers** (commsgun-ui, happybabytracker-ui, webhooks-mcp): `extra_hosts: ["auth.public.<domain>:host-gateway"]`,
  reaching Caddy (host networking) with the right name, certificate and issuer (S18).
- Every back-channel request carries `Host: auth.public.<domain>`, and Caddy passes `Host` through unchanged: v4.39.28
  serves discovery only for its configured portal URL and answers `400` for any other host (S10), so an IP address or an
  alias cannot stand in for the name.

### 4.3 Caddy
- Zones (EDGE-12 prefixes): `authelia_login` (`POST /api/firstfactor`, `/api/firstfactor/*` (re-authentication), `/api/secondfactor/*`, `/api/reset-password`, `/api/reset-password/*`, `/api/user/session/elevation`, `/api/user/session/elevation/*`; 10/min, owner 2026-11-27; per
  IP, tight); `authelia_backchannel` (discovery, `/jwks.json`, `/api/oidc/token`, `/api/oidc/revocation`; generous, S15);
  `authelia_general`.
- `authelia_session` (scoped to `public.<domain>`) stripped by `header_up Cookie` before every app upstream.
- Logs on the auth host and the newly public apps: query, `Referer` and `Location` dropped, token path segments
  (`/invite/<token>`, `/reset/<token>`) redacted, as the `*.private` logger does.
- Local login POSTs (commsgun, happybabytracker, xstockstrat `POST /api/auth/login`) are public, behind `<site>_login` and
  `<site>_login_hourly` zones like xstockstrat's today; the app refuses them unless its mode is `local` or `both`.
- **Every browser POST that takes a password** sits behind the same two zones (owner, 2026-11-24; a cutover gate: the zone
  is deployed before that app's first `both`):
  - xstockstrat: `/api/auth/login` and the Link confirmation `/accounts/api/sso/confirm` (345 FR-16);
  - commsgun: `/login` and `/account` (re-auth; commsgun spec 12.5);
  - happybabytracker: `/login`, `/invite/*`, `/reset/*`, `/account`, `/account/link-sso`, `/admin/account/link-sso`
    (its spec § 5.5, § 18).
- `caddy-routing-test.sh` pins every rule above.

### 4.4 Regulation
`modes: [user]`, `max_retries: 3`, `find_time: 2m`, `ban_time: 5m`. Unban: `authelia storage bans user revoke`. Whether bans
key on the normalised username: S25.

### 4.5 Lifetimes
Access token 1 h; MCP clients' refresh token 30 days, rotated on use; authorization code default (≥ 30 s, 345 D-73);
password-reset link 1 h; portal session `expiration` 1 h, `inactivity` 15 min, `remember_me` 30 days.

### 4.6 Clients
Every MCP client: `access_token_signed_response_alg: RS256`, an explicit `audience`, `requested_audience_mode: implicit`.

| Client | Type | Group | Scopes | Redirect | Audience |
|---|---|---|---|---|---|
| `xstockstrat[-staging]-web` | confidential, `client_secret_basic` + PKCE | `xstockstrat[-staging]` | `openid profile email` | `${APP_URL}/api/auth/sso/callback` | — |
| `xstockstrat[-staging]-mcp-claude` | public PKCE | same | `xstockstrat.*`, `offline_access` | `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback` | `${APP_URL}/agent` |
| `xstockstrat[-staging]-mcp-claude-ro` | public PKCE | same | `xstockstrat.read`, `offline_access` | same | same |
| `webhooks-mcp-claude` | public PKCE, access tokens 5 min (§ 4.11) | `webhooks-mcp` | `webhooks-mcp`, `offline_access` | Claude callbacks | `https://webhooks-mcp.public.<domain>/mcp` |
| `commsgun` | confidential, `client_secret_basic` + PKCE | `commsgun` | `openid profile email` | `${ORIGIN}/auth/callback` | — |
| `happybabytracker` | confidential, `client_secret_basic` + PKCE (owner, 2026-11-27) | `happybabytracker` | `openid profile email` | `${ORIGIN}/auth/callback` | — |

No Claude Code (loopback) or client-credentials client (owner, 2026-11-27): 30 days of edge access logs show only
Claude.ai's connector (`Claude-User`, Anthropic's egress) and Gatus on `/agent` and `/mcp`. xstockstrat's
`ui/oidc.mcp_client_id_claude_code*` keys stay empty, which hides those lines on its card (345 FR-11).

The three web clients (xstockstrat, commsgun, happybabytracker) share a `claims_policy` whose `id_token` list holds `email`
and `preferred_username`: 345's Link confirmation and commsgun's read them from the ID token. 345 also needs `auth_time`
in it (D-111): S17 showed Authelia includes it without the policy listing it (the spike's policy listed only `email`
and `preferred_username`). Scopes amended 2026-11-24 (owner):
the xstockstrat web client gains `profile`.

### 4.7 Identity, secrets and data
- **Pinned `sub`:** each lldap user carries an `sso-sub` attribute (lldap v0.6.3 accepts only `a-z`, `A-Z`, `0-9` and `-`
  in attribute names, so not `sso_sub`: P0 spike S22, 2026-11-27). Before Authelia serves, a `compose run --rm` step runs
  the storage migration, then reconciles each member of `sso_people` from `authelia storage user identifiers export`
  (read first: `identifiers add` exits 1 on any existing row for that user, even an identical one, S19): Authelia has an identifier and lldap none (first login
  minted it) → copy it to lldap; lldap has one and Authelia none (Authelia's database lost) → `authelia storage user
  identifiers add`; both and different → **fail the deploy** (S19). lldap must be up first (`depends_on`). Offboarding
  clears the attribute (otherwise a later deploy would re-apply it) and leaves the identifier: 4.39.28's CLI has no
  identifier delete, and the row is harmless (out of `sso_people`, ignored by the reconcile, usernames never reused; owner,
  2026-11-27).
  Between a first login and the next deploy, the `sub` exists only in Authelia (and its twice-daily backup).
- **Secrets** (SOPS, `init_secrets.py fill_missing`, EDGE-08, `no_log`): session secret, storage encryption key, OIDC HMAC
  secret, OIDC JWKS RSA key, client secrets **and their pbkdf2 hashes, generated once** (as `ntfy_alerts_password_bcrypt`,
  so the rendered config is stable), lldap JWT secret, `key_seed`, lldap admin password, Authelia's bind password, the SMTP
  listener's credential, CA, certificate and key (mounted read-only for commsgun's `SMTP_TLS_CERT_FILE` /
  `SMTP_TLS_KEY_FILE`), and happybabytracker's `SESSION_SEAL_KEY` (UI, ≥ 32 characters). The edge's SOPS is the source of truth for client secrets; xstockstrat's are
  copied into its config service at cutover (runbook step).
- **Never rotate:** lldap's `key_seed` (stored passwords depend on it). lldap's admin password applies only at first start;
  rotating it is a UI or CLI step, documented when the key lands in `secrets/README.md`.
- **Rotation runbook:** JWKS — add the new key, publish, wait one access-token lifetime, retire the old. Client secrets — a
  short planned outage per client.
- **Backup:** `authelia-data`, `lldap-data` and `commsgun-data` join `edge-backup.sh` in the uid-10001 private-bind-mount
  shape, with drill `integrity_check`s (`scripts/edge-backup-test.py`, `recovery.md`). A deploy runs the backup before
  changing the Authelia or lldap digest (both migrate on start); rollback = restore (`docs/versions.md`).

### 4.8 Config delivery
**A** (S12, 2026-11-27: Compose 5.5.1 kept a running container on the old `configs.content`): a directory mount plus an
unconditional hash-compare restart task, never a handler. No users file exists (lldap holds people); `compose.yml` stays free of secrets;
Authelia's `{{ env }}` template syntax is escaped from Jinja; CI validates the rendered config with the pinned image (S20): `validate-config` exits 0 but warns about every custom
scope (`xstockstrat.*`, `webhooks-mcp`), so CI passes on exit 0 and does not fail on warnings.

### 4.9 Monitoring
- **P1:** `authelia-public` (`/api/health`) and `authelia-discovery` (exact `issuer`, non-empty JWKS) through 1.1.1.1;
  `lldap-private` (its UI's health through the private name); `verify.yml` cases for the new hosts.
- **P5:** a TCP probe on the SMTP listener's fixed `authelia-smtp` address (§ 9); the commsgun probes move to the public name.
- **P6:** the happybabytracker probes move to the public name.

### 4.10 lldap (user store)
- Compose service `lldap` from a pinned **`*-rootless`** image with `user: "10001:10001"`, `<<: *hardening`, named volume
  `lldap-data` (SQLite), `read_only`, `mem_limit` 128m (§ 6, S23). LDAP (container port 3890) on the `authelia` network only.
- **Admin UI at `lldap.private.<domain>`** (owner, 2026-11-23): `lldap_ui_private_hostname`, container port 17170 published
  as `127.0.0.1:{{ lldap_ui_port }}` (EDGE-05), wired in the four EDGE-07 places (hostname var, a `host` matcher under
  `*.{{ private_zone }}` after `import deny_staging_vm`, the split-horizon `_names`, the Gatus probe). The deploy steps use
  the same loopback port. A signed-in lldap user could change their password outside Authelia's policy; the UI is for admins,
  who sign in as lldap's own `admin` (its password from SOPS, applied at first start).
- Authelia binds as a service account in `lldap_password_manager` (S21). The deploy creates and owns it (owner, 2026-11-27):
  `authelia`, in `lldap_password_manager` only, refused if it is in any SSO group, its password set from SOPS on every deploy.
- **lldap owns people** (decision 26): an admin creates each person in the UI (username, display name, email, groups, always
  `sso_people`) and offboards them by removing `sso_people`. A deploy only ensures that the group names Authelia's access
  rules reference and the `sso-sub` attribute exist; it never changes a person or a membership (S22), the bind account
  above being the single exception (owner, 2026-11-27). A new person sets a
  password with "Forgot password". No audit trail beyond lldap's own database, and no rebuild of people without a backup:
  both accepted with decision 26.
- Accepted residual: a brand-new account's first password and first second factor both come from emailed codes; an admin
  can enrol with the person to close it.

### 4.11 Revocation of MCP access (owner, 2026-11-27: C, after S27)
Access tokens are JWTs that webhooks-mcp checks offline until they expire, and a refresh survives a group removal (S4).
Decided: **C**. The `webhooks-mcp-claude` client's access tokens live **5 min** (an Authelia custom lifespan for that
client only); revoking webhooks-mcp means removing `david_claude` from `sso_people`, which stops its refresh (S24), so
access ends within 5 min. `david_claude` holds only the `webhooks-mcp` group, so that removal revokes nothing else.
Removing only the `webhooks-mcp` group does not end access and is not the procedure.

Rejected, A (S27, 2026-11-27): Authelia's bearer check (`/api/authz/forward-auth`) refuses a removed person at once, but
only for opaque `authelia_at_` access tokens; it never accepts the JWT access tokens webhooks-mcp verifies. A would have
meant redesigning webhooks-mcp to introspect opaque tokens at Authelia, ~185 ms and an lldap lookup on every MCP call,
`/mcp` down whenever Authelia or lldap is, a decision 4 exception, and a Claude.ai connector that supports PAR,
`form_post` and explicit consent (unverified). Introspection does not reflect group or `sso_people` removal either.

xstockstrat's MCP is out of scope here: it accepts its built-in server's tokens in `local`/`both`, so an Authelia check
cannot sit in front of it, and disabling the user in the app already refuses its tokens within ~60 s (345 @AC-14: the MCP resolve cache lives 60 s).

## 5. Per app

| App | Edge | App repo |
|---|---|---|
| xstockstrat | Legacy `/agent/oauth/*` and register routes stay for `local` and `both` (DigitalOcean, the linking window, break-glass and rollback); the password login routes and `/accounts/api/sso/confirm` sit behind the login zones (§ 4.3); VMs back-channel per § 4.2 | 345 amended: `auth.mode` (`local`/`oidc`/`both`); explicit "Link SSO"; no email linking, no SSO-created users, no `LINKED_BY_EMAIL`/`CREATED`; unlinked MCP tokens → 403 `not linked`; issuer `https://auth.public.<domain>`; Claude.ai clients only (Claude Code keys left empty); D-74 → path-scoped zones; D-96 without access-token `email`; config migration takes the next free number at execute (060 on main-dev today) |
| webhooks-mcp | `bearer_mcp` also proxies `/.well-known/oauth-protected-resource/mcp`; `extra_hosts`; Gatus still expects 401; `AUTH_MODE`, `OAUTH_ISSUER` (no trailing slash) and `OAUTH_RESOURCE` (path exactly `/mcp`) set and removed together (C4) | `AUTH_MODE` `static`/`static+oauth`/`oauth`; JWKS `TokenVerifier` + `AuthSettings`; issuer + audience + required `webhooks-mcp` scope, no allowlist (decision 16 as amended); C1/C4 amended; CI against a stub issuer. Every `MCP_AUTH_KEY` consumer moves during `static+oauth`: Claude.ai's connector is the only one (`sso.md` § 6b). In `oauth` the edge omits `MCP_AUTH_KEY` entirely, never templates it empty: webhooks-mcp refuses a present-but-empty value at start (webhooks-mcp PR #17; P4) |
| commsgun | `commsgun.public.<domain>`; EDGE-07 undone for the private name, `ORIGIN` follows (EDGE-11); redaction moves; `extra_hosts`; a network `authelia-smtp` holding only `authelia` and `commsgun-api`; `commsgun_senders` and `commsgun_apps` gain `authelia` (`smtp_submission: true`, `send_window_seconds: 3600`); fixed IPv4 subnets (owner, 2026-11-27; outside Docker's default pools, free on the droplet): `commsgun` `172.16.200.0/28` (`ip_range` `172.16.200.0/29`, commsgun-api `172.16.200.10`) and `authelia-smtp` `172.16.200.16/28` (`ip_range` `172.16.200.16/29`, commsgun-api `172.16.200.26`); pinning the existing `commsgun` network's subnet recreates it, a short planned commsgun outage at P5 (Compose 5.5.1 recreates the network in place non-interactively, S26b: commsgun-api and commsgun-ui are stopped and recreated, bounded by the API's 30 s `stop_grace_period`); API env `ENQUEUE_ADDR`/`ADMIN_ADDR` (the `commsgun` address, 9200/9201), `SMTP_ADDR` (the `authelia-smtp` address, 9203), `SMTP_TLS_CERT_FILE`/`SMTP_TLS_KEY_FILE`, `AUTH_MODE`; UI env `OIDC_*` (never `OIDC_AUTO_PROVISION`); the CLI runs as a `commsgun-admin` service with `network_mode: none` (a plain `compose run` of the API would clash with its fixed addresses); login zones on `/login` and `/account` | § 3 contract; users table + role (`admin`); revocable sessions (U4 amended); SMTP submission listener with STARTTLS, reachable by Authelia and by host-network processes on the droplet, never published (listeners bound per network, § 9; STARTTLS and the app key are the control), app `authelia`; its messages expire with the reset link (1 h); anti-goal and single-admin non-goal amended; threat-model pass before P5 |
| happybabytracker | `happybaby.public.<domain>`; EDGE-07 undone, `ORIGIN`/`PUBLIC_ORIGIN` follow; token paths and the callback query redacted; `extra_hosts`; API env `AUTH_MODE`; UI env `OIDC_*` and `SESSION_SEAL_KEY` (never `OIDC_AUTO_PROVISION`); login zones on its six password routes (§ 4.3) | § 3 contract; admin principal table + identities; invites redeemable by SSO (password redemption refused in `oidc`); non-goal (`:54`) and anti-goal (`:69`) amended; threat-model pass before P6 |

## 6. Memory budget
Re-measure on the droplet before P1 (`free -m`, `docker stats --no-stream`): at least 400 MiB must stay free with Authelia's
and lldap's limits added (declared limits are 1024m today on 2 GB). With lldap, password checks are LDAP binds: S8 and S23
measure both services under N parallel logins.

Measured 2026-11-27 (P0; `docker stats` snapshots, not true peaks): the droplet used 743 MiB with 1221 MiB available;
Authelia 60 MiB idle and 68 MiB after 20 parallel logins; lldap 3 MiB idle and 54 MiB while hashing those logins.
Limits `authelia` 128m and `lldap` 128m (about twice the observed peak; owner, 2026-11-27).

## 7. People

| Username | Groups (all also in `sso_people`) | Links to |
|---|---|---|
| `david` | `xstockstrat`, `xstockstrat-staging`, `happybabytracker` | xstockstrat: the seeded admin row in each environment, by "Link SSO" after a password login; the Claude.ai connector signs in as `david`. happybabytracker: his existing parent row |
| `david_claude` | `webhooks-mcp` | nothing to link: the group grants the `webhooks-mcp` scope (decision 16 as amended) |
| `david_admin` | `commsgun`, `happybabytracker` | commsgun's imported admin row and happybabytracker's admin principal, each by "Link SSO" from the local admin login |
| `natalia` | `happybabytracker` | a new parent: the admin creates her and sends an invite she redeems by SSO |

The admin creates them in lldap's UI during P1, each with a unique email; nothing about them goes into the repo. `david`,
`david_admin` and `david_claude` use separate browser profiles (decision 24).

## 8. Lifecycles (`sso.md` carries the steps)
- **Add a person:** create them in lldap's UI (username, display name, email, groups incl. `sso_people`); they use "Forgot
  password", enrol a first second factor, and link in each app (the app in `both` for the window where a local login is
  needed); the next deploy copies their `sub` into lldap.
- **Password reset:** self-service; the link lasts 1 h; login still needs the second factor.
- **Lost every device:** the admin deletes the registrations (`authelia storage user totp delete`, `… webauthn delete`) and
  re-enrolment happens with the admin present (once no device is registered, the emailed code alone would suffice).
- **Offboard:** remove `sso_people` and the app groups in lldap's UI (no login, no reset); in each app disable the user
  (ends sessions) and refuse that `(iss, sub)`; webhooks-mcp has no app account (decision 16 as amended), so its access ends
  within 5 min of the `sso_people` removal (§ 4.11); clear `sso-sub` and leave the Authelia identifier (4.39.28 has no
  delete; harmless, § 4.7; owner, 2026-11-27).
  S24 shows whether a removed person's refresh token fails; if not, the app-side refusal is what stops them.
  happybabytracker's disable (and an admin unlink) also deletes the parent's push subscriptions and ntfy topic; until the
  admin does it, medication reminders keep reaching the person's devices (owner, 2026-11-24).
- **Authelia DB lost:** restore from the edge backup before anyone logs in. Without a backup, the `sso-sub` attributes are
  re-applied before serving and links survive, but every account is briefly recoverable through its mailbox alone:
  re-enrol everyone with an admin present (an accepted, recorded residual).
- **lldap DB lost:** restore from the edge backup; without one, the admin recreates each person by hand (same username),
  the next deploy copies each `sub` back from Authelia, and each person resets their password. If both databases are lost
  without backups, people relink in every app (`sso.md` § 7), each app's unlink first (happybabytracker's admin:
  `admin unlink-sso`).

## 9. Notifier
Authelia emails reset links and enrolment codes through an **SMTP submission listener inside commsgun-api**: STARTTLS with a
certificate from an edge-local CA that Authelia trusts (`certificates_directory`, `tls.server_name`), on the `authelia-smtp`
network only, authenticated as commsgun app `authelia`,
`disable_startup_check: true`. Its messages expire with the reset link (1 h). commsgun-api binds each listener to one
network's fixed address (owner, 2026-11-24): enqueue and admin on `commsgun`, SMTP on `authelia-smtp`, so Authelia can
reach the SMTP port alone; Gatus (host networking) probes that address directly, with no loopback publish. Until P5 ships, the **file notifier** runs and an admin relays links and codes. commsgun down → no emails;
existing logins unaffected. Sender `auth@<domain>` (a `commsgun_senders` entry); `authelia` limits 5 a minute, 30 a day
(owner, 2026-11-23).

## 10. Phases and pass conditions (Rule 6)

| Phase | Gated on | Pass condition |
|---|---|---|
| P0 spike | — | Every S-item except S1, S5, S7, S15 and S18 (moved to P1) recorded here with observed output, under the § 11 guardrails. **Done 2026-11-27** (§ 11 P0 record) |
| P1 edge infra | P0 | Memory check (§ 6); `ci` green incl. routing cases (§ 4.3) and config validation; deploy; verify; the P1 Gatus probes (§ 4.9) green; backup + drill include `authelia-data`, `lldap-data`, `commsgun-data`; a person created in lldap's UI resets their password and logs in, and the next deploy copies their `sub` into lldap; a `sub` survives an Authelia DB wipe and redeploy (restored from lldap), and a mismatched one fails the deploy; S1, S5, S7, S15 and S18 recorded against the deployed portal |
| P2 xstockstrat-staging | 345 amended + shipped; people created in lldap (P1); before `both`, 345's design § 6 gates: the § 4.3 confirm zone, S17 (fresh `auth_time`), the web client's scopes and claims policy (§ 4.6), the seed row's password | In `both`: `david` signs in with password, links, then by SSO; with Authelia stopped, password login still works. Then `oidc`: the Claude.ai connector connects, lists tools, and still work after > 2 h idle; an unlinked SSO user is refused on web and MCP; switching to `local` without a login rehearsed |
| P3 xstockstrat prod | P2 | Same checks on prod |
| P4 webhooks-mcp | C1/C4 amended + shipped | In `static+oauth` both kinds of client work; then `oauth`: unauthenticated `POST /mcp` → 401 with `resource_metadata`; wrong audience → 401; a token without the `webhooks-mcp` scope → 403; a person removed from `sso_people` is refused within 5 min (§ 4.11) |
| P5 commsgun | change shipped; threat-model pass (commsgun § 13 accepted by the owner, 2026-11-27); § 4.3 login zones deployed | S16 against the real listener; the P5 Gatus probes green; a reset email arrives through the SMTP listener over STARTTLS; SSO login completes; a person outside `commsgun` refused; in `both` with Authelia stopped the local login works; disabling a user ends their session; old private name 404 |
| P6 happybabytracker | change shipped; threat-model pass (happybabytracker § 18 accepted by the owner, 2026-11-27); § 4.3 login zones deployed | the P6 Gatus probes green; `natalia` redeems an invite by SSO and lands on her kids; `david` links his parent row; `david_admin` reaches the admin area by SSO; in `both` with Authelia stopped the local admin login works; an outsider is refused |

## 11. Spike items
- **S1** `resource` parameter accepted. **S2** JWT access tokens with scopes (`scp` array on v4.39.28, 345 spike (b)) and `aud`. **S3** custom scopes. **S4** group re-check on
  refresh. Already observed on v4.39.28 (xstockstrat 345 spike (d), D-98): refresh still succeeds after removal from the
  group; P0 re-checks it with lldap. Consequence for webhooks-mcp: § 4.11. **S5** pre-registered Claude.ai clients.
- **S7** regulation keys on Caddy's single overwritten XFF entry. **S8** Authelia RSS idle and under N parallel logins.
- **S10** issuer follows `Host`. **S12** compose recreates on `configs.content` change. **S15** back-channel request rate
  per VM over a day. **S16** (P5, against commsgun's real listener; P0 checks only Authelia's side against a stand-in)
  STARTTLS submission with the edge-local CA, `AUTH PLAIN`, header `From` matching envelope `MAIL FROM`, and a
  `Message-ID` from Authelia's notifier (commsgun spec 12.8).
- **S17** `prompt=login` with `max_age=1` forces re-authentication, and the new ID token carries a fresh `auth_time`,
  `email` and `preferred_username` (the openid web client, § 4.6). **S18** an edge container reaches Caddy via `host-gateway` and gets the
  public issuer. **S19** `storage user identifiers add` on an existing identifier (repeat-safe, mismatch detectable).
  **S20** the pinned image's config-validation command.
- **S21** Authelia password reset against the pinned lldap rootless release. **S22** the deploy's lldap step (groups and the
  `sso-sub` attribute) is idempotent and never touches people, memberships or passwords (amended 2026-11-27, owner: except
  the bind account it creates and owns, § 4.10); the `sub` reconcile handles all
  three cases. **S23** lldap RSS under N parallel binds. **S24** refresh fails once a person leaves `sso_people` or their
  group. **S25** regulation bans by normalised username (case and email variants share one counter).
- **S26** a container on two compose networks with per-address listener binds: the `127.0.0.1` publish of one port still
  reaches the listener bound to that network's address, and a host-network probe reaches the other (commsgun, § 9). Runs
  in P0 with a generic two-network container. Fallback if the publish misses: drop it, and point Caddy's
  `reverse_proxy` and Gatus at the fixed `commsgun` address, as the SMTP probe already does.

- **S27** (§ 4.11) Authelia's bearer authz with a PAR + `form_post` + explicit-consent public client, for a JWT and an
  opaque access token: allowed at first, refused right after a group or `sso_people` removal, allowed again after re-adding,
  refused after revocation; introspection's `active` alongside. Its Claude.ai side (can the connector complete PAR,
  `form_post` and explicit consent) was to run with S5 in P1; no longer needed, A was rejected (§ 4.11).
- **S26b** pinning a subnet on an existing compose network with the pinned Compose, non-interactively: recreated in place,
  or refused (then a new network name and a one-off removal of the old one).

(S6, S9, S11, S13 and S14 were retired: S9 and the client-credentials check with the unused clients, 2026-11-27.)

**P0 record (2026-11-27, edge droplet, Compose 5.5.1, Authelia 4.39.28, lldap v0.6.3 rootless, mailpit v1.33.1).**

| Item | Observed | Consequence |
|---|---|---|
| S20 | `validate-config` exit 0, one warning: custom scope `xstockstrat.read` outside the standard scopes | § 4.8: CI passes on exit 0 with warnings |
| S22 | groups created once, `changes: none` on the second run, people and memberships unchanged; `sso_sub` refused ("Valid characters: a-z, A-Z, 0-9, and dash") | the attribute is `sso-sub` (§ 4.7) |
| S10 | `auth.spike.test`: issuer and JWKS on the same origin, no `end_session_endpoint`; `auth2.spike.test` (same cookie domain and certificate): `400 Bad Request` | one portal name (decision 3); `Host` passed through (§ 4.2) |
| S12 | container ID and start time unchanged after a `configs.content` edit; the container still saw `probe-v1` | decision 20 and § 4.8: A |
| S26 | publish reaches the net_a-bound listener; host reaches net_b's address; a net_b-only peer reaches net_b's listener, is refused on net_a's port at net_b's address, cannot reach net_a | § 9 as designed; no fallback |
| S26b | unpinned network got `172.22.0.0/16`; pinning `172.31.252.0/28` and `up -d </dev/null` recreated it in place, exit 0, container recreated | P5 recreates `commsgun` in place (§ 5) |
| S21 + S16 (Authelia side) | reset mail from `auth@` over STARTTLS with the spike CA; new password set through lldap; login 200 | full S16 in P5 |
| S2 / S3 | access token `RS256`, `typ` `at+jwt`, `kid`; `scp` array; `aud` from `requested_audience_mode: implicit`; 3600 s; refresh token; `iss` in the authorization response; custom scope granted; `offline_access` forces the consent page | webhooks-mcp reads `scp`; S5 expects a consent step |
| S17 | ID token has `auth_time`, `email`, `preferred_username`; `prompt=login` → `require_login: true`, no code without re-authentication; after `/api/firstfactor/reauthenticate`: `max_age=0` still refused, `max_age=1` and `prompt=login` alone issue a code with a fresh `auth_time` | § 3: links send `max_age=1` |
| S4 / S24 | refresh 200 after leaving the app group; 500 (`The error is unrecognizable`) after leaving `sso_people` | § 4.11 C holds; a removed connector sees a server error, not `invalid_grant` |
| S25 | `Alice`/`ALICE`/`alice` share one counter; ban listed as `alice` for 5 min; `bans user revoke` works | `sso.md` § 4 unban as written |
| S27 | JWT access token: authz 401 always; opaque: 200, 403 at once after the group removal, 401 at once after `sso_people`, 401 after revocation, ~185 ms per check; introspection `active` stays true until revocation | § 4.11: A rejected, C |
| S19 | first add exit 0; the same pair again: exit 1 (`UNIQUE … identifier`); another UUID for the same user: exit 1 (`UNIQUE … username`), nothing overwritten; export lists every pinned `sub` | § 4.7: read the export first |
| S8 / S23 | Authelia 60 → 68 MiB, lldap 3 → 54 MiB under 20 parallel logins (6.2 s, all 200) | § 6: 128m each (owner) |
| Guardrails | `guard`: no production restart (before and after teardown); `down -v`: 0 containers, 0 volumes left; available memory 1221 → 1200 MiB | — |

**Where the spike runs (owner, 2026-11-23): the edge droplet itself.** It is production, so:
- a separate compose project (`authelia-spike`) with its own networks and volumes, ports bound to `127.0.0.1` only, test
  secrets only (never SOPS), and no change to Caddy, the cloud firewall, `compose.yml` or any running service: the public
  surface stays 80/443/3478 (Rule 2);
- `free -m` before starting (≥ 400 MiB free with the spike's limits) and `docker stats --no-stream` during it; stop at once if
  any production container restarts or Gatus alerts;
- items that need the real public name or Caddy (S1, S5 with Claude.ai, S7, S15, S18) move to P1 and run against the
  deployed `auth.public`, before any app is wired;
- `docker compose -p authelia-spike down -v` at the end, and its output recorded here.

## 12. Open

- The admin creates the four people in lldap's UI during P1.

## 13. Handoff: work per repo

| Repo | Work | Must honour |
|---|---|---|
| homelab-edge | P0 spike; P1 (§ 4, § 6); per-app edge wiring (§ 5); `sso.md`, `recovery.md`, `alerts.md`, `onboard-service.md`, `secrets/README.md`, `versions.md`; `commsgun.md` and `happybabytracker.md` change **before the bump PR that brings each app's first SSO-capable image is merged**: that image imports the env admin on first start, so SOPS rotation of the admin password stops working and moves to `admin reset-password`, commsgun's session-secret rotation (`commsgun.md`) no longer signs everyone out, and rotating happybabytracker's `INTERNAL_API_TOKEN` no longer ends admin sessions (`happybabytracker.md` § 4 step 3; the admin fingerprint is gone) | Rules 1–8, EDGE-01…14 |
| xstockstrat | Amend 345 per § 5, re-run `/sdd-review`, then `/sdd-execute` | 345 `design.md`, `debate.md` (D-6, D-73, D-74, D-96, D-97), `merge-order.md` |
| webhooks-mcp | § 5 row; spec § 6 amendment | `docs/contracts.md` C1, C4, C8; WM-08 |
| commsgun | § 3 contract, § 5 row (incl. the SMTP listener) | K1–K7, U1–U4, CG-01…09 |
| happybabytracker | § 3 contract, § 5 row | spec §5.1, §5.4, §13, §15; its `CLAUDE.md` Rule 1 |

## 14. Sources
[Authelia elevated session](https://www.authelia.com/configuration/identity-validation/elevated-session/) ·
[lldap integration](https://www.authelia.com/integration/ldap/lldap/) ·
[SMTP notifier](https://www.authelia.com/configuration/notifications/smtp/) ·
[OIDC clients](https://www.authelia.com/configuration/identity-providers/openid-connect/clients/) ·
[OIDC provider](https://www.authelia.com/configuration/identity-providers/openid-connect/provider/) ·
[`storage` CLI](https://www.authelia.com/reference/cli/authelia/authelia_storage/) ·
[NTP](https://www.authelia.com/configuration/miscellaneous/ntp/) ·
[regulation](https://www.authelia.com/configuration/security/regulation/) ·
[passwords guide](https://www.authelia.com/reference/guides/passwords) ·
[lldap#750](https://github.com/lldap/lldap/issues/750) ·
[lldap Docker Hub](https://hub.docker.com/r/nitnelave/lldap) ·
[Anthropic connector callbacks](https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers)
