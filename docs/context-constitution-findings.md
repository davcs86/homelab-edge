# homelab-edge — Constitution Findings

Defects and drift surfaced by `/context-constitution` (context-forge) on 2026-11-15; last refreshed 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `45e0b71` while deriving the constitution. These are for triage,
not governance: a defect is not a rule. This log is maintained (**CF-N12**): `/context-constitution refresh` re-verifies
every open row and retires the rows that no longer reproduce to `## Resolved`.

## Documentation that lies (docs claim behavior the code lacks)

| What the docs say | What the code does | Evidence | Suggested action |
|---|---|---|---|
| `docs/decisions.md` "Rate-limit zone names are global": `public_routes` prefixes zones with `prod_`, `staging_` | Zones are prefixed with `site.name` (`xstockstrat_`, `xstockstrat-staging_`) | `roles/stack/templates/Caddyfile.j2#(public_routes)` ("zone {args[1]}_general") | Correct the doc |
| `docs/decisions.md` (Monitoring): "two dead-man heartbeats (Proxmox …, nightly backup)"; `docs/runbooks/alerts.md` lists 3 | 4 `external-endpoints` (plus `db-heartbeat`, `pgbackrest-backup`) | `roles/stack/templates/gatus-config.yaml.j2` (`external-endpoints`) | Update both docs |
| `docs/decisions.md`: the deploy "probes every managed SNI (hs, xstockstrat, `*.ts`)" | `_cert_check` probes every `edge_sites` hostname, including staging and webhooks-mcp | `roles/stack/tasks/main.yml` (`_cert_check`) | Correct the doc |
| `README.md#Repository map`: the `.github/workflows/` row lists 7 workflows; the playbooks rows and the `roles/stack/templates/` row are also incomplete | `app-host.yml` and `db-host.yml` exist too; so do `playbooks/app.yml`, `playbooks/db.yml`, `playbooks/diag-boot.yml`, `gatus-config.yaml.j2` and `commsgun-config.json.j2` | `.github/workflows/`, `playbooks/`, `roles/stack/templates/` | Update the README |
| `ci.yml` step "Workflows that commit to main (App token only when needed …)" and the `scripts/workflow-push-test.py` docstring say "App token" | `docs/decisions.md` records the owner's fine-grained PAT (`PUSH_TOKEN`), chosen over a GitHub App | `.github/workflows/ci.yml` ("App token only when needed"), `scripts/workflow-push-test.py` | Reword to "PAT" |
| `docs/versions.md#Dependabot`: only the webhooks-mcp digest stays with `bump-webhooks-mcp` | The commsgun pins and `bump-commsgun` are omitted; the `scripts/bump_image_pin.py` docstring names only bump-webhooks-mcp | `docs/versions.md`, `scripts/bump_image_pin.py` | Mention commsgun |
| `policy/policy.hujson` comment: "tailnet-only staging route" | Staging is a public 443 site (decisions.md "Staging is a public name on 443") | `policy/policy.hujson` ("tailnet-only staging route") | Update the comment |
| `versions.yml` header: every pin is ≥2 weeks old | The owner images are exempt (`docs/versions.md`) | `inventories/prod/group_vars/all/versions.yml` header | Note the exemption |
| `docs/runbooks/recovery.md#After a deploy that failed part-way`: "the re-run reloads it" | Nothing reloads an unchanged Caddyfile; the handler needs a change, and the inode check compares file hashes, not the loaded config | `roles/stack/tasks/main.yml` ("Does the running Caddy read the Caddyfile on disk?") | Verify on a real failed run, then fix the runbook or the play (reasoned from code, not run) |

## Latent bugs (looks broken, not merely non-obvious)

| Issue | Impact | Evidence |
|---|---|---|
| The external verify asserts `GET /trader/api/health` → **404** on prod, but Caddy routes it to the UI (200), and `caddy-routing-test.sh` and Gatus both expect 200 | The daily external verify fails whenever the app is up | `.github/workflows/verify.yml` ("for p in / /anything-else /agent/api/tools /trader /trader/api/health"), `roles/stack/templates/Caddyfile.j2` (`path /trader/api/health`), `scripts/caddy-routing-test.sh` ("UI health probe path is public") |
| The `bearer_mcp` rate-limit zone is the bare `{{ site.limits }}` (= `webhooks_mcp`), not site-prefixed | A second `bearer_mcp` site with the same `limits` would share one counter per IP. Owner (2026-11-15): latent bug | `roles/stack/templates/Caddyfile.j2` ("zone {{ site.limits }}") |
| Tailnet Gatus probes omit `[CERTIFICATE_EXPIRATION]`, so the `*.ts` wildcard cert expiry is never probed; staging ingest `:8443` has no probe at all | Cert expiry and staging-ingest outages go unalerted. Owner (2026-11-15): gap | `roles/stack/templates/gatus-config.yaml.j2` (`x-tailnet`) |
| No check keeps `templates/app-deploy/deploy.yml` tailscale pins in step with `versions.yml` | Silent drift between the template and the pins | `templates/app-deploy/deploy.yml#TAILSCALE_VERSION`, `scripts/app-deploy-template-test.py` |

## Dead / orphaned code

| What | Why it looks dead | Evidence |
|---|---|---|
| `versions.yml#headscale_version` | Read by no code; only `headscale_image` (tag + digest) is used, so a bump can leave the two disagreeing | `inventories/prod/group_vars/all/versions.yml` ("headscale_version"); grep: zero consumers |

## Open questions (unresolved *why* — needs a maintainer)

- `roles/stack/templates/Caddyfile.j2#log default` excludes logger `http.log.error.commsgun_ui`, tied by name to the
  per-host `log commsgun_ui`. Should a rename be guarded beyond the canary in `caddy-routing-test.sh`? Status: **open**.
- Open markers: `TODO(provide)` ×1 (`inventories/prod/group_vars/all/main.yml#backup_host_known_hosts`; empty = trust
  on first use) and `TODO(verify)` ×2 (Anthropic egress allowlist: `roles/stack/templates/Caddyfile.j2`,
  `docs/decisions.md`). Status: **open** (owner-only, per `CLAUDE.md`).

## Dismissed (won't fix)

| What the docs say / issue | Evidence (was) | Resolved | How confirmed |
|---|---|---|---|
| `CLAUDE.md#Context Guide`: "cross-repo contracts C1–C8" | webhooks-mcp `docs/contracts.md` ("C9") | 2026-11-15 | Context Guide now says C1–C9 (step-3 refresh); webhooks-mcp `docs/contracts.md` lists C1–C9 |

## Resolved

_None._

---
_Surfaced by [context-forge](https://github.com/davcs86/agent-plugins). Open items above are defects to
action, not rules to keep — nothing the scan found is discarded (CF-N8); resolved and dismissed items
stay on record rather than being deleted (CF-N12). Re-run `/context-constitution refresh` to catch
newly-resolved rows and pick up new defects._
