# Letting workflows commit to main (GitHub App)

`main` only accepts pull requests (a repository ruleset). Three workflows must still commit state they generate:
`init-secrets` (`secrets/edge.sops.yaml`), `node-admin` → `mint-ci-key` (`secrets/ci-authkey.sops.yaml`) and `deploy`
(`generated.yml`, `known_hosts`, the rotated `secrets/deploy-authkey.sops.yaml`). The built-in `GITHUB_TOKEN` cannot be put on a
ruleset's bypass list, so they push with the token of a GitHub App that is. The error without it is
`GH013: Changes must be made through a pull request`.

## One-time setup (repository owner)

1. **Create the App** (Settings → Developer settings → GitHub Apps → New GitHub App): any name, any homepage URL, **Webhook: inactive**,
   **Repository permissions: Contents → Read and write** (Metadata read-only is added automatically) and nothing else,
   *Only on this account*.
2. **Generate a private key** (a `.pem` downloads) and note the App's **Client ID**.
3. **Install the App** on this account → *Only select repositories* → `homelab-edge`.
4. **Restrict the `production` environment to `main`** (Settings → Environments → production → *Deployment branches and tags* →
   *Selected branches and tags* → `main`). Do this before step 5: otherwise a workflow edited on any branch could use the key
   once approved.
5. In the same environment add the variable **`PUSH_APP_CLIENT_ID`** (the Client ID) and the secret **`PUSH_APP_PRIVATE_KEY`**
   (the whole `.pem`). Delete the downloaded `.pem`.
6. **Add the App to the ruleset's bypass list** (Settings → Rules → Rulesets → the one for `main` → Bypass list → Add bypass → the App,
   *Always*). A bypass applies to every rule in that ruleset, not to one path.

## What the workflows do

Each stages its files; only if something changed does it mint a token (`actions/create-github-app-token`, `contents: write`,
this repository only) and `git push` with it. A run with nothing to commit never touches the App. If the variable is missing the run
fails with a message pointing here. `scripts/workflow-push-test.py` checks all of this in CI, including that the key is referenced
only by jobs in the `production` environment.

## Rotating or revoking

- Rotate: generate a new key in the App's settings, replace `PUSH_APP_PRIVATE_KEY`, delete the old key in the App.
- Revoke: remove the App from the bypass list (pushes then fail with GH013 again), or suspend/uninstall it.

## If a push still fails

- `GH013` again: the App is not on the bypass list, or you edited a different ruleset than the one that targets `main`.
- `Resource not accessible` / 403 creating the token: the App is not installed on this repository, or lacks Contents write.
- The job waits forever: the run needs approval on `production` (it always does).
