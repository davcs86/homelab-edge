# Decisions and known traps

Durable reasoning that the code cannot show. One entry per item; add one when a fix took more than one attempt.

## Design decisions

- **Policy tests run in CI only.** headscale re-runs a policy's `tests` on every reload and would reject updates
  while tagged nodes are missing (fresh tailnet, rebuilt node). The edge receives the policy with `tests` stripped;
  `scripts/policy-test.sh` registers one fake node per user/tag in a throwaway headscale and runs them.
- **The edge reaches its own control plane on 127.0.0.1** (`/etc/hosts`), so nothing depends on hairpinning through
  the reserved IP. Direct WireGuard to the edge uses hole punching (no inbound UDP besides 3478); DERP is the fallback.
- **Caddy uses host networking** so rate limits and the tailnet split see real client IPs. Upstreams resolve via
  MagicDNS (`100.100.100.100`); the edge itself runs with `--accept-dns=false`.
- **Login hardening is upstream's gap.** xstockstrat login has no lockout or MFA; the Caddy limits (5/min, 30/h per
  IP on `POST /api/auth/login`) are the only brute-force control until the app adds them.
- **Access logs** drop OAuth `code`, `state`, `txn` and `agent_cb` query values; Caddy redacts credential headers.
- **Monitoring is Gatus**: public probes resolve through 1.1.1.1 (what the internet sees), home probes through
  MagicDNS; two dead-man heartbeats (Proxmox every 60 s, nightly backup + drill) alert when silent.
  healthchecks.io is the dead-man for the edge itself, independent of ntfy and the tailnet.
- **Daily verify runs unattended.** Its external job has no `production` gate; its only secret is the
  `firewall:read` repo secret `DIGITALOCEAN_READ_TOKEN`. The tailnet job stays gated and manual-only.
- **TODO(verify): MCP source-range allowlist**, once Anthropic publishes stable connector egress ranges. Sketch for
  the `xstockstrat` site in the Caddyfile:
  ```
  @not_anthropic {
      path /agent /agent/ /agent/oauth/token /agent/oauth/register
      not remote_ip <ranges>
  }
  respond @not_anthropic 403
  ```

## Known traps

- **deSEC token subnet lock uses egress addresses, not the reserved IP.** DO sends outbound traffic from the
  droplet's own public IPv4 *and* IPv6 (desec.io has AAAA). Inbound to the reserved IP lands on the anchor address
  (10.48.0.x). A new droplet means a new token.
- **DNS-01 needs `propagation_delay`.** deSEC's anycast nodes converge after Caddy's own check passes; Let's
  Encrypt's multi-perspective lookup then sees NXDOMAIN and caches it for the zone's negative TTL (300 s), so every
  retry fails. `propagation_delay 2m` / `propagation_timeout 10m` in the `(acme)` snippet fixes it.
- **`caddy reload` keeps ACME jobs already in backoff** on the old settings; only a restart drops them. The deploy
  restarts Caddy only while some site lacks a certificate, and probes every managed SNI (hs, xstockstrat, `*.ts`).
- **Tailscale's STUN server ignores generic STUN probes.** A request needs SOFTWARE `tailnode` and a FINGERPRINT
  attribute; XOR-MAPPED-ADDRESS may come back as IPv4-mapped IPv6. `scripts/stun-probe.py` builds the right one.
- **The verify test node must run userspace (`TS_USERSPACE=true`).** Kernel mode in a container fails on iptables,
  and tailscaled then skips applying its DNS config, so MagicDNS appears broken. Assert with
  `tailscale dns query`, which asks the node's own resolver.
- **headscale's container is distroless** — no shell, no `cat`; read its files from the host side.
- **headscale JSON omits false fields** (`online` is absent when offline) — test with `defined`, not equality.
- **Ansible 2.21 rejects `{{ }}` inside `when:` / `that:`.** Build the value in `vars`/`loop` and test it bare.
- **`-e key=value` splits on spaces.** Workflows pass user input as a JSON vars file (`-e @vars.json`).
- **The dead-man must check "running", not only "unhealthy".** After the 2026-10-16 unattended reboot, STUN and
  `tailscale up` failed for 4.5 days (headscale not serving) with no alert: `docker ps --filter health=unhealthy`
  does not list exited containers. `edge-deadman` now compares `compose config --services` with
  `compose ps --status running`. Why headscale did not restart: run node-admin → `diagnose-boot`.
- **`Automatic-Reboot-Time` follows the host clock (UTC).** A Pacific reboot window needs a systemd timer with a
  time zone in `OnCalendar` (`edge-reboot.timer`), which also tracks DST.
- **Bootstrap must be re-runnable.** Root's keys are removed partway; the play probes the deploy user first and
  connects as root only if that fails.
- **Staging shares the DB server; isolation is in Postgres and PgBouncer, not the tailnet.** Grants name `tag:db`
  ports, not databases. Per-workload role, `REVOKE CONNECT`, `pg_hba` and PgBouncer entries are in `onboard-service.md`;
  nothing in this repo verifies them.
- **Staging reaches the edge on its own port.** A tailnet grant is per `tag:edge:port` and Caddy cannot tell tags apart
  by source IP, so staging on 443 would also reach prod's `@tailnet` handle. `:8443` serves only
  `staging_ui_hostname`; the grant is `tag:xstockstrat-staging → tag:edge:8443`, never 443. Members (`david@`) and admins reach 8443 too.
- **A route name cannot equal a node's MagicDNS name.** `xstockstrat-staging.ts.<domain>` resolves to the staging VM;
  an extra record for it would contend with the node record. The edge-facing name is `xstockstrat-staging-ui`.
- **Hostnames follow tags, and duplicates register as `name-1`.** `app_tailnet_host` and `staging_tailnet_host` are the
  Caddy and Gatus upstreams, so the node must carry that hostname before the deploy.
- **App deploys use hosted runners over the tailnet, not a self-hosted runner.** No standing runner or deploy rights on the
  VM, nothing to patch, and the key is ephemeral. The cost is that a dead edge blocks deploys (admins deploy by hand,
  `recovery.md`). The transport is one script (`app-deploy.sh`), so switching later is a `runs-on` change.
- **The app deploy key is a forced command, not a shell.** Docker-group access is root-equivalent, so the key can only
  `put`, `deploy`, `rollback` and `status` one app directory, and the script is installed by hand on the VM.
- **The Proxmox toolkit only touches what it marked.** Guests carry the `pve-setup` tag, backup jobs the `pve-setup-` id
  prefix, users the comment `created by pve-setup`; status shows them and undo removes only them, behind a typed
  confirmation. Config restore applies only the files the toolkit itself wrote: cluster config, network and fstab
  are verified and planned but never applied (a wrong `/etc/pve` or `interfaces` file takes effect at once).
- **The Proxmox host joins as `pve` with Tailscale DNS off.** The edge probes `pve.ts.<domain>:8006` and names its heartbeat for
  `pve`; MagicDNS would rewrite `/etc/resolv.conf`, which apt and the guests depend on. With DNS off the heartbeat cannot resolve
  `status.ts.<domain>`, so its curl config pins the edge's tailnet IP.
