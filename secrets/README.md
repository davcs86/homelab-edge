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
printed. Operator values (deSEC token, ping URL, passwords) and the pgBackRest PKI are never created this way. Committing the result to `main` needs the push token (`docs/runbooks/github-push-token.md`).

Read a value locally:

```sh
SOPS_AGE_SSH_PRIVATE_KEY_FILE=~/keys/sops_admin sops decrypt --extract '["ntfy_alerts_token"]' secrets/edge.sops.yaml
```
