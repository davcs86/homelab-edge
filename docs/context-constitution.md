# homelab-edge — Constitution

Derived by `/context-constitution` (context-forge) on 2026-11-15; last refreshed 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `45e0b71` — the repo state this analysis reflects; a later
`refresh` reruns against a newer commit. This file captures the **non-obvious**: patterns the roles, templates and
workflows follow but never wrote down, the places that break them, and the contracts with `davcs86/webhooks-mcp`,
`davcs86/commsgun` and home-side pushers. It does **not** restate `CLAUDE.md`, `docs/decisions.md` (design and
Known traps), the runbooks or CI (see `## Pointers`). Most scars already live in `docs/decisions.md` § Known traps;
this file adds the patterns *between* them.

> **ID scheme (CF-N5).** The repo had no ID'd rules (`CLAUDE.md` `## Rules` 1–7 is a process list;
> `docs/decisions.md` is an unnumbered design/trap log). Codebase invariants use `EDGE-*`.

## Floor (`EDGE-*`) — never-do, non-overridable

| ID | Rule | Why | Evidence |
|---|---|---|---|
| **EDGE-01** | Never reformat, re-quote or add a trailing comment to a pin line in `versions.yml` or `generated.yml`. Each key's quoting style is read by awk/cut/sed, not a YAML parser: quoted values are split on `"`, unquoted ones are field `$2`. | yamllint stays green while the CI Caddy build, the runner's sops/tailscale install, `policy-test.sh` or the bump guard silently get an empty or wrong value. | `.github/actions/setup/action.yml` (`sops_version` `$2`, `tailscale_package_version` `-F'"'`), `.github/workflows/ci.yml` ("Build Caddy with pinned plugins"), `scripts/policy-test.sh` (`headscale_image`), `.github/workflows/bump-commsgun.yml` (`pinned_digest`), `.github/workflows/deploy.yml` and `verify.yml` (`do_reserved_ipv4`), `verify.yml` (`domain`) (×8) |
| **EDGE-02** | Never configure Caddy `trusted_proxies`, or `header_up X-Forwarded-*`, on a site that fronts webhooks-mcp or commsgun. | With no `trusted_proxies`, Caddy overwrites inbound XFF, so it has exactly one entry and first == last. webhooks-mcp (C5) and commsgun-api (K6) read the *first* entry; commsgun-ui (U2, `XFF_DEPTH=1`) reads the *last* and keys its login limiter on it. Any trust makes `source_ip` spoofable and first ≠ last. Confirmed from both sides. | `roles/stack/templates/Caddyfile.j2` (global `servers` block, `@commsgun_enqueue`); webhooks-mcp `src/webhooks_mcp/ingest_app.py#_source_ip`; commsgun `api/internal/enqueue/submit.go#SourceIP` |

## Rules (`EDGE-*`) — binding, easy-to-miss conventions

| ID | Rule | Why | Evidence | Example |
|---|---|---|---|---|
| **EDGE-03** | Restart or reload a container with `docker compose -f {{ edge_root }}/compose.yml <verb> <svc>` and `changed_when: true`. Never use a `community.docker` container module; the single exception is `docker_compose_v2` in "Start the stack". | Module handlers bypass the compose project and its `*hardening` anchor. | `roles/stack/handlers/main.yml` ("Restart headscale", "Reload caddy", "Restart gatus", "Restart ntfy"), `roles/tailscale/tasks/main.yml` ("Mint pre-auth key"), `roles/stack/tasks/main.yml` ("Restart Caddy to restart issuance") (×8) | `roles/stack/handlers/main.yml` ("Restart headscale") |
| **EDGE-04** | Config passed through `env_file` needs no handler, because compose's config hash recreates the container on every run, including the re-run after a failed play. Config in a bind-mounted file needs a handler (and a failed play loses it: `docs/decisions.md#Known traps`). Prefer `env_file` for a new service. | A redundant handler is noise; a bind-mounted config strands the service after a failed deploy. | `roles/stack/tasks/main.yml` ("Compose recreates the container when this env file's content changes"; the webhooks-mcp and both commsgun env tasks have no `notify`) vs gatus/ntfy/headscale/Caddyfile handlers | `roles/stack/tasks/main.yml` ("Webhooks-mcp secrets") |
| **EDGE-05** | Every compose service uses `<<: *hardening`, publishes as `127.0.0.1:{{ <x>_port }}:<literal container port>`, and keeps data in a named volume (uid 10001 + `read_only`). The `*_port` variables are **host** ports. The container ports 9100/9101/9200/9201/9202 are literal because they are cross-repo contracts. | Treating `webhooks_mcp_port` and friends as the container port breaks the service. (bind mounts: `docs/decisions.md#Known traps`) | `roles/stack/templates/compose.yml.j2#webhooks-mcp`, `#commsgun-api`, `#commsgun-ui`, `#ntfy`, `#headscale` (7/7 hardening) | `roles/stack/templates/compose.yml.j2#webhooks-mcp` |
| **EDGE-06** | Resolve a tailnet peer at deploy time from `headscale nodes list -o json` (select on `given_name`) and write `/etc/hosts` or a fact. Never put `*.ts.<domain>` straight into an edge config. A missing peer never fails the deploy: a "Say why …" debug fires and the play continues. | The edge runs tailscale with `--accept-dns=false`, so MagicDNS names don't resolve there, and the failure shows up later (backup, stanza-create), not at deploy. | `roles/stack/tasks/main.yml` ("Tailnet addresses of the staging VM"), `roles/backup/tasks/main.yml` ("Tailnet address of the backup host"), `roles/pgbackrest_repo/tasks/main.yml` ("Tailnet address of the database host") (×3); commit `f2d43b2` | `roles/backup/tasks/main.yml` ("Tailnet address of the backup host") |
| **EDGE-07** | A `*.ts` or `*.private` hostname is wired by hand in **four** places: its hostname var in `group_vars/all/main.yml`, a `host` matcher under `*.{{ magicdns_base_domain }}` or `*.{{ private_zone }}` in `Caddyfile.j2`, the split-horizon list `_names` in `roles/tailscale/tasks/main.yml`, and a Gatus probe. | Adding only the Caddy handle leaves tailnet clients resolving the name nowhere (or to the public IP), so they get a 404. | `inventories/prod/group_vars/all/main.yml#webhooks_ingest_hostname`, `roles/stack/templates/Caddyfile.j2`, `roles/tailscale/tasks/main.yml` (`_names`), `roles/stack/templates/gatus-config.yaml.j2#webhooks-ingest` (×5 hosts) | — |
| **EDGE-08** | Read secrets only in shapes `scripts/secret_keys.py#used_keys` recognises: `edge_secrets.<name>`, `edge_secrets['name']`, `edge_secrets[item.key]` with `key:` on the loop items, inventory `password_key:`/`gate_sops_key:`, or `{% for x in <registry> %}…edge_secrets[x.key]`. | An indirection like `edge_secrets[my_var]` escapes `secrets-keys-test.py`, so a missing SOPS key surfaces only mid-deploy. | `scripts/secret_keys.py#used_keys`; `roles/pgbackrest_repo/tasks/main.yml` (`key: pgbackrest_edge_client_key`), `roles/stack/templates/commsgun-config.json.j2` (`edge_secrets[app.key]`) | `roles/stack/templates/commsgun-config.json.j2` |
| **EDGE-09** | A third-party apt pin is three steps: `apt name=pkg={{ var }}` with `allow_downgrade: true`, then `dpkg_selections hold`, and **on the edge** an entry in the literal `Unattended-Upgrade::Package-Blacklist` in `roles/base/tasks/main.yml`. | Without the blacklist entry, unattended-upgrades moves the package off the pin. | `roles/docker/tasks/main.yml` ("Install pinned Docker packages"), `roles/tailscale/tasks/main.yml` ("Install pinned tailscale"), `roles/pgbackrest_repo/tasks/main.yml`, `roles/tailnet_host/tasks/tailscale.yml`, `roles/db_host/tasks/packages.yml` (×5); `roles/base/tasks/main.yml` ("Package-Blacklist") | `roles/docker/tasks/main.yml` |
| **EDGE-10** | Gatus `external-endpoints` keys (`<group>_<name>`) are a URL contract with pushers on other hosts. Never rename an endpoint's `name` or `group` without re-installing every pusher. | The home pushers (installed by hand by `tools/pve-setup.py` on Proxmox and the DB LXC) 404 until re-installed, and the dead-man alerts. | `roles/stack/templates/gatus-config.yaml.j2` (`external-endpoints`); `tools/pve-setup.py` ("home_proxmox-heartbeat"), `roles/backup/tasks/main.yml`, `roles/pgbackrest_repo/tasks/main.yml` | — |
| **EDGE-11** | Owner-app wiring is a cross-repo contract. webhooks-mcp `INGEST_BASE_URLS` must keep a `default` key. The commsgun dashboard's `ORIGIN`/`API_BASE_URL` depend on `commsgun_ui_private_hostname`, the compose service name `commsgun-api` and the network `commsgun`. The commsgun secret-length assert mirrors `MinSecretLen` (32; 16 for the password). The `ops` app id is the probe's fixed app. | Renaming any piece on one side breaks startup or the probe. Confirmed from both sides. | `roles/stack/tasks/main.yml` (`_ingest_base_urls`, "Commsgun dashboard environment", "Commsgun secrets are as long as the service requires"), `inventories/prod/group_vars/all/main.yml` (`commsgun_apps`); webhooks-mcp `config.py#_parse_bases`; commsgun `api/internal/constants/constants.go#MinSecretLen`, `api/internal/admin/probe.go#probeApp` | — |

## Norms (`EDGE-*`) — defaults & asymmetry guidance

| ID | Norm | Why | Evidence | Example |
|---|---|---|---|---|
| **EDGE-12** | Prefix every Caddy rate-limit zone with its site name, as `public_routes` (`{args[1]}_<zone>`) and `bearer_mcp` (`{{ site.name }}_{{ site.limits }}`) do. | Zone names are global in caddy-ratelimit (decisions.md); `caddy-routing-test.sh` fails on a zone shared across sites. | `roles/stack/templates/Caddyfile.j2#(public_routes)`, `site.kind == 'bearer_mcp'`; `scripts/caddy-routing-test.sh` ("every rate-limit zone") | `roles/stack/templates/Caddyfile.j2#(public_routes)` |
| **EDGE-13** | A port change for ntfy (`2586`) or headscale (`8080`) touches every literal: ntfy in compose, Caddyfile (`@ntfy`) and Gatus (`alerting` url and healthcheck); headscale in compose, Caddyfile and `headscale-config.yaml.j2` (`listen_addr`). The other services use `*_port` variables. | They were never re-mapped, so the literals are scattered. | `roles/stack/templates/compose.yml.j2#ntfy`, `roles/stack/templates/Caddyfile.j2#@ntfy`, `roles/stack/templates/gatus-config.yaml.j2#alerting`, `roles/stack/templates/headscale-config.yaml.j2` ("listen_addr: 0.0.0.0:8080") | — |
| **EDGE-14** | One kind of name per zone: `*.ts` = machines (headscale's base domain), `*.private` = tailnet-only services on the edge, `*.public` = internet services. A new service goes under a portal zone, never under `*.ts`; the legacy `*.ts` services are a fixed list that only shrinks. | A service name under `*.ts` can collide with a node's MagicDNS name (`docs/decisions.md#Known traps`), and only `*.private` has the DNS route Linux nodes need for edge-served names. | `scripts/portal-zones-test.py#ZoneRules` (`LEGACY_TS_SERVICES`); `inventories/prod/group_vars/all/main.yml#private_zone`; `roles/stack/templates/headscale-config.yaml.j2` (`split`) | `inventories/prod/group_vars/all/main.yml#commsgun_ui_private_hostname` |

## Gotchas & scars

- **No in-container healthcheck for webhooks-mcp, commsgun-api or gatus, on purpose.** The images are distroless
  (no shell or wget). "Running", via the edge dead-man, plus Gatus probes is the health signal (owner answer
  2026-11-15), and `commsgun-ui depends_on: [commsgun-api]` has no condition for the same reason. Don't add a
  `healthcheck:` that execs a binary the image lacks. Evidence: `roles/stack/templates/compose.yml.j2#webhooks-mcp`,
  `#commsgun-api`, `#gatus`; decisions.md "The dead-man must check 'running'".
- **SMS limits are staged ahead of a provider.** `commsgun_providers.sms: null`, yet every `commsgun_apps` entry
  declares `sms` limits. This is intentional (owner answer 2026-11-15), so don't "clean them up". Evidence:
  `inventories/prod/group_vars/all/main.yml` (`commsgun_providers`, `commsgun_apps`).
- **The staging-VM lookup is the only fail-soft headscale query** (`failed_when: false`). It runs before "Start the
  stack", when headscale may still be down on a fresh build. The backup and pgbackrest lookups run later and may
  fail hard. Evidence: `roles/stack/tasks/main.yml` ("Staging VM node, as headscale knows it").
- **Two Tailscale install implementations.** `roles/tailscale` (edge, Ubuntu repo, mints its own key) and
  `roles/tailnet_host/tasks/tailscale.yml` (DB and app hosts) share only the version variable, so a flag or behaviour
  change is made twice. The app-deploy template carries literal copies of the tailscale pin
  (`templates/app-deploy/deploy.yml#TAILSCALE_VERSION`), which `scripts/app-deploy-template-test.py` keeps equal to `versions.yml`.

## Candidate rules (unverified)

| Candidate | Why suspected | What would confirm it |
|---|---|---|
| `pgbackrest_package_version` (edge) and `db_pgbackrest_package_version` must share an upstream version | `versions.yml` comment says "must equal"; no assert or test compares them | An assert, or the owner |
| `init-secrets` should join the `deploy-edge` concurrency group | It commits to `main` with `PUSH_TOKEN` like deploy, but has no group | Reading `init-secrets.yml`/`db-host.yml` for a push race |
| The external verify should cover the public `webhooks-mcp.<domain>` (`bearer_mcp`) host | verify hardcodes `hs.`, `xstockstrat.`, `xstockstrat-staging.` (plus an `other.` negative probe) only | Owner intent |

## Pointers (already documented or CI-enforced — not restated here)

| What | Where |
|---|---|
| Never assume; public surface 80/443/3478; no secrets / `no_log`; pin everything + `docs/versions.md`; Ansible only; success shown; ≤2-line comments | `CLAUDE.md#Rules` |
| Design decisions and Known traps (ACME, STUN, DNS, Caddy reload inode, staging isolation, body caps, commsgun env quoting, `{{ }}` in `when:`) | `docs/decisions.md` |
| Firewall exactly 80/443/3478 | `.github/workflows/verify.yml` ("Declared firewall = exactly 80/tcp, 443/tcp, 3478/udp") |
| `tailnet_only`/`deny_staging_vm` first handle; routing tests | `scripts/caddy-routing-test.sh` |
| Every secret key exists in SOPS | `.github/workflows/ci.yml` (`secrets-keys-test.py`) |
| Lint gates; `CI Gate` is the only required check | `.github/workflows/ci.yml` (`lint`), `.ansible-lint` |
| `generated.yml`/`known_hosts` are bot-written | `inventories/prod/group_vars/all/generated.yml`, `.github/workflows/deploy.yml` ("Commit generated state") |
| Body caps equal the listener caps; webhooks-mcp contracts C1–C9 | `docs/decisions.md`; webhooks-mcp `docs/contracts.md` |

---
_Forged by [context-forge](https://github.com/davcs86/agent-plugins). It captures the
non-obvious — nothing here is invented; re-run `/context-constitution` to refresh after the code changes._
