# homelab-edge — Constitution Findings

Defects and drift surfaced by `/context-constitution` (context-forge) on 2026-11-15; last refreshed 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `45e0b71` while deriving the constitution. These are for triage,
not governance: a defect is not a rule. This log is maintained (**CF-N12**): `/context-constitution refresh` re-verifies
every open row and retires the rows that no longer reproduce to `## Resolved`.

## Documentation that lies (docs claim behavior the code lacks)

_None open._

## Latent bugs (looks broken, not merely non-obvious)

_None open._

## Dead / orphaned code

_None open._

## Open questions (unresolved *why* — needs a maintainer)

- `roles/stack/templates/Caddyfile.j2#log default` excludes logger `http.log.error.commsgun_ui`, tied by name to the
  per-host `log commsgun_ui`. Should a rename be guarded beyond the canary in `caddy-routing-test.sh`? Status: **open**.
- Open markers: `TODO(provide)` ×1 (`inventories/prod/group_vars/all/main.yml#backup_host_known_hosts`; empty = trust
  on first use) and `TODO(verify)` ×2 (Anthropic egress allowlist: `roles/stack/templates/Caddyfile.j2`,
  `docs/decisions.md`). Status: **open** (owner-only, per `CLAUDE.md`).
- verify now requires `GET /trader/api/health` = 200, so the daily run also fails while the prod app is offline (run 46
  saw 502), whereas `POST /agent` tolerates 502. Is "app down fails verify" intended, or should this path accept "not
  404" like `/agent`? Status: **open** (owner).

## Dismissed (won't fix)

| What the docs say / issue | Evidence (was) | Resolved | How confirmed |
|---|---|---|---|
| `CLAUDE.md#Context Guide`: "cross-repo contracts C1–C8" | webhooks-mcp `docs/contracts.md` ("C9") | 2026-11-15 | Context Guide now says C1–C9 (step-3 refresh); webhooks-mcp `docs/contracts.md` lists C1–C9 |

## Resolved

Fixed on branch `claude/fix-constitution-findings`; checks run locally on 2026-11-15.

| What the docs say / issue | Evidence (was) | Resolved | How confirmed |
|---|---|---|---|
| verify asserted `GET /trader/api/health` → 404 on prod; Caddy routes it to the UI (200) | `.github/workflows/verify.yml` (`for p in … /trader/api/health …`) | 2026-11-15 | verify now expects 200 for that path only; `/`, `/anything-else`, `/agent/api/tools`, `/trader`, `/api/health` stay 404. Run 46 (2026-11-14) failed on exactly this step (`GET /trader/api/health -> 502`, app offline); runs 38–45 failed earlier steps (apt, STUN). Not re-run live (out of scope) |
| The `bearer_mcp` rate-limit zone was the bare `{{ site.limits }}` | `roles/stack/templates/Caddyfile.j2` (`zone {{ site.limits }}`) | 2026-11-15 | Now `{{ site.name }}_{{ site.limits }}` (renders `webhooks-mcp_webhooks_mcp`). New `caddy-routing-test.sh` check adapts the Caddyfile plus a twin `bearer_mcp` site and fails on a shared zone: FAIL (`webhooks_mcp`) with the old line, PASS (14 zones) with the fix; full run 97 PASS / 0 FAIL |
| Tailnet Gatus probes omitted `[CERTIFICATE_EXPIRATION]`; staging ingest `:8443` had no probe | `roles/stack/templates/gatus-config.yaml.j2` (`x-tailnet`) | 2026-11-15 | `webhooks-ingest` (on the `*.ts` wildcard) asserts `[CERTIFICATE_EXPIRATION] > 336h`; new `webhooks-ingest-staging` probes `https://webhooks.<ts>:8443/healthz`. `gatus-config-test.py` covers both (16 tests OK) |
| Nothing kept `templates/app-deploy/deploy.yml` tailscale pins in step with `versions.yml` | `scripts/app-deploy-template-test.py` | 2026-11-15 | `test_tailscale_matches_the_edge_pins` compares `TAILSCALE_VERSION`/`TAILSCALE_SHA256` with `tailscale_package_version`/`tailscale_tgz_amd64_sha256`; fails on a drifted version (checked with 1.98.9), passes on main's values |
| `versions.yml#headscale_version` was read by nothing | `inventories/prod/group_vars/all/versions.yml` | 2026-11-15 | Removed; `headscale_image` (tag + digest) is the only pin, reason recorded in `docs/versions.md` (headscale row). grep: no consumer before or after; `policy-test.sh` (reads `headscale_image`) passes |
| decisions.md: `public_routes` zone prefixes `prod_`, `staging_` | `docs/decisions.md` (Rate-limit zone names are global) | 2026-11-15 | Doc now names the `site.name` prefixes (`xstockstrat_`, `xstockstrat-staging_`, `webhooks-mcp_`); the `webhooks_mcp` zone mention updated too |
| decisions.md "two dead-man heartbeats"; alerts.md listed 3 | `docs/decisions.md` (Monitoring), `docs/runbooks/alerts.md` | 2026-11-15 | Both list the four `external-endpoints` (Proxmox, DB host, nightly backup, pgBackRest), matching `gatus-config-test.py#test_heartbeats_are_unchanged`; alerts.md also lists `webhooks-ingest-staging` |
| decisions.md: deploy probes "hs, xstockstrat, `*.ts`" | `docs/decisions.md` (`caddy reload` keeps ACME jobs) | 2026-11-15 | Doc now says hs, every `edge_sites` hostname, `*.ts`, matching `_cert_check` in `roles/stack/tasks/main.yml` |
| README repository map incomplete | `README.md#Repository map` | 2026-11-15 | Adds `app-host`, `db-host`, `playbooks/app.yml`, `db.yml`, `diag-boot.yml`, and the Gatus and commsgun templates; checked against `ls playbooks roles/stack/templates .github/workflows` |
| `ci.yml` step and `workflow-push-test.py` docstring said "App token" | `.github/workflows/ci.yml`, `scripts/workflow-push-test.py` | 2026-11-15 | Both say the `PUSH_TOKEN` PAT; `workflow-push-test.py` 10 tests OK |
| `docs/versions.md#Dependabot` and `bump_image_pin.py` docstring omitted commsgun | `docs/versions.md`, `scripts/bump_image_pin.py` | 2026-11-15 | Both name `bump-commsgun`; `bump-image-pin-test.py` 76 tests OK |
| `policy.hujson` comment "tailnet-only staging route" | `policy/policy.hujson` | 2026-11-15 | Comment now says the public staging route on 443; `policy-test.sh` passes |
| `versions.yml` header omitted the owner-image exemption | `inventories/prod/group_vars/all/versions.yml` header | 2026-11-15 | Header names the webhooks-mcp and commsgun exemption, as `docs/versions.md` does; no pin line touched (EDGE-01), yamllint OK |
| recovery.md: "the re-run reloads it" (Caddyfile) | `docs/runbooks/recovery.md#After a deploy that failed part-way` | 2026-11-15 | Reasoned from `roles/stack/tasks/main.yml`: on the re-run the template is unchanged (no `Reload caddy` notify), compose does not recreate Caddy for a bind-mounted file, and the hash check passes because the mounted directory already shows the new file, so nothing reloads it. Runbook now gives the `caddy reload` command; play unchanged. Not reproduced on a real failed run |

---
_Surfaced by [context-forge](https://github.com/davcs86/agent-plugins). Open items above are defects to
action, not rules to keep — nothing the scan found is discarded (CF-N8); resolved and dismissed items
stay on record rather than being deleted (CF-N12). Re-run `/context-constitution refresh` to catch
newly-resolved rows and pick up new defects._
