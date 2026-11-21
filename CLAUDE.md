<!-- context-forge:behavioral-contract:start -->
## How to Act

Read this first — it governs *how* you work here; everything below is the *what* you work with.
These four behaviors are the operating defaults; the rest of this file (and the constitution) is
context you load per task.

1. **Don't assume — ask, and surface tradeoffs.** Before acting, always list every assumption you
   are making on your own — scope, interpretation, defaults, a choice between options — each with
   its basis, or say "none". On ambiguity, a missing detail, or a design fork, stop and raise it.
   *Red flag:* "it's obvious" / "too small to ask" — that *is* the assumption; list it.
   *(Enforced by `## Rules` 1 and the cross-repo seams **EDGE-02/EDGE-10/EDGE-11**.)*
2. **Write the minimum that solves the stated problem.** Nothing speculative — no abstraction,
   option, or "while I'm here" scaffolding. *Red flag:* "might need it later", "for flexibility".
   *(Enforced by **EDGE-03/EDGE-04**: one restart shape, `env_file` over new handlers.)*
3. **Touch only what the task requires.** Surgical, auditable diffs; clean up only orphans *you*
   introduced. *Red flag:* "cleaner if I also…" — propose it, don't do it.
   *(Enforced by **EDGE-01**: reformatting a pin line silently breaks CI and the bump guard.)*
4. **Define success up front, then loop until verified.** State the pass condition before you
   start; claim done only with the check's output from this session in hand.
   *Red flag:* "should work", "looks right", "probably passes".
   *(Enforced by `## Rules` 6 — `ci`, `deploy`, `verify` output — and **EDGE-07**'s four-place wiring.)*

No exceptions for small or obvious tasks — honoring the letter while dodging the intent is a violation.

> Litmus test for any future line in this file: *does it shape how the agent thinks (a behavior), or
> restate a fact the agent can read from the code?* If it's a fact already in the repo, leave it out.
<!-- context-forge:behavioral-contract:end -->
# homelab edge — CLAUDE.md

Ansible (run from GitHub Actions) for one DigitalOcean droplet: headscale + embedded DERP/STUN, Caddy as the only
public entry point, Gatus, ntfy, nightly age backups, a pgBackRest repo. Human overview: `README.md`.

## Rules

1. **Never assume a value the owner has not stated** — provider setting, hostname, version, schedule, home-side
   detail. Ask first. Open `TODO(provide)` / `TODO(verify)` markers are the known gaps; resolve them with the owner,
   never by guessing.
2. **Public surface is exactly 80/tcp, 443/tcp, 3478/udp.** Never open SSH publicly outside `deploy → bootstrap`,
   which closes it again and proves it; anything else reaches the host over the tailnet.
3. **No secret in the repo, a log, or a commit message.** Secrets enter only through SOPS (`secrets/`) or GitHub
   secrets; any task that handles one sets `no_log: true`.
4. **Pin everything** (image by digest, binary by checksum, package by version) in
   `inventories/prod/group_vars/all/versions.yml`, and record why in `docs/versions.md` in the same change.
5. **Ansible only** — no second IaC tool; the DO API is driven through `uri`.
6. **Success is shown, not claimed.** State the check before changing anything, run it (`ci`, `deploy`, `verify`,
   or the playbook), and report its output. A green lint is not a working edge.
7. **Comments state constraints only, ≤ 2 lines.** How something works or why it got that way goes in the PR
   description or `docs/decisions.md`.
8. **One kind of name per zone.** `*.ts` holds machines only (named by headscale); `*.private` holds tailnet-only
   services and `*.public` internet services, wherever they run (edge or home): the edge's Caddy serves the name and
   proxies to the service. Never add a service under `*.ts` or a machine under `*.private` / `*.public`;
   `scripts/portal-zones-test.py` fails CI on either.

## Context Guide

Read only the row that matches the task.

| Task | Read |
|---|---|
| Rebuild, first-time setup, GitHub secrets | `README.md` § Rebuild from scratch |
| Add / approve / tag / revoke a device; CI pre-auth keys | `docs/runbooks/tailnet-devices.md` |
| Rotate the deSEC ACME token; let it issue for a new public hostname | `docs/runbooks/acme-token.md` |
| Restore headscale; edge unreachable (break glass); stack down after reboot | `docs/runbooks/recovery.md` |
| Alert setup (healthchecks.io email, ntfy on the phone) | `docs/runbooks/alerts.md` |
| Backup host, Proxmox heartbeat, app VM | `docs/runbooks/home-side.md` |
| Build or change the DB host (PostgreSQL, TimescaleDB, PgBouncer, pgBackRest) | `docs/runbooks/db-host.md`, `playbooks/db.yml` |
| Add a workload (tag, grants, shared Postgres, staging route) | `docs/runbooks/onboard-service.md` |
| Bumping or adding a pin, including the bot-opened webhooks-mcp and commsgun digest bumps | `docs/versions.md` |
| Changing a webhooks-mcp port, body cap, `X-Forwarded-For` handling or hostname (cross-repo contracts C1–C9) | `davcs86/webhooks-mcp` `docs/contracts.md`, then `docs/decisions.md` § Known traps |
| Deploy, configure or operate commsgun (email/SMS hub) | `docs/runbooks/commsgun.md` |
| Deploy, configure or operate happybabytracker (family tracker): admin and parents, secret rotation, restore | `docs/runbooks/happybabytracker.md` |
| SSO (Authelia): people, groups, emails, linking app accounts, recovery | `docs/runbooks/sso.md`, `docs/decisions.md` § SSO |
| Why the design is the way it is; traps already hit (ACME, STUN, DNS, Ansible) | `docs/decisions.md` |
| Patterns between the traps an edit can silently break (pin-line quoting, XFF trust, restart/handler shape, `*.private` four-place wiring, secret-read shapes, apt pin recipe, owner-app seams) — `EDGE-*` — plus the defects/doc-drift log | `docs/context-constitution.md`, `docs/context-constitution-findings.md` |
| Changing grants / tags | `policy/policy.hujson`; test with `scripts/policy-test.sh` |
| Deploy a Compose app (xstockstrat or other) to the homelab; build or change an app VM | `docs/runbooks/deploy-compose-app.md`, `templates/app-deploy/`, `playbooks/app.yml` |
| Set up the local Proxmox host by hand | `docs/runbooks/proxmox-setup.md`, `tools/pve-setup.py` |
| Changing public routes or limits | `roles/stack/templates/Caddyfile.j2`; test with `scripts/caddy-routing-test.sh` |
| What each secret file holds | `secrets/README.md` |
| A workflow fails to push to `main` (GH013); the commit-back token and ruleset bypass | `docs/runbooks/github-push-token.md` |

## Teardown

If a change alters behaviour a doc above describes, update that doc in the same PR, and re-read every doc you
touched against the code before pushing. A new hard-won trap gets one entry in `docs/decisions.md`.
