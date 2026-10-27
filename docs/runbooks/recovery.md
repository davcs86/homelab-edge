# Recovery

## Restore headscale from backup

Archives (`edge-<UTC>.tar.age`) are on the backup host and in `/var/backups/edge` (7 days). Decrypt with the
**offline** backup key: `age -d -i backup edge-….tar.age | tar -x` → `headscale/{db.sqlite,noise_private.key,derp_server_private.key,…}`.
On the edge: `docker compose -f /opt/edge/compose.yml stop headscale`, copy `db.sqlite` and both keys into
`/var/lib/docker/volumes/edge_headscale-data/_data/`, start headscale. The noise key is what lets existing clients
reconnect without re-registering. The nightly job already restores every archive into a network-less scratch
headscale and compares nodes and noise key with production; a failure alerts via ntfy.

## Stack missing after a reboot

**node-admin → `diagnose-boot`** (read-only) writes to the run's Summary: reboots, expected vs running services, each
container's state/exit code/restart count, the Docker journal of the last three boots, OOM kills, unattended-upgrades
and dead-man history, and headscale's start/error lines. If the runner cannot join the tailnet, use break glass below.

Security updates that need a reboot apply at 04:00 America/Los_Angeles (`edge-reboot.timer`, `reboot_schedule`),
only when `/var/run/reboot-required` exists.

## After a deploy that failed part-way

Handlers queued by the failed run (reload headscale policy, restart Gatus/Caddy/ntfy) are skipped, and the re-run does not queue them again
because the files already match. Fix the cause, re-run `deploy` (mode `tailnet`), then apply what the failed run wrote but never loaded. Read
the failed run's log: every task marked `changed` before the failure names the file.

| Changed task before the failure | Apply after the successful re-run |
|---|---|
| Headscale policy | `ssh david_admin@edge 'sudo docker compose -f /opt/edge/compose.yml kill -s HUP headscale'` |
| Headscale config | `... restart headscale` |
| Gatus config or secrets | the re-run restarts Gatus itself when the env file changes; otherwise `... restart gatus` |
| Caddy secrets / Caddyfile | `... restart caddy` / the re-run reloads it |

A missing secret key is the most common cause: **init-secrets → `fill_missing`** (see `secrets/README.md`). Then run **verify**.

## Break glass

If headscale is down, SSH over the tailnet is too. Use the DigitalOcean web console (Droplet → Access → Recovery/Droplet
Console), log in as `david_admin` with the console password (SSH stays key-only), and `sudo docker compose -f /opt/edge/compose.yml up -d`.
Last resort: `deploy` → `bootstrap` only works on a fresh droplet (root login is disabled afterwards).
