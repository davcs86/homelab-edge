# SSO (Authelia): platform admin reference

> **Status: design approved 2026-11-23, not deployed.** No `authelia` or `lldap` role, Caddy site or SOPS key exists yet.
> The PR that adds them updates this page to match the code. Items marked `TODO(verify)` are settled by the spike on the
> pinned release, not here. Decision record: `docs/decisions.md` § Design decisions, "SSO is Authelia"; full design and open
> items: `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md`.

Authelia on the edge (`https://auth.public.<domain>`) is the one login (password + passkey or TOTP) and the one OAuth/OIDC
token issuer for the homelab apps. It proves who a person is and gates which apps they may open; each app owns its users and
roles. People and passwords live in lldap (admin UI, tailnet-only: `https://lldap.private.<domain>`). DigitalOcean
xstockstrat deployments never use SSO (`AUTH_MODE=local`).

## 1. Model

| Concept | Lives in | Owner | Notes |
|---|---|---|---|
| Person (username, display name, email, groups) | lldap only (never SOPS or this repo) | platform admin, in lldap's UI | One account per person. Nobody edits their own email. Every person is in `sso_people`. |
| Password | lldap only (never SOPS) | the person | Set and reset with **Forgot password**; the link lasts 1 h. |
| `sub` (UUID) | Authelia's SQLite and the person's `sso_sub` attribute in lldap | the deploy | Minted at first login; each deploy copies it to lldap, and restores either copy from the other, so links survive losing one database. |
| Second factor (passkey / TOTP) | Authelia SQLite | the person | Any TOTP app (e.g. Authy) works. Another device needs an existing one; losing every device needs an admin. |
| App access | the person's groups | platform admin | One group per app; a person holds any number. |
| App account, role, link to the `sub` | each app's database | each app's admin | Never in Authelia. xstockstrat roles: `manage-users.py update-roles`. |

| Group | Opens |
|---|---|
| `sso_people` | the portal itself (everyone; removing it is how a person is disabled) |
| `xstockstrat` | xstockstrat (prod) web + MCP (Claude.ai and Claude Code) |
| `xstockstrat-staging` | xstockstrat-staging web + MCP |
| `webhooks-mcp` | webhooks-mcp `/mcp` (only this group's clients get the `webhooks-mcp` scope the app checks) |
| `commsgun` | commsgun dashboard |
| `happybabytracker` | happybabytracker (parent or admin, as the app's own account says) |

Each app runs `AUTH_MODE=oidc`. Switching an app to `both` turns its local login back on (public, behind Caddy's login
limits) for linking or break-glass; switch it back afterwards. Gatus and Proxmox stay outside SSO (a later phase). headscale
device login never uses SSO.

## 2. Hard rules

1. **Never rename or reuse a username, and never change a pinned `sub`.** Every app link is keyed on `(issuer, sub)`; a
   reused username would inherit the old person's links. Change the display name or email instead; for a rename, § 7.
2. **Never rename `auth.public.<domain>`, and never rotate lldap's `key_seed`.** The hostname is every link's issuer and every
   passkey's RP ID; stored passwords depend on the seed.
3. **Group membership opens the door; the app grants the role.** A person in a group but not linked in that app is refused
   there until they link (§ 5).
4. **Authelia's and lldap's SQLite are backed up twice a day and must stay so.** lldap's is the only copy of people and
   passwords; each `sub`, once a deploy has copied it, survives the loss of either database, not both (§ 8).
5. **Passwords never go into SOPS or this repo.** lldap is their only writer.
6. **One browser profile per account** for anyone with several (`david`, `david_admin`, `david_claude`): the portal cookie
   is shared, so one profile would land you in an app as the wrong account.

## 3. Add a person

1. Decide the username (permanent, § 2.1), email and groups.
2. In lldap's UI (`https://lldap.private.<domain>`, signed in as lldap's `admin`), create the user with that username,
   display name and email, no password, and add them to `sso_people` and their app groups.
3. At `https://auth.public.<domain>` they press **Forgot password**, set a password from the emailed link (valid 1 h), log
   in, and enrol a passkey or TOTP app with an emailed code. Until commsgun's SMTP listener ships, the file notifier holds
   the link and the code; an admin reads them on the edge and passes them on. `TODO(verify)`: path and command. For a new
   account this first enrolment rests on the mailbox alone; enrol with the person to close that.
4. In each app, link (§ 5).
5. Deploy: it copies their new `sub` into lldap (until then it exists only in Authelia and its backup).

## 4. Change access, password, second factor, or remove a person

| Change | Steps | Takes effect |
|---|---|---|
| Grant an app | add the group in lldap's UI; the person links in that app (§ 5) | after linking |
| Revoke an app | remove the group in lldap's UI; disable the user in that app (webhooks-mcp has none) | new logins at once; app sessions end when disabled in the app; **webhooks-mcp: not ended** (refresh survives a group removal, spike S4; only removing the person does it, owner call in SSO spec § 12) |
| Reset a password | the person uses **Forgot password** | at once; login still needs the second factor |
| Lost every device | `authelia storage user totp delete <user>` / `authelia storage user webauthn delete <user>`, then re-enrol **with the admin present** (with no device left, the emailed code alone would suffice) | next login |
| Unban after regulation | `authelia storage bans user revoke <user>` | at once; bans also expire after 5 min |
| Change email | edit it in lldap's UI | at once for Authelia's emails; xstockstrat keeps its own copy, commsgun and happybabytracker store none; nothing syncs it |
| Remove a person | remove `sso_people` and their app groups in lldap's UI (no login, no reset); in every app disable them (xstockstrat, in each environment: deactivate them on `/config-ui/users`, then `manage-users.py unlink-sso <email>`; commsgun: `docker compose run --rm -T commsgun-admin admin disable <username>`; happybabytracker: the admin disables or unlinks the parent, which also deletes their push subscriptions and ntfy topic: until then medication reminders keep reaching their devices; if the person is happybabytracker's admin, `docker compose run --rm -T happybabytracker-api admin unlink-sso <username>` and `admin reset-password <username>`, since the admin is never deleted; webhooks-mcp: nothing beyond the group); delete their Authelia identifier and clear their `sso_sub` in lldap (or a later deploy re-applies it) | at once for new logins; refresh tokens per spike S24, app-side refusal otherwise (webhooks-mcp has none: an issued access token lasts ≤ 1 h) |
| Change an xstockstrat role | `manage-users.py update-roles <email> --roles …` against that environment | web: at the next access-JWT refresh, up to `jwt.access_ttl_seconds` (default 900 s); MCP: within ~60 s |

## 5. Linking app accounts

Linking is always explicit. No app matches by email or username, and none creates accounts on first login (no app
implements auto-provisioning; never set `OIDC_AUTO_PROVISION`, which commsgun and happybabytracker refuse even as `false`).
Where a local login is needed, switch the app to `both` for the window. For xstockstrat a `both` window cuts every
Authelia-connected MCP connector off until it is back on `oidc` (its MCP uses the built-in OAuth server in `both`);
reconnect each one afterwards (§ 6).

- **xstockstrat (each environment):** the person signs in with their local password, presses **Link SSO**, re-authenticates,
  and confirms the account. A new person is first created with `manage-users.py create-user` (every xstockstrat user keeps a
  password). An unlinked SSO login or MCP token is refused with `not linked`.
- **commsgun / happybabytracker admin:** the admin signs in locally, re-authenticates and presses **Link SSO**.
- **happybabytracker parents:** an existing parent links like the admin; a new parent is created by the admin, whose invite
  link is redeemed with SSO in `oidc` (a password redemption works only in `local`/`both`; reset links only in `both`). The admin can unlink a parent's SSO identity; unlinking also deletes
  their push subscriptions and ntfy topic.
- **webhooks-mcp:** no accounts and nothing to link: group `webhooks-mcp` grants the scope it checks.

## 6. xstockstrat cutover (once per environment)

Gates before the first `both` (xstockstrat 345 design § 6); each is a hard stop:
- the login zones cover `POST /accounts/api/sso/confirm` (SSO spec § 4.3), pinned in `caddy-routing-test.sh`;
- spike S17 shows a fresh `auth_time` on `prompt=login` + `max_age=0`;
- the web client is registered with `openid profile email` and the claims policy (SSO spec § 4.6);
- the seeded admin row has the owner's password (step 1).

1. Before the first SSO login, the seeded admin row (`admin@localhost`, `services/xstockstrat-identity/migrations/002_seed_admin.up.sql`)
   gets the owner's password with `manage-users.py reset-password` if it is not known.
2. In that environment's config set `ui/oidc.issuer`, `ui/oidc.client_id`, `ui/oidc.client_secret` (copied from the edge's
   SOPS), the four `ui/oidc.mcp_client_id*` keys and `agent/oidc.issuer` (empty in `oidc`, the agent answers 503), then
   `manage-users.py set-auth-mode both --environment <production|staging> --reason …` (345 FR-17). xstockstrat's own
   runbook, `docs/runbooks/authelia-sso-cutover.md` (added by 345), has the exact steps. From here until step 4, MCP
   connectors use the built-in OAuth server.
3. Sign in with the password, **Link SSO**, sign out, sign in with SSO. Stop Authelia briefly: the password login still works.
4. `set-auth-mode oidc`. Until then MCP stays on xstockstrat's built-in OAuth server (in `local` and `both`).
5. Connect the Claude.ai and Claude Code connectors to Authelia with the pre-registered client IDs (xstockstrat
   `docs/runbooks/mcp-tools.md`, 345 FR-13); they sign in as the same person.

Rollback at any point: `manage-users.py set-auth-mode local --environment … --reason …`, run over the tailnet without
a login. The built-in server registers clients dynamically: clear each
connector's pre-registered client ID, then reconnect; paste it back when returning to `oidc`.

## 6b. webhooks-mcp cutover

1. Deploy with `AUTH_MODE=static+oauth`, `OAUTH_ISSUER=https://auth.public.<domain>` (no trailing slash) and
   `OAUTH_RESOURCE=https://webhooks-mcp.public.<domain>/mcp` (ending exactly in `/mcp`); the three are set and removed
   together (webhooks-mcp contract C4). Both the static key and Authelia tokens work.
2. Move every `MCP_AUTH_KEY` consumer: the Claude.ai and Claude Code connectors sign in as `david_claude`; the headless
   consumer (`TODO(provide)`: which) uses `webhooks-mcp-automation`, whose plaintext secret lives in the edge's SOPS
   (SSO spec § 4.7) and is copied to that consumer by hand.
3. Set `AUTH_MODE=oauth` and retire `MCP_AUTH_KEY`. Rollback: back to `static+oauth` (or `static`), the key still in SOPS
   until this step is proven.

`SEED_USER_ID` (the `user_id` that owns seeded and legacy rows) is unaffected: linking keeps every `user_id`.

## 7. A username must change anyway

1. Create the new person (§ 3); they get a new `sub` at first login.
2. In each app, unlink the old identity and link the new one (§ 5): xstockstrat `manage-users.py unlink-sso <email>`;
   commsgun `docker compose run --rm -T commsgun-admin admin unlink <username>`; happybabytracker parents through the
   admin's unlink, its admin through `docker compose run --rm -T happybabytracker-api admin unlink-sso <username>`;
   webhooks-mcp nothing to unlink (give the new person group `webhooks-mcp`).
3. Remove the old person (§ 4), including their Authelia identifier; the old username is never reused.

## 8. Outages and recovery

| Situation | Effect | Action |
|---|---|---|
| Authelia down | no new SSO logins; open app sessions keep working; MCP calls work until their access token expires | Gatus alerts via ntfy; fix and restart. Break-glass: switch the app to `both` and use its local login. A happybabytracker parent who joined by SSO has no password: the admin sends a reset link, redeemable in `both`. For xstockstrat that ends MCP access through Authelia at once: its MCP moves to the built-in server (§ 6 rollback). |
| Authelia restarted | portal sessions end (they are in memory); app sessions and refresh tokens survive | none |
| Authelia SQLite lost | second factors and refresh tokens gone; `sub`s re-applied from lldap before it serves | Restore from the edge backup (`docs/runbooks/recovery.md`) **before anyone logs in**. Without one: redeploy and re-enrol everyone with an admin present; app links survive. |
| lldap down | no logins and no resets | Gatus alerts; fix and restart. Break-glass as for Authelia down. |
| lldap SQLite lost | people and passwords gone | Restore from the edge backup. Without one: recreate each person by hand with the same username; the next deploy copies each `sub` back from Authelia; everyone resets their password. Both databases lost without backups: people relink in every app (§ 7). |
| commsgun down | no reset or enrolment emails; logins unaffected | fix commsgun; meanwhile switch Authelia to the file notifier and relay by hand |

headscale, SSH and the tailnet do not depend on Authelia; the break-glass path in `docs/runbooks/recovery.md` is unchanged.
