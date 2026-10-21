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

## Break glass

If headscale is down, SSH over the tailnet is too. Use the DigitalOcean web console (Droplet → Access → Recovery/Droplet
Console), log in as `david_admin` with the console password (SSH stays key-only), and `sudo docker compose -f /opt/edge/compose.yml up -d`.
Last resort: `deploy` → `bootstrap` only works on a fresh droplet (root login is disabled afterwards).
