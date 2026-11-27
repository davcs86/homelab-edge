# Recovery

## Restore headscale from backup

Archives (`edge-<UTC>.tar.age`) are on the backup host and in `/var/backups/edge` (7 days). Decrypt with the
**offline** backup key: `age -d -i backup edge-….tar.age | tar -x` → `headscale/{db.sqlite,noise_private.key,derp_server_private.key,…}`.
On the edge: `docker compose -f /opt/edge/compose.yml stop headscale`, copy `db.sqlite` and both keys into
`/var/lib/docker/volumes/edge_headscale-data/_data/`, start headscale. The noise key is what lets existing clients
reconnect without re-registering. Each backup run (twice a day) already restores its archive into a network-less scratch
headscale and compares nodes and noise key with production, and checks the archived happybabytracker, Authelia, lldap and
commsgun databases with `PRAGMA integrity_check`; a failure alerts via ntfy.

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

The three old files are moved aside together, not deleted: the `-wal` and `-shm` belong to the database being replaced and,
renamed to `*.pre-restore`, the API no longer sees them beside the restored file. To undo the restore, stop both containers,
`mv` any of the three `*.pre-restore` files that exist (the `-wal` and `-shm` copies often do not) back over their names, and
start them again:

```sh
sudo docker compose -f /opt/edge/compose.yml stop happybabytracker-ui happybabytracker-api
sudo rm -f "$V"/happybabytracker.db-wal "$V"/happybabytracker.db-shm
for f in happybabytracker.db happybabytracker.db-wal happybabytracker.db-shm; do
  if sudo test -e "$V/$f.pre-restore"; then sudo mv "$V/$f.pre-restore" "$V/$f"; fi
done
sudo docker compose -f /opt/edge/compose.yml start happybabytracker-api happybabytracker-ui
```

Once the health check passes and you can sign in, remove them (only then; they are the only copy of anything
written since the archive):

```sh
sudo sh -c 'rm -f "${1:?}"/*.pre-restore' sh "$V"
```

Run that cleanup before restoring a second time: otherwise the `mv` overwrites the earlier `*.pre-restore` files, which are the
only copy of anything written since the archive.

The `chown` is needed because the API runs as uid 10001 and cannot open a file that belongs to root. The `curl` prints the
API's health through the UI's host port (retry after a few seconds if the API is still starting). The restored copy is as old
as the archive: parents created and passwords changed since then are lost.

Last, remove the decrypted database (it is health data) from the edge, where you ran the commands above in `david_admin`'s
home, and from your workstation (delete what `tar -x` extracted there, `happybabytracker/` included):

```sh
rm -f ./happybabytracker.db
```

## Restore Authelia, lldap or commsgun

The same archive holds `authelia/db.sqlite3`, `lldap/users.db` and `commsgun/commsgun.db`. Decrypt it with the **offline**
key, copy the one file you restore to `david_admin`'s home on the edge, set the three values from its row, and run the
happybabytracker steps' shape:

| Restore | `S` (services to stop) | `VOL` | `DB` |
|---|---|---|---|
| Authelia | `authelia` | `edge_authelia-data` | `db.sqlite3` |
| lldap | `authelia lldap` (Authelia too, so it serves only after the reconcile) | `edge_lldap-data` | `users.db` |
| commsgun | `commsgun-ui commsgun-api` | `edge_commsgun-data` | `commsgun.db` |

```sh
S='authelia lldap' VOL=edge_lldap-data DB=users.db   # the lldap row, for example
sudo docker compose -f /opt/edge/compose.yml stop $S
V=/var/lib/docker/volumes/$VOL/_data
for f in "$DB" "$DB-wal" "$DB-shm"; do
  if sudo test -e "$V/$f"; then sudo mv "$V/$f" "$V/$f.pre-restore"; fi
done
sudo cp "./$DB" "$V"/
sudo chown 10001:10001 "$V/$DB"
```

- **Authelia or lldap:** do not `start` them; run **deploy** (`tailnet`). It starts lldap, runs the ensure and the `sub`
  reconcile (an identifier the restored Authelia copy lacks comes back from lldap; a `sub` the restored lldap copy lacks
  comes back from Authelia), and only then "Start the stack" starts Authelia, so it never serves before the reconcile. A
  mismatch stops the deploy: `docs/runbooks/sso.md` § 9.5.
- **commsgun:** `sudo docker compose -f /opt/edge/compose.yml start commsgun-api commsgun-ui`.

The restored copy is as old as the archive: Authelia loses second factors enrolled and refresh tokens issued since then,
lldap loses people and password changes, commsgun the messages queued. Undo and clean up the `*.pre-restore` files as in
§ Restore happybabytracker, with these `V` and `DB` (for Authelia or lldap, deploy instead of `start`), and delete the
decrypted file from the edge and your workstation: it holds credentials.

## Stack missing after a reboot

**node-admin → `diagnose-boot`** (read-only) writes to the run's Summary: reboots, expected vs running services, each
container's state/exit code/restart count, the Docker journal of the last three boots, OOM kills, unattended-upgrades
and dead-man history, and headscale's start/error lines. If the runner cannot join the tailnet, use break glass below.

Security updates that need a reboot apply at 02:00 America/Los_Angeles (`edge-reboot.timer`, `reboot_schedule`),
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
| Authelia configuration | nothing: the re-run compares the file's hash with the last applied one and restarts Authelia itself (no handler) |

A missing secret key is the most common cause: **init-secrets → `fill_missing`** (see `secrets/README.md`). Then run **verify**.

## Break glass

If headscale is down, SSH over the tailnet is too. Use the DigitalOcean web console (Droplet → Access → Recovery/Droplet
Console), log in as `david_admin` with the console password (SSH stays key-only), and `sudo docker compose -f /opt/edge/compose.yml up -d`.
Last resort: `deploy` → `bootstrap` only works on a fresh droplet (root login is disabled afterwards).
