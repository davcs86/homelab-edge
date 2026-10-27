#!/usr/bin/env bash
# Forced-command gate for one compose app; the deploy key can run nothing else. Install on the app VM as
# /usr/local/bin/app-deploy, then pin it: restrict,command="/usr/local/bin/app-deploy /opt/myapp myapp [wait seconds]" ssh-ed25519 AAAA… gha-deploy
set -euo pipefail

app_dir="${1:?usage: app-deploy <app dir> <compose project> [wait seconds]}"
project="${2:?usage: app-deploy <app dir> <compose project> [wait seconds]}"
wait_seconds="${3:-120}"
keep=5

die() { echo "app-deploy: $*" >&2; exit 2; }
if ! [[ "$wait_seconds" =~ ^[0-9]{1,3}$ ]] || (( wait_seconds < 30 || wait_seconds > 900 )); then
  die "wait seconds must be 30-900"
fi
compose() { local d="$1"; shift; docker compose --project-name "$project" --env-file "$d/.env" -f "$d/compose.yml" "$@"; }

read -r -a cmd <<<"${SSH_ORIGINAL_COMMAND:-}"
action="${cmd[0]:-}"
release="${cmd[1]:-}"
[[ "$action" == status ]] || [[ "$release" =~ ^[0-9a-f]{7,40}-[0-9]+$ ]] || die "release must look like <git sha>-<run id>"
rel="$app_dir/releases/$release"

newest_first() { find "$app_dir/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-; }
switch_to() { ln -sfn "$1" "$app_dir/current.new" && mv -T "$app_dir/current.new" "$app_dir/current"; }

case "$action" in
  put)
    name="${cmd[2]:-}"
    [[ "$name" == compose.yml || "$name" == .env ]] || die "put takes compose.yml or .env"
    install -d -m 0750 "$rel"
    umask 077
    cat >"$rel/$name.tmp"
    mv "$rel/$name.tmp" "$rel/$name"
    ;;

  deploy)
    ghcr_user="${cmd[2]:-}"
    [[ "$ghcr_user" =~ ^[A-Za-z0-9-]{1,39}$ ]] || die "deploy takes the registry user"
    [[ -f "$rel/compose.yml" && -f "$rel/.env" ]] || die "release $release is incomplete"
    IFS= read -r token
    previous="$(readlink -f "$app_dir/current" 2>/dev/null || true)"
    # A dotenv parse error quotes .env content; only missing-variable names may reach the job log.
    if ! err="$(compose "$rel" config -q 2>&1 >/dev/null)"; then
      grep -oE 'required variable [A-Za-z0-9_]+ is missing a value' <<<"$err" >&2 || true
      die "release $release does not render (details suppressed; an admin can run compose config on the VM)"
    fi

    # Credentials live in a throwaway Docker config; nothing persists on the VM.
    cfg="$(mktemp -d)"
    trap 'rm -rf "$cfg"' EXIT
    printf '%s' "$token" | DOCKER_CONFIG="$cfg" docker login ghcr.io -u "$ghcr_user" --password-stdin >/dev/null
    DOCKER_CONFIG="$cfg" compose "$rel" pull

    switch_to "$rel"
    if compose "$rel" up -d --pull never --remove-orphans --wait --wait-timeout "$wait_seconds"; then
      echo "deployed $release"
      cur="$(readlink -f "$app_dir/current")"
      while read -r d; do
        [[ "$d" == "$cur" || "$d" == "$previous" ]] || rm -rf -- "$d"
      done < <(newest_first | tail -n +$((keep + 1)))
    else
      echo "release $release did not become healthy" >&2
      if [[ -n "$previous" && -d "$previous" ]]; then
        switch_to "$previous"
        compose "$previous" up -d --pull never --remove-orphans --wait --wait-timeout "$wait_seconds" \
          && echo "rolled back to $(basename "$previous")" >&2
      fi
      exit 1
    fi
    ;;

  rollback)
    [[ -f "$rel/compose.yml" && -f "$rel/.env" ]] || die "release $release is not on this host"
    switch_to "$rel"
    compose "$rel" up -d --pull never --remove-orphans --wait --wait-timeout "$wait_seconds"
    ;;

  status)
    newest_first | head -n "$keep" | xargs -r -n1 basename
    echo "current: $(basename "$(readlink -f "$app_dir/current")")"
    compose "$app_dir/current" ps
    ;;

  *) die "unknown action '$action' (put, deploy, rollback, status)" ;;
esac
