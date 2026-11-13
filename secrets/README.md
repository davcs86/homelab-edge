# secrets/

Only SOPS-encrypted files live here (`*.sops.yaml`); recipients are listed in `../.sops.yaml`.

| File | Written by | Contents |
|---|---|---|
| `edge.sops.yaml` | `init-secrets` workflow (`scripts/init_secrets.py`); `fill_missing` adds a generated credential that a newer release needs without touching the existing ones | deSEC token, healthchecks.io ping URL, admin console password hash, ntfy credentials, pgBackRest mTLS PKI, commsgun credentials (generated admin password, session secret, admin API token and per-app keys; owner-added Resend key and sender map) |
| `ci-authkey.sops.yaml` | `node-admin` workflow (`mint-ci-key`) | reusable, ephemeral `tag:ci` pre-auth key (24 h default) and its expiry |
| `deploy-authkey.sops.yaml` | `deploy` workflow (`roles/deploy_key`) | reusable, ephemeral `tag:deploy` pre-auth key (90 days) |

A new release that needs another generated credential (the `gatus_heartbeat_db_token` from PR #20 is the example) fails the deploy at the task
that reads it, because the committed file predates the key. Run **Actions → init-secrets** with `fill_missing` (not `overwrite`): it decrypts
with the deploy key in memory, adds only the generated keys that are absent, re-encrypts and commits; nothing existing changes and no value is
printed. Operator values (deSEC token, ping URL, passwords) and the pgBackRest PKI are never created this way. The database host's keys are: the four `db_role_*_password` values (`admin`, `superadmin`, one per workload), `gatus_heartbeat_pgbackrest_token`, `webhooks_mcp_auth_key` (48 URL-safe characters; the listener refuses fewer than 32), the database TLS CA and server cert (`db_tls_*`), and the pgBackRest server cert for the database host and client cert for the edge (`pgbackrest_db_server_*`, `pgbackrest_edge_client_*`, signed by the existing pgBackRest CA). A certificate set is created whole; a partly present set is refused. The app VMs' gate keys (`app_gate_xstockstrat_<env>_public_key`, `HAND_KEYS` in `scripts/init_secrets.py`) are public keys the owner adds with `sops set`; their private halves live only in the app repo (`deploy-compose-app.md` section 1.1). `fill_missing` keeps them; `overwrite` drops them, so add them again after one. The commsgun delivery hub's keys: five are generated (48 URL-safe characters each): `commsgun_admin_password` (the dashboard login; read it with `sops decrypt --extract '["commsgun_admin_password"]' secrets/edge.sops.yaml` (key setup as below) and replace it any time with `sops set secrets/edge.sops.yaml '["commsgun_admin_password"]' '"<new value>"'`), `commsgun_session_secret` (signs dashboard sessions), `commsgun_admin_api_token` (the dashboard's token to the hub's admin API), and `commsgun_app_key_xstockstrat` / `commsgun_app_key_xstockstrat_staging` / `commsgun_app_key_ops` (the bearer key each publishing app sends to the enqueue endpoint). Two are the owner's, added with `sops set` after `fill_missing`, which neither requires nor creates them and keeps them (they are `HAND_KEYS`): `resend_api_key`, which must be a full-access Resend key because bounce tracking reads email status and a sending-only key breaks it, and `commsgun_senders`, a map (not a string) from app id to `senders` (the allowed From addresses) and `default_sender` (one of them), with an entry for every app in the inventory registry. `overwrite` deletes `resend_api_key` and `commsgun_senders`; keep a copy before running it, since Resend shows an API key only once.

```sh
sops set secrets/edge.sops.yaml '["commsgun_senders"]' '{"xstockstrat": {"senders": ["Alerts <alerts@example.com>"], "default_sender": "Alerts <alerts@example.com>"}, "ops": {"senders": ["Ops <ops@example.com>"], "default_sender": "Ops <ops@example.com>"}}'
```

CI (`scripts/secrets-keys-test.py`) fails when a role reads a key this file lacks, so the gap shows up on the pull request, not mid-deploy. Committing the result to `main` needs the push token (`docs/runbooks/github-push-token.md`).

Read a value locally:

```sh
SOPS_AGE_SSH_PRIVATE_KEY_FILE=~/keys/sops_admin sops decrypt --extract '["ntfy_alerts_token"]' secrets/edge.sops.yaml
```
