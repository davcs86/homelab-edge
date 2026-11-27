# Authelia SSO P1 (edge infra) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Authelia at `auth.public.<domain>` and lldap at `lldap.private.<domain>` on the edge, with every § 4.6 client registered, people kept in lldap, each `sub` pinned in two places, both databases (and commsgun's) in the twice-daily backup, Gatus and verify covering them, and no app wired yet.

**Architecture:** Two compose services on their own network `authelia` (LDAP 3890 never published), each `127.0.0.1`-published for Caddy. Authelia's configuration is a rendered file in a mounted directory, with no plaintext secrets in it, plus an `env_file` that holds the secrets for its `{{ env }}` templates; a hash-compare task restarts it when the file changes. A stdlib-plus-PyYAML script (`scripts/sso_sync.py`), run on the edge by the stack role before the stack starts, makes sure the lldap groups, the `sso-sub` attribute and the bind account exist, then reconciles each `sub` between Authelia's storage and lldap.

**Tech Stack:** Ansible (Jinja2), docker compose 5.5.1, Caddy 2.11.9 + caddy-ratelimit, Authelia 4.39.28, lldap v0.6.3 rootless, Gatus, SOPS, GitHub Actions, Python 3.13 / bash test scripts.

**Spec:** `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md` (§ 1, § 3 edge parts, § 4.1–4.11, § 6, § 10 P1, § 11 P0 record), plus `docs/runbooks/sso.md`. Evidence for config keys: the P0 spike kit (`homelab-edge-spike/spike_render.py`, `spike.sh`, `spike_driver.py`), which validated and ran on the pinned digests with `one_factor` policies and no hardening; production is `two_factor` and hardened.

## Global Constraints

- `CLAUDE.md` binds every task: list assumptions; minimum change; touch only what the task needs; comments state constraints only, ≤ 2 lines. Rule 1: no value the owner has not stated (see **Open questions**). Rule 2: public surface stays 80/tcp, 443/tcp, 3478/udp. Rule 3: no secret in the repo, a log or a commit message; every task that handles one sets `no_log: true`. Rule 4: pins in `versions.yml` with their `docs/versions.md` row in the same change. Rule 5: Ansible only. Rule 6: success is the check's output. Rule 8: `auth` under `*.public`, `lldap` under `*.private`.
- Edge seams: EDGE-01 (copy an existing image line's shape: double quotes, no trailing comment), EDGE-02 (no `trusted_proxies`, no `header_up X-Forwarded-*` anywhere), EDGE-03 (restart only via `docker compose -f {{ edge_root }}/compose.yml <verb> <svc>` with `changed_when: true`), EDGE-04 (env-file changes get no `notify:`), EDGE-05 (`<<: *hardening`, `127.0.0.1:{{ <x>_port }}:<literal container port>`, named volume, uid 10001, `read_only`), EDGE-07 (four places for `lldap.private`), EDGE-08 (secret keys as literals or registry `key:` loops), EDGE-12 (zone names `authelia_login`, `authelia_backchannel`, `authelia_general`).
- Pins (verbatim): `authelia_image: "authelia/authelia:4.39.28@sha256:bd97cff4fcbf715b5ff1f9ae286afbe6033afce385302520b0368122d43a6f54"`, `lldap_image: "lldap/lldap:v0.6.3-alpine-rootless@sha256:ba2c50930ea998eefd5454aa678a7977448019248b1827da87d330df0b71c284"`.
- Services (§ 4.1, § 4.10, § 6): `authelia` and `lldap`, each `user: "10001:10001"`, `read_only: true`, `mem_limit: 128m`, volumes `authelia-data:/data`, `lldap-data:/data`; Authelia container port 9091, lldap UI 17170, LDAP 3890 on network `authelia` only; the `authelia` network keeps outbound access (startup NTP check); `authelia` `depends_on` lldap.
- Authelia (§ 1, § 4.1, § 4.4, § 4.5): `default_policy: deny`, `two_factor` in every policy; `implementation: lldap`; `users_filter` requires `sso_people` and matches `uid` only; password reset on, `identity_validation.reset_password.jwt_lifespan: 1h`, `identity_validation.elevated_session.require_second_factor: true`; regulation `modes: [user]`, `max_retries: 3`, `find_time: 2m`, `ban_time: 5m`; access token 1h; MCP refresh token 30 days (`720h`), rotating; authorization code default; session `expiration: 1h`, `inactivity: 15m`, `remember_me: 720h`, cookie domain `public.<domain>`, cookie `authelia_session`; SQLite storage, in-memory sessions; file notifier until P5.
- Clients (§ 4.6), exactly nine: `xstockstrat-web`, `xstockstrat-staging-web`, `commsgun`, `happybabytracker` (confidential, `client_secret_basic` + PKCE, `openid profile email`, claims policy `id_token: [email, preferred_username]`); `xstockstrat-mcp-claude`, `xstockstrat-staging-mcp-claude` (public PKCE, `xstockstrat.read xstockstrat.write xstockstrat.admin xstockstrat.trade offline_access`; scope set from xstockstrat 345 `product-spec.md:99-102`), `xstockstrat-mcp-claude-ro`, `xstockstrat-staging-mcp-claude-ro` (`xstockstrat.read offline_access`), `webhooks-mcp-claude` (`webhooks-mcp offline_access`, access tokens 5 min); every MCP client `access_token_signed_response_alg: RS256`, explicit `audience`, `requested_audience_mode: implicit`, redirects `https://claude.ai/api/mcp/auth_callback` and `https://claude.com/api/mcp/auth_callback`. No Claude Code, loopback or client-credentials client.
- Identity (§ 4.7, § 4.10): lldap attribute `sso-sub` (dash, not underscore: S22); the deploy only ensures the groups and the attribute (and the bind account, Open question 3), never a person or a membership; `identifiers add` exits 1 on any existing row, so the reconcile reads `authelia storage user identifiers export` first (S19); both present and different fails the deploy; lldap up first; `key_seed` never rotates.
- Config delivery (§ 4.8): option A, a directory mount plus an unconditional hash-compare restart task, never a handler; `compose.yml` holds no secret; Authelia's `{{ … }}` template syntax is escaped from Jinja; CI runs `validate-config` with the pinned image and passes on exit 0 with warnings (S20).
- Caddy (§ 4.1–4.3): a dedicated `@authelia host` handle in `*.{{ public_zone }}` before the `edge_sites` loop; `reverse_proxy 127.0.0.1:{{ authelia_port }}`; `Host` passed through unchanged (S10: any other host gets 400); `authelia_session` removed by `header_up Cookie` before every app upstream; the auth host's logs drop query, `Referer` and `Location`.
- Backup (§ 4.7, decision 17): `authelia-data`, `lldap-data`, `commsgun-data` join `edge-backup.sh` in the uid-10001 private-bind-mount shape with drill `integrity_check`s; a deploy runs the backup before changing the Authelia or lldap digest.
- Memory (§ 6): ≥ 400 MiB available after adding both 128m limits, re-measured on the droplet before deploy.
- Tooling: Docker, `sudo` and `sops` may be missing where the plan is executed (the authoring container had a Docker CLI but no daemon). `caddy-routing-test.sh`, the new `sso` CI job and the real `init-secrets` run in CI only; push and read the job logs for what cannot run locally. Commits end with the session's attribution trailers; one PR carries this plan and its implementation.

## Open questions for the owner (Task 0 records the answers; tasks below are written to the proposals)

| # | Question | Proposal | Used in |
|---|---|---|---|
| 1 | Host ports (EDGE-05: `*_port` are host ports) | `authelia_port: 9091`, `lldap_ui_port: 17170` (the container ports; nothing in the repo binds either; Task 0 checks the droplet) | Tasks 2–6 |
| 2 | lldap base DN (spec silent; renaming later rewrites every DN Authelia uses) | `lldap_base_dn: "dc=natanddavehouse,dc=com"` | Tasks 4, 5 |
| 3 | Authelia's bind account (spec: a service account in `lldap_password_manager`, password in SOPS; not who creates it) | the deploy creates it if missing (`authelia_ldap_bind_user: authelia`, email `authelia@ldap.invalid`, never deliverable), adds it to `lldap_password_manager` only, refuses to continue if it is in any `sso_groups` entry, and sets its password from SOPS on every deploy. It is the only user the deploy writes | Task 6 |
| 4 | `default_policy: deny` with no `access_control` rules (OIDC uses per-client policies; the spike always had rules) | no rules. If the pinned image's `validate-config` refuses that (Task 4 shows it), add one rule: `domain: '*.{{ public_zone }}'`, `policy: two_factor`, `subject: 'group:sso_people'` | Task 4 |
| 5 | Zone values and paths (spec: "tight", "generous") | `ratelimit.authelia_login: {events: 10, window: 1m}` on `POST` to `/api/firstfactor`, `/api/firstfactor/*` (re-authentication takes a password: S17 used `/api/firstfactor/reauthenticate`), `/api/secondfactor/*`, `/api/reset-password/*`; `ratelimit.authelia_backchannel: {events: 600, window: 1m}` on `/.well-known/openid-configuration`, `/.well-known/oauth-authorization-server`, `/jwks.json`, `/api/oidc/token`, `/api/oidc/revocation`; `authelia_general` reuses `ratelimit.general`. Owner: name any other password-taking endpoint of 4.39.28 that should join the login zone | Task 3 |
| 6 | Redacting the auth host's log (Caddy picks a per-host logger by exact lowercase name, `caddy-routing-test.sh` "mixed-case host"; a `?token=` reset link on `AUTH.public…` would reach the `public` log) | the whole `*.public` site logs to a named `public_zone` logger that drops query, `Referer` and `Location`, as `*.private` does (file stays `public.access.log`); its error logger joins the redacted set. Cost: xstockstrat and webhooks-mcp access logs lose their query strings (today only OAuth parameters are deleted) | Task 3 |
| 7 | Reconcile versus the running Authelia | the reconcile's `compose run --rm` runs beside a live Authelia (S19 ran against a live one); only when the Authelia digest changes does the deploy back up, then stop Authelia, so the storage migration never runs under an older binary. A fresh volume (rebuild, wipe) has no container yet, so nothing serves before the reconcile | Task 6 |
| 8 | `consent_mode` (spec silent) | `implicit` on every client, as the spike ran it; `offline_access` still shows the consent page (S2/S3), which S5 expects | Task 4 |
| 9 | "`authelia-discovery` (exact issuer, non-empty JWKS)" needs two URLs | `authelia-discovery` on `/.well-known/openid-configuration` (`[BODY].issuer`, `[BODY].jwks_uri`) and `authelia-jwks` on `/jwks.json` (`len([BODY].keys) > 0`) | Task 3 |
| 10 | Redirects of the not-yet-public apps | registered now with the § 5 names: `https://commsgun.{{ public_zone }}/auth/callback`, `https://happybaby.{{ public_zone }}/auth/callback`; P5/P6 swap them to their hostname variables | Task 4 |
| 11 | Secrets beyond § 4.7's list | add `authelia_reset_password_jwt_secret` (the reset flow needs it; the spike's config has it); JWKS key RSA 2048 (as the spike). Defer the SMTP credential, CA, certificate and key to P5 and `SESSION_SEAL_KEY` to P6 (no consumer in P1) | Task 1 |
| 12 | S1, S5, S15 and a first `sub` with no app wired (a `sub` is minted at an OIDC authorization, not at a portal login: assumed, unverified) | S1 + S5: by hand, an authorization request for `xstockstrat-staging-mcp-claude` with `resource=https://xstockstrat-staging.public.<domain>/agent`, PKCE from a local verifier, the code read from the blocked `claude.ai` redirect in the browser's dev tools and exchanged with `curl`; this also mints the check person's `sub`. The connector end to end moves to P2. S15: record a day of the back-channel zone's counters from Caddy's log for both VMs (expected zero before P2) plus a burst of 600 requests in a minute from each VM without a 429; the real-load measurement repeats in P2 | Task 10 |
| 13 | A throwaway person for the wipe and mismatch checks | `sso-p1-check` (never reused), offboarded at the end; the four § 7 people are created in the same window (§ 12) | Task 10 |
| 14 | `deny_staging_vm` on the auth handle | not imported: the staging VM is an OIDC client of the portal (it arrives from the home WAN anyway, F9); pinned by a routing case | Task 3 |

## Review Focus

1. **Images the spike never ran hardened.** Under `read_only`, `user: "10001:10001"`, `cap_drop: [ALL]` and `no-new-privileges`, a fresh named volume whose `_data` is not owned by 10001, or a write the image makes outside `/data`, stops Authelia or lldap at start. Expected: both reach healthy on a fresh volume and after a restart. Pinned by Task 5's `sso-stack-test.py` hardened start in CI.
2. **A self-editable or wrong-shaped `sso-sub`.** If the attribute is user-editable (or visible, a list, or not a string), a person could set someone else's `sub`, and the next restore from lldap would hand them that identity. Expected: `ensure` creates it `STRING`, single, hidden and not editable, and refuses to continue when an existing one differs; a non-UUID value fails the reconcile. Pinned in Task 6 (`sso-sync-test.py`).
3. **Reconcile inputs that are not the simple case.** Username case between Authelia's export and lldap's ids, identifiers of other services or sectors, users outside `sso_people` with a stale attribute, and a mismatch found after other users would already be written. Expected: matched case-insensitively, `openid` with empty sector only, `sso_people` members only, nothing written when any mismatch exists. Pinned in Task 6.
4. **`authelia_session` reaching an app.** The cookie must be removed in any position (alone, first, middle, last) and when HTTP/2 splits it over several `cookie` fields, and must still reach Authelia itself. Pinned by Task 3's routing cases.
5. **A reset token or code in a log line.** Mixed-case `Host` on the auth name, an upstream 502 (Authelia down) logged by `http.log.error.*` or `http.handlers.reverse_proxy` on stderr, and the `Location` of an authorization redirect. Expected: no query, `Referer` or `Location` value in any access log or stderr line. Pinned by Task 3's canary cases.

## File Structure

| File | Change |
|---|---|
| `scripts/init_secrets.py`, `scripts/init-secrets-test.py`, `secrets/README.md` | generated keys, client secret pairs, pbkdf2 and RSA generators |
| `inventories/prod/group_vars/all/main.yml` | hostnames, ports, base DN, bind user, `sso_groups`, `authelia_oidc_clients`, two `ratelimit` entries |
| `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md` | two pins and rows |
| `roles/tailscale/tasks/main.yml`, `roles/stack/templates/Caddyfile.j2`, `roles/stack/templates/gatus-config.yaml.j2` | `lldap.private` wiring, auth handle, zones, cookie strip, logging, probes |
| `scripts/portal-zones-test.py`, `scripts/gatus-config-test.py`, `scripts/caddy-routing-test.sh` | new cases |
| `roles/stack/templates/authelia-config.yml.j2` (new) | Authelia configuration |
| `roles/stack/templates/compose.yml.j2`, `roles/stack/tasks/main.yml` | services, network, volumes, env files, volume ownership, pre-change backup, sync, hash-compare restart, deploy checks |
| `scripts/sso_sync.py` (new), `scripts/sso-sync-test.py` (new) | lldap ensure and `sub` reconcile |
| `scripts/authelia-config-test.py` (new), `scripts/sso-stack-test.py` (new), `.github/workflows/ci.yml` | config, compose and deploy-order tests; Docker job `sso` |
| `roles/backup/templates/edge-backup.sh.j2`, `edge-backup-verify.sh.j2`, `roles/backup/tasks/main.yml`, `scripts/edge-backup-test.py` | three more databases |
| `.github/workflows/verify.yml` | portal and lldap cases |
| `docs/runbooks/{sso,recovery,alerts,commsgun}.md`, `docs/decisions.md`, `README.md`, the spec's § 11/§ 12 | docs and the P1 record |

---

### Task 0: Owner prerequisites (no repo change)

- [ ] **Step 1:** The owner answers every row of **Open questions**; the implementer records the answers in the PR description and adjusts the tasks before starting.
- [ ] **Step 2:** On the droplet: `free -m`, `docker stats --no-stream`, `ss -ltn '( sport = :9091 or sport = :17170 )'`. Pass: `available` − 256 ≥ 400 MiB, and neither port listens. Paste the output into the PR.

---

### Task 1: Secrets

**Files:** Modify `scripts/init_secrets.py`, `scripts/init-secrets-test.py`, `secrets/README.md`.

**Interfaces — Produces:**
- `GENERATED` gains `authelia_session_secret`, `authelia_storage_encryption_key`, `authelia_reset_password_jwt_secret`, `authelia_oidc_hmac_secret`, `authelia_ldap_bind_password`, `lldap_jwt_secret`, `lldap_key_seed`, `lldap_admin_password` (each `secrets.token_urlsafe(48)`, 64 characters) and `authelia_oidc_jwks_key` (`rsa_private_key_pem()`).
- `OIDC_CLIENT_KEYS = ["authelia_client_secret_xstockstrat_web", "authelia_client_secret_xstockstrat_staging_web", "authelia_client_secret_commsgun", "authelia_client_secret_happybabytracker"]`; each is a pair `<key>` (`token_urlsafe(48)`) + `<key>_pbkdf2` (`pbkdf2_sha512_digest(<key value>)`), created whole by both modes; `fill_missing` refuses a half pair, as it refuses a partial certificate set.
- `pbkdf2_sha512_digest(secret: str, salt: bytes | None = None, iterations: int = 310000) -> str`: `hashlib.pbkdf2_hmac("sha512", …, dklen=64)`, 16 random salt bytes; returns `$pbkdf2-sha512$310000$<salt>$<digest>` in adapted base64 (standard alphabet, `+` → `.`, no padding).
- `rsa_private_key_pem() -> str`: `openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048`, PKCS#8 PEM.

- [ ] **Step 1: Failing tests** in `scripts/init-secrets-test.py`: `test_sso_generated_keys_are_filled_when_missing_and_kept_when_present` (an existing `lldap_key_seed` and `authelia_oidc_jwks_key` are never replaced); `test_sso_secrets_are_64_url_safe_characters`; `test_the_jwks_key_is_a_2048_bit_rsa_pkcs8_pem` (`openssl pkey -noout -text`); `test_the_client_digest_has_authelias_pbkdf2_format_and_rederives` (regex `^\$pbkdf2-sha512\$310000\$[./A-Za-z0-9]{22}\$[./A-Za-z0-9]{86}$`; decoding salt and digest and re-running `pbkdf2_hmac` gives the digest; two calls differ); `test_a_client_pair_is_created_whole_and_a_half_pair_is_refused` (file untouched, message names the key only); `test_no_sso_secret_contains_a_quote_backslash_or_line_break` (the PEM excepted: it reaches the env file base64-encoded). Extend `test_the_full_generator_and_the_fill_mode_agree_on_the_key_set`.
- [ ] **Step 2: Run** `python3 scripts/init-secrets-test.py` (venv with `bcrypt`; sops cases run in CI). Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the interfaces above in `init_secrets.py`; update the module docstring's list.
- [ ] **Step 4: Run** the test again → PASS (sops cases in CI).
- [ ] **Step 5: `secrets/README.md`**: the new keys in the `edge.sops.yaml` row and one paragraph: all generated, none the owner's; `lldap_key_seed` must never rotate (stored passwords depend on it); `lldap_admin_password` applies only at lldap's first start, so a later change is made in lldap's UI and in SOPS together; each client secret's `_pbkdf2` is its hash for Authelia, the plain value goes to the app at its cutover (xstockstrat: copied into its config service); `overwrite` replaces every one of them, so never run it once SSO is live; JWKS rotation and client-secret rotation steps live in `sso.md` (Task 9).
- [ ] **Step 6: Commit** `feat(sso): generate the Authelia and lldap secrets`.

---

### Task 2: `lldap.private` in the four EDGE-07 places

**Files:** Modify `inventories/prod/group_vars/all/main.yml`, `roles/tailscale/tasks/main.yml` (`_names`), `roles/stack/templates/Caddyfile.j2`, `roles/stack/templates/gatus-config.yaml.j2`, `scripts/portal-zones-test.py`, `scripts/gatus-config-test.py`, `scripts/caddy-routing-test.sh`.

**Interfaces — Produces:** `lldap_ui_private_hostname: "lldap.{{ private_zone }}"`, `lldap_ui_port: 17170` (new `# ---------- SSO ----------` section), matcher `@lldap_ui`, probe `lldap-private`, routing mock `lldap:17170`.

- [ ] **Step 1: Failing tests.** `portal-zones-test.py`: `test_zone_and_tenant_names` asserts `lldap.private.<d>`; `test_private_name_gets_a_record_and_a_certificate_probe` asserts `lldap_ui_private_hostname` in `_names`. `gatus-config-test.py`: `test_the_lldap_name_is_probed_over_the_tailnet` (group `edge`, `1m`, url `https://<host>/health`, conditions `["[STATUS] == 200", "[CERTIFICATE_EXPIRATION] > 336h"]`, tailnet resolver, ntfy alert). `caddy-routing-test.sh`: `llpriv_host` in `hosts.j2`; mock `lldap:17170`; cases "lldap UI aborts publicly" `000`, "lldap UI from the tailnet" `200 lldap GET /health`, "staging VM denied on the lldap UI" `000`.
- [ ] **Step 2: Red order:** add the vars, run `python3 scripts/portal-zones-test.py`, see `test_caddy_serves_nothing_under_the_machine_zone` FAIL; add `_names` → PASS.
- [ ] **Step 3: Implement** the Caddy handle after `import deny_staging_vm` (`reverse_proxy 127.0.0.1:{{ lldap_ui_port }}`, nothing else) and the probe beside `commsgun-ui-private`.
- [ ] **Step 4: Run** `python3 scripts/gatus-config-test.py`, `python3 scripts/portal-zones-test.py`, `yamllint -s .` → PASS; routing cases in the CI `caddy` job.
- [ ] **Step 5: Commit** `feat(sso): wire lldap.private (EDGE-07)`.

---

### Task 3: `auth.public` in Caddy, its zones, the cookie strip, redacted logs, public probes

**Files:** Modify `inventories/prod/group_vars/all/main.yml`, `roles/stack/templates/Caddyfile.j2`, `roles/stack/templates/gatus-config.yaml.j2`, `scripts/caddy-routing-test.sh`, `scripts/gatus-config-test.py`, `scripts/portal-zones-test.py`.

**Interfaces — Produces:** `authelia_public_hostname: "auth.{{ public_zone }}"`, `authelia_port: 9091`, `ratelimit.authelia_login`, `ratelimit.authelia_backchannel`; matcher `@authelia`; zones `authelia_login`, `authelia_backchannel`, `authelia_general`; logger `public_zone`; probes `authelia-public`, `authelia-discovery`, `authelia-jwks`.

- [ ] **Step 1: Failing routing cases** (`caddy-routing-test.sh`). Mock: `/echo-headers` also appends `host=[…]` and `cookie=[…]` after `xff=[…]` (existing prefix expectations keep passing); the `authelia` mock (port 9091) sends `Location` like `commsgun-ui`. Cases: health and discovery reach `authelia`; `Host` arrives unchanged (also for a mixed-case request: the upstream sees what the client sent); spoofed `X-Forwarded-For` arrives as the single client entry (S7's Caddy side); `authelia_session` reaches Authelia; for the UI, the agent and webhooks-mcp `/mcp`, `Cookie: authelia_session=SECRET` alone, first, middle and last arrive without `SECRET` and with every other cookie, and the same with two `-H 'Cookie: …'` over `--http2`; 11 `POST /api/firstfactor` from one client → ten `200` then `429`; `POST /api/firstfactor/reauthenticate`, `/api/secondfactor/totp` and `/api/reset-password/identity/start` count in that zone (fresh client each: they share it with `/api/firstfactor`); `GET /api/health` and 11 `POST /api/oidc/token` are not limited by it; the staging VM's tailnet address gets `200` on the auth host (Open question 14); an unknown `*.public` name stays `404`. Log canaries: `GET /reset-password/step2?token=canarytoken` with `Referer` canary on the auth host, the same with a mixed-case host, then kill the `authelia` mock and repeat for a `502`; `grep canary` finds nothing in any access log or in Caddy's stderr, while the requests themselves are logged; the existing "access log redacts OAuth code/state" case still passes.
- [ ] **Step 2: Failing probe tests** (`gatus-config-test.py`): `test_the_portal_is_probed_from_the_internet_resolver` (`authelia-public`, group `public`, `2m`, `https://<auth>/api/health`, `["[STATUS] == 200"]`, `tcp://1.1.1.1:53`); `test_the_discovery_probe_pins_the_exact_issuer` (`authelia-discovery`: `[BODY].issuer == https://<auth>` with no trailing slash and `[BODY].jwks_uri == https://<auth>/jwks.json`); `test_the_jwks_probe_requires_a_key` (`authelia-jwks`: `len([BODY].keys) > 0`). `portal-zones-test.py`: `test_zone_and_tenant_names` asserts `auth.public.<d>`; `test_public_zone_names_get_no_split_horizon_record` asserts `authelia_public_hostname` is not in `_names`.
- [ ] **Step 3: Run** `python3 scripts/gatus-config-test.py`, `python3 scripts/portal-zones-test.py` → FAIL.
- [ ] **Step 4: Implement.** Vars as in Interfaces (values per Open questions 1 and 5). In `*.{{ public_zone }}`, before the `edge_sites` loop: `@authelia host {{ authelia_public_hostname }}`, a `handle` with a `rate_limit` block (the three zones, `key {remote_host}`, `ipv6_prefix 64`, paths and methods of Open question 5) and `reverse_proxy 127.0.0.1:{{ authelia_port }}` with no `header_up` and no `import deny_staging_vm`. Replace the site's `import access_log public` with `log public_zone` writing `/var/log/caddy/public.access.log` (same roll settings) whose filter drops the query, `Referer` and `Location`, as `private_zone`'s does; add `http.log.error.public_zone` to the default logger's `exclude` and to `redacted_requests`' `include`. In `(app_ui)`, `(agent)` and `bearer_mcp_body`'s `reverse_proxy`, one `header_up Cookie <regex> <replacement>` that removes the `authelia_session` pair. Probes beside `webhooks-mcp-portal-requires-auth`, using `*public`.
- [ ] **Step 5: Run** the two Python tests and `yamllint -s .` → PASS; push and read the CI `caddy` job (routing and "every rate-limit zone … is unique") → PASS.
- [ ] **Step 6: Commit** `feat(sso): serve auth.public with path-scoped limits, the cookie strip and a redacted log`.

---

### Task 4: Authelia configuration and the client registry

**Files:** Create `roles/stack/templates/authelia-config.yml.j2`, `scripts/authelia-config-test.py`, `scripts/sso-stack-test.py`; modify `inventories/prod/group_vars/all/main.yml`, `.github/workflows/ci.yml`.

**Interfaces — Consumes:** Task 1 keys; Task 3 hostname. **Produces:**
- `lldap_base_dn`, `authelia_ldap_bind_user: authelia`, `authelia_ldap_bind_email: authelia@ldap.invalid`, `sso_groups: [sso_people, xstockstrat, xstockstrat-staging, webhooks-mcp, commsgun, happybabytracker]`.
- `authelia_oidc_clients`: list of `{id, kind: web|mcp, group, …}`; `web` entries carry `redirect_uri` and `key: authelia_client_secret_<id with _>_pbkdf2`; `mcp` entries carry `audience` and `scopes`; `webhooks-mcp-claude` also `access_token_lifespan: 5m`. Values: Global Constraints and Open question 10.
- The template reads secrets only as Authelia templates (Jinja-escaped): `{{ env "EDGE_SESSION_SECRET" }}`, `EDGE_STORAGE_ENCRYPTION_KEY`, `EDGE_RESET_PASSWORD_JWT_SECRET`, `EDGE_OIDC_HMAC_SECRET`, `EDGE_LDAP_BIND_PASSWORD`, and the key as `{{ env "EDGE_OIDC_JWKS_KEY_B64" | b64dec | mindent 10 "|" | msquote }}` (no `AUTHELIA_` prefix: Authelia maps those to config keys). Client hashes are inline from `edge_secrets[client.key]` (EDGE-08 registry loop). The rendered file is `0644`, readable by uid 10001, and holds no plaintext secret.
- Fixed values: `server.address: tcp://0.0.0.0:9091/`; `storage.local.path: /data/db.sqlite3`; `notifier.filesystem.filename: /data/notification.txt`; `ldap.address: ldap://lldap:3890`; bind DN `uid={{ authelia_ldap_bind_user }},ou=people,{{ lldap_base_dn }}`; `users_filter: '(&({username_attribute}={input})(objectClass=person)(memberOf=cn=sso_people,ou=groups,{{ lldap_base_dn }}))'`; `session.name: authelia_session`; one authorization policy per `sso_groups` entry except `sso_people` (default deny, one `two_factor` rule for `group:<name>`); claims policy `web` with `id_token: [email, preferred_username]`; lifespans `access_token: 1h`, `refresh_token: 720h`, custom `webhooks-mcp` with `access_token: 5m`.

- [ ] **Step 1: Write `scripts/authelia-config-test.py`** in the style of `happybabytracker-config-test.py` (`load_vars`, fake secrets, render with StrictUndefined plus the `b64encode` filter shim). Tests, each against the parsed render: `test_portal_and_cookie_follow_the_public_name`; `test_policies_deny_by_default_and_require_two_factor` (no `one_factor` anywhere in the text); `test_regulation_matches_the_spec`; `test_password_reset_and_elevated_session`; `test_ldap_logs_in_by_uid_only_and_only_sso_people` (exact filter and bind DN); `test_lifespans` (only `webhooks-mcp-claude` uses the 5 min lifespan); `test_clients_match_the_spec_table` (the nine ids; per client its policy, `public` or `client_secret_basic`, `require_pkce` with `S256`, exact redirect list, scopes, audience, RS256, `requested_audience_mode: implicit`, `consent_mode: implicit`, grant types `authorization_code` (+ `refresh_token` for MCP); no `client_credentials`, no loopback or `localhost` redirect); `test_every_client_group_is_declared_and_every_app_group_is_used`; `test_the_client_keys_match_init_secrets` (`init_secrets.OIDC_CLIENT_KEYS` + `_pbkdf2` == the `key:` of every `authelia_oidc_clients` entry that has one); `test_confidential_clients_read_their_hash_through_the_registry` (render contains the fake hash; `secret_keys.used_keys()` sees all four `_pbkdf2` keys); `test_no_secret_value_is_rendered_and_every_env_template_is_literal` (no fake secret string in the render; the six `{{ env "EDGE_…" }}` strings appear literally); `test_the_notifier_is_the_file_notifier_until_p5` (no `smtp`).
- [ ] **Step 2: Run** `python3 scripts/authelia-config-test.py` → FAIL (no template).
- [ ] **Step 3: Implement** the vars and the template; settle Open question 4 by Step 5's output.
- [ ] **Step 4: Run** the test and `python3 scripts/secrets-keys-test.py` (red only on `test_every_key_the_roles_read_is_in_the_committed_file` until Task 10's `fill_missing`; say so in the PR) → otherwise PASS.
- [ ] **Step 5: Create `scripts/sso-stack-test.py`** (Docker; `SSO_STACK_TEST_REQUIRE_DOCKER=1` turns a skip into a failure) with its first two checks: `validate-config` with the pinned image on the rendered config and an env file of generated test secrets → exit 0 (warnings printed, not failed, S20); `authelia crypto hash validate --password <value> -- '<digest>'` with the pinned image on a pair from `init_secrets` → exit 0. Add CI job `sso` (ubuntu-24.04, setup-python, `pip install -r requirements.txt`, run the script) and add `sso` to `ci-gate`'s `needs` and its echo list; add `authelia-config-test.py` to `lint`. Push; read the `sso` log.
- [ ] **Step 6: Commit** `feat(sso): Authelia configuration and the nine OIDC clients`.

---

### Task 5: Compose services, pins, env files, volume ownership

**Files:** Modify `roles/stack/templates/compose.yml.j2`, `roles/stack/tasks/main.yml`, `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md`, `scripts/authelia-config-test.py`, `scripts/sso-stack-test.py`.

**Interfaces — Consumes:** Tasks 1–4. **Produces:** services `authelia`, `lldap`; network `authelia`; volumes `authelia-data`, `lldap-data`; `{{ edge_root }}/authelia/config/configuration.yml` (`0644`, dir `0755`), `{{ edge_root }}/authelia/authelia.env` and `{{ edge_root }}/lldap/lldap.env` (`0600`); a block "SSO secrets and configuration" (`no_log: true`) after happybabytracker's.
- `authelia.env`: `X_AUTHELIA_CFG_FILTERS='template'` and the six `EDGE_*` names (the JWKS PEM `| b64encode`).
- `lldap.env`: `LLDAP_JWT_SECRET`, `LLDAP_KEY_SEED`, `LLDAP_LDAP_USER_PASS`, `LLDAP_LDAP_BASE_DN`, `LLDAP_DATABASE_URL='sqlite:///data/users.db?mode=rwc'` (explicit: the backup reads that path).
- `authelia`: `command: ["authelia", "--config", "/config/configuration.yml"]`, volumes `./authelia/config:/config:ro` and `authelia-data:/data`, `ports: ["127.0.0.1:{{ authelia_port }}:9091"]`, `depends_on: {lldap: {condition: service_healthy}}`, the image's own healthcheck. `lldap`: `ports: ["127.0.0.1:{{ lldap_ui_port }}:17170"]`, healthcheck `["CMD", "wget", "-qO-", "http://127.0.0.1:17170/health"]` (the spike's health path).
- Before the stack starts: `docker compose -f {{ edge_root }}/compose.yml create --no-recreate lldap authelia`, then `ansible.builtin.file` sets `/var/lib/docker/volumes/edge_authelia-data/_data` and `…/edge_lldap-data/_data` to `10001:10001`, `0700`.

- [ ] **Step 1: Failing tests** in `authelia-config-test.py`: `test_services_are_hardened_read_only_and_loopback_only` (merged `*hardening`, `user`, `read_only`, `mem_limit` `128m` each, exact `ports`, no `3890` publish anywhere, both only on `authelia`, the network not `internal`, data volume at `/data`); `test_assertions_run_before_any_file_is_written` (present, string, no quote, backslash, CR or LF; PEM shape `-----BEGIN PRIVATE KEY-----`; failure message names keys only); `test_env_files_hold_exactly_the_contracted_variables` (parsed single-quoted lines; `0600`; `no_log`; no `notify`); `test_compose_holds_no_secret` (render compose, grep fake values); `test_volume_ownership_is_set_before_lldap_starts` (task order); `test_docker_compose_config_reads_back` (when Docker exists). Pins: a test that each pin line matches `^<var>: ".*@sha256:[0-9a-f]{64}"$` once and that `docs/versions.md` has one row naming each var.
- [ ] **Step 2: Run** → FAIL. **Implement** the pins (Global Constraints, EDGE-01 shape) and rows (Authelia: latest release 2026-09-17, P0 validated this digest, migrates storage on start, so the deploy backs up first and rollback is a restore; lldap: latest release 2026-04-30, rootless image (F10), newer tags are dated main builds); add `authelia` and `lldap` (`0750`) and `authelia/config` (`0755`) to "Stack directories"; the services, network, volumes, block and the ownership tasks. Run the test, `yamllint -s .`, `ansible-lint` → PASS.
- [ ] **Step 3: Extend `sso-stack-test.py`:** render the two services into a throwaway project with test secrets, run the same create → chown (`sudo -n`) → `up -d --wait` sequence, then assert both healthy within 120 s, `GET /api/health` `200` on the published port, lldap `/health` `200`, `docker inspect` shows uid `10001`, `ReadonlyRootfs: true`, `CapDrop: [ALL]`; restart both and assert healthy again; `down -v` at the end. If an image cannot run read-only, the test names the path from its log; the fix is a `tmpfs:` for that path only, recorded in `docs/decisions.md` (Task 9). Push; read the `sso` log → PASS.
- [ ] **Step 4: Commit** `feat(sso): run Authelia and lldap hardened on the edge`.

---

### Task 6: lldap ensure, `sub` reconcile and the deploy order

**Files:** Create `scripts/sso_sync.py`, `scripts/sso-sync-test.py`; modify `roles/stack/tasks/main.yml`, `scripts/authelia-config-test.py`, `scripts/sso-stack-test.py`, `.github/workflows/ci.yml`.

**Interfaces — Produces** (`scripts/sso_sync.py`, stdlib + PyYAML; prints one JSON object of usernames and counts, never a secret):
- CLI (the deploy passes `--lldap-url http://127.0.0.1:{{ lldap_ui_port }}` and `--compose {{ edge_root }}/compose.yml`): `sso_sync.py ensure --lldap-url URL --compose FILE --groups G… --bind-user NAME --bind-email EMAIL` (env `LLDAP_ADMIN_PASSWORD`, `LLDAP_BIND_PASSWORD`); `sso_sync.py reconcile --lldap-url URL --compose FILE` (env `LLDAP_ADMIN_PASSWORD`). Exit 0 done, 2 refused (unsafe state or mismatch; nothing written), 1 error.
- `SUB_ATTRIBUTE = "sso-sub"`; `PEOPLE_GROUP = "sso_people"`.
- `ensure_plan(state: dict, groups: list[str], bind_user: str) -> EnsurePlan` (`create_groups: list[str]`, `create_attribute: bool`, `create_bind_user: bool`, `add_bind_to_password_manager: bool`); raises `Refused` when an existing `sso-sub` is not a `STRING`, is a list, is visible or is editable, or when `bind_user` is in any of `groups`. `state` is lldap's GraphQL `{ groups { id displayName } users { id groups { displayName } attributes { name value } } schema { userSchema { attributes { name attributeType isList isVisible isEditable } } } }` (the spike's query).
- `parse_export(text: str) -> dict[str, str]`: username lower-cased → identifier, `service: openid` with empty `sector_identifier` only; a duplicate username raises.
- `lldap_subs(users: list[dict]) -> dict[str, str | None]`: `sso_people` members only, id lower-cased.
- `reconcile_plan(authelia: dict[str, str], lldap: dict[str, str | None]) -> ReconcilePlan` (`copy_to_lldap: dict[str, str]`, `add_to_authelia: dict[str, str]`, `mismatched: list[str]`); a non-UUID lldap value counts as mismatched.
- lldap writes: GraphQL `createGroup`, `addUserAttribute(name, attributeType: STRING, isList: false, isVisible: false, isEditable: false)`, `createUser`, `addUserToGroup` (bind user and `lldap_password_manager` only), `updateUser(user: {id, insertAttributes: [{name: "sso-sub", value: [<uuid>]}]})`; bind password via `docker compose -f FILE exec -T lldap /app/lldap_set_password` (S21; it takes the password as an argument, as in the spike, so it is briefly visible to root's `ps` on the edge and never in a log). Authelia: `docker compose -f FILE run --rm --no-deps -T authelia authelia storage migrate up`, `… storage user identifiers export --file /data/.sub-export.yml` (read, then removed), `… storage user identifiers add <user> --identifier <uuid> --service openid`, each with `--config /config/configuration.yml`.

- [ ] **Step 1: Write `scripts/sso-sync-test.py`** (unittest; a fake GraphQL server on a thread; a fake `docker` on `PATH` recording argv). Tests: `test_ensure_creates_only_the_missing_groups_and_the_attribute`; `test_a_second_ensure_changes_nothing`; `test_ensure_refuses_an_editable_visible_list_or_non_string_sub_attribute` (each, exit 2, no mutation sent); `test_ensure_refuses_a_bind_user_in_any_sso_group`; `test_ensure_never_mutates_another_user` (every mutation names a group, the attribute, or the bind user); `test_the_bind_password_reaches_lldap_set_password_and_nothing_is_printed` (stdout and stderr hold no secret); `test_parse_export_keeps_openid_without_sector_and_lowercases` (fixture: other service, a sector, `Alice`); `test_reconcile_copies_a_first_login_sub_to_lldap`; `test_reconcile_restores_an_authelia_sub_from_lldap`; `test_reconcile_leaves_matching_subs_alone`; `test_reconcile_matches_usernames_case_insensitively`; `test_reconcile_ignores_users_outside_sso_people`; `test_a_mismatch_or_a_non_uuid_writes_nothing_and_exits_2` (other users' pending copies are not written either; message names usernames only); `test_the_export_file_is_removed`.
- [ ] **Step 2: Run** `python3 scripts/sso-sync-test.py` → FAIL. **Implement** `sso_sync.py` to the interfaces; run → PASS. Add the test to CI `lint`.
- [ ] **Step 3: Failing deploy-order tests** in `authelia-config-test.py` (`find_task` over `roles/stack/tasks/main.yml`): `test_python3_yaml_is_installed_before_the_sync` (apt `python3-yaml`, Ubuntu archive); `test_a_digest_change_backs_up_then_stops_authelia_before_the_sync` (reads each container's `.Config.Image`; when a container exists and differs from the pin, `systemctl start edge-backup.service` runs, and for Authelia `docker compose … stop authelia` follows; both before "Start the stack"); `test_lldap_is_up_and_synced_before_the_stack_starts` (`up -d --wait lldap`, `ensure`, `reconcile` in that order, all before "Start the stack"; `ansible.builtin.script` with `executable: python3`, `environment:` reading `edge_secrets.lldap_admin_password` and `edge_secrets.authelia_ldap_bind_password`, `no_log: true`, a follow-up `debug` of the JSON summary); `test_the_config_restart_is_an_unconditional_hash_compare` (after "Start the stack" and the handler flush: sha256 of `configuration.yml` compared with `{{ edge_root }}/authelia/config.applied.sha256`; on a difference `docker compose … restart authelia` with `changed_when: true`, a wait until healthy, then the new hash written; no `notify:` on the config template); `test_the_deploy_checks_the_portal_on_its_public_name` (`curl -fsS --resolve <auth>:443:127.0.0.1` on `/api/health`, and the discovery `issuer` equals `https://<auth>` exactly). Run → FAIL; implement the tasks; run → PASS.
- [ ] **Step 4: Extend `sso-stack-test.py`** against the running test project: `ensure` twice (second prints no change; people and memberships unchanged, S22); a test person in `sso_people`, one outside it, and lldap's `admin`: `POST /api/firstfactor` as the outsider, `admin` and the bind user → refused; reconcile case 1 (`identifiers add` for the person, lldap empty → copied, attribute read back), case 2 (`down`, remove `authelia-data`, recreate, reconcile → added back, export shows the same `sub`), case 3 (a different UUID in lldap → exit 2, both stores unchanged). Push; read `sso` → PASS.
- [ ] **Step 5: Commit** `feat(sso): ensure lldap's groups and attribute, reconcile each sub before Authelia serves`.

---

### Task 7: Back up Authelia's, lldap's and commsgun's databases

**Files:** Modify `roles/backup/templates/edge-backup.sh.j2`, `roles/backup/templates/edge-backup-verify.sh.j2`, `roles/backup/tasks/main.yml` (unit and timer `Description=`), `scripts/edge-backup-test.py`.

**Interfaces — Produces:** archive layout `headscale/`, `happybabytracker/happybabytracker.db`, `authelia/db.sqlite3`, `lldap/users.db`, `commsgun/commsgun.db`; a script function `snapshot_as_app <volume> <file> <dir>` that holds today's happybabytracker lines (guard, private bind mount, `setpriv --reuid=10001 --regid=10001 --clear-groups sqlite3 … .backup`, `integrity_check` of the copy, move into `$work/<dir>/`); heartbeat `nightly-backup` keeps its name (EDGE-10).

- [ ] **Step 1: Failing tests:** parametrise `test_the_database_is_snapshotted_as_the_apps_uid`, `test_the_copy_is_integrity_checked_and_manifested` and `test_the_drill_checks_the_restored_copy_and_prints_no_rows` over `DATABASES = [("happybabytracker-data", "happybabytracker.db", "happybabytracker"), ("authelia-data", "db.sqlite3", "authelia"), ("lldap-data", "users.db", "lldap"), ("commsgun-data", "commsgun.db", "commsgun")]` (one call each; the drill has exactly four `sqlite3` lines, all `integrity_check`; the manifest `find` names all five dirs; nothing copies `notification.txt`); point `snapshot_sections()` and the live-writer functional test at the function; `test_the_units_describe_every_database`.
- [ ] **Step 2: Run** `python3 scripts/edge-backup-test.py` → FAIL. **Implement**; run (root cases in CI's `sudo -n` step), `gatus-config-test.py::test_heartbeats_are_unchanged`, `yamllint -s .` → PASS.
- [ ] **Step 3: Commit** `feat(sso): back up Authelia, lldap and commsgun twice a day`.

---

### Task 8: verify

**Files:** Modify `.github/workflows/verify.yml`.

- [ ] **Step 1:** In `Targets`, add `auth=auth.public.$d` and `lldap=lldap.private.$d` (leave the `awk` lines as they are: EDGE-01). New external step "SSO portal - health, exact issuer, JWKS, login limit; lldap not public": `GET /api/health` → `200`; discovery `.issuer` equals `https://auth.public.$d` (`jq -r`); `jwks.json` has ≥ 1 key; 11× `POST /api/firstfactor` with `{}` includes a `429`; `curl --resolve "$lldap:443:$ip" https://$lldap/health` → `000`.
- [ ] **Step 2:** `yamllint -s .github` → PASS; the run itself happens in Task 10.
- [ ] **Step 3: Commit** `feat(sso): verify the portal from outside`.

---

### Task 9: Documentation

**Files:** Modify `docs/runbooks/sso.md`, `docs/runbooks/recovery.md`, `docs/runbooks/alerts.md`, `docs/runbooks/commsgun.md` (only lines that say its store is not backed up, if any), `docs/decisions.md`, `README.md`.

- [ ] **Step 1: `sso.md`**: status "P1 deployed; no app wired"; full commands (`docker compose -f /opt/edge/compose.yml exec -T authelia authelia storage … --config /config/configuration.yml`), each checked against the pinned image's `--help`; the file notifier's path and read command (settles § 3's `TODO(verify)`); lldap's `admin` sign-in and where its password is (`lldap_admin_password`); what each deploy does to lldap (groups, attribute, bind account) and never does; a reconcile failure (exit 2): which usernames, how to decide which `sub` is right, how to fix it; the bind account is never added to `sso_people` or an app group; JWKS rotation (add, publish, wait 1 h, retire) and client-secret rotation (planned outage per client); removing a person's Authelia identifier as the pinned CLI allows.
- [ ] **Step 2: `recovery.md`**: "Restore Authelia, lldap or commsgun": decrypt with the offline key; `docker compose -f … stop <svc>`; copy into `_data`; remove stale `-wal`/`-shm`; `chown 10001:10001`; start; Authelia **before anyone logs in**; then deploy so the reconcile runs.
- [ ] **Step 3: `decisions.md`**: amend the commsgun bullet (its store is now in the backup, decision 17); one Known-traps entry for P1: the `*.public` zone logger and why (per-host loggers are exact-name), the `_data` ownership step, the `sso-sub` shape guard, `consent_mode`, any `tmpfs` Task 5 needed. `alerts.md`: the four probes. `README.md`: diagram, zone list, repository map (`sso_sync.py`, the new template and tests).
- [ ] **Step 4: Re-read every touched doc against the code** (Teardown) and fix drift; `yamllint -s .`.
- [ ] **Step 5: Commit** `docs(sso): P1 runbooks, recovery and decisions`.

---

### Task 10: PR, owner steps, deploy and the P1 pass condition (§ 10)

Owner or CI; the implementer opens the PR and pastes each output into it.

- [ ] **Step 1:** Push and open the PR (CI red only on `secrets-keys-test.py` until Step 2).
- [ ] **Step 2 (owner):** allow the branch in the `production` environment, run **init-secrets** with `fill_missing` on it, revert the setting (`docs/runbooks/commsgun.md` § 2.3).
- [ ] **Step 3:** CI Gate green on the PR head: `lint` (incl. `authelia-config-test.py`, `sso-sync-test.py`), `secrets`, `caddy` (routing cases of § 4.3), `sso` (config validation, hardened start, sync).
- [ ] **Step 4 (owner):** Task 0 Step 2 again if more than a day has passed; **deploy** (`tailnet`). The log shows the ensure and reconcile summaries and "Authelia serves its exact issuer".
- [ ] **Step 5:** **verify** (`workflow_dispatch`): external steps green including the SSO step; the tailnet job's `backup-now` journal shows `authelia`, `lldap`, `commsgun` in the archive and "restore drill OK". Gatus `authelia-public`, `authelia-discovery`, `authelia-jwks`, `lldap-private` green.
- [ ] **Step 6 (owner):** in lldap's UI, create `sso-p1-check` and the § 7 people (`sso_people` plus their groups). `sso-p1-check` presses **Forgot password**; the admin relays the link from the file notifier; it sets a password, logs in, enrols TOTP (code relayed the same way). Then the Open question 12 flow as `sso-p1-check` mints its `sub`. Deploy: the summary lists `sso-p1-check` under `copied_to_lldap`, and its `sso-sub` in lldap equals the export.
- [ ] **Step 7 (owner):** wipe: `docker compose -f /opt/edge/compose.yml stop authelia`, `rm -f authelia`, `docker volume rm edge_authelia-data`; deploy: `added_to_authelia: [sso-p1-check]`; the export shows the same `sub`. Mismatch: set a different UUID on `sso-p1-check`'s `sso-sub`, deploy with no other change pending: it fails at the reconcile naming `sso-p1-check`; restore the value; deploy green.
- [ ] **Step 8:** record against the deployed portal: **S1** (token `aud` and acceptance of `resource`), **S5** (consent then redirect to `claude.ai` with a code), **S7** (a failed `POST /api/firstfactor` with `X-Forwarded-For: 203.0.113.9` from outside: Authelia's log shows the real client address, never the spoofed one), **S15** (Open question 12), **S18** (for each of `edge_default`, `edge_commsgun`, `edge_happybabytracker`: `docker run --rm --network <net> --add-host <auth>:host-gateway --entrypoint wget <authelia_image> -qO- https://<auth>/.well-known/openid-configuration` returns issuer `https://<auth>`).
- [ ] **Step 9:** offboard `sso-p1-check` (`sso.md` § 4). Add a "P1 record" table to the spec's § 11 (observed output per check above) and close § 12's open item; commit `docs(sso): P1 record`.

## Done when

The § 10 P1 pass condition holds, each part with its output in the PR: memory check; `ci` green incl. routing cases and config validation; deploy; verify; the four P1 probes green; backup and drill include `authelia-data`, `lldap-data`, `commsgun-data`; a person created in lldap's UI resets their password and logs in, and the next deploy copies their `sub` into lldap; a `sub` survives an Authelia DB wipe and redeploy, and a mismatched one fails the deploy; S1, S5, S7, S15 and S18 recorded against the deployed portal.

## Not in this plan (later phases, § 5 and § 10)

- **P2/P3 xstockstrat:** config-service keys and the client-secret copy, `/accounts/api/sso/confirm` in the login zones, the 345 gates, the connector end to end.
- **P4 webhooks-mcp:** `extra_hosts`, `AUTH_MODE`/`OAUTH_ISSUER`/`OAUTH_RESOURCE`, the PRM route on `bearer_mcp`.
- **P5 commsgun:** public name, fixed subnets and the `authelia-smtp` network, the SMTP listener and its CA, certificate, key and credential, `commsgun_senders`/`commsgun_apps` entries for `authelia`, the SMTP notifier, login zones, `extra_hosts`, the P5 probes.
- **P6 happybabytracker:** public name, `SESSION_SEAL_KEY`, token-path redaction, login zones, `extra_hosts`, the P6 probes.
- `commsgun.md` and `happybabytracker.md` changes due before each app's first SSO-capable bump (§ 13); any body cap on the auth host; a `forward_auth` consumer.
