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
| Person (username, display name, email, groups, pinned `sub`) | SOPS, synced into lldap at deploy | platform admin | One account per person. Nobody edits their own email. Every person is in `sso_people`. |
| Password | lldap only (never SOPS) | the person | Set and reset with **Forgot password**; the link lasts 1 h. |
| `sub` (UUID) | SOPS, applied to Authelia's SQLite before it serves | platform admin | Pinned, so restores and rebuilds keep every app link. |
| Second factor (passkey / TOTP) | Authelia SQLite | the person | Any TOTP app (e.g. Authy) works. Another device needs an existing one; losing every device needs an admin. |
| App access | the person's groups | platform admin | One group per app; a person holds any number. |
| App account, role, link to the `sub` | each app's database | each app's admin | Never in Authelia. xstockstrat roles: `manage-users.py update-roles`. |

| Group | Opens |
|---|---|
| `sso_people` | the portal itself (everyone; removing it is how a person is disabled) |
| `xstockstrat` | xstockstrat (prod) web + MCP (Claude.ai and Claude Code) |
| `xstockstrat-staging` | xstockstrat-staging web + MCP |
| `webhooks-mcp` | webhooks-mcp `/mcp` (the app also checks its `sub` allowlist) |
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
4. **Authelia's and lldap's SQLite are backed up twice a day and must stay so.** Pinned `sub`s survive Authelia's loss;
   second factors do not. People survive lldap's loss; passwords do not (§ 8).
5. **Passwords never go into SOPS or this repo.** lldap is their only writer.
6. **One browser profile per account** for anyone with several (`david`, `david_admin`, `david_claude`): the portal cookie
   is shared, so one profile would land you in an app as the wrong account.

## 3. Add a person

1. Decide the username (permanent, § 2.1), email and groups; generate a UUID for the `sub`.
2. Add them to SOPS (no password). `TODO(verify)`: the SOPS key.
3. Deploy (creates them in lldap in `sso_people` with no password, applies the `sub` before Authelia serves).
4. At `https://auth.public.<domain>` they press **Forgot password**, set a password from the emailed link (valid 1 h), log
   in, and enrol a passkey or TOTP app with an emailed code. Until commsgun's SMTP listener ships, the file notifier holds the
   link and the code; an admin reads them on the edge and passes them on. `TODO(verify)`: path and command. For a new account
   this first enrolment rests on the mailbox alone; enrol with the person to close that.
5. In each app, link (§ 5).

## 4. Change access, password, second factor, or remove a person

| Change | Steps | Takes effect |
|---|---|---|
| Grant an app | add the group in SOPS, deploy; the person links in that app (§ 5) | after linking |
| Revoke an app | remove the group in SOPS, deploy; disable the user in that app | new logins at once; app sessions end when disabled in the app |
| Reset a password | the person uses **Forgot password** | at once; login still needs the second factor |
| Lost every device | `authelia storage user totp delete <user>` / `authelia storage user webauthn delete <user>`, then re-enrol **with the admin present** (with no device left, the emailed code alone would suffice) | next login |
| Unban after regulation | `authelia storage bans user revoke <user>` | at once; bans also expire after 5 min |
| Change email | edit in SOPS, deploy | next login (xstockstrat copies it to linked accounts) |
| Remove a person | remove from SOPS, deploy (drops `sso_people`: no login, no reset); disable them in every app; drop their `sub` from webhooks-mcp's allowlist; delete their Authelia identifier | at once for new logins; refresh tokens per spike S24, app-side refusal otherwise |
| Change an xstockstrat role | `manage-users.py update-roles <email> --roles …` against that environment | next session JWT (web) or within ~60 s (MCP) |

## 5. Linking app accounts

Linking is always explicit. No app matches by email or username, and none creates accounts on first login unless its
auto-provision flag is on (off on the homelab). Where a local login is needed, switch the app to `both` for the window.

- **xstockstrat (each environment):** the person signs in with their local password, presses **Link SSO**, re-authenticates,
  and confirms the account. A new person is first created with `manage-users.py create-user` (every xstockstrat user keeps a
  password). An unlinked SSO login or MCP token is refused with `not linked`.
- **commsgun / happybabytracker admin:** the admin signs in locally and presses **Link SSO**.
- **happybabytracker parents:** an existing parent links like the admin; a new parent is created by the admin, whose invite
  link can be redeemed with a password or with SSO.
- **webhooks-mcp:** no accounts; the person's pinned `sub` goes into its allowlist.

## 6. xstockstrat cutover (once per environment)

1. Before the first SSO login, the seeded admin row (`admin@localhost`, `services/xstockstrat-identity/migrations/002_seed_admin.up.sql`)
   gets the owner's password with `manage-users.py reset-password` if it is not known.
2. Set the confidential client secret and the issuer in that environment's config, and `auth.mode` to `both`.
3. Sign in with the password, **Link SSO**, sign out, sign in with SSO. Stop Authelia briefly: the password login still works.
4. Connect the Claude.ai and Claude Code connectors; they sign in as the same person.
5. Set `auth.mode` to `oidc`. Rollback at any point: `auth.mode` to `local`, set over the tailnet without a login.

`SEED_USER_ID` (the `user_id` that owns seeded and legacy rows) is unaffected: linking keeps every `user_id`.

## 7. A username must change anyway

1. Create the new person (§ 3) with a new `sub`.
2. In each app, unlink the old identity (xstockstrat: `manage-users.py unlink-sso`) and link the new one (§ 5).
3. Remove the old person (§ 4), including their Authelia identifier; the old username is never reused.

## 8. Outages and recovery

| Situation | Effect | Action |
|---|---|---|
| Authelia down | no new SSO logins; open app sessions keep working; MCP calls work until their access token expires | Gatus alerts via ntfy; fix and restart. Break-glass: switch the app to `both` and use its local login. |
| Authelia restarted | portal sessions end (they are in memory); app sessions and refresh tokens survive | none |
| Authelia SQLite lost | second factors and refresh tokens gone; `sub`s re-applied before it serves | Restore from the edge backup (`docs/runbooks/recovery.md`) **before anyone logs in**. Without one: redeploy and re-enrol everyone with an admin present; app links survive. |
| lldap down | no logins and no resets | Gatus alerts; fix and restart. Break-glass as for Authelia down. |
| lldap SQLite lost | passwords gone; people recreated from SOPS at the next deploy | Restore from the edge backup; without one, redeploy and everyone resets their password. |
| commsgun down | no reset or enrolment emails; logins unaffected | fix commsgun; meanwhile switch Authelia to the file notifier and relay by hand |

headscale, SSH and the tailnet do not depend on Authelia; the break-glass path in `docs/runbooks/recovery.md` is unchanged.
