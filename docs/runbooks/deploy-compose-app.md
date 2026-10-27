# Deploy a Docker Compose app to the homelab

Template: `templates/app-deploy/` (`deploy.yml`, `app-deploy.sh`, `compose.example.yml`); test: `scripts/app-deploy-test.sh`. Works for xstockstrat and any other Compose app.

```
push / dispatch → build job: docker build → push to GHCR → image digest
                → deploy job: pinned tailscale → join tailnet as ephemeral tag:ci node
                              → ssh deploy@VM (forced command = app-deploy.sh): put compose.yml, put .env, deploy
                              → VM: pull by digest → up --wait → healthy: prune old releases / unhealthy: roll back
```

The deploy key can run only `put`, `deploy`, `rollback` and `status` for one app directory. The VM never keeps registry
credentials, and the digest in `.env` is the exact image that was built.

## 0. Open items before the first deploy

- [x] Staging SSH grant for `tag:ci` (section 2, first bullet).
- [ ] Reusable `tag:ci` key stored as `TS_AUTHKEY` and `TS_AUTHKEY_EXPIRES` in the app repo (section 2, second bullet).
- [ ] Each target VM prepared (section 1) and the app repo configured (sections 3 and 4).
- [ ] Optional: a 1Password vault, items and service account per app and environment (section 3.1). Until then the GitHub secrets are used.

`deploy.yml` has not run yet; expect to fix small things on the first staging run, and strike the items above as they are done.

## 1. One-time VM prep (as an admin, over the tailnet)

1. Docker Engine and the compose plugin installed (same pins as the edge, see `docs/versions.md`).
2. `sudo useradd -m -s /bin/bash deploy && sudo usermod -aG docker deploy`. Docker-group membership is root-equivalent on this VM, which is why the key is forced to the gate script and the VM should run only this app.
3. `sudo install -d -o deploy -g deploy -m 0750 /opt/<app>/releases`
4. `sudo install -m 0755 templates/app-deploy/app-deploy.sh /usr/local/bin/app-deploy`. Re-install it by hand when the script changes; the deploy key cannot.
5. Generate a key pair for this app and pin it (public half only on the VM), in `/home/deploy/.ssh/authorized_keys`:
   `restrict,command="/usr/local/bin/app-deploy /opt/<app> <app> [wait seconds]" ssh-ed25519 AAAA… gha-deploy-<app>`.
   The optional third argument is how long `up --wait` may take (30–900, default 120); the deploy key cannot change it.
6. Host key for pinning: from an admin shell on the tailnet, `ssh-keyscan -t ed25519 <vm-hostname> | sed 's/^[^ ]*/<vm-hostname>/'`. Compare the fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the VM, then store the line as `DEPLOY_KNOWN_HOSTS`.

## 2. Tailnet side (this repo)

- The VM's tailnet hostname follows its tag, and a new workload needs its own tag and grants (`docs/runbooks/onboard-service.md`).
- `policy/policy.hujson` needs `{"src": ["tag:ci"], "dst": ["tag:<workload>"], "ip": ["tcp:22"]}` plus a test that `tag:ci` reaches `<workload>:22` and nothing else.
  Both `tag:xstockstrat` and `tag:xstockstrat-staging` have it.
- A reusable, ephemeral key that outlives one job: on the edge, `sudo docker compose -f /opt/edge/compose.yml exec -T headscale headscale preauthkeys create --tags tag:ci --reusable --ephemeral --expiration 2160h` (90 days). The `node-admin` workflow's 24 h key is for this repo's own CI. Put the key in the GitHub secret `TS_AUTHKEY` and its expiry date in the variable `TS_AUTHKEY_EXPIRES`; do not commit it.
- One shared `tag:ci` key means every app repo holding it can join as `tag:ci`. The SSH key is per app and forced to one gate, so the key alone reaches nothing, but rotate it if any repo is compromised.

## 3. GitHub side (app repo)

Create environments `staging` and `production`; on `production` require reviewers. Secrets come from 1Password (3.1) when the environment has `OP_SERVICE_ACCOUNT_TOKEN`, otherwise from the same-named GitHub secrets below; if both exist, 1Password wins.

| Kind | Name | Value |
|---|---|---|
| var (per environment) | `DEPLOY_HOST` | the VM's tailnet hostname, e.g. `xstockstrat-staging` |
| var | `TAILNET_LOGIN_SERVER` | `https://hs.<domain>` |
| var | `TS_AUTHKEY_EXPIRES` | e.g. `2027-01-20` (the job fails when it is past, warns inside 14 days) |
| secret | `TS_AUTHKEY` | key from step 2 |
| secret (per environment) | `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` | private key from step 1.5, line from 1.6 |
| secret (per environment) | `APP_ENV` | the app's whole dotenv, one `KEY=value` per line |

The package must be readable by the repository's `GITHUB_TOKEN` (GHCR package settings → Manage Actions access).

### 3.1 1Password (one vault per app and environment)

| Item (exact name) | Type | Field | Becomes |
|---|---|---|---|
| `deploy-ssh-key` | SSH Key | `private key` (read in OpenSSH format) | `DEPLOY_SSH_KEY` |
| `deploy-known-hosts` | Secure Note or Login | `known_hosts` | `DEPLOY_KNOWN_HOSTS` |
| `tailnet-authkey` | Login or Password | `authkey`; `expires` as a **text** field holding a date such as `2027-01-20` | `TS_AUTHKEY`, `TS_AUTHKEY_EXPIRES` |
| `app-env` | Secure Note | the note body (the whole dotenv) | `APP_ENV` |

1. Create a shared vault per app and environment (for example `<app>-staging`) with service accounts enabled, and the four items above. Create all four: if any reference fails to resolve the whole load fails and every secret falls back to GitHub.
2. Create one service account per vault. A service account cannot be given more vaults afterwards, and it cannot read your Personal, Private or Employee vaults:
   `op service-account create "<app>-<environment>" --vault "<vault>:read_items" --expires-in 365d`.
   The token is shown once: save it in a vault this account cannot read (your Private vault) before leaving the terminal.
3. In the GitHub **environment** (so `production`'s reviewers gate it): secret `OP_SERVICE_ACCOUNT_TOKEN`; variables `OP_VAULT` (the vault name) and `OP_TOKEN_EXPIRES` (the token's expiry date).
4. Rotate yearly: create a new service account for the same vault, update the secret and `OP_TOKEN_EXPIRES`, delete the old account.

The first steps of the deploy job pick the source, install a pinned `op` (the action would otherwise download an unverified "latest"), load the secrets as masked step outputs (never exported to the whole job) and write a **Secret sources** list to the run summary. An expired token (`OP_TOKEN_EXPIRES` in the past), an unreachable 1Password or a missing item falls back to the GitHub secrets with a warning; a secret found in neither source fails the job. The token's expiry warns 14 days ahead; an unparseable `OP_TOKEN_EXPIRES` fails the job.

Once you trust the 1Password path, delete the GitHub copies; the fallback is then gone and a 1Password outage blocks deploys.

## 4. The app repo

- Copy `deploy.yml` to `.github/workflows/`, resolve each `# EDIT` line, and keep a `Dockerfile` at the root.
- Copy `compose.example.yml` to `compose.yml`: the image must be `${APP_IMAGE}`, and every service needs a `healthcheck`, because `--wait` and the rollback rely on it.
- Everything environment-specific comes from `APP_ENV`; nothing secret goes in `compose.yml`.

## 4.1 xstockstrat

The app repo already carries this; it differs from the template in five ways.

> **Production is live trading.** The homelab `production` stack runs `TRADING_MODE=live` against the same broker
> accounts as the DigitalOcean production app, in parallel with it. Both place real orders; enable it knowingly.

- **Pipeline:** `.github/workflows/deploy-homelab.yml`, called by `deploy-dev.yml` (push to `main-dev` → `staging`) and
  `deploy-prod.yml` (push to `main` → `production`) next to the DigitalOcean deploy, from the same `docker-build.yml`
  images. Staging runs only while the repository variable `HOMELAB_STAGING_ENABLED` is `true`, production only while
  `HOMELAB_PRODUCTION_ENABLED` is `true`; enable production last. A manual run takes an environment and an already-built short
  SHA and deploys that commit's compose file; production runs only from `main`. On the `production` environment also set
  **Deployment branches** to `main` only.
- **Images:** 13 (12 services + `xstockstrat-db-migrator`). Each `:<short sha>` tag is resolved to a digest and written as
  `IMAGE_<SERVICE>=…@sha256:…` instead of one `APP_IMAGE`.
- **Compose file:** `deploy/homelab/compose.yml` (a root `compose.yml` would shadow local dev's `docker-compose.yml`). It needs
  Docker Compose ≥ v2.17.0 on the VM (a one-shot `service_completed_successfully` dependency counts toward `--wait`); install
  the edge's pins (`versions.yml`). `db-migrator` runs on every deploy before anything else starts, so a failed migration
  fails the release and rolls it back.
- **Rollback restores images, not schema;** migrations must stay backward compatible. A rolled-back migrator that finds the
  database clean and *ahead* of its own migrations skips that schema; dirty and ahead, it refuses and the rollback fails
  closed (fix by hand, `recovery.md`). This path is tested offline only, not yet against a real rollback.
- **Gate wait:** pin the key with `… /opt/xstockstrat xstockstrat 300`; twelve services start in dependency order. After any
  change to `app-deploy.sh` (here: a release that does not render prints only the missing variable names) re-install it on the VM.

Extra inputs per environment, both in GitHub (environment secret) and 1Password (item in the same vault):

| GitHub | 1Password item | Value |
|---|---|---|
| `MTLS_CA` | `mtls-ca` (Secure Note, body) | base64 `{ca_cert, ca_key}` of this environment's own CA; leaves are minted per deploy |
| `APP_ENV` | `app-env` | `DB_PASSWORD`, `JWT_SECRET`, `BROKER_ACCOUNTS_ENCRYPTION_KEY`, `CONFIG_SECRETS_ENCRYPTION_KEY`, `SEED_USER_ID`, `APP_URL`; optional `SLACK_WEBHOOK_URL`, `SENDGRID_API_KEY`, `VAPID_*`. One single-line `KEY=value` per line |

Create `mtls-ca` with the other four items: one missing item sends every secret to the GitHub fallback.

- `DB_PASSWORD` is the role's SOPS value, `sops decrypt --extract '["db_role_xstockstrat_<env>_password"]' secrets/edge.sops.yaml`.
  It must be URL- and dotenv-safe: no `$`, `@`, `:`, `/` or `#` (hex is).
- `APP_URL` is `https://xstockstrat.natanddavehouse.com` (production) or `https://xstockstrat-staging-ui.ts.natanddavehouse.com:8443`
  (staging). `AGENT_PUBLIC_URL` and `UI_BASE_URL` are derived from it; do not set them.
- The pipeline appends `DB_HOST=db.ts.natanddavehouse.com`, `DB_NAME`/`DB_USER` (`xstockstrat-production`/`xstockstrat_production`
  or `xstockstrat-staging`/`xstockstrat_staging`, as in `db-host.md`), `APPLICATION_ENV` (`production`/`development`),
  `TRADING_MODE` (`live`/`paper`), the image digests and the mTLS leaves after `APP_ENV`, which cannot override them.
- Six services use PgBouncer (6432), the rest connect directly (5432), all with `sslmode=require`. OTel is off.

New CA for one environment: in the xstockstrat repo,
`d=$(mktemp -d) && CERTS_DIR=$d CA_CN=xstockstrat-homelab-<env>-ca scripts/gen-dev-certs.sh >/dev/null && python3 -c 'import base64,json,sys; d=sys.argv[1]; print(base64.b64encode(json.dumps({"ca_cert":open(d+"/_ca/ca.pem").read(),"ca_key":open(d+"/_ca/ca-key.pem").read()}).encode()).decode())' "$d"`, then `rm -rf "$d"`.

Before the first deploy: the VM must be on the tailnet **before** the **db-host** run that writes its `hostssl` line. Then prove
both ports from the VM (a user-defined network resolves tailnet names the way the compose project does):

```sh
read -rs PGPASSWORD && export PGPASSWORD   # the role password, typed, not on the command line
img='<db_probe_image from inventories/prod/group_vars/all/versions.yml>'
docker network create dbprobe
for port in 5432 6432; do
  docker run --rm --network dbprobe -e PGPASSWORD "$img" \
    psql "host=db.ts.natanddavehouse.com port=$port user=xstockstrat_<env> dbname=xstockstrat-<env> sslmode=require" -c 'select 1'
done
docker network rm dbprobe; unset PGPASSWORD
```

## 5. Operating it

| Need | Do |
|---|---|
| Deploy staging | push to `main` |
| Deploy production | Actions → deploy → Run workflow → `production` (waits for reviewers) |
| What is running | on the VM: `sudo -u deploy env SSH_ORIGINAL_COMMAND=status /usr/local/bin/app-deploy /opt/<app> <app>` (the forced command ignores shell requests, so there is no interactive login with the deploy key) |
| Roll back by hand | same, with `SSH_ORIGINAL_COMMAND='rollback <release>'`; releases are the directory names in `/opt/<app>/releases` (last 5 kept) |
| Unhealthy release | the VM switches back to the previous release itself and the job fails |

## 6. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Edge or headscale down | the job cannot join the tailnet, so no deploy; running apps are unaffected | `docs/runbooks/recovery.md`; deploy by hand from an admin shell with the gate command above |
| `TS_AUTHKEY` expired | job fails at the first step with a clear error | mint a new key, update the secret and variable |
| 1Password unreachable, token expired or revoked, or an item missing | the job warns and uses the GitHub secrets where they exist (see the run summary); with none, it fails naming the secret | fix the vault or token (3.1), or restore the GitHub secret |
| GHCR unavailable | build or pull fails before anything changes | re-run the job |
| New release unhealthy | automatic rollback, job fails | read the container logs on the VM |
| Release does not render | the gate exits 2 before pulling; the log names only missing variables | add the variable to `APP_ENV`; anything else, run `docker compose config` on the VM as an admin |
| xstockstrat: Gatus UI probe red with 307 | `/trader/api/health` redirects to login | the app's public-health exemption (feature 271) is not deployed yet |
| xstockstrat: Gatus agent probe red with a 200 | `[BODY].resource` differs from the expected URL | `APP_URL` in `APP_ENV` is wrong for this environment |
| Job killed mid-deploy | `current` may point at a release that is not up | re-run, or `rollback <previous release>` |

## 7. Self-hosted runner instead

The gate script is the only thing the deploy needs, so the transport is replaceable: run a runner on the VM, drop the
tailscale and ssh steps from `deploy.yml`, and call `SSH_ORIGINAL_COMMAND=… /usr/local/bin/app-deploy …` locally. It
deploys while the edge is down, but the runner is a long-lived, patched-by-you process holding deploy rights that
executes whatever workflow code reaches it. The default here is the hosted runner (see `docs/decisions.md`).
