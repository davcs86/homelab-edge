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

## 1. One-time VM prep (as an admin, over the tailnet)

1. Docker Engine and the compose plugin installed (same pins as the edge, see `docs/versions.md`).
2. `sudo useradd -m -s /bin/bash deploy && sudo usermod -aG docker deploy`. Docker-group membership is root-equivalent on this VM, which is why the key is forced to the gate script and the VM should run only this app.
3. `sudo install -d -o deploy -g deploy -m 0750 /opt/<app>/releases`
4. `sudo install -m 0755 templates/app-deploy/app-deploy.sh /usr/local/bin/app-deploy`. Re-install it by hand when the script changes; the deploy key cannot.
5. Generate a key pair for this app and pin it (public half only on the VM), in `/home/deploy/.ssh/authorized_keys`:
   `restrict,command="/usr/local/bin/app-deploy /opt/<app> <app>" ssh-ed25519 AAAA… gha-deploy-<app>`
6. Host key for pinning: from an admin shell on the tailnet, `ssh-keyscan -t ed25519 <vm-hostname> | sed 's/^[^ ]*/<vm-hostname>/'`. Compare the fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` on the VM, then store the line as `DEPLOY_KNOWN_HOSTS`.

## 2. Tailnet side (this repo)

- The VM's tailnet hostname follows its tag (`docs/runbooks/onboard-service.md`).
- `policy/policy.hujson` needs `{"src": ["tag:ci"], "dst": ["tag:<workload>"], "ip": ["tcp:22"]}` plus a test that `tag:ci` reaches `<workload>:22` and nothing else. `tag:xstockstrat` already has it; every other workload, staging included, needs its own grant in a PR.
- A reusable, ephemeral key that outlives one job: on the edge, `sudo docker compose -f /opt/edge/compose.yml exec -T headscale headscale preauthkeys create --tags tag:ci --reusable --ephemeral --expiration 2160h` (90 days). The `node-admin` workflow's 24 h key is for this repo's own CI. Put the key in the GitHub secret `TS_AUTHKEY` and its expiry date in the variable `TS_AUTHKEY_EXPIRES`; do not commit it.
- One shared `tag:ci` key means every app repo holding it can join as `tag:ci`. The SSH key is per app and forced to one gate, so the key alone reaches nothing, but rotate it if any repo is compromised.

## 3. GitHub side (app repo)

Create environments `staging` and `production`; on `production` require reviewers.

| Kind | Name | Value |
|---|---|---|
| var (per environment) | `DEPLOY_HOST` | the VM's tailnet hostname, e.g. `xstockstrat-staging` |
| var | `TAILNET_LOGIN_SERVER` | `https://hs.<domain>` |
| var | `TS_AUTHKEY_EXPIRES` | e.g. `2027-01-20` (the job fails when it is past, warns inside 14 days) |
| secret | `TS_AUTHKEY` | key from step 2 |
| secret (per environment) | `DEPLOY_SSH_KEY`, `DEPLOY_KNOWN_HOSTS` | private key from step 1.5, line from 1.6 |
| secret (per environment) | `APP_ENV` | the app's whole dotenv, one `KEY=value` per line |

The package must be readable by the repository's `GITHUB_TOKEN` (GHCR package settings → Manage Actions access).

## 4. The app repo

- Copy `deploy.yml` to `.github/workflows/`, resolve each `# EDIT` line, and keep a `Dockerfile` at the root.
- Copy `compose.example.yml` to `compose.yml`: the image must be `${APP_IMAGE}`, and every service needs a `healthcheck`, because `--wait` and the rollback rely on it.
- Everything environment-specific comes from `APP_ENV`; nothing secret goes in `compose.yml`.

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
| GHCR unavailable | build or pull fails before anything changes | re-run the job |
| New release unhealthy | automatic rollback, job fails | read the container logs on the VM |
| Job killed mid-deploy | `current` may point at a release that is not up | re-run, or `rollback <previous release>` |

## 7. Self-hosted runner instead

The gate script is the only thing the deploy needs, so the transport is replaceable: run a runner on the VM, drop the
tailscale and ssh steps from `deploy.yml`, and call `SSH_ORIGINAL_COMMAND=… /usr/local/bin/app-deploy …` locally. It
deploys while the edge is down, but the runner is a long-lived, patched-by-you process holding deploy rights that
executes whatever workflow code reaches it. The default here is the hosted runner (see `docs/decisions.md`).
