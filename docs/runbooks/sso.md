# SSO (Authelia): platform admin reference

> **Status: design approved 2026-11-23, not deployed.** No `authelia` role, Caddy site or SOPS key exists yet. The PR that
> adds them updates this page to match the code. Items marked `TODO(verify)` are settled by the spike on the pinned release,
> not here. Decision record: `docs/decisions.md` § Design decisions, "SSO is Authelia"; full design and open items:
> `docs/superpowers/specs/2026-11-23-authelia-sso-respec.md`.

Authelia on the edge (`https://auth.public.<domain>`) is the one login (password + passkey or TOTP) and the one OAuth/OIDC
token issuer for the homelab apps. It proves who a person is and gates which apps they may open; each app owns its users and
roles. DigitalOcean xstockstrat deployments never use it (`AUTH_MODE=local`).

## 1. Model

| Concept | Lives in | Owner | Notes |
|---|---|---|---|
| Person (username, display name, email, password hash, groups, pinned `sub`) | `users_database.yml` and SOPS | platform admin | One account per person. Nobody edits their own password or email. |
| `sub` (UUID) | SOPS, applied to Authelia's SQLite at deploy | platform admin | Pinned per person, so restores and rebuilds keep every app link. |
| Second factor (passkey / TOTP) | Authelia SQLite | the person, enrolled with an admin-relayed code | Any TOTP app (e.g. Authy) works. |
| App access | the person's `groups` | platform admin | One group per app; a person holds any number. |
| App account, role, link to the `sub` | each app's database | each app's admin | Never in Authelia. xstockstrat roles: `manage-users.py update-roles`. |

| Group | Opens |
|---|---|
| `xstockstrat` | xstockstrat (prod) web + MCP (Claude.ai and Claude Code) |
| `xstockstrat-staging` | xstockstrat-staging web + MCP |
| `webhooks-mcp` | webhooks-mcp `/mcp` (the app also checks its `sub` allowlist) |
| `commsgun` | commsgun dashboard |
| `happybabytracker` | happybabytracker (parent or admin, as the app's own account says) |

Every app also keeps a local login for break-glass: Caddy admits its POST from the tailnet only. Gatus and Proxmox stay
outside SSO (a later phase). headscale device login never uses SSO.

## 2. Hard rules

1. **Never rename a username, and never change a pinned `sub`.** Every app link is keyed on `(issuer, sub)`. Change the
   display name or email instead. If a rename is unavoidable, see § 7.
2. **Never rename `auth.public.<domain>`.** It is every link's issuer and every passkey's RP ID.
3. **Group membership opens the door; the app grants the role.** A person in a group but not linked in that app is refused
   there until they link (§ 5).
4. **Authelia's SQLite is backed up twice a day and must stay so.** Pinned `sub`s survive its loss; second factors do not (§ 8).

## 3. Add a person

1. Decide the username (permanent, § 2.1), email and groups; generate a UUID for the `sub`.
2. Add them to SOPS with an argon2id hash made with the pinned image's hash command and the configured parameters.
   `TODO(verify)`: the SOPS key and the exact command.
3. Deploy (renders the users file, applies the `sub`).
4. They log in at `https://auth.public.<domain>` and start enrolling a passkey or TOTP app. Authelia writes a one-time code to
   its file notifier; an admin reads it on the edge and gives it to them. `TODO(verify)`: path and command.
5. In each app, link (§ 5).

## 4. Change access, password, second factor, or remove a person

| Change | Steps | Takes effect |
|---|---|---|
| Grant an app | add the group in SOPS, deploy; the person links in that app (§ 5) | after linking |
| Revoke an app | remove the group in SOPS, deploy; disable the user in that app; revoke their refresh tokens | new logins at once; app sessions end when disabled in the app |
| Reset a password | new hash in SOPS, deploy | next login |
| Lost device / reset second factor | delete the registration with the `authelia storage user` TOTP/WebAuthn commands (`TODO(verify)`), then § 3 step 4 | next login |
| Unban after regulation | `authelia storage bans` (`TODO(verify)` subcommand) | at once; bans also expire after 5 min |
| Change email | edit in SOPS, deploy | next login (xstockstrat copies it to linked accounts) |
| Remove a person | remove from SOPS, deploy; disable them in every app; drop their `sub` from webhooks-mcp's allowlist | as revoke |
| Change an xstockstrat role | `manage-users.py update-roles <email> --roles …` against that environment | next session JWT (web) or within ~60 s (MCP) |

## 5. Linking app accounts

Linking is always explicit. No app matches by email or username, and none creates accounts on first login unless its
auto-provision flag is on (off on the homelab).

- **xstockstrat (each environment):** the person signs in with their local password, presses **Link SSO**, re-authenticates,
  and confirms the account. A new person is first created with `manage-users.py create-user` (every xstockstrat user keeps a
  password). An unlinked SSO login or MCP token is refused with `not linked`.
- **commsgun / happybabytracker admin:** the admin signs in locally from the tailnet and presses **Link SSO**.
- **happybabytracker parents:** an existing parent links like the admin; a new parent is created by the admin, whose invite
  link can be redeemed with a password or with SSO.
- **webhooks-mcp:** no accounts; the person's pinned `sub` goes into its allowlist.

## 6. xstockstrat cutover (once per environment)

1. Before the first SSO login, the seeded admin row (`admin@localhost`, `services/xstockstrat-identity/migrations/002_seed_admin.up.sql`)
   gets the owner's password with `manage-users.py reset-password` if it is not known.
2. Set the confidential client secret and the issuer in that environment's config, and `auth.mode` to `both`.
3. Sign in with the password, **Link SSO**, sign out, sign in with SSO.
4. Connect the Claude.ai and Claude Code connectors; they sign in as the same person.
5. Rollback at any point: `auth.mode` back to `local`.

`SEED_USER_ID` (the `user_id` that owns seeded and legacy rows) is unaffected: linking keeps every `user_id`.

## 7. A username must change anyway

1. Create the new person (§ 3) with a new `sub`.
2. In each app, unlink the old identity (xstockstrat: `manage-users.py unlink-sso`) and link the new one (§ 5).
3. Remove the old person.

## 8. Outages and recovery

| Situation | Effect | Action |
|---|---|---|
| Authelia down | no new SSO logins; open app sessions keep working; MCP calls work until their access token expires | Gatus alerts via ntfy; fix and restart. Break-glass: each app's local login, from the tailnet. |
| Authelia restarted | portal sessions end (they are in memory); app sessions and refresh tokens survive | none |
| Authelia SQLite lost | second factors and refresh tokens gone; `sub`s re-applied at deploy | Restore from the edge backup (`docs/runbooks/recovery.md`). Without one: redeploy, everyone re-enrols a second factor; app links survive. |
| `users_database.yml` lost | nobody can log in | redeploy from SOPS |

headscale, SSH and the tailnet do not depend on Authelia; the break-glass path in `docs/runbooks/recovery.md` is unchanged.
