# homelab edge — CLAUDE.md

Ansible (run from GitHub Actions) for one DigitalOcean droplet: headscale + embedded DERP/STUN, Caddy as the only
public entry point, Gatus, ntfy, nightly age backups, a pgBackRest repo. Human overview: `README.md`.

## How to Act

1. **Never assume a value the owner has not stated** — provider setting, hostname, version, schedule, home-side
   detail. Ask first. Open `TODO(ask)` markers are the known gaps; resolve them with the owner, never by guessing.
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

## Context Guide

Read only the row that matches the task.

| Task | Read |
|---|---|
| Rebuild, first-time setup, GitHub secrets | `README.md` § Rebuild from scratch |
| Add / approve / tag / revoke a device; CI pre-auth keys | `docs/runbooks/tailnet-devices.md` |
| Rotate the deSEC ACME token | `docs/runbooks/acme-token.md` |
| Restore headscale; edge unreachable (break glass); stack down after reboot | `docs/runbooks/recovery.md` |
| Alert setup (healthchecks.io email, ntfy on the phone) | `docs/runbooks/alerts.md` |
| Backup host, Proxmox heartbeat, app VM | `docs/runbooks/home-side.md` |
| Build or change the DB host (PostgreSQL, TimescaleDB, PgBouncer, pgBackRest) | `docs/runbooks/db-host.md`, `playbooks/db.yml` |
| Add a workload (tag, grants, shared Postgres, staging route) | `docs/runbooks/onboard-service.md` |
| Planned work, and what is not built yet | `docs/roadmap.md` |
| Bumping or adding a pin | `docs/versions.md` |
| Why the design is the way it is; traps already hit (ACME, STUN, DNS, Ansible) | `docs/decisions.md` |
| Changing grants / tags | `policy/policy.hujson`; test with `scripts/policy-test.sh` |
| Deploy a Compose app (xstockstrat or other) to the homelab; build or change an app VM | `docs/runbooks/deploy-compose-app.md`, `templates/app-deploy/`, `playbooks/app.yml` |
| Set up the local Proxmox host by hand | `docs/runbooks/proxmox-setup.md`, `tools/pve-setup.py` |
| Changing public routes or limits | `roles/stack/templates/Caddyfile.j2`; test with `scripts/caddy-routing-test.sh` |
| What each secret file holds | `secrets/README.md` |
| A workflow fails to push to `main` (GH013); the commit-back token and ruleset bypass | `docs/runbooks/github-push-token.md` |

## Teardown

If a change alters behaviour a doc above describes, update that doc in the same PR, and re-read every doc you
touched against the code before pushing. A new hard-won trap gets one entry in `docs/decisions.md`.
