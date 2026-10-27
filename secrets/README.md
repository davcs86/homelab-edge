# secrets/

Only SOPS-encrypted files live here (`*.sops.yaml`); recipients are listed in `../.sops.yaml`.

| File | Written by | Contents |
|---|---|---|
| `edge.sops.yaml` | `init-secrets` workflow (`scripts/init_secrets.py`); `fill_missing` adds a generated credential that a newer release needs without touching the existing ones | deSEC token, healthchecks.io ping URL, admin console password hash, ntfy credentials, pgBackRest mTLS PKI |
| `ci-authkey.sops.yaml` | `node-admin` workflow (`mint-ci-key`) | reusable, ephemeral `tag:ci` pre-auth key (24 h default) and its expiry |
| `deploy-authkey.sops.yaml` | `deploy` workflow (`roles/deploy_key`) | reusable, ephemeral `tag:deploy` pre-auth key (90 days) |

A new release that needs another generated credential (the `gatus_heartbeat_db_token` from PR #20 is the example) fails the deploy at the task
that reads it, because the committed file predates the key. Run **Actions → init-secrets** with `fill_missing` (not `overwrite`): it decrypts
with the deploy key in memory, adds only the generated keys that are absent, re-encrypts and commits; nothing existing changes and no value is
printed. Operator values (deSEC token, ping URL, passwords) and the pgBackRest PKI are never created this way. The database host's keys are: the four `db_role_*_password` values (`admin`, `superadmin`, one per workload), `gatus_heartbeat_pgbackrest_token`, the database TLS CA and server cert (`db_tls_*`), and the pgBackRest server cert for the database host and client cert for the edge (`pgbackrest_db_server_*`, `pgbackrest_edge_client_*`, signed by the existing pgBackRest CA). A certificate set is created whole; a partly present set is refused. The app VMs' gate keys (`app_gate_xstockstrat_<env>_public_key`) are operator values: public keys whose private halves live only in the app repo (`deploy-compose-app.md` section 1.1). CI (`scripts/secrets-keys-test.py`) fails when a role reads a key this file lacks, so the gap shows up on the pull request, not mid-deploy. Committing the result to `main` needs the push token (`docs/runbooks/github-push-token.md`).

Read a value locally:

```sh
SOPS_AGE_SSH_PRIVATE_KEY_FILE=~/keys/sops_admin sops decrypt --extract '["ntfy_alerts_token"]' secrets/edge.sops.yaml
```
