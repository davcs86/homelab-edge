# Authelia SSO P2 (xstockstrat staging cutover) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move xstockstrat **staging** (`xstockstrat-staging.public.<domain>`) from `auth.mode` `local` through `both` to `oidc` against the P1 portal, and meet the § 10 P2 pass condition with each check's output in hand. Prod (P3) is out of scope.

**Architecture:** P1 already registered both staging clients and the claims policy, so the edge needs one code change: the Link confirmation `POST /accounts/api/sso/confirm` joins the two existing password-login zones. That is 345 design § 6 gate 1 and SSO spec § 4.3. Everything else is operational. The owner sets the staging config rows in config-ui, runs `manage-users.py` against the staging database over the tailnet, and drives the browser and Claude.ai checks. The VMs' back-channel to `auth.public` goes over the home WAN (spec § 4.2), which needs no edge change. No xstockstrat code or deploy change is needed (Task 6, Q14).

**Tech Stack:** Caddy 2.11.9 + caddy-ratelimit (`Caddyfile.j2`), bash routing test (`scripts/caddy-routing-test.sh`), GitHub Actions (`ci`, `deploy`, `verify`), Authelia 4.39.28, xstockstrat 345 (`scripts/manage-users.py`, config-ui), Claude.ai custom connectors.

**Spec:** `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md`:
- § 3, the app auth contract: modes, linking with `prompt=login` + `max_age=1`, switching mode without a login;
- § 4.2 back-channel, § 4.3 zones (`:136-142`), § 4.6 clients and claims (`:157-159`, `:168-172`);
- § 5, the xstockstrat row (`:250`); § 10, the P2 row (`:314`); § 13 handoff (`:390-391`).

Also `docs/runbooks/sso.md` § 6 (`:94-117`), and on xstockstrat `main-dev` at `df9532c`:
- `docs/runbooks/authelia-sso-cutover.md`;
- `docs/roadmap/features/345-authelia-sso/design.md` § 6 (`:361-382`), D-138 and D-139 (`:35-47`);
- `docs/runbooks/mcp-tools.md` (`:86-126`).

## Global Constraints

- `CLAUDE.md` binds every task: list your assumptions; make the minimum change; touch only what the task needs; comments state constraints only, in ≤ 2 lines.
  - Rule 1: no value the owner has not stated. Every unknown is an **Owner question** below.
  - Rule 2: the public surface stays 80/tcp, 443/tcp, 3478/udp. P2 opens nothing.
  - Rule 3: no secret in the repo, a log, a PR or a commit message. The plain client secret travels from SOPS to config-ui's password field only, never through a terminal echo, a chat or the Claude.ai connector's `set_config`. `sub`s and emails never go into a PR either: record "equal" / "refused", not values.
  - Rule 6: success is the check's output from this session, pasted into the PR that a task names.
  - Rule 8: no new name. Staging stays `xstockstrat-staging.public.<domain>` (`inventories/prod/group_vars/all/main.yml:48`).
- Edge seams:
  - **EDGE-01:** no pin changes.
  - **EDGE-02:** no `trusted_proxies` or `header_up X-Forwarded-*`. The zone change adds a path to two matchers and nothing else.
  - **EDGE-12:** the zone names stay `{args[1]}_login` and `{args[1]}_login_hourly` (`Caddyfile.j2:152-171`).
  - EDGE-03/04/05/07/08/10/11 are not touched: no service, env file, `*.private` name, secret read, Gatus key or owner-app wiring changes.
- Cross-repo contracts. These are stated in the xstockstrat docs and checked against the edge config:
  - issuer `https://auth.public.natanddavehouse.com`: no trailing slash, byte-equal to Authelia's discovery `issuer` (cutover runbook § 3.2; `verify.yml` "SSO portal" step);
  - web client `xstockstrat-staging-web`, redirect `${APP_URL}/api/auth/sso/callback` (`main.yml:147-148`);
  - MCP clients `xstockstrat-staging-mcp-claude[-ro]`, audience `${APP_URL}/agent` = `AGENT_PUBLIC_URL` (`main.yml:155-160`; xstockstrat `deploy/homelab/compose.yml:308,352`);
  - Claude.ai clients only: the two `oidc.mcp_client_id_claude_code*` rows stay empty (`mcp-tools.md:104-107`).
- Domain: `natanddavehouse.com`, as the owner states it in `docs/runbooks/deploy-compose-app.md:155` and `docs/runbooks/db-host.md:31`. Staging's `APPLICATION_ENV` is `development`, so every `--environment` below is `staging` (`deploy-compose-app.md:158`; cutover runbook § 5).
- Tooling: `caddy-routing-test.sh` needs Docker and `sudo` (`scripts/caddy-routing-test.sh:3`). Where those are missing, push and read the CI `caddy` job (`.github/workflows/ci.yml:105-131`). Commits end with the session's attribution trailers.

## Assumptions (each with its basis; the owner may overturn any)

1. **The P1 client wiring is enough for gate 3.** `xstockstrat-staging-web` is rendered with `scopes: [openid, profile, email]` and `claims_policy: web`, whose `id_token` is `[email, preferred_username]`. The authorization-code lifespan is left at Authelia's default, as cutover runbook § 4.3 asks. Basis: `roles/stack/templates/authelia-config.yml.j2:69-78,88-101`; `scripts/authelia-config-test.py:152` (`test_clients_match_the_spec_table`). Task 4 Step 3 checks the deployed copy.
2. **Gate 2 (S17) is already proven, and the first real link re-proves it.** The P0 record has S17 at spec `:364`, and 345 design § 6 gate 2 says the same (`design.md:364-368`). The link in Task 5 completes only with a fresh `auth_time` (spec § 3 "Linking"), so that link is S17 on the deployed portal. No separate spike re-run.
3. **The zone change applies to prod's site too.** `public_routes` is shared by every `app` site (`Caddyfile.j2:114-116`). Prod's confirm route therefore gets the zone in P2. That is harmless (the prod app is offline, PR #137) and P3 needs it.
4. **No Gatus change.** Every existing staging probe holds in all three modes. Spec § 4.9 has no P2 row. The evidence:
   - the protected-resource document keeps `resource` = `AGENT_PUBLIC_URL` in every mode (xstockstrat `services/xstockstrat-agent/app/oauth_metadata.py:22-44`);
   - an unauthenticated `POST /agent` still answers 401 (`services/xstockstrat-agent/app/main.py:172-182`);
   - `/trader/api/health` does not depend on the mode.

   These are the probes at `gatus-config.yaml.j2:194-223,246-263` (Q15).
5. **The VM back-channel needs no edge change.**
   - Public names have no split-horizon (spec F9), so the staging VM reaches `auth.public` from the home WAN IP.
   - The auth handle deliberately does not import `deny_staging_vm`: `Caddyfile.j2:338-341`, pinned by "staging VM reaches the portal" at `caddy-routing-test.sh:169`.
   - Back-channel paths sit in the 600/min `authelia_backchannel` zone (`Caddyfile.j2:354-361`).
6. **config-ui can write the secret row.** Its editor renders `isSecret` keys as a password input and targets the row's own environment. Basis: xstockstrat `services/xstockstrat-ui/src/app/config-ui/[namespace]/NamespaceEditor.tsx:98-113,272-280`.

## Owner questions (answer before execution; each has a recommendation and its basis)

| # | Question | Recommendation | Basis |
|---|---|---|---|
| 1 | How should `POST /accounts/api/sso/confirm` be rate-limited: a path added to the existing `{site}_login` and `{site}_login_hourly` matchers (one counter shared with password login), or new zones? | **Add the path to the two existing matchers.** One shared counter: 5/min, 30/h per IP across both password-taking POSTs | Spec § 4.3 "behind the same two zones" (`:136-139`); `Caddyfile.j2:150-171`; EDGE-12 names unchanged |
| 2 | Should the confirm POST also get the 8 KB `body_limits.login` cap that `@login_api` has (`Caddyfile.j2:219-228`)? | **No.** Neither spec § 4.3 nor 345 design § 6 asks for it, and the body has not been measured, so a cap risks a 413 on a real link | `CLAUDE.md` How to Act 2 (minimum) |
| 3 | Should `verify.yml` gain a live confirm-zone case beside its login case (`verify.yml:98-100`)? | **No.** Pin the rule in `caddy-routing-test.sh` (spec § 4.3 "pins every rule above") and prove the deployed zone once by hand (Task 3 Step 3) | Minimum change; the spec names only the routing test |
| 4 | Does the **homelab** staging VM already run a `main-dev` image that has 345 and config migration `060`? The brief says only "deployed to DO dev". Homelab staging deploys only while `HOMELAB_STAGING_ENABLED` is `true` | **Check first** (Task 4 Step 1: the `auth.mode` rows exist), and deploy `main-dev` to homelab staging if they do not | `deploy-compose-app.md:126-129`; xstockstrat `services/xstockstrat-config/migrations/060_authelia_sso_keys.up.sql` |
| 5 | Where do `set-auth-mode` and `reset-password` connect from? | **`superadmin` from `macbook-pro`, direct to 5432.** That is the only operator path `pg_hba` admits to a workload database. The workload role is admitted only from the VM's IP, and `superadmin` never goes through PgBouncer | `roles/db_host/templates/pg_hba.conf.j2:11,18`; `docs/runbooks/db-host.md:20,25-31`; `main.yml:202` |
| 6 | Is staging's seeded `admin@localhost` still on the migration's default password `admin`? If so, it is exposed now: the staging UI and `POST /api/auth/login` are public (`main.yml:123-124`) | **Reset it now with `reset-password` (gate 4), whatever the answer**, before any other step | xstockstrat `services/xstockstrat-identity/migrations/002_seed_admin.up.sql:4,11`; 345 design § 6 gate 4 |
| 7 | Which staging xstockstrat users hold `admin`, and who links? The spec links only `david` → the seeded admin row | **`list-users` in Task 4.** Link `david` to `admin@localhost`. Any other admin either links in the same `both` window or is recorded as break-glass-only (§ 6 rollback) before `oidc` | Spec § 7 (`:268`); cutover runbook § 5 step 3 |
| 8 | How do we test "an unlinked SSO user is refused on web and MCP"? Nobody but `david` is in `xstockstrat-staging` (spec § 7) | **Add `david_admin` to `xstockstrat-staging` in lldap for the test**, then remove the group. Web: an SSO login in `david_admin`'s browser profile shows `not linked`. MCP: a manual PKCE token for `david_admin` (the P1 Q12 flow) gets `403 not linked` on `POST /agent`. Unlinking `david` instead would need a new `both` window, which cuts the connector off (spec § 3) | Spec § 7, decision 24, decision 26 (lldap owns people; a membership change is the admin's); `mcp-tools.md:121-122` |
| 9 | Which client does the Claude.ai connector use for the pass condition: full, read-only, or both? | **Full (`xstockstrat-staging-mcp-claude`).** It is the steady-state connector and the client P1 S5 already exercised. Adding `-ro` is optional and not a pass item | Spec § 4.6 (`:158-159`); `mcp-tools.md:97-100` |
| 10 | Does an existing Claude.ai connector on staging (DCR against the built-in server) get edited or replaced? | **Replace it.** Add a new connector with the Authelia client ID once staging is in `oidc`, and remove the old one: its grant stops working in `oidc` anyway | Cutover runbook § 5 step 4; `mcp-tools.md:86-95` |
| 11 | The Authelia-stopped check stops the portal for every client: today only xstockstrat-staging, since webhooks-mcp is P4. Gatus fires `authelia-public`, `authelia-discovery` and `authelia-jwks` while it is stopped. Accept? | **Accept.** Keep the stop to ≤ 2 min; portal sessions end at the restart, which is expected | `sso.md` § 8 (`:144-145`); `gatus-config.yaml.j2:78-104` |
| 12 | What counts as "still works after > 2 h idle"? | **One `tools/call` of a read-only tool (`list_strategies`) at T0 + 2 h 05 min, with no re-authentication prompt.** The access token has lived 1 h, so this proves the 30-day refresh | Spec § 4.5; `main.yml:153-160`; `mcp-tools.md:1250` |
| 13 | How far does the `local` rehearsal go? | **`oidc` → `local` → password login works and the built-in AS metadata answers 200 → back to `oidc` → the connector works again (reconnect if Claude.ai asks).** End state `oidc` | Spec § 10 P2 "switching to `local` without a login rehearsed"; `sso.md` § 6 rollback (`:115-117`) |
| 14 | Where does the P2 record land, and is any xstockstrat change wanted? | **A follow-up homelab-edge docs PR (Task 7), as in P1 (P1 Q17). No xstockstrat PR:** the config rows are operational data, and its runbooks already describe these steps generically (`auth.example.com` is an OSS example) | P1 plan Owner answers row 17; xstockstrat cutover runbook § 2 |
| 15 | Add a mode-pinned probe, for example `authorization_servers[0] == https://auth.public…` on `staging-mcp-oauth-metadata`? | **No.** It would alert on every legitimate rollback to `local`/`both`, and spec § 4.9 has no P2 probe | Assumption 4; `oauth_metadata.py:22-44` |
| 16 | `sso.md` § 6 step 2 says `agent/oidc.issuer` is "(empty in `oidc`, the agent answers 503)". That sentence reads as "leave it empty", but xstockstrat's runbook says to set it to the issuer. Fix the wording? | **Yes (Task 2).** Set it to the issuer. The sentence becomes "an empty or invalid value makes every `oidc` MCP call answer 503" | `sso.md:104-105`; xstockstrat cutover runbook § 2 (`:36`); `oauth_metadata.py:22-30`; `mcp-tools.md:116-117` |
| 17 | Is P1 Task 10 a hard gate? P1 Task 10 covers the four people in lldap, `david`'s `sub` copied, and the wipe and mismatch checks with their exact restores | **Yes, Steps 6–9 done.** The P1 record docs PR need not be merged. P2 touches `david`'s identity, so his `sub` must already be pinned in both places | Spec § 10 P2 "people created in lldap (P1)"; P1 plan Task 10 |

## Review Focus

1. **A confirm request reaching the UI unthrottled.** The confirm POST must share the counter with `POST /api/auth/login` on the same site, stay independent between prod and staging, and leave `GET /accounts/api/sso/confirm` (and `/accounts/api/sso`, `/accounts/api/sso/link`) outside both zones. Pinned by Task 1's routing cases.
2. **A client secret in a log.** The plain `authelia_client_secret_xstockstrat_staging_web` must go only from `sops decrypt` to the clipboard to config-ui's password field. Expected: no step prints it, and the secret row shows `[secret]` (Task 4 Step 6).
3. **A wrong `--environment`.** Every database holds both environments' rows, and staging's services read `staging` (`APPLICATION_ENV=development`). Expected: each `set-auth-mode` output shows the `staging` row changed and the `production` row unchanged (cutover runbook § 5).
4. **Locking the owner out in `oidc`.** Before `oidc`, every staging admin is linked or recorded as break-glass-only (Q7), and the rollback command is tested (Task 6 Step 6).

## File Structure

| Repo | File | Change |
|---|---|---|
| homelab-edge | `roles/stack/templates/Caddyfile.j2` | `/accounts/api/sso/confirm` added to the two login-zone matchers in `public_routes` (`:152-171`) |
| homelab-edge | `scripts/caddy-routing-test.sh` | confirm-zone cases for prod and staging |
| homelab-edge | `docs/runbooks/sso.md`, `docs/decisions.md` | § 6 gate 1 done, the `agent/oidc.issuer` wording (Q16), the staging record; the login-hardening line (`decisions.md:14-15`) |
| homelab-edge | `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md` | P2 record (Task 7, follow-up docs PR) |
| xstockstrat | — | none (Q14). Staging config rows and `auth.mode` are data, set by the owner |

---

### Task 0: Gates (owner; no repo change)

- [ ] **Step 1:** P1 Task 10 Steps 6–9 are done (Q17). In lldap's UI, `david` is in `sso_people` and `xstockstrat-staging`. Pass: the last deploy's "Sub reconcile summary" lists `david` under `unchanged`.
- [ ] **Step 2:** The owner has answered Q1–Q17 in the execution PR.

---

### Task 1 (homelab-edge): the Link confirmation joins the login zones

**Files:**
- Modify `roles/stack/templates/Caddyfile.j2` (`:150-171`).
- Modify `scripts/caddy-routing-test.sh`: after `:142` (prod) and after `:282` (staging).

**Interfaces — Produces:** both matchers read `path /api/auth/login /accounts/api/sso/confirm`. The zones, the method and the `@login_api` handle are unchanged, and the confirm route keeps reaching the UI through the `ui_public` catch-all (`Caddyfile.j2:270-281`).

- [ ] **Step 1: Failing tests** in `caddy-routing-test.sh`. Each case uses a loopback alias no other case uses: the file uses `.3` as `req`'s default (`:90`), `.4`, `.5`, `.7` and `127.0.0.$n` from 21 up (`:177-182`). Before adding a case, check with `grep -n 'interface 127' scripts/caddy-routing-test.sh` that `.8`, `.9` and `.10` are free.
  - `expect "Link confirm shares the login zone (prod)" "200 200 200 200 200 429"` sends 3× `POST /api/auth/login` and then 3× `POST /accounts/api/sso/confirm` from `127.0.0.8`.
  - `expect "staging Link confirm limit (5/min) 429" "200 200 200 200 200 429"` sends 6× `POST /accounts/api/sso/confirm` from `127.0.0.9` with `sreq`.
  - `expect "staging confirm limit is independent of prod" "200 ui POST /accounts/api/sso/confirm"` sends one staging confirm from `127.0.0.8` after the prod case.
  - `expect "GET on the confirm path is outside the login zone" "200 ui GET /accounts/api/sso/confirm"` sends 6× GET from `127.0.0.10` and expects the last one to answer 200.
- [ ] **Step 2: Run** `scripts/caddy-routing-test.sh edge-caddy:ci` locally if Docker and `sudo` are there. Otherwise push the test-only commit and read the `caddy` job. Expected: the three POST cases FAIL, with six `200`s and no `429`. The GET case PASSES.
- [ ] **Step 3: Implement.** Add `/accounts/api/sso/confirm` to the `path` line of both `{args[1]}_login` and `{args[1]}_login_hourly`. Update the comment above them (`:151`) so it still states the constraint in ≤ 2 lines, for example: "Password POSTs (login, Link confirm) have no lockout or MFA upstream; these two zones are the only brute-force control."
- [ ] **Step 4: Run** the routing test again, plus `yamllint -s .` and `ansible-lint`, the `lint` job's commands at `.github/workflows/ci.yml`. Expected: every case PASSES and the CI Gate is green on the PR head.
- [ ] **Step 5: Commit** `feat(sso): rate-limit the xstockstrat Link confirmation with the login zones`.

---

### Task 2 (homelab-edge): docs for the edge change

**Files:**
- Modify `docs/runbooks/sso.md` § 6: the gate 1 line (`:97`) says it is in place; step 2 (`:104-105`) is fixed per Q16.
- Modify `docs/decisions.md:14-15`: the login-hardening entry names both password POSTs.

- [ ] **Step 1:** Make the edits. Do not restate xstockstrat's runbook; link it (`sso.md:107` already does).
- [ ] **Step 2: Verify (Teardown).** Re-read every touched paragraph against `Caddyfile.j2`. Expected: `grep -n "sso/confirm" roles/stack/templates/Caddyfile.j2 docs/runbooks/sso.md docs/decisions.md` lists the matcher and both docs.
- [ ] **Step 3: Commit** `docs(sso): the confirm zone and agent/oidc.issuer at the xstockstrat cutover`. Open one PR with Tasks 1–2 to `main`, ready for review.

---

### Task 3 (owner): merge, deploy, prove the zone on the deployed edge

- [ ] **Step 1:** CI Gate green on the PR head (`lint`, `secrets`, `caddy`, `sso`). Merge to `main`.
- [ ] **Step 2:** With the owner's go-ahead, run **deploy** from `main` (`tailnet`, as in P1 Task 10 Step 4), then **verify** (`workflow_dispatch`). Pass: both green. The staging step still shows `7x POST /api/auth/login -> … 429` (`verify.yml:98-100`).
- [ ] **Step 3:** From an off-tailnet client, in a minute in which you have not used staging's login:

  ```sh
  u=https://xstockstrat-staging.public.natanddavehouse.com
  for _ in 1 2 3 4 5 6; do curl -s -o /dev/null -w '%{http_code} ' -X POST -H 'content-type: application/json' -d '{}' "$u/accounts/api/sso/confirm"; done; echo
  ```

  Pass: the sixth code is `429`. The first five are the app's answer in `local`, a `404` outside `both` (345 product-spec `:272`). Paste the line into the PR. **Gate 1 is met.**

---

### Task 4 (owner): staging pre-checks, gates 3 and 4, config rows

Run from `macbook-pro` on the tailnet. Set the connection once per shell (Q5). The password stays in the environment, never in argv or history:

```sh
export SOPS_AGE_SSH_PRIVATE_KEY_FILE=~/keys/sops_admin   # in a homelab-edge checkout
export DATABASE_URL="postgresql://superadmin:$(sops decrypt --extract '["db_role_superadmin_password"]' secrets/edge.sops.yaml)@db.ts.natanddavehouse.com:5432/xstockstrat-staging?sslmode=require"
# then, in an xstockstrat main-dev checkout: uv run scripts/manage-users.py …
```

The superadmin password is `token_urlsafe` (`scripts/init_secrets.py:76`), so it is URL-safe as it stands.

- [ ] **Step 1: 345 is on homelab staging (Q4).**

  ```sh
  psql "$DATABASE_URL" -Atc "select environment, value_data from config.config_values where namespace='identity' and key='auth.mode' and user_id is null order by 1"
  ```

  Pass: exactly `production|local` and `staging|local`. Zero rows means migration 060 has not run: deploy `main-dev` to homelab staging first.
- [ ] **Step 2: Admins (Q7).** `uv run scripts/manage-users.py list-users`. Record how many users hold `admin` and which will link. Record counts and the decision only, not emails.
- [ ] **Step 3: Gate 3 on the deployed config.** On the edge:

  ```sh
  sudo grep -n -A14 "client_id: 'xstockstrat-staging-web'" /opt/edge/authelia/config/configuration.yml | grep -v client_secret
  sudo grep -n -A2 'claims_policies' /opt/edge/authelia/config/configuration.yml
  ```

  The config path is from `roles/stack/tasks/main.yml:368`; `client_secret` is filtered out even though it is a hash. Pass: `scopes: [openid, profile, email]`, `claims_policy: web`, `redirect_uris: ['https://xstockstrat-staging.public.natanddavehouse.com/api/auth/sso/callback']`, `web: id_token: [email, preferred_username]`.
- [ ] **Step 4: Gate 4 (Q6).** Run `uv run scripts/manage-users.py reset-password admin@localhost` and type the owner's new password at the prompt (`manage-users.py:170-214`). Pass: "Password updated for admin@localhost". Then sign in at `https://xstockstrat-staging.public.natanddavehouse.com/auth/login` with it.
- [ ] **Step 5: The VM reaches the issuer (cutover runbook § 3.4).** Run on the staging VM (`ssh david_admin@xstockstrat-staging.ts.natanddavehouse.com`), from inside the UI container so its DNS and egress are the ones that matter:

  ```sh
  sudo docker exec "$(sudo docker ps -qf label=com.docker.compose.service=xstockstrat-ui)" node -e \
    'fetch("https://auth.public.natanddavehouse.com/.well-known/openid-configuration").then(r=>r.json()).then(j=>console.log(j.issuer))'
  ```

  Pass: `https://auth.public.natanddavehouse.com`, exactly.
- [ ] **Step 6: Config rows.** Signed in as an admin at `https://xstockstrat-staging.public.natanddavehouse.com/config-ui/ui`, environment `staging`, set:

  | Namespace / key | Value |
  |---|---|
  | `ui` / `oidc.issuer` | `https://auth.public.natanddavehouse.com` |
  | `ui` / `oidc.client_id` | `xstockstrat-staging-web` |
  | `ui` / `oidc.client_secret` | the SOPS value `authelia_client_secret_xstockstrat_staging_web` (the plain value, **not** `_pbkdf2`) |
  | `ui` / `oidc.mcp_client_id` | `xstockstrat-staging-mcp-claude` |
  | `ui` / `oidc.mcp_client_id_readonly` | `xstockstrat-staging-mcp-claude-ro` |
  | `ui` / `oidc.mcp_client_id_claude_code`, `…_claude_code_readonly` | left empty |
  | `agent` / `oidc.issuer` (at `/config-ui/agent`) | `https://auth.public.natanddavehouse.com` |

  The secret goes to the clipboard and never to the screen. Basis: `sso.md` § 9.9 step 3, `secrets/README.md`.

  ```sh
  sops decrypt --extract '["authelia_client_secret_xstockstrat_staging_web"]' secrets/edge.sops.yaml | tr -d '\n' | pbcopy
  ```

  Paste it into the password field, save, then clear the clipboard. The UI reads it only through `GetSecret` (060 migration `:57-67`).

  Pass:

  ```sh
  psql "$DATABASE_URL" -Atc "select namespace, key, case when is_secret then (value_encrypted is not null)::text else value_data end from config.config_values where environment='staging' and user_id is null and key like 'oidc.%' order by 1,2"
  ```

  Expected: the six values above, with `ui|oidc.client_secret|true`.

---

### Task 5 (owner): `both`, link, then SSO; password login with Authelia stopped

- [ ] **Step 1:**

  ```sh
  uv run scripts/manage-users.py set-auth-mode both --environment staging --reason "P2: SSO linking window"
  ```

  Pass: the "after" table shows `staging both` and `production local`.
- [ ] **Step 2: SSO start works end to end.** This proves issuer, client ID, secret and the VM back-channel:

  ```sh
  curl -s -o /dev/null -D - -X POST -H 'Origin: https://xstockstrat-staging.public.natanddavehouse.com' \
    https://xstockstrat-staging.public.natanddavehouse.com/api/auth/sso/start | grep -i '^location:' | sed 's/[?&]\(state\|nonce\|code_challenge\)=[^&]*//g'
  ```

  Pass: a `location` on `https://auth.public.natanddavehouse.com/api/oidc/authorization` with `client_id=xstockstrat-staging-web`. A `location` on `/auth/login?error=not_configured` means a Task 4 Step 6 row is wrong. Basis: `services/xstockstrat-ui/src/app/api/auth/sso/start/route.ts:22-31,52`.
- [ ] **Step 3: Link.** In `david`'s browser profile:
  1. sign in with `admin@localhost`'s password;
  2. open `/accounts/sso` and press **Link SSO**;
  3. at the portal, sign in as `david` again (password and second factor), then accept the consent page (`sso.md` § 9.7);
  4. confirm "You are linking …" with the xstockstrat password.

  Pass: `/accounts/sso` shows the link. This is also S17 on the deployed portal (Assumption 2). On a refusal, read WARN `sso_link_refused` and `identity.link.failures{reason}` (cutover runbook § 5 Verification).
- [ ] **Step 4: SSO login.** Sign out, then press **Sign in with SSO**. Pass: you land signed in as the `admin@localhost` user. Any other admin in Q7 links now, in the same way.
- [ ] **Step 5: Authelia stopped (Q11).** On the edge, `sudo docker compose -f /opt/edge/compose.yml stop authelia`. Reload `/auth/login`, then sign in with the password. Then `sudo docker compose -f /opt/edge/compose.yml start authelia`.

  Pass:
  - the login page renders;
  - the password login lands in the app;
  - `curl -fsS https://auth.public.natanddavehouse.com/api/health` answers again after the start.

  Record the stopped interval (≤ 2 min) and the Gatus alerts it raised.

---

### Task 6 (owner): `oidc`, the connector, the unlinked refusal, the rehearsal

- [ ] **Step 1:**

  ```sh
  uv run scripts/manage-users.py set-auth-mode oidc --environment staging --reason "P2: SSO steady state"
  ```

  Pass:
  - the "after" table shows `staging oidc`;
  - `curl -fsS https://xstockstrat-staging.public.natanddavehouse.com/.well-known/oauth-protected-resource/agent | jq -c '{resource, authorization_servers}'` prints `{"resource":"https://xstockstrat-staging.public.natanddavehouse.com/agent","authorization_servers":["https://auth.public.natanddavehouse.com"]}`;
  - `curl -s -o /dev/null -w '%{http_code}\n' https://xstockstrat-staging.public.natanddavehouse.com/agent/.well-known/oauth-authorization-server` prints `404` (xstockstrat `main.py:218-222`);
  - the password form is gone from `/auth/login`.
- [ ] **Step 2: Connector (Q9, Q10).** In Claude.ai:
  1. add a custom connector with URL `https://xstockstrat-staging.public.natanddavehouse.com/agent`;
  2. in its advanced settings, set OAuth Client ID `xstockstrat-staging-mcp-claude` with no secret (`mcp-tools.md:92-95`);
  3. connect, sign in as `david`, and accept the consent page.

  Pass: the tool list appears, and `list_strategies` returns. Note the time T0.
- [ ] **Step 3: Unlinked refusal (Q8).**
  1. In lldap's UI, add `david_admin` to `xstockstrat-staging`.
  2. **Web:** in `david_admin`'s browser profile, press **Sign in with SSO**. Pass: refused with `not linked`.
  3. **MCP:** run the P1 Q12 flow as `david_admin`.
     - Open `https://auth.public.natanddavehouse.com/api/oidc/authorization?response_type=code&client_id=xstockstrat-staging-mcp-claude&redirect_uri=https%3A%2F%2Fclaude.ai%2Fapi%2Fmcp%2Fauth_callback&scope=xstockstrat.read&state=<random>&code_challenge=<S256 of a local verifier>&code_challenge_method=S256&resource=https%3A%2F%2Fxstockstrat-staging.public.natanddavehouse.com%2Fagent`.
     - After consent, read `code` from the blocked `claude.ai` redirect.
     - `curl -s https://auth.public.natanddavehouse.com/api/oidc/token -d grant_type=authorization_code -d client_id=xstockstrat-staging-mcp-claude -d code="$code" -d redirect_uri=https://claude.ai/api/mcp/auth_callback -d code_verifier="$v" | jq -r .access_token` and store the result in `AT`; never echo it.
     - `curl -s -w ' %{http_code}\n' -X POST -H "Authorization: Bearer $AT" -H 'content-type: application/json' -H 'accept: application/json, text/event-stream' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' https://xstockstrat-staging.public.natanddavehouse.com/agent`, then `unset AT code v`.

     Pass: `not linked 403` (`mcp-tools.md:121-122`).
  4. Remove `david_admin` from `xstockstrat-staging` in lldap. Pass: lldap shows only `david` in the group.
- [ ] **Step 4: Idle (Q12).** At T0 + 2 h 05 min, with no use in between, call `list_strategies` through the connector. Pass: it returns with no sign-in prompt.
- [ ] **Step 5: Rehearse `local` (Q13).**
  1. `uv run scripts/manage-users.py set-auth-mode local --environment staging --reason "P2: rollback rehearsal"`. No app login is needed. Pass: the "after" table shows `staging local`, `/auth/login` shows the password form, the password login works, and `/agent/.well-known/oauth-authorization-server` answers 200.
  2. `uv run scripts/manage-users.py set-auth-mode oidc --environment staging --reason "P2: back to steady state"`. Pass: Step 1's checks again, and `list_strategies` works through the connector (reconnect if Claude.ai asks).
- [ ] **Step 6:** End state `staging oidc`. Remove the old DCR connector (Q10).

---

### Task 7 (homelab-edge): P2 record (follow-up docs PR)

**Files:** Modify the spec (§ 10 P2 row "Done <date>" plus a P2 record table under § 11 in the P0 record's shape) and `docs/runbooks/sso.md` (status line, § 6 "staging done").

- [ ] **Step 1:** One row per check, from Tasks 3–6, giving the observed output and the consequence. Record outcomes only: no `sub`, email, token, code or secret.
- [ ] **Step 2: Verify (Teardown).** Re-read both docs against the code and the outputs. Open the PR to `main`, ready for review.

## Done when

The § 10 P2 pass condition holds. Each part has its output in the Task 1–2 PR (Task 3) or in the Task 7 PR:
- gate 1, the deployed confirm zone (Task 3 Step 3); gates 2–4 (Assumption 2, Task 4 Steps 3–4);
- in `both`: password, link, SSO; password login with Authelia stopped (Task 5);
- in `oidc`: connector connects and lists tools, and works after > 2 h idle; an unlinked SSO user is refused on web and MCP (Task 6 Steps 2–4);
- `local` rehearsed without a login and returned to `oidc` (Task 6 Step 5).

## Not in this plan

- **P3 prod:** the same steps on `xstockstrat.public.<domain>` once the prod app runs. The confirm zone is already deployed there (Assumption 3).
- **P4–P6.**
- Retiring the built-in OAuth routes at the edge: they stay for `local`/`both` (spec § 5 xstockstrat row).
