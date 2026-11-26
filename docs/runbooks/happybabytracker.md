# happybabytracker (family tracker)

Parents track their children's feeding, sleep, growth and health in a web app. The admin account only creates parents and
issues invite and reset links; it can never see a child. Two containers on the edge, behind Caddy on one tailnet-only name
(no ACME work needed):

| Name | Container | Host port | Use |
|---|---|---|---|
| `happybaby.private.<domain>` (`happybabytracker_ui_private_hostname`) | `happybabytracker-ui` | `127.0.0.1:9212` | the only thing a browser talks to (its `ORIGIN`); also serves `/healthz` and `/healthz/api` for Gatus |
| none (compose network `happybabytracker`) | `happybabytracker-api` | not published (9210) | reached by the UI only, as `http://happybabytracker-api:9210` |

The UI answers `400` on every path except the health paths when the request's host is not its `ORIGIN`, so it is usable on
one name at a time. `ORIGIN` and `PUBLIC_ORIGIN` (the host of the invite and reset links) are set in
`roles/stack/tasks/main.yml` ("Happybabytracker API environment", "Happybabytracker UI environment"). Who may reach the
name is accepted as for the commsgun dashboard: `docs/decisions.md` § Known traps.

The service's own docs: `README.md` and `docs/superpowers/specs/` in `davcs86/happybabytracker`. Traps: `docs/decisions.md`.
Secret shapes: `secrets/README.md`.

Commands below run from the repository root; `sops` needs `SOPS_AGE_SSH_PRIVATE_KEY_FILE=~/keys/sops_admin` (as in
`secrets/README.md` § Read a value locally).

## 1. First deploy

Before the deploy, in this repository:

1. **Generated secrets.** Run **Actions → init-secrets** with `fill_missing` (never `overwrite`) on the branch that holds
   the code, or on `main` once it is merged. It adds `happybabytracker_admin_password`, `happybabytracker_internal_api_token`
   and `happybabytracker_vapid_private_key` and keeps every existing key. The run needs approval on the `production`
   environment, which `docs/runbooks/github-push-token.md` restricts to `main`: on any other branch it is blocked until the
   branch is allowed (`docs/runbooks/commsgun.md` § 2 step 3 describes the same constraint and what to read first).
2. **Admin username.** Choose it, then add it by hand (`fill_missing` keeps it, `overwrite` deletes it):

   ```sh
   sops set secrets/edge.sops.yaml '["happybabytracker_admin_username"]' '"<name>"'
   ```

   It must match `^[a-z0-9._-]{3,32}$` (lowercase only) and, because the deploy writes it into a single-quoted env file,
   contain no quote, backslash or line break. It must not equal the username of any parent, now or later: creating such a
   parent is refused, and an API that finds one at start refuses to start. Commit and get it merged to `main`
   before step 3; the deploy does not see a value that is only in your working copy or on an unmerged branch.
3. **Deploy.** Run **Actions → deploy** (`tailnet`). Merging the PR that brought the code deployed nothing. The stack role
   starts the containers under `--wait`, but the API has no healthcheck and runs `restart: unless-stopped`, so a bad secret or a
   username that collides with a parent makes the API refuse to start and crash-loop while the deploy may still pass. After the
   deploy, check `curl http://127.0.0.1:9212/healthz/api` from the edge, the Gatus probe `happybabytracker-api-private`, and
   `sudo docker compose -f /opt/edge/compose.yml logs happybabytracker-api`. If the deploy
   fails anywhere in the stack role, follow `docs/runbooks/recovery.md` § "After a deploy that failed part-way" before
   re-running. On the first deploy Gatus may alert once for the two happybabytracker probes and then resolve: they start before
   the deploy writes the split-horizon record for the name.

   The app's first start creates `happybabytracker.db` in its volume, and the backup job fails, headscale part included,
   whenever that file is missing (`edge-backup` stops before it writes the archive). The deploy starts the stack before it installs the
   timer, so this holds after any successful deploy; do not remove the volume `edge_happybabytracker-data` without
   starting the app again before the next 00:00 or 12:00 UTC.

Verify from a tailnet device that may reach the edge on 443 (your own devices). Replace `<domain>`:

```sh
for p in healthz healthz/api login; do curl -sS -o /dev/null -w "$p %{http_code}\n" "https://happybaby.private.<domain>/$p"; done
```

Each prints `200`. Then check:

- **Unreachable from outside.** From a device off the tailnet (phone on mobile data, tailscale off),
  `curl -sS --max-time 10 -o /dev/null -w '%{http_code}\n' https://happybaby.private.<domain>/healthz` prints `000` or fails to
  resolve; it may not return an HTTP status.
- **Gatus.** `https://status.private.<domain>`, group `edge`: `happybabytracker-ui-private` (`/healthz`, certificate lifetime
  too) and `happybabytracker-api-private` (`/healthz/api`) are green.
- **The API is not published.** `ssh david_admin@edge 'sudo docker compose -f /opt/edge/compose.yml ps happybabytracker-api happybabytracker-ui'`
  shows both running and a port mapping only for the UI (`127.0.0.1:9212->9212`).
- **Backup.** `ssh david_admin@edge 'systemctl list-timers edge-backup.timer'` shows the next run at 00:00 or 12:00 UTC (up
  to 10 minutes later). To prove it now:
  `ssh david_admin@edge 'sudo systemctl start edge-backup.service; sudo journalctl -u edge-backup.service --since -15min --no-pager -o cat'`
  ends with `happybabytracker database: integrity ok` and `restore drill OK`, and the service finishes without a failure
  (it then pushes the heartbeat).
- **Sign in.** Open `https://happybaby.private.<domain>/login`: the admin username from SOPS and the password from
  `sops decrypt --extract '["happybabytracker_admin_password"]' secrets/edge.sops.yaml`. The admin lands on the Parents page.

If a deploy stops at one of the secret checks, Ansible 2.21 hides which key failed (the block runs under `no_log: true`, and
the failing assert's message is censored with it); the task name still shows. Read it, then check the three values without
printing them (this prints each key's length and whether it holds a forbidden character):

```sh
for k in happybabytracker_admin_username happybabytracker_admin_password happybabytracker_internal_api_token; do
  printf '%s: ' "$k"
  sops decrypt --extract "[\"$k\"]" secrets/edge.sops.yaml | python3 -c 'import sys; v = sys.stdin.read(); print(len(v), "characters;", "BAD (quote, backslash or line break)" if any(c in v for c in "\x27\\\r\n") else "no forbidden character")'
done
```

| Failing task | Cause | Limit |
|---|---|---|
| "Happybabytracker secrets are present and fit a single-quoted env line" | a key is missing, empty, or holds a quote, backslash or line break | all three keys, the username included |
| "Happybabytracker secrets are as long as the app requires" | the password or the token is too short | password 16 or more, token 32 or more (generated ones are 48) |
| "Happybabytracker admin username has the shape the app accepts" | the username has an uppercase letter or another character, or is not 3 to 32 long | `^[a-z0-9._-]{3,32}$` |
| "Happybabytracker VAPID key has the shape the app accepts" | `happybabytracker_vapid_private_key` is missing or is not 43 unpadded base64url characters | `^[A-Za-z0-9_-]{43}$` |

## 2. Add a parent

1. Sign in as the admin and open **Parents**. Enter a username (3 to 32 characters of `a-z`, `0-9`, `.`, `_`, `-`;
   permanent) and a display name, then **Add parent**.
2. The page shows an invite link once, in that response only: copy it now and send it to the parent. It works once and
   expires in 72 hours. If it is lost, press **Reset link** on the parent's row: for a parent who has not set a password
   yet, that issues a new invite.
3. The parent opens the link and chooses a password (at least 12 characters). Status **Active** on the Parents page confirms it.

The admin can also disable and enable parents. The API refuses to disable a parent who is the only active guardian of a
child, and says only that, with no name or count.

## 3. Reset a password

On the Parents page press **Reset link** on the parent's row and send the link. It works once and expires in 1 hour;
using it signs that parent out everywhere. A parent who is signed in changes their own password in the app. Nobody,
the admin included, can read a password.

Check: the parent opens the link, sets a new password and signs in with it; the old password is refused.

## 4. Rotate the admin password, the admin username or the internal token

1. Replace the value in SOPS. The admin password needs at least 16 characters and the token at least 32; none may contain a
   quote, backslash or line break:

   ```sh
   sops set secrets/edge.sops.yaml '["happybabytracker_admin_password"]' "\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(36))')\""
   ```

   The same works for `happybabytracker_internal_api_token`. Never rotate `happybabytracker_vapid_private_key` casually
   (section 7). A new admin username must not equal any parent's.
2. Commit, merge, run **Actions → deploy** (`tailnet`). The deploy rewrites `/opt/edge/happybabytracker/api.env` and `ui.env` and
   recreates the containers whose environment changed (the token is in both files, the password and username only in the API's);
   the data volume is kept.
3. The admin's sessions end at that start: the API stores a keyed fingerprint of the admin username, password and internal
   token, and deletes every admin session when it differs from the last start, so changing any of the three signs the admin
   out. Parents' sessions are not affected. Sign in again with the new value.

Check: the old admin password is refused at `/login` and the new one works.

## 5. Restore the data

`docs/runbooks/recovery.md` § Restore happybabytracker.

## 6. Backups

The edge backup job covers this app twice a day (00:00 and 12:00 UTC, up to 10 minutes later): `edge-backup` snapshots
`happybabytracker.db` into the same `age`-encrypted archive as headscale, copies it to the backup host and the restore drill
checks the archived database with `PRAGMA integrity_check`. The Gatus heartbeat is still named `nightly-backup` although it
now runs twice a day (the pushers use that name: EDGE-10); it alerts after 14 hours of silence. SSH host-key pinning for the
backup host is deferred: `backup_host_known_hosts` keeps its `TODO(provide)`, so the first connection trusts the key it
sees (`StrictHostKeyChecking=accept-new`). The archives now hold health data; give the value (the output of
`ssh-keyscan` for the backup host) when you have it.

## 7. The VAPID key (Web Push)

The deploy renders `happybabytracker_vapid_private_key` into `api.env` as `VAPID_PRIVATE_KEY`, with
`VAPID_SUBJECT='https://<happybabytracker_ui_private_hostname>'`, always as a pair (the API refuses to start with only one of
the two). Before it writes the file, "Happybabytracker VAPID key has the shape the app accepts" checks that the key is
43 unpadded base64url characters. `HBT_ALLOW_FAKE_PUSH` is a test switch and is never rendered on the edge.

An API image older than the app's reminders release (plan 6) does not read either variable, so Web Push stays off until
that image is deployed. From then on the API logs a `channels` line with `push` `on` at every start:

```sh
ssh david_admin@edge 'sudo docker compose -f /opt/edge/compose.yml logs happybabytracker-api | grep channels | tail -n 1'
```

and a parent's **Account → Notifications** page (`/account/notifications`) no longer says "Web Push is not configured on this server.". Each
parent turns notifications on per device there; on iPhone and iPad only from the app added to the Home Screen (iOS and
iPadOS 16.4 or later). The app's `docs/runbooks/reminders.md` covers a reminder that did not arrive.

Changing the key stops delivery to every registered device: each parent must turn notifications off and on again on each
device. `init-secrets` `fill_missing` never replaces it; `overwrite` does, so avoid `overwrite` once the app is live.

To turn Web Push off, remove both `VAPID_*` lines (and the assert) from "Happybabytracker API environment" and deploy;
devices stay registered.

## 7a. The US provider lookup (NPI_SEARCH)

`happybabytracker_npi_search` in `inventories/prod/group_vars/all/main.yml` (default `true`) renders `NPI_SEARCH='1'`
into `api.env`; `false` renders no line, which turns the lookup off. The API accepts only `1` or unset and refuses to start
with any other value, so never set the variable another way. With it on, the API searches the US national provider
registry (NPPES) at `https://npiregistry.cms.hhs.gov` over the droplet's open outbound HTTPS; no secret and no egress rule
is needed. An API image older than happybabytracker #25 (plan 10) ignores the variable.

To turn it off: set `happybabytracker_npi_search: false`, merge, run **Actions → deploy** (`tailnet`); the deploy rewrites
`api.env` and recreates the API container. After the first deploy with it on, check one search by hand in the visit form
("Find a US provider"): the app's smoke test never calls the real registry.

## 8. Image updates

The `bump-happybabytracker` workflow opens one PR (branch `bump/happybabytracker`) that pins both
`happybabytracker_api_image` and `happybabytracker_ui_image`: daily, or at once from **Actions → bump-happybabytracker →
Run workflow** (optional digests to pin instead of the newest). It proposes nothing when the two images' revision labels
differ or are missing, so the API and the UI never ship from different commits. Merging deploys nothing: run
**Actions → deploy** (`tailnet`) afterwards. A docs- or CI-only merge in `happybabytracker` also produces a bump PR that
changes nothing on the edge. Pin format and rationale: `docs/versions.md`. Check: after the deploy,
`ssh david_admin@edge 'sudo docker compose -f /opt/edge/compose.yml ps happybabytracker-api happybabytracker-ui'` shows both
running and the Gatus probes stay green.

## 9. Logs

The containers log JSON lines of ids and enumerated facts, never a name, username, note, value or secret:

```sh
ssh david_admin@edge 'sudo docker compose -f /opt/edge/compose.yml logs --since 1h happybabytracker-api happybabytracker-ui'
```

Invite and reset tokens travel in the URL path (`/invite/<token>`, `/reset/<token>`). The `*.private` site's Caddy access log,
`/var/log/caddy/private.access.log`, drops query strings, `Referer` and redirect `Location` but keeps the path, so it holds
those tokens until the active file reaches 50 MiB and its rolled copy ages out (`roll_keep_for` 720 hours; at most 5 rolled copies). The owner accepted that: a token works once and expires in 72 hours
(invite) or 1 hour (reset). Caddy's `redacted_requests` logger also keeps the path for `http.handlers.reverse_proxy` and `http.log.error.private_zone` entries
and writes it to stderr, so the tokens are in the Docker `local` log (10 MiB, 3 files) of the `caddy` container as well. Nothing is shipped from the edge to OTEL: no log shipper exists in `roles/`, `inventories/` or
`playbooks/`. Whoever adds one must scrub `/invite/*` and `/reset/*` before anything is exported, because the app's own rule
is that those tokens never reach OTEL.
