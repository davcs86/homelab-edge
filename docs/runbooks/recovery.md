# Recovery

## Restore headscale from backup

Archives (`edge-<UTC>.tar.age`) are on the backup host and in `/var/backups/edge` (7 days). Decrypt with the
**offline** backup key: `age -d -i backup edge-….tar.age | tar -x` → `headscale/{db.sqlite,noise_private.key,derp_server_private.key,…}`.
On the edge: `docker compose -f /opt/edge/compose.yml stop headscale`, copy `db.sqlite` and both keys into
`/var/lib/docker/volumes/edge_headscale-data/_data/`, start headscale. The noise key is what lets existing clients
reconnect without re-registering. Each backup run (twice a day) already restores its archive into a network-less scratch
headscale and compares nodes and noise key with production, and checks the archived happybabytracker database with
`PRAGMA integrity_check`; a failure alerts via ntfy.

## Restore happybabytracker

Decrypt the same archive with the **offline** key (`age -d -i backup edge-….tar.age | tar -x`): the database is
`happybabytracker/happybabytracker.db`. Copy it to `david_admin`'s home on the edge, then run there:

```sh
sudo docker compose -f /opt/edge/compose.yml stop happybabytracker-ui happybabytracker-api
V=/var/lib/docker/volumes/edge_happybabytracker-data/_data
for f in happybabytracker.db happybabytracker.db-wal happybabytracker.db-shm; do
  if sudo test -e "$V/$f"; then sudo mv "$V/$f" "$V/$f.pre-restore"; fi
done
sudo cp ./happybabytracker.db "$V"/
sudo chown 10001:10001 "$V"/happybabytracker.db
sudo docker compose -f /opt/edge/compose.yml start happybabytracker-api happybabytracker-ui
curl -fsS http://127.0.0.1:9212/healthz/api
```

The three old files are moved aside together, not deleted, so the restore can be undone: the `-wal` and `-shm` belong to the
database being replaced and, renamed to `*.pre-restore`, the API no longer sees them beside the restored file. Once the
health check passes and you can sign in, remove them (only then; they are the only copy of anything
written since the archive):

```sh
sudo sh -c 'rm -f "${1:?}"/*.pre-restore' sh "$V"
```

The `chown` is needed because the API runs as uid 10001 and cannot open a file that belongs to root. The `curl` prints the
API's health through the UI's host port (retry after a few seconds if the API is still starting). The restored copy is as old
as the archive: parents created and passwords changed since then are lost.

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
| Caddy secrets / Caddyfile | `... restart caddy` / `... exec -T caddy caddy reload --config /etc/caddy/Caddyfile` (nothing in the re-run reloads it: the file is unchanged, and the inode check passes because the container already sees it) |

A missing secret key is the most common cause: **init-secrets → `fill_missing`** (see `secrets/README.md`). Then run **verify**.

## Break glass

If headscale is down, SSH over the tailnet is too. Use the DigitalOcean web console (Droplet → Access → Recovery/Droplet
Console), log in as `david_admin` with the console password (SSH stays key-only), and `sudo docker compose -f /opt/edge/compose.yml up -d`.
Last resort: `deploy` → `bootstrap` only works on a fresh droplet (root login is disabled afterwards).
