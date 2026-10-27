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
- **Workflows commit to `main` with the owner's fine-grained PAT, which bypasses the pull-request rule.** The rule blocked `init-secrets`,
  and `GITHUB_TOKEN` cannot be a bypass actor. The owner chose a PAT over a GitHub App and over branch-and-PR. What it costs: the token
  acts as the owner and expires; whoever can approve a `production` run and get workflow code onto `main` can change any file on `main`,
  secrets files included. Mitigations, checked by `scripts/workflow-push-test.py` or required by `docs/runbooks/github-push-token.md`: the
  token is a secret of the `production` environment restricted to `main`, scoped to this repository with Contents write only, used only
  when a run has something to commit, and the default `GITHUB_TOKEN` is read-only and no longer kept by the checkout. Not chosen: an App
  (short-lived per-run tokens, not tied to a person, but more to set up) and a branch plus a hand-opened PR (no standing credential).
- **A new generated secret is added by `init-secrets` `fill_missing`, never by hand or by regenerating the file.** Regenerating rotates every
  credential, including the pgBackRest PKI the DB host holds. The mode adds only absent plain credentials, decrypts in memory with the deploy
  key, and refuses when an operator value or a PKI key is missing. Without it, a release that reads a new key fails the deploy mid-play.
- **A failed play loses the handlers it had queued.** Ansible skips them, and the re-run finds the files already correct so it never
  queues them again; a policy file written by the failed run is then never loaded. After a failed deploy, a successful re-run must be
  followed by the reloads in `docs/runbooks/recovery.md` (headscale policy HUP, and Gatus if its files changed).
- **Gatus endpoints are named after the workload tag, staging included.** `xstockstrat-ui`, `xstockstrat-staging-agent`, … in group
  `home`, matching the hostname rule below; the earlier `app-ui`/`app-agent` outlived the move from `tag:app`. Staging probes are always on and
  alert like prod, so the staging VM must be registered before the deploy that adds them.
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
- **The SSH hardening is judged by `sshd -T`, not by the file it wrote.** sshd takes the first value it sees and includes
  `sshd_config.d/*.conf` in name order, so an earlier-sorted drop-in can silently win. `sshd -T` prints `without-password`
  for `prohibit-password`, so both spellings count. A failed `sshd -t` removes the drop-in again, because the next start
  would otherwise fail.
- **`authorized_keys` is appended to in place.** On Proxmox it is a symlink into `/etc/pve`; the toolkit's file writer
  replaces files, which would turn the symlink into a regular file and leave the cluster copy stale.
- **Password SSH is disabled behind a typed proof and an automatic revert.** A lockout on a remote-only host costs a trip to
  the box, so `ssh-harden` arms a transient `systemd-run` timer (no `at` dependency) that removes the drop-in after
  10 minutes unless `ssh-harden-keep` stops it.
- **Two SSH keys, agent forwarding off.** The owner keeps keys in the 1Password SSH agent, which is desktop-only and can be locked or
  unavailable, so a remote-only host needs a second key that does not depend on it (the gate checks for two). The drop-in forbids agent
  forwarding so a mistaken `ssh -A` cannot expose the 1Password keys to root on the host. A client that offers every agent key hits
  `MaxAuthTries` (6), so the proof commands use `IdentitiesOnly` with the public key file.
- **App-deploy secrets come from 1Password first and fall back to GitHub secrets.** One service account per app and environment
  vault (its vaults are fixed at creation), its token the only secret in the GitHub environment. The edge's own workflows keep
  GitHub secrets and SOPS, so rule 3 is unchanged. Workload Identity (v5, public preview) was not used: it still needs a GitHub
  secret and loads every variable of a 1Password Environment, with no per-item references or `ssh-format`.
- **The firewall's automatic revert restores the previous `cluster.fw`, it does not stop the firewall.** A timer that only runs
  `pve-firewall stop` would leave the host unfiltered and with no record of what was there; the toolkit saves the rules from before
  its first change (a later run keeps that first copy, and a hand edit since counts as a new original) and a transient `systemd-run`
  timer writes them back. `/etc/pve` is pmxcfs: files there are rewritten in place (no rename, no chmod, no symlink), and
  `pve-firewall compile` runs before the restart so a rule file that does not parse never reaches the running firewall.
- **`config.db` goes into a config backup only encrypted, and a restore never applies it.** It holds the whole cluster config
  including secrets, so the archive that carries it is `age`-encrypted to recipients the owner supplies, and the unattended timer
  refuses to run without them. It is copied with SQLite's online backup API so a running `pve-cluster` cannot tear it. A copy never
  equals the live file byte for byte, so the restore plan marks it "rebuild only" instead of reporting a permanent difference.
- **The Proxmox heartbeat reports health, not liveness.** It pushes `success=false&error=…` when `zpool status -x`, the NVMe SMART
  status or pool capacity (≥ 85%) fail, and an unreadable pool counts as a failure, so a check that stops working cannot look
  healthy. The token is in a 0600 curl `-K` file because argv is world-readable in `/proc`.
- **The DB heartbeat is installed into the container with `pct`, and reports health, not just liveness.** The host that must be
  watched is a guest, so the Proxmox toolkit pushes the script, units and a 0600 token file in with `pct push`: `pct exec` arguments
  are visible in the host's process list, a file is not. The token is staged on tmpfs and removed even when the plan fails. The checks
  are `pg_isready` on PostgreSQL and PgBouncer, the pgBackRest TLS port and the data disk; an unreadable disk is a failure so a broken
  check cannot look healthy. The address the checks use is asked, not assumed, because the services may listen only on the tailnet address.
- **The DB host is configured by Ansible over the tailnet from `tag:ci`, in two stages.** The container has no tailnet or deploy user
  when it is born, so a root bootstrap over the LAN (users, sshd, pinned Tailscale, join with a `tag:db` key from the environment)
  precedes the `db-host` workflow. The cost: `tag:ci`, which xstockstrat's deploys also use, can SSH to the DB host as a user with
  passwordless sudo. `tag:deploy` would narrow it. The owner's own login (`admin_user`, key only, passwordless sudo because the container has no
  console password) is created too, so manual work does not depend on the CI key. PostgreSQL's passwords are set only when a login with the SOPS value fails, and
  PgBouncer's user list is built from PostgreSQL's own SCRAM verifiers, so no cleartext reaches PgBouncer or the repo.
- **Archiving follows the stanza.** `archive_command` against a missing stanza fails forever and WAL fills the data disk, and `stanza-create`
  needs PostgreSQL running, so the first `db-host` run cannot archive. `archive.yml` asks the edge (`pgbackrest info`, status 1 = missing
  stanza path) and turns `archive_mode` on only when the stanza exists, so the flags can stay on. The edge's backup service has
  `ConditionPathExists` on the stanza's backup directory, so the timers are skipped, not failed, until then.
- **The database TLS CA is not the pgBackRest CA.** A client cert from the pgBackRest CA must never open a SQL connection and a database
  server cert must never authenticate to the backup channel. The pull direction's extra certs (DB server, edge client) do come from the
  pgBackRest CA. `init_secrets.py --fill-missing` creates each set whole and refuses a partial one.
- **Two administrative roles: `admin` is read-only, `superadmin` is a superuser.** Routine inspection and automation should not be able to drop
  production data, but manual repairs and restores need a superuser. `superadmin` is reachable only by `hostssl` from named devices, has no
  PgBouncer entry and 3 connections; it is a network-reachable superuser guarded by a password and a source address, which the owner chose
  over `ssh` + `sudo -u postgres psql` alone. Leave `db_superadmin_tailnet_hosts` empty to keep it local-only.
- **The edge pushes a heartbeat after each pgBackRest backup.** `OnFailure` only fires when the unit runs and fails; a timer that never fires
  (disabled, edge down, stanza missing) is silent. A Gatus external endpoint (`edge_pgbackrest-backup`, 26 h) turns that silence into an
  alert, as `nightly-backup` does for headscale. The token is passed to curl on stdin (argv is world-readable) by a root-run `ExecStartPost=+`.
  A Gatus outage right after a good backup marks the unit failed and notifies; that is the same trade as `edge-backup.service`.
- **Owner roles get `idle_in_transaction_session_timeout = 10min` and no `statement_timeout`.** A leaked transaction holds locks and pins
  vacuum, and 60 connections is not many to lose; a statement timeout would also apply to the migrator (same role) and to long bar scans.
- **PgBouncer entries do not force `user=`.** With SCRAM verifiers in `userlist.txt`, a forced user makes the server login fail (`wrong password type`); passing
  the client's SCRAM keys through works. Isolation is the role's `CONNECT` grant and its `pg_hba` line, tested with both roles against both databases.
- **Connection sizing follows the 3 GiB, 2-core container.** Worst case per environment: the 8 direct pool connections (ledger 2, identity 2,
  config 2, notify 1, ui 1) doubled by a rolling deploy, 2 for the migrator, and 1 PgBouncer pool of 5 = 23, so each role is limited to 25.
  Both environments deploy together (the daily promotion): 2 x 25 + admin 3 + superadmin 3 + 3 reserved slots = 59, so `max_connections` is 60 (the
  playbook asserts it). The build guide's 100 would not fit in memory with swap off: roughly 10 MB per backend plus two `work_mem` nodes is 26 MB, so
  60 x 26 MB + 768 MB of `shared_buffers` is about 2.3 GiB of 3 (an estimate, not measured), and 100 would be 3.4 GiB. Three autovacuum workers
  at the 256 MB `maintenance_work_mem` could add 768 MB, so `autovacuum_work_mem` is 64 MB. The pool stays 5 per database: xstockstrat bounds
  `max_concurrent_bars_fetches` to [1, 5] with the marketdata PgBouncer pool as the ceiling, so a smaller pool queues work it allows, and 5 is
  already 2 x cores + 1. Raise nothing before measuring.
- **Traps:** a Debian LXC without a UTF-8 locale creates a `SQL_ASCII` cluster, so `createcluster.d` forces `--encoding=UTF8`;
  PostgreSQL and PgBouncer bind the tailnet address, which tailscaled brings up after boot, so their units wait for it
  (`wait-tailnet-ip`); a `postgres` login with `ALTER ROLE … PASSWORD` can log the statement on error, so it sets
  `log_min_error_statement = panic` first.
- **Kernel pin changes are followed by `proxmox-boot-tool refresh`.** The Proxmox Host Bootloader page says the ESPs must be
  synchronised after setting or clearing a pin on systems that use proxmox-boot-tool; the runbook's 5.4 steps leave it out, so
  the toolkit's `update` note includes it.
- **The `op` CLI is preinstalled and pinned.** `load-secrets-action` downloads "latest" from `cache.agilebits.com` with no checksum
  when no `op` is on the runner. The template installs a checksum-pinned `op` first (version and signature recorded in
  `docs/versions.md`), which also makes the action skip its own download. Loaded values stay step outputs (`export-env: false`).
- **A tailnet deploy re-applies the users role.** `bootstrap-users.yml` probes the `deploy` user and falls back to root only when
  it cannot log in, so it is safe on a configured edge. Without it in the tailnet run, a rotated `admin_console_password_hash`
  never reached the host and `david_admin` could not `sudo` (the first rotation, #34, had no effect).
- **PgBouncer is restarted before the verification step.** apt starts it on the package default (loopback only); the restart that
  applies `listen_addr` was a handler, which runs at the end of the play, after `verify.yml` had already failed on
  `<tailnet IP>:6432`. A failed run drops its notified handlers and the next run sees an unchanged file, so a plain re-run failed
  the same way. `pgbouncer.yml` now checks the tailnet listener itself (a closed port notifies the restart, so a host already in that state heals on the next run) and ends with `flush_handlers`.
