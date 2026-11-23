# SSO (Authelia): platform admin reference

> **Status: design approved 2026-11-15, not deployed.** No `authelia` role, Caddy site or SOPS key exists yet. The PR
> that adds them updates this page to match the code. Items marked `TODO(verify)` are settled by the spike on the
> pinned release, not here. Decision record: `docs/decisions.md` § Design decisions, "SSO is Authelia".

Authelia on the edge is the one login (password + passkey or TOTP) and the one OAuth/OIDC token issuer for every
**public** app. It knows people and which apps they may open; it does not know app roles or xstockstrat environments.

## 1. Model

| Concept | Lives in | Owner | Notes |
|---|---|---|---|
| Person (username, display name, email, password hash, groups) | `users_database.yml`, rendered from SOPS | platform admin | One account per person. Users cannot edit their own email. |
| `sub` (opaque UUID) | Authelia SQLite | Authelia | Generated at a person's first login and bound to the **username**. Every app links to it. |
| Second factor (passkey / TOTP) | Authelia SQLite | the person | Enrolled at first login. Any TOTP app (e.g. Authy) works. |
| App access | the person's `groups` | platform admin | One group per app; a person holds any number. |
| xstockstrat role (`admin` / `trader` / `viewer`) | `identity.users.roles`, per environment | xstockstrat admin | Never in Authelia. Managed with `SetUserRoles` / `manage-users.py update-roles`. |

Groups (each app's client or route denies everyone outside its group, and every rule requires two factors):

| Group | Opens | Mechanism |
|---|---|---|
| `xstockstrat` | xstockstrat (prod) web + MCP | OIDC clients with `authorization_policy: xstockstrat` |
| `xstockstrat-staging` | xstockstrat-staging web + MCP | OIDC clients with `authorization_policy: xstockstrat-staging` |
| `commsgun` | commsgun dashboard | Caddy `forward_auth`; commsgun trusts `Remote-User` |
| `webhooks-mcp` | webhooks-mcp `/mcp` | OIDC client; webhooks-mcp validates Authelia JWTs |

Gatus and Proxmox stay tailnet-only and outside SSO (a later phase). headscale device login never uses SSO.

## 2. Hard rules

1. **Never rename a username.** The `sub` is bound to it: a rename gives the person a new `sub`, every app treats them as
   a stranger, and the old app accounts are already linked to the old `sub`, so they will not re-link by email.
   Change the display name or email instead. If a rename is unavoidable, see § 7.
2. **Emails are unique and correct.** xstockstrat links an existing account by email, once. A wrong email in SOPS
   attaches a person to someone else's account and roles. Check twice before deploying a new or changed email.
3. **Group membership is the approval.** Adding someone to `xstockstrat` lets them log in, and their first login creates
   an account with role `trader` unless their email already matches one. Trading still needs their own broker account;
   there are no shared accounts.
4. **Authelia's SQLite file is backed up twice a day and must stay so.** Losing it loses every `sub` and every passkey/TOTP
   enrolment (§ 8).

## 3. Add a person

1. Decide their username (permanent, § 2.1), email and groups.
2. Add them to `users_database.yml` in SOPS with an argon2id password hash. `TODO(verify)`: the exact SOPS key and the
   hash command (`authelia crypto hash generate argon2` in the pinned image).
3. Deploy.
4. They log in at `https://auth.<domain>` and enrol a passkey or a TOTP app.
5. They open each app. What xstockstrat does on their first login is § 5.

## 4. Change access, email or remove a person

| Change | Steps | Takes effect |
|---|---|---|
| Grant an app | add the group in SOPS, deploy | next login |
| Revoke an app | remove the group in SOPS, deploy; for xstockstrat also `SetUserActive false` in that environment | new logins at once; existing xstockstrat MCP calls within ~60 s (role/active cache). `TODO(verify)`: whether Authelia re-checks the group on token refresh. |
| Change email | edit it in SOPS, deploy | xstockstrat copies it to `identity.users.email` at the person's next login. If another row already holds that email, the old one is kept and the clash is logged. |
| Remove a person | remove them from SOPS, deploy; deactivate their xstockstrat accounts | as revoke |
| Change an xstockstrat role | `manage-users.py update-roles <email> --roles …` against that environment | next session JWT (web) or within ~60 s (MCP) |

## 5. How xstockstrat matches a login to an account

Per environment (prod and staging have separate `identity.users`), in this order:

1. The `sub` is already linked → that account.
2. Exactly one **active, unlinked** row has the login's email (case-insensitive) → link it, record a ledger audit event.
3. No row has that email → create one with roles written explicitly as `{trader}`.
4. Anything else is refused: several matches, the only match is linked to another `sub`, the account is inactive, or
   the token has no email. This applies to the web and to MCP; MCP tokens carry the email claim too.

An app account whose email does not match the person's Authelia email is **not** linked automatically. Make them
match before the person's first login (§ 6).

## 6. Linking existing xstockstrat accounts (cutover, once per environment)

Accounts that predate SSO (for example the seeded `admin@localhost`, `services/xstockstrat-identity/migrations/002_seed_admin.up.sql`)
are linked by giving them the person's real email **before** that person's first SSO login:

1. `uv run scripts/manage-users.py list-users` against the environment; note each row's email and `user_id`.
2. For each row that should belong to a person, set its email to the person's Authelia email. `manage-users.py` has
   no command for this today (`reset-password`, `create-user`, `list-users`, `update-roles`, `delete-user`): an
   `update-email` command is part of the xstockstrat SSO feature.
3. Set roles with `update-roles` (keep at least one active `admin` per environment: `SetUserRoles` refuses to remove the last one).
4. The person logs in; rule 5.2 links the row.

`SEED_USER_ID` (the `user_id` that owns seeded and legacy rows, e.g. strategies) is unaffected: this procedure keeps
every `user_id`. On the homelab it is already per environment (resolved from SOPS by `deploy-homelab.yml` with
`--env`). If it must point at a different user: rows written by migrations that already ran keep the old owner,
applied migrations are never edited, and only migrations not yet applied pick up the new value.

## 7. A username must change anyway

1. Create the new account (§ 3).
2. Unlink the old `sub` from the person's accounts in every xstockstrat environment. `TODO(verify)`: the unlink tooling
   is defined by the xstockstrat SSO feature.
3. The person logs in with the new username; rule 5.2 links by email.
4. Remove the old account.

## 8. Outages and recovery

| Situation | Effect | Action |
|---|---|---|
| Authelia down | no new logins anywhere; the commsgun dashboard is unreachable; xstockstrat MCP calls keep working until their access token expires | Gatus alerts via ntfy; fix and restart. For xstockstrat, re-enable password login (the rollback flag) if it must stay usable. |
| Authelia SQLite lost | every `sub` and second factor gone | **Restore it from the edge backup** (the same `edge-<UTC>.tar.age` archives as headscale, `docs/runbooks/recovery.md`; the role PR adds the Authelia step). Without a backup, every person re-enrols, and each xstockstrat environment needs every row unlinked (§ 7 step 2) before rule 5.2 can relink them. |
| `users_database.yml` lost | nobody can log in | redeploy from SOPS |

headscale, SSH and the tailnet do not depend on Authelia; the break-glass path in `docs/runbooks/recovery.md` is unchanged.
