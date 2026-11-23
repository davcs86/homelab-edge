# Authelia SSO — re-spec (2026-11-23)

Status: **design approved by the owner, not deployed.** It re-specs the 2026-11-15 design against `main` at `c06667e` and
the app repos at xstockstrat `6ce536f`, webhooks-mcp `c9be704`, commsgun `017db3e`, happybabytracker `d3820ab`. Summaries
live in `docs/decisions.md` § "SSO is Authelia" and `docs/runbooks/sso.md`; this file is the full record. Open: § 12.

## 1. Decisions

| # | Decision |
|---|---|
| 1 | Authelia **v4.39.28** (latest, 2026-09-17), digest-pinned. **lldap** as the user store (LDAP backend), SQLite storage, in-memory sessions (no Redis), `default_policy: deny`, `two_factor` everywhere |
| 2 | Scope: **homelab only**. xstockstrat prod and staging (web + MCP), webhooks-mcp, the commsgun dashboard, happybabytracker. DigitalOcean deployments stay on local auth. Gatus and Proxmox later; headscale never |
| 3 | Portal **`auth.public.<domain>`** under the existing `*.public` wildcard (Rule 8). No `auth.private`: v4.39 derives the issuer from the request host (§ 4.2) |
| 4 | Every app is a **provider-neutral OIDC client** (OSS: forks must not depend on Authelia). webhooks-mcp is an OAuth 2.1 resource server. **No `forward_auth` for our own apps**; allowed only for third-party software without OIDC |
| 5 | Each app has **`AUTH_MODE`**: `local` (default) \| `oidc` \| `both`; webhooks-mcp: `static` \| `oauth`. The homelab runs `both` (local is the break-glass) |
| 6 | **Apps own users and roles** in their database. Authelia only proves identity and gates which app a person may open (one group per app). No group→role mapping |
| 7 | **Explicit linking** everywhere: a signed-in local user presses "Link SSO"; or an invite is redeemed by SSO (happybabytracker). Unlinked SSO logins and MCP tokens are refused. Just-in-time provisioning only behind a flag, default off |
| 8 | **No passwordless accounts in xstockstrat**: 345's SSO-only end state, its SSO-created users with unusable passwords (D-6) and any link-by-token invite are dropped. Every user has a local password; `create-user` then "Link SSO" |
| 9 | Env-seeded superusers are replaced by a **"create if empty" CLI bootstrap** plus a one-time migration importing today's env admin as a real row |
| 10 | Identities keyed on **`(issuer, subject)`**; the issuer hostname and the passkey RP ID (`auth.public.<domain>`) are permanent |
| 11 | VMs reach `auth.public` over the home WAN; edge containers through `host-gateway` (§ 4.2). Rate limits **path-scoped**, no IP exemption (the WAN IP is dynamic) |
| 12 | Regulation `modes: [user]` with a short ban; Caddy per-IP limits on top; a global zone on first factor (§ 4.4) |
| 13 | MCP refresh tokens **30 days**, rotating; explicit session lifetimes (§ 4.5) |
| 14 | **Self-service password reset** by email for everyone: lldap holds passwords (single writer); SOPS says who exists and their groups, never passwords (§ 4.10) |
| 15 | **Second-factor reset is admin-only**: `identity_validation.elevated_session.require_second_factor: true`, so registering another device needs an existing one; losing every device needs an admin |
| 16 | webhooks-mcp authorizes by an app-side **`(iss, sub)` allowlist** plus scope; a **client-credentials** client for headless use (smoke tests, agents) |
| 17 | commsgun's database joins the **edge backup** (it now holds users and identities) |
| 18 | Local break-glass logins accept their POST **from the tailnet only** (Caddy rule; apps stay neutral) |
| 19 | Each person's **`sub` is pinned**: a UUID in SOPS, applied at deploy (§ 4.7) |
| 20 | Config delivery: spike compose `configs:` content (C), fall back to a mounted directory + hash-compare restart (A) (§ 4.8) |
| 23 | Notifier: commsgun's SMTP submission listener (Q21 resolved); file notifier until it ships (§ 9) |
| 21 | Claude.ai **and** Claude Code clients per environment; both Claude.ai callback hosts registered |
| 22 | People: § 7 |

## 2. Findings that drove the changes

| # | Finding | Evidence |
|---|---|---|
| F1 | `auth.<domain>` broke Rule 8; `*.public` already has a wildcard cert | `CLAUDE.md` Rule 8; `Caddyfile.j2` `*.{{ public_zone }}` |
| F2 | commsgun's spec makes internet exposure an anti-goal; single fixed `admin`, stateless HMAC cookie (U4) with no principal | commsgun `docs/superpowers/specs/2026-11-13-commsgun-design.md:48,56`; `ui/src/lib/server/session.ts:19` |
| F3 | webhooks-mcp: static `MCP_AUTH_KEY` (`hmac.compare_digest`); MCP SDK 2.3.0 has `AuthSettings`/`TokenVerifier`/PRM routes, unused; no PRM route; single owner, no user model | webhooks-mcp `src/webhooks_mcp/mcp_app.py:176-197`; `docs/contracts.md` C1, C4 |
| F4 | happybabytracker: env-only admin, invited parents, `identities(parent_id, provider, subject)` seam; admin structurally not a parent (`kid_access` FK to `parents`); internet exposure an anti-goal | its spec `2026-11-20-happybabytracker-design.md` §5.1, §5.4 (`:137-148,184-193`), `:54,69` |
| F5 | xstockstrat 345 is `implementation-ready`, 0/52 steps done; links by email and auto-creates `{trader}`; migration `053` stale (next free `056`); no Claude Code client | `345-authelia-sso/status.md`, `product-spec.md:54-67,364`, `merge-order.md:274` |
| F6 | Authelia v4.39 derives issuer and discovery URLs from the request host; no fixed-issuer setting | [authelia#12250](https://github.com/authelia/authelia/discussions/12250), OIDC roadmap |
| F7 | 345 needs the cookie domain to cover each `APP_URL` → `public.<domain>`; the cookie reaches every `*.public` upstream | `345-authelia-sso/design.md:373-375` |
| F8 | The edge never hairpins through its reserved IP; edge app containers are on bridge networks | `docs/decisions.md:10`; `compose.yml.j2:111,138` |
| F9 | The file backend writes reset passwords into the users file | [Authelia passwords guide](https://www.authelia.com/reference/guides/passwords) |
| F10 | argon2id defaults to 64 MiB per check; refresh tokens default to 90 min; no back-channel/RP-initiated logout in 4.39; access tokens opaque unless `access_token_signed_response_alg` is set | Authelia docs: [file backend](https://www.authelia.com/configuration/first-factor/file/), [provider](https://www.authelia.com/configuration/identity-providers/openid-connect/provider/), [OIDC FAQ](https://www.authelia.com/integration/openid-connect/frequently-asked-questions/) |
| F11 | The edge backup covers headscale and happybabytracker only | `roles/backup/templates/edge-backup.sh.j2:3` |
| F12 | App digests reach the edge through bot PRs that change only the digest | `.github/workflows/bump-{commsgun,happybabytracker,webhooks-mcp}.yml` |
| F13 | Claude.ai asks for both `claude.ai` and `claude.com` callbacks to be allowlisted | [Anthropic connector docs](https://support.anthropic.com/en/articles/11503834-building-custom-connectors-via-remote-mcp-servers) |

## 3. The app auth contract (all four repos)

Same names and behaviour in every app; stacks differ (Go + SvelteKit, Python).

- **Config:** `AUTH_MODE`, `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`, `OIDC_SCOPES`, `OIDC_AUTO_PROVISION`
  (default `false`). xstockstrat keeps its config-service keys (`ui/oidc.*`, `agent/oidc.issuer`) and gains an `auth.mode`
  key in place of 345's issuer-presence switch and `password_login_enabled`.
- **Modes:** `local` = password login (xstockstrat MCP: its built-in OAuth server); `oidc` = SSO only (MCP: the external
  issuer); `both` = SSO and password on one login page (MCP: the external issuer). Default `local`, so a fork runs with no IdP.
- **Data:** a users (or principals) table with a role; `identities(user_id, issuer, subject)` unique on `(issuer, subject)`;
  local passwords live with the user, not as an identity row. happybabytracker keeps its admin in a separate principal table
  with its own identities, preserving "the admin is never a parent".
- **Relying party:** the UI server does the code flow (PKCE, `state` and `nonce` bound to the browser session, an OIDC
  transaction cookie `SameSite=Lax`), validates the ID token, then calls the API's internal route (for example
  `POST /v1/session/oidc {iss, sub, email, name}`); the API alone resolves the identity and issues the session.
- **Linking:** "Link SSO" is available only to a signed-in local user, re-authenticates locally first, sends
  `prompt=login` (S17), and shows "you are linking <IdP user> to <account>" before binding. happybabytracker invites may be
  redeemed by password **or** by SSO, which binds the `sub`. Unlinked logins: refused with `not linked`, unless
  `OIDC_AUTO_PROVISION`.
- **Bootstrap and break-glass:** `<app> admin create <username>` (create if no admin exists) and
  `<app> admin reset-password <username>`, run as `docker compose run --rm` (EDGE-03 shape). A migration imports today's env
  admin once; afterwards the env password is ignored with a warning, and its SOPS key is retired in the same edge change.
- **Sessions:** server-side or versioned, so disabling a user or removing a role ends their sessions (commsgun amends U4).
- **Upgrade safety (expand/contract):** a new image keeps today's behaviour by default (`AUTH_MODE=local`, the env admin
  still accepted) until the edge sets the new variables; the admin import is idempotent; migrations are forward-only, so a
  digest is never reverted past one.
- **Conformance:** one shared list of OIDC test cases (bad `state`, wrong `iss`/`aud`, expired token, unlinked `sub`,
  replayed code, link-while-other-IdP-session) run in commsgun and happybabytracker.

## 4. Edge design

### 4.1 Authelia service
- `authelia_public_hostname: "auth.{{ public_zone }}"`; a dedicated `@authelia host` handle in `*.{{ public_zone }}` before
  the `edge_sites` loop; `reverse_proxy 127.0.0.1:{{ authelia_port }}`; no `trusted_proxies` (EDGE-02; S7).
- Compose: `<<: *hardening`, `127.0.0.1:{{ authelia_port }}:9091`, named volume `authelia-data`, uid 10001, `read_only`,
  `mem_limit` from S8 (EDGE-05). Digest in `versions.yml` + `docs/versions.md` row (Rule 4, EDGE-01).
- argon2id parameters set explicitly (≤ 32 MiB, `parallelism: 1`) for the hashes and `password_policy`; S8 runs N parallel
  logins against the limit.
- Password reset on (decision 14); `identity_validation.elevated_session.require_second_factor: true` (decision 15).

### 4.2 Back-channel paths
- **VMs** (xstockstrat UI and agent): `auth.public.<domain>` over the home WAN; the VMs already hold `tag:edge:443`.
- **Edge containers** (commsgun-ui, happybabytracker-ui, webhooks-mcp): `extra_hosts: ["auth.public.<domain>:host-gateway"]`,
  so they reach Caddy (host networking) with the right name and certificate and the issuer matches (S18).
- Never a second name: a different `Host` is a different issuer (F6).

### 4.3 Caddy
- Path-scoped zones (EDGE-12 prefixes): `authelia_login` (`POST /api/firstfactor`, `/api/secondfactor/*`, `/api/reset-password/*`; per IP, tight);
  `authelia_login_global` (first factor, keyed on a constant, caps concurrent hashing across IPs); `authelia_backchannel`
  (discovery, `/jwks.json`, `/api/oidc/token`, `/api/oidc/revocation`; generous, sized by S15); `authelia_general`.
- `authelia_session` (scoped to `public.<domain>`) stripped by `header_up Cookie` before every app upstream.
- Logs on the auth host and the newly public apps: query, `Referer` and `Location` dropped, token path segments
  (`/invite/<token>`, `/reset/<token>`) redacted, as the `*.private` logger does.
- Local break-glass login POSTs (commsgun, happybabytracker, xstockstrat `POST /api/auth/login`) admitted from the tailnet
  only; everything else on those names is public. `caddy-routing-test.sh` pins every rule above.

### 4.4 Regulation
`modes: [user]`, `max_retries: 3`, `find_time: 2m`, `ban_time: 5m` (a nuisance lockout of a known username is accepted;
`ip` mode would ban the whole household's shared WAN IP). Unban: `authelia storage bans` (`sso.md`).

### 4.5 Lifetimes
Access token 1 h; MCP clients' refresh token 30 days, rotated on use (a custom `lifespans` entry); authorization code
default (≥ 30 s, 345 D-73); portal session `expiration` 1 h, `inactivity` 15 min, `remember_me` 30 days. P2/P4 check that a
connector still works after > 2 h idle.

### 4.6 Clients
`access_token_signed_response_alg: RS256` and an explicit `audience` on every MCP client.

| Client | Type | Group | Scopes | Redirect | Audience |
|---|---|---|---|---|---|
| `xstockstrat[-staging]-web` | confidential, `client_secret_basic` + PKCE | `xstockstrat[-staging]` | `openid email` | `${APP_URL}/api/auth/sso/callback` | — |
| `xstockstrat[-staging]-mcp-claude` | public PKCE | same | `xstockstrat.*`, `offline_access` | `https://claude.ai/api/mcp/auth_callback`, `https://claude.com/api/mcp/auth_callback` | `${APP_URL}/agent` |
| `xstockstrat[-staging]-mcp-claude-ro` | public PKCE | same | `xstockstrat.read`, `offline_access` | same | same |
| `xstockstrat[-staging]-mcp-claude-code[-ro]` | public PKCE | same | as above | loopback (S9) | same |
| `webhooks-mcp-claude`, `webhooks-mcp-claude-code` | public PKCE | `webhooks-mcp` | `webhooks-mcp`, `offline_access` | Claude callbacks / loopback | `https://webhooks-mcp.public.<domain>/mcp` |
| `webhooks-mcp-automation` | confidential, `client_credentials` | — | `webhooks-mcp` | — | same |
| `commsgun` | confidential | `commsgun` | `openid profile email` | `${ORIGIN}/auth/callback` | — |
| `happybabytracker` | confidential | `happybabytracker` | `openid profile email` | `${ORIGIN}/auth/callback` | — |

No `email` claim in access tokens: linking no longer uses it (345 D-96 amended).

### 4.7 Identity, secrets and data
- **Pinned `sub`:** `authelia_users[].sub` (UUID) in SOPS; a deploy task runs `authelia storage user identifiers add <user>
  --identifier <uuid>` when absent (exact flags and idempotence: S19). Restores and rebuilds keep every link.
- **Secrets** (SOPS, `init_secrets.py fill_missing`, EDGE-08, `no_log`): session secret, storage encryption key, OIDC HMAC
  secret, OIDC JWKS RSA key, client secrets (hash in Authelia config). Source of truth for confidential client secrets is the
  edge's SOPS; the xstockstrat values are copied into its config service with `set_config` at cutover (a runbook step).
- **Rotation runbook:** JWKS — add the new key, publish, wait one access-token lifetime, retire the old. Client secrets — a
  short planned outage per client (Authelia holds one secret per client).
- **Backup:** `authelia-data`, `lldap-data` and `commsgun-data` join `edge-backup.sh` in the uid-10001 private-bind-mount shape, with drill
  `integrity_check`s; `scripts/edge-backup-test.py` and `recovery.md` follow. The storage encryption key lives in SOPS.
- **Upgrades:** a backup before every Authelia bump (storage migrates on start); rollback = restore, noted in `docs/versions.md`.

### 4.8 Config delivery
C if S12 shows compose 5.5.1 recreating on a `configs.content` change, else A (directory mount + an unconditional
hash-compare restart task, never a handler). There is no users file (lldap holds people), so no password hash goes
through Compose; `compose.yml` stays free of secrets; Authelia's `{{ env }}` template syntax is escaped from Jinja; CI validates the rendered config with
the pinned image (`authelia validate-config`, exact command S20).

### 4.9 Monitoring
Gatus: `authelia-public` (`/api/health`) and `authelia-discovery` asserting the exact `issuer` and a non-empty JWKS, through
1.1.1.1; the commsgun and happybabytracker probes move to their public names; `verify.yml` cases for the new hosts.

### 4.10 lldap (user store)
- Compose service `lldap` with `<<: *hardening`, named volume `lldap-data` (SQLite), uid 10001, `read_only`, `mem_limit` from
  S23; digest-pinned (Rule 4). LDAP (container port 3890) on the `authelia` compose network only, never published.
- **Admin UI at `lldap.private.<domain>`** (owner, 2026-11-23): `lldap_ui_private_hostname`, its web port (container 17170,
  lldap's default) published as `127.0.0.1:{{ lldap_ui_port }}` (EDGE-05), wired in the four EDGE-07 places (hostname var,
  a `host` matcher under `*.{{ private_zone }}` after `import deny_staging_vm`, the split-horizon `_names`, a Gatus probe,
  which is also lldap's health check). Tailnet-only, so not on the internet; the zone's redacting logger covers it. The UI
  lets a signed-in user change their own password, outside Authelia's password policy: lldap's admin account is for
  admins, and people reset through Authelia.
- Authelia binds as a service account in lldap's `lldap_password_manager` group, which is what lets it reset passwords
  (`implementation: lldap`, bind shape S21).
- **People from SOPS, passwords never:** a deploy step creates or updates each person (username, display name, email,
  groups) and disables removed ones through lldap's API, idempotently (S22). A new person has no password; they set one with
  "Forgot password".
- Secrets (SOPS, `fill_missing`): lldap JWT secret, key seed, admin password, Authelia's bind password.
- Accepted residual: a brand-new account's first password and first second factor both come from emailed codes, so its
  first enrolment is only as strong as the mailbox; an admin can enrol with the person to close it.

## 5. Per app

| App | Edge | App repo |
|---|---|---|
| xstockstrat | Legacy `/agent/oauth/*`, register and login routes stay (DO and `local`); `POST /api/auth/login` tailnet-only on the homelab; VMs back-channel per § 4.2 | 345 amended: `auth.mode` (`local`/`oidc`/`both`); explicit "Link SSO"; no email linking, no SSO-created users, no `LINKED_BY_EMAIL`/`CREATED` (unless an auto-provision flag, default off); unlinked MCP tokens → 403 `not linked`; issuer `https://auth.public.<domain>`; Claude Code clients; D-74 → path-scoped zones; D-96 without access-token `email`; migration renumbered (056 at `6ce536f`) |
| webhooks-mcp | `bearer_mcp` also proxies `/.well-known/oauth-protected-resource/mcp`; `extra_hosts`; Gatus still expects 401 | `AUTH_MODE` `static`/`oauth`; JWKS `TokenVerifier` + `AuthSettings`; `OAUTH_ALLOWED_SUBJECTS` (`iss`+`sub` pairs) + required scope; C1/C4 amended; CI against a stub issuer |
| commsgun | `commsgun.public.<domain>`; EDGE-07 undone for the private name, `ORIGIN` follows (EDGE-11); redaction moves; local POST tailnet-only; `extra_hosts`; `commsgun_senders` + `commsgun_apps` gain `authelia`; Authelia joins the `commsgun` network | § 3 contract; users table + role (`admin`); revocable sessions (U4 amended); anti-goal and single-admin non-goal amended; SMTP submission listener on the compose network only, app `authelia`, its message bodies never stored or shown (they carry reset links and codes); threat-model pass before P5 |
| happybabytracker | `happybaby.public.<domain>`; EDGE-07 undone, `ORIGIN`/`PUBLIC_ORIGIN` follow; token paths redacted; local POST tailnet-only; `extra_hosts` | § 3 contract; admin principal table + identities; invites redeemable by SSO; non-goal (`:54`) and anti-goal (`:69`) amended; threat-model pass before P6 |

## 6. Memory budget
Re-measure on the droplet before P1 (`free -m`, `docker stats --no-stream`): at least 400 MiB must stay free with Authelia's
and lldap's limits added (declared limits are 1024m today on 2 GB).

## 7. People

| Username | Groups | Links to |
|---|---|---|
| `david` | `xstockstrat`, `xstockstrat-staging`, `happybabytracker` | xstockstrat: the seeded admin row in each environment, by "Link SSO" after a password login; the Claude.ai and Claude Code connectors sign in as `david`. happybabytracker: his existing parent row, by "Link SSO" |
| `david_claude` | `webhooks-mcp` | webhooks-mcp's allowlist (its pinned `sub`) |
| `david_admin` | `commsgun`, `happybabytracker` | commsgun's imported admin row and happybabytracker's admin principal, each by "Link SSO" from the local admin login |
| `natalia` | `happybabytracker` | a new parent: the admin creates her and sends an invite she redeems by SSO |

Emails: one unique email each, in the template outside the repo, then SOPS.

## 8. Lifecycles (`sso.md` carries the steps)
- **Add a person:** SOPS entry (username, display name, email, groups, pinned `sub`), deploy (lldap creates them with no
  password), they set a password with "Forgot password", enrol a first second factor, link in each app.
- **Password reset:** self-service by email ("Forgot password"); login still needs the second factor.
- **Lost device / second-factor reset:** admin deletes the registration with the `authelia storage user` TOTP/WebAuthn
  commands (`TODO(verify)` exact names in P0), the person re-enrols (with no remaining device, `require_second_factor` does not apply, so the emailed code suffices).
- **Offboard:** remove from SOPS and deploy (lldap disables them); in each app disable the user (ends sessions); revoke refresh tokens; webhooks-mcp
  drops the `sub` from its allowlist. A test proves a removed person's refresh fails.
- **Authelia DB lost:** restore from the edge backup; if no backup, pinned `sub`s are re-applied at deploy and links survive;
  only second factors are re-enrolled.
- **lldap DB lost:** restore from the edge backup; if no backup, the deploy recreates every person from SOPS without a
  password and each resets theirs.

## 9. Notifier
Authelia emails password-reset links and one-time codes (elevation for enrolment) through an **SMTP submission listener
inside commsgun-api** (compose network only, app `authelia`; Q21 resolved 2026-11-23). Authelia side:
`notifier.smtp.address: submission://commsgun-api:<port>`, plain on the internal network (S16), `disable_startup_check:
true`. Until P5 ships it runs on the **file notifier** and an admin relays codes and reset links. commsgun down → no
emails; existing logins unaffected.

## 10. Phases and pass conditions (Rule 6)

| Phase | Gated on | Pass condition |
|---|---|---|
| P0 spike | — | S1–S24 each recorded here with observed output |
| P1 edge infra | P0 | Memory check (§ 6); `ci` green incl. routing cases (§ 4.3) and config validation; deploy; verify; Gatus `authelia-public` + `authelia-discovery` green; backup + drill include `authelia-data`, `lldap-data` and `commsgun-data`; a person created from SOPS resets their password and logs in; a test person logs in with password + passkey/TOTP; a pinned `sub` survives a DB wipe and redeploy |
| P2 xstockstrat-staging | 345 amended + shipped; emails | `auth.mode=both`; `david` signs in with password, links, then signs in by SSO; Claude.ai and Claude Code connect and list tools, and still work after > 2 h idle; an unlinked SSO user is refused on web and MCP; `auth.mode=local` rollback rehearsed |
| P3 xstockstrat prod | P2 | Same checks on prod |
| P4 webhooks-mcp | C1/C4 amended + shipped | Unauthenticated `POST /mcp` → 401 with `resource_metadata`; both clients connect; wrong audience → 401; a `sub` outside the allowlist → 403; the automation client works headless |
| P5 commsgun | commsgun change shipped; threat-model pass | A reset email sent through the SMTP listener arrives and its body is not kept; SSO login completes; a person outside `commsgun` refused; local login works from the tailnet with Authelia stopped and is refused from the internet; disabling the user ends their session; old private name 404 |
| P6 happybabytracker | change shipped; threat-model pass | `natalia` redeems an invite by SSO and lands on her kids; `david` links his parent row; `david_admin` reaches the admin area by SSO; local admin works from the tailnet only; an outsider is refused |

## 11. Spike items
- **S1–S5** the 2026-11-15 list (`resource` param, JWT access tokens with `scope`, custom scopes, group re-check on refresh,
  pre-registered Claude clients), re-run against the edge shape.
- **S7** regulation keys on Caddy's single overwritten XFF entry. **S8** RSS idle and under N parallel logins.
- **S9** Claude Code loopback redirect: any-port `127.0.0.1` accepted, or a fixed port Claude Code can be told to use.
- **S10** issuer follows `Host`. **S12** compose recreates on `configs.content` change. **S15** back-channel request rate
  per VM over a day. **S16** the SMTP notifier accepts a plain submission to a non-loopback compose host.
- **S17** `prompt=login` forces re-authentication in v4.39. **S18** an edge container reaches Caddy via `host-gateway` with
  the public name and gets the public issuer. **S19** `storage user identifiers add` on an existing identifier is safe to
  repeat. **S20** the config-validation command of the pinned image.
- **S21** Authelia password reset against the pinned lldap release (bind shape, `lldap_password_manager`). **S22** the
  SOPS→lldap people step is idempotent and never touches passwords. **S23** lldap RSS. **S24** `require_second_factor`
  blocks enrolling a second device with the emailed code alone.

## 12. Open

Nothing. Q21 resolved 2026-11-23: keep commsgun's SMTP listener, with `require_second_factor` (decision 15).

## 13. Handoff: work per repo

| Repo | Work | Must honour |
|---|---|---|
| homelab-edge | P0 spike; P1 (§ 4, § 6); per-app edge wiring (§ 5); `sso.md`, `recovery.md`, `alerts.md`, `onboard-service.md`, `secrets/README.md`, `versions.md` | Rules 1–8, EDGE-01…14 |
| xstockstrat | Amend 345 per § 5 row, re-run `/sdd-review`, then `/sdd-execute` | 345 `design.md`, `debate.md` (D-6, D-73, D-74, D-96, D-97), `merge-order.md` |
| webhooks-mcp | § 5 row; spec § 6 amendment | `docs/contracts.md` C1, C4, C8; WM-08 |
| commsgun | § 3 contract, § 5 row (incl. the SMTP listener) | K1–K6, U1–U4, CG-01…09 |
| happybabytracker | § 3 contract, § 5 row | spec §5.1, §5.4, §13, §15; its `CLAUDE.md` Rule 1 |
