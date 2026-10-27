# Letting workflows commit to main (push token)

`main` only accepts pull requests (a repository ruleset). Three workflows must still commit state they generate:
`init-secrets` (`secrets/edge.sops.yaml`), `node-admin` → `mint-ci-key` (`secrets/ci-authkey.sops.yaml`) and `deploy`
(`generated.yml`, `known_hosts`, the rotated `secrets/deploy-authkey.sops.yaml`). The built-in `GITHUB_TOKEN` cannot be put on a
ruleset's bypass list, so they push with a fine-grained personal access token of the repository owner, who can. The error without
it is `GH013: Changes must be made through a pull request`.

## One-time setup (repository owner)

1. **Restrict the `production` environment to `main`** (Settings → Environments → production → *Deployment branches and tags* →
   *Selected branches and tags* → `main`). Do this before step 3: otherwise a workflow edited on any branch could use the token
   once approved.
2. **Create the token** (Settings → Developer settings → Personal access tokens → Fine-grained tokens): resource owner = you, *Only
   select repositories* → `homelab-edge`, repository permission **Contents: Read and write** and nothing else, and an expiry date.
3. **Store it** as the secret **`PUSH_TOKEN`** in the `production` environment (not as a repository secret). Do not keep a copy.
4. **Add the Repository admin role to the ruleset's bypass list** (Settings → Rules → Rulesets → the one for `main` → Bypass list →
   Add bypass → *Repository admin*, *Always*). A bypass applies to every rule in that ruleset, not to one path.

## What the workflows do

Each stages its files; only if something changed does it `git push` to the branch it runs on, with `PUSH_TOKEN` in the URL. The
default `GITHUB_TOKEN` is read-only and the checkout does not keep it. A run with nothing to commit never uses the token. If
the secret is empty the run fails with a message pointing here. `scripts/workflow-push-test.py` checks all of this in CI, including
that `PUSH_TOKEN` is referenced only by jobs in the `production` environment.

## Rotating or revoking

- Rotate before the expiry date: create a new token, replace `PUSH_TOKEN`, delete the old token. An expired token shows up as
  `GH013` or an authentication error on the push step of the next run that has something to commit.
- Revoke: delete the token in your developer settings, or remove the Repository admin role from the bypass list.

## If a push still fails

- `GH013` again: your role is not on the bypass list, or you edited a different ruleset than the one that targets `main`.
- `403` / `Permission denied`: the token lacks Contents write on this repository, or has expired.
- The job waits forever: the run needs approval on `production` (it always does).
