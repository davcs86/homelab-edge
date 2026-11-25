# Hermes Agent + local Ollama — design (2026-11-25)

Status: **draft for the owner's approval, not built.** This design replaces the unwritten "local Ollama + OpenClaw" plan, which
survives only in `policy/policy.hujson` (`tag:llm`, `tag:agent`). Hermes Agent (Nous Research) takes OpenClaw's place, and its
dashboard sits behind the Authelia SSO in `2026-11-23-authelia-sso-respec.md` (the "SSO spec"). Open items: § 11.

## 1. Intent

The owner wants a personal agent that runs at home and works on xstockstrat for him:
- he chats with it from his phone or desktop;
- it runs **scheduled jobs against xstockstrat** through its MCP and reports the results to him;
- its web dashboard is behind the same SSO login as the rest of the homelab.

A local model handles the scheduled jobs. A cloud model handles chat, where speed and quality matter (owner, 2026-11-24, option B).

## 2. Goals, non-goals, anti-goals

**Goals**
- Hermes reachable in three ways only:
  - the dashboard at `hermes.private.<domain>`, behind Authelia OIDC;
  - a private Discord bot;
  - its own scheduler.
- Scheduled jobs run on Ollama on the UM890 Pro, call the xstockstrat MCP, and post their results to a Discord channel.
- Chat runs on a cloud model through OpenRouter.
- No new public port: rule 2 holds (Discord is outbound only).
- Everything converged by Ansible from this repo and pinned (rules 4 and 5). Secrets only in SOPS (rule 3).

**Non-goals**
- The catalogue of scheduled jobs. The owner creates jobs at runtime in Discord or the dashboard. The repo holds none.
- Other chat platforms (Telegram is demoted: owner, 2026-11-24; WhatsApp and Slack were rejected, § 4).
- xstockstrat calling Ollama. The existing `tag:xstockstrat → tag:llm:11434` grant stays unchanged, but xstockstrat has no LLM
  caller today (no `ollama`, `11434` or LLM base URL in its tree).
- Granular xstockstrat scopes. Those are xstockstrat feature 363 (§ 10).

**Anti-goals** (each of these is a failure, even if everything else works)
- Any way into Hermes that skips both Authelia and the Discord allowlist. This includes the API server (`:8642`), Telegram and
  the WhatsApp bridge.
- Hermes running shell commands on its host with no isolation (`terminal.backend: local`).
- Ollama reachable without a tailnet grant: no LAN listener and no public name.
- An agent able to trade or change strategies before feature 363 ships (§ 6.4, interim guard).

## 3. Decisions

| # | Decision | Basis |
|---|---|---|
| H1 | Hermes Agent replaces OpenClaw. `tag:agent` keeps its name; its tagOwners comment becomes "Hermes Agent VM" | owner, 2026-11-24 |
| H2 | Interfaces: dashboard, Discord, scheduler | owner, 2026-11-24 |
| H3 | **Discord** is the chat app. Reasons: outbound Gateway WebSocket (no public URL), native Approve/Deny buttons, threads, role and user allowlists | owner delegated the pick, 2026-11-24; § 4 |
| H4 | The **API server stays disabled** (`API_SERVER_ENABLED` unset, its default). Telegram, Slack and WhatsApp variables are never set | anti-goal |
| H5 | Topology **A**: LXC container `llm` (`tag:llm`) runs Ollama with the Radeon 780M shared through `/dev/dri`. VM `hermes` (`tag:agent`) runs Hermes in Docker | owner, 2026-11-25; § 4 |
| H6 | Models: chat on **OpenRouter** (`OPENROUTER_API_KEY`). Scheduled jobs pinned to Ollama through Hermes's `cron.model` / `cron.model_provider` (`provider: custom`, `base_url: http://llm.ts.<domain>:11434/v1`) | owner, 2026-11-24/25 (B, "likely OpenRouter") |
| H7 | Hermes's xstockstrat identity is **`david`** for now, in **both prod and staging** (separate URLs and groups), through its own OIDC client per environment: `xstockstrat-mcp-hermes` and `xstockstrat-staging-mcp-hermes` | owner, 2026-11-24 / 2026-11-25 |
| H8 | **Interim guard:** the xstockstrat MCP entry in Hermes allows only read tools (`tools.include`) until 363 ships | owner, 2026-11-25 |
| H9 | The dashboard is an Authelia **public PKCE client** `hermes`, gated on a new lldap group **`hermes`** that holds only `david`. Hermes has no allowlist of its own: anyone the identity provider admits is owner-level | Hermes docs (dashboard OIDC: public clients only, no allowlist) |
| H10 | Command sandbox: `terminal.backend: docker`, `docker_network: false`, approvals `manual`, `cron.allow_agent_scheduling: false` | anti-goals |
| H11 | Memory: about 8 GiB for `llm`, 3 GiB for `hermes`, inside the 12 GiB the owner has free (18 GiB is already allocated) | owner, 2026-11-25 (12 GiB free) |

## 4. Approaches

**Chat app.**
- WhatsApp is rejected in both forms:
  - Baileys is unofficial: a ban risk, an extra Node bridge, and re-pairing when the session expires.
  - The official Cloud API needs a public webhook, and its 24-hour window fails scheduled deliveries after a quiet day (Graph
    error `131047`).
- Slack is a close second. It also connects outbound only (Socket Mode). It loses on approvals: replies go into threads, which
  block slash commands, so approving means typing `!approve`.
- Discord wins: approvals are buttons.

**Topology.**

| | Shape | Trade-off |
|---|---|---|
| **A (chosen)** | `llm` container plus `hermes` VM, two new roles | Agent and model are isolated from each other; sharing the GPU with a container is reliable; Docker sandboxing gets a real kernel. Cost: two guests and about 0.5 GiB of VM overhead |
| B | One VM with the 780M passed through | iGPU passthrough on the 780M is fragile (reset bug), and one compromise exposes both |
| C | A, but Hermes deployed through the `app-deploy` gate (`apps` group) | That gate exists to deploy an app repo's CI output. Hermes has no app repo |

**Model split.**
- Rejected: Ollama only. Interactive chat on a 780M with a 64k context window is slow, and tool use is capped by what fits in about 8 GiB.
- Rejected: cloud only. The owner chose B.

## 5. Constraints

- **Hermes floor:** a context window of at least 64,000 tokens for the main model. Docs don't say whether a model pinned to a job
  is held to the same floor (spike HS3). Ollama defaults to 4,096 tokens, so `OLLAMA_CONTEXT_LENGTH=65536` is required.
- **Hardware:** UM890 Pro: Ryzen 9 8945HS, Radeon 780M (`gfx1103`, no official ROCm), shared DDR5.
  - Vulkan is the expected backend (HS1).
  - About 8 GiB for weights plus a 64k cache limits Ollama to roughly 7–8B dense models at 4-bit. The model is chosen after
    measurement (§ 11).
- **Pins:** third-party pins must be at least two weeks old (`versions.yml` header). Hermes `v0.23.3` (released 2026-11-19)
  qualifies from 2026-12-03. Ollama follows the same rule.
- **SSO:** the dashboard needs SSO phase P1. The MCP move to Authelia needs P2 for staging and P3 for prod. Hermes is SSO
  phase **P7**.

## 6. Design

### 6.1 Hosts (`docs/runbooks/home-side.md` gains both rows)
- **`llm`** (`tag:llm`): an LXC container like `db`. Debian 13, about 8 GiB, `/dev/dri` bind-mounted. Ollama pinned by release
  checksum, as a systemd service:
  - listens on its **tailnet address only** (`OLLAMA_HOST=<tailnet IP>:11434`);
  - `OLLAMA_CONTEXT_LENGTH=65536`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`;
  - the Vulkan switch, as HS1 establishes it.
  - Models are pulled by the role from a pinned list in `versions.yml` (name plus digest).
  - The container still initiates nothing on the tailnet. Model downloads go straight to the internet, as the policy comment says.
- **`hermes`** (`tag:agent`): a Debian 13 VM, 3 GiB, 2 vCPU. It uses the existing `tailnet_host` and `docker` roles plus a
  new `agent_host` role that renders one compose project:
  - service `hermes` from `nousresearch/hermes-agent:<ver>@sha256:…` (`versions.yml`);
  - `HERMES_DASHBOARD=1`, dashboard bound to the **tailnet address** on `9119`;
  - named volume `hermes-data` at `/opt/data`;
  - `config.yaml` rendered by Ansible; secrets through `env_file` (EDGE-04 shape).
- **Playbook:** `playbooks/agent.yml` (both hosts), run by a new `agent-host` workflow as an ephemeral `tag:ci` node with
  `production` approval, the same shape as `db-host.yml`.
  - First run is `bootstrap` over the LAN, as `app.yml` documents.
  - Guest IDs and addresses: § 11.

### 6.2 Hermes configuration (rendered `config.yaml` + `.env`)
- **Main model:** OpenRouter, model name per § 11.
- **Scheduled jobs:** `cron.model_provider: custom` plus `cron.model: <ollama model>` with the Ollama `base_url`.
  - Jobs pinned this way never fall back, so a dead Ollama fails the job loudly instead of silently changing model and cost.
  - No `fallback_providers` are configured.
  - This is not a privacy boundary. Chat (OpenRouter) uses the same xstockstrat MCP, and job output goes to Discord; the owner
    accepted both with option B.
- **Discord:**
  - `DISCORD_BOT_TOKEN` from SOPS;
  - `DISCORD_ALLOWED_USERS` = the owner's ID;
  - `DISCORD_HOME_CHANNEL` = the job-output channel;
  - `DISCORD_REQUIRE_MENTION` left at its default (true);
  - Discord-side settings: the bot is in one private server, "Public Bot" is off, Message Content and Server Members intents are on.
- **Dashboard:**
  - `HERMES_DASHBOARD_OIDC_ISSUER=https://auth.public.<domain>`, `HERMES_DASHBOARD_OIDC_CLIENT_ID=hermes`;
  - `HERMES_DASHBOARD_PUBLIC_URL=https://hermes.private.<domain>`;
  - `dashboard.trusted_proxies` = the edge's tailnet address only;
  - no basic-auth username or password is ever set.
- **Off:** `API_SERVER_ENABLED` unset; no Telegram, Slack or WhatsApp variables.
- **Sandbox:** H10. Hermes reaches the VM's Docker daemon to start sandbox containers (HS8). The **VM is the security boundary**:
  a compromised Hermes gets the VM and its secrets. Tailnet grants confine the VM (§ 6.5).
- **MCP servers `xstockstrat` (prod) and `xstockstrat-staging`**, one entry per environment, identical apart from the URL:
  - `url` = that environment's MCP endpoint (public name or tailnet; HS5);
  - `auth: oauth`;
  - `tools.include` = the read tools only (H8): every `list_*`, `get_*` and `query_*` tool, plus `screen_symbols`. Write and
    trading tools are excluded, including `set_strategy_live`, `manage_*`, `ingest_signal`, `emit_alert`, `trigger_backfill`,
    `cancel_backfill`, `set_config`, `set_user_metadata`, `admin_set_user_metadata`, `instantiate_template` and `test_formula`.
    The exact list comes from the live tool catalog at build time.
  - **Deferred to H2 (owner, 2026-11-25):** whether `run_backtest`, `run_fundamentals_scan`, `extract_email_content` and
    `extract_website_content` join the list. Until the owner decides, all four stay excluded. Changing it later is a one-line
    edit to the rendered `config.yaml` (§ 11).
  - Tokens live in `hermes-data` (`mcp-tokens/`), one per server, each obtained once with `hermes mcp login <server>` (HS4).

### 6.3 Dashboard login
- **Authelia client** (SSO spec § 4.6, new row): `hermes`, public, PKCE S256, group `hermes`, scopes `openid profile email`,
  redirect `https://hermes.private.<domain>/auth/callback`.
- **lldap:** new group `hermes`, which the deploy ensures exists (decision 26). Its only member is `david`; the admin adds him in
  the UI. SSO spec § 7 gains `hermes` in `david`'s row.
- **Authorization rule:** only `hermes` group members get a token for client `hermes`, the same per-app group pattern as every
  other client.

### 6.4 Hermes as an xstockstrat MCP client
Each environment moves on its own clock: staging at SSO P2, prod at P3.
- **Before its SSO cutover** (that environment's `auth.mode=local`): Hermes registers dynamically with that environment's
  built-in OAuth server and signs in as `david`. Each environment's `agent.oauth.allowed_redirect_uris` (today only the two
  Claude.ai callbacks, `home-side.md`) gains Hermes's loopback callback. This is a config-value change on the xstockstrat side,
  not a new key.
- **After its cutover:**
  - a new Authelia client per environment, `xstockstrat-mcp-hermes` (group `xstockstrat`) and `xstockstrat-staging-mcp-hermes`
    (group `xstockstrat-staging`): public PKCE, scopes **`xstockstrat.read`** and `offline_access` (like the `-ro` clients),
    loopback redirect (or device flow, HS4), audience that environment's `${APP_URL}/agent`;
  - with that scope, 345 FR-7 narrows the token to READ on the server side, so after cutover `tools.include` is the second
    guard, not the only one (my call, in line with the owner's "read access for MCP");
  - separate clients from Claude's, so revoking Hermes never logs out Claude sessions;
  - Hermes logs in again once per environment.
- **Refresh tokens:** 30 days, rotated on use (SSO spec § 4.5). A dead refresh token parks the server silently, so § 6.7 alerts on it.
- **Interim guard (H8):** stays until 363 ships. Then it is the owner's call to keep it or rely on 363's read-only default.

### 6.5 Tailnet policy (`policy/policy.hujson`, `scripts/policy-test.sh`)
- **Comments:** `tag:agent` → "Hermes Agent VM"; the "OpenClaw → xstockstrat MCP" comment renamed.
- **New grants:**
  - `tag:edge → tag:agent tcp:9119`: Caddy proxies the dashboard;
  - `tag:agent → tag:edge tcp:443`: heartbeat push to Gatus via `status.private`. Grants are per port, so Caddy confines it
    (§ 6.6);
  - `tag:ci → tag:agent, tag:llm tcp:22`: converge;
  - `tag:agent → tag:xstockstrat-staging tcp:9000`: mirrors the prod grant below, under the same HS5 condition.
- **Kept:**
  - `tag:agent → tag:llm tcp:11434`;
  - `tag:agent → tag:xstockstrat tcp:9000`, until HS5 shows whether the MCP is reached by tailnet or public name. If public,
    both MCP grants (prod and staging) are removed;
  - `tag:xstockstrat → tag:llm tcp:11434`;
  - `tag:llm` initiates nothing.
- **Tests** gain these accepts and denies:
  - `tag:agent` accepted to `tag:xstockstrat-staging:9000` (while that grant exists);
  - `tag:agent` denied `tag:db:*`, `tag:edge:22`, `tag:xstockstrat:22`, `tag:xstockstrat:3000`,
    `tag:xstockstrat-staging:22` and `tag:xstockstrat-staging:3000`;
  - `tag:edge` accepted to `tag:agent:9119` and denied `tag:agent:22`;
  - `tag:ci` accepted to `tag:agent:22` and `tag:llm:22`.

### 6.6 Edge wiring
- `hermes_private_hostname: "hermes.{{ private_zone }}"`, wired in the four EDGE-07 places.
- The Caddy matcher sits under `*.{{ private_zone }}` after `import deny_staging_vm` and does
  `reverse_proxy hermes.ts.<domain>:9119` with no `trusted_proxies` change (EDGE-02). Caddy's overwritten XFF is what Hermes
  trusts.
- The access log drops query strings on this host (OIDC `code`/`state`), as decisions.md already does for OAuth routes.
- **`deny_agent_vm`** (my call; modelled on `deny_staging_vm`): requests from the `hermes` VM's tailnet address get 403 on every
  hostname except `POST /api/v1/endpoints/home_agent-heartbeat/external` on `status.private`. This keeps a compromised agent
  away from commsgun, lldap, ntfy, webhooks and its own dashboard.
- `caddy-routing-test.sh` and `portal-zones-test.py` pin the new name and the deny rule.

### 6.7 Monitoring (`docs/runbooks/alerts.md`)
- **Gatus `hermes-private`:** the dashboard through its private name answers its unauthenticated response (status per HS6).
- **Gatus external endpoint `home_agent-heartbeat`** (EDGE-10): a timer on `hermes` pushes every 5 min with
  `success=true` only while all three hold:
  - the Hermes container is healthy;
  - `GET http://llm.ts.<domain>:11434/api/version` answers;
  - the MCP check (HS10) confirms both xstockstrat tokens still work (prod and staging).

  Otherwise it pushes `success=false&error=<hermes|ollama|mcp-auth-prod|mcp-auth-staging>`. Silence for 11 min alerts.
- `llm` itself pushes nothing: the agent checks it, which keeps `tag:llm` silent on the tailnet.

### 6.8 Secrets (`secrets/edge.sops.yaml`, `secrets/README.md`, `no_log`)
- Owner-added:
  - `hermes_discord_bot_token`;
  - `hermes_openrouter_api_key`: a dedicated OpenRouter key with a credit limit (§ 11). The OpenRouter account must refuse
    providers that train on prompts.
- Hermes's own runtime secrets (MCP tokens, `auth.json`) stay in `hermes-data` and never enter SOPS.
- The dashboard client is public, so it has no secret.

### 6.9 Data
- `hermes-data` holds `state.db` (SQLite, WAL), memories, skills, cron jobs and MCP tokens.
- Backup: § 11 (owner). Never two gateways on one data directory.

## 7. Phases and pass conditions (rule 6)

| Phase | Gated on | Pass condition |
|---|---|---|
| H0 spike | — | Every HS item recorded here with its observed output. `llm` built by hand, throwaway, test secrets only |
| H1 `llm` | H0; model signed off (§ 11) | `ci` green incl. `policy-test.sh`; `agent-host` run converges `llm`; `ollama ps` shows the model on the GPU with a 65536 context; `tag:agent` reaches 11434 and `tag:edge`, members and the LAN do not |
| H2 `hermes` gateway | H1 | Discord: the owner's DM gets an answer; a second account's DM is refused; a sandboxed `curl` fails with no network; a risky command waits for the Approve button. A scheduled job pinned to Ollama runs a read-only report against each environment and posts to the home channel. A write tool is absent from both servers' tool lists. The owner settles the four deferred tools (§ 6.2). With Ollama stopped, the job fails and the heartbeat reports `ollama`. API port 8642 is closed (`ss -ltn`) |
| H3 dashboard | H2; SSO P1 | `david` logs in through Authelia; a person outside `hermes` is refused by Authelia; no basic-auth form exists; `hermes-private` Gatus probe green; four-place wiring checked (`verify`) |
| H4a MCP via Authelia, staging | SSO P2 | Hermes logs in again with `xstockstrat-staging-mcp-hermes`; a staging job still succeeds after more than 2 h idle (refresh rotates); a write tool called with that token (tools.include bypassed, test only) gets 403; revoking the client turns the heartbeat red with `mcp-auth-staging` while prod jobs keep working |
| H4b MCP via Authelia, prod | SSO P3 | The same checks against prod with `xstockstrat-mcp-hermes` and `mcp-auth-prod` |

## 8. Spike items (H0)
- **HS1** Ollama with Vulkan on the 780M in an LXC container with `/dev/dri`: the model sits on the GPU. Measure prompt-read and
  generation tokens per second at a 64k context for two or three candidates under an 8 GiB limit.
- **HS2** How the BIOS's dedicated GPU memory (UMA) and Linux's shared GPU memory (GTT) count against the container's memory
  limit; the right BIOS setting.
- **HS3** A job pinned to the Ollama `custom` provider is accepted (64k floor?) and completes a read-only MCP job five times out of five.
- **HS4** `hermes mcp login`:
  - against xstockstrat's built-in OAuth: dynamic registration plus loopback paste-back;
  - against Authelia: a pre-registered public client, paste-back or device flow (is the device grant supported?);
  - in both: refresh works headless.
- **HS5** The MCP URL: does the tailnet `:9000` URL pass Hermes's OAuth discovery checks (resource and audience must match), or
  must it be the public name?
- **HS6** Dashboard OIDC with Authelia: a public client, a `*.private` redirect, Caddy in front with `trusted_proxies`; what an
  unauthenticated request returns (for the probe).
- **HS7** Discord: approval buttons; a non-allowlisted DM is denied; the bot cannot be invited elsewhere.
- **HS8** The Docker sandbox from a containerised Hermes (socket access, `docker_network: false`), the sandbox image's size and
  memory.
- **HS9** Hermes's memory use with the sandbox under a 3 GiB VM.
- **HS10** A command that exits non-zero when the xstockstrat MCP token is parked (e.g. `hermes mcp test xstockstrat`).

## 9. Left to the builder
- Role internals, task names and variable names (follow EDGE-03/04/05 and the existing `tailnet_host`/`docker` role shapes).
- The exact `tools.include` list, built from the live tool catalog under the rule in § 6.2.
- The heartbeat script language and timer unit (`tools/pve-setup.py` heartbeat is the pattern).
- Docs at implementation:
  - a new runbook `docs/runbooks/hermes-agent.md` (Discord setup, login, re-login, rotation, recovery);
  - the CLAUDE.md context-guide row;
  - `home-side.md`, `alerts.md`, `secrets/README.md`, `versions.md`;
  - one `decisions.md` entry;
  - SSO spec amendments (§ 10).

## 10. Cross-repo handoff
- **xstockstrat feature 363 `sso-granular-scopes`** (`draft`, branch `feature/sso-granular-scopes`): granular scope catalog,
  read-only default per MCP client, full web access, app-side Scope Picker. Next step there: `/sdd-review sso-granular-scopes product-spec`.
- **xstockstrat config:** add Hermes's loopback callback to `agent.oauth.allowed_redirect_uris` per environment (H2).
- **SSO spec:**
  - § 4.6 gains the `hermes` and `xstockstrat[-staging]-mcp-hermes` rows;
  - § 7 adds `hermes` to `david`'s groups;
  - § 10 gains P7 (= H3/H4 here);
  - § 4.10's ensured groups gain `hermes`.

## 11. Open (owner, `TODO(provide)`)
1. The owner's Discord user ID and the home-channel ID.
2. The OpenRouter chat model and the key's credit limit.
3. The Ollama model, chosen from HS1/HS3's measurements.
4. Proxmox guest IDs, hostnames (proposed `llm`, `hermes`) and LAN addresses, and whether `llm`'s storage is `local-zfs` like `db`.
5. Backups of `hermes-data`. It holds memories, job definitions and tokens. Should it go to the backup host (a new grant
   `tag:agent → tag:backup:22` and an rrsync user), or is a rebuild without it acceptable?
6. At H2, when the first job is written: whether `run_backtest`, `run_fundamentals_scan` and the two `extract_*` tools join
   `tools.include` (§ 6.2). Recommendation: allow the two analysis tools (no trades, no strategy or config change) and keep
   the `extract_*` URL fetchers out (server-side request forgery under prompt injection).

## 12. Sources
Hermes docs:
[dashboard](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard) ·
[env vars](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) ·
[Docker](https://hermes-agent.nousresearch.com/docs/user-guide/docker) ·
[MCP](https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp) ·
[cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron) ·
[fallback providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers) ·
[providers](https://hermes-agent.nousresearch.com/docs/integrations/providers) ·
[API server](https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server) ·
[Discord](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/discord) ·
[Slack](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/slack) ·
[WhatsApp](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/whatsapp) ·
[WhatsApp Cloud](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/whatsapp-cloud) ·
[releases](https://github.com/NousResearch/hermes-agent/releases).

Hardware:
[UM890 Pro review](https://www.notebookcheck.net/Minisforum-EliteMini-UM890-Pro-review-A-powerful-mini-PC-with-AMD-Ryzen-9-and-whisper-quiet-cooling.982755.0.html) ·
[Radeon 780M](https://www.notebookcheck.org/AMD-Radeon-780M.771800.0.html) ·
[llama.cpp Vulkan](https://github.com/ggml-org/llama.cpp/discussions/10879).
