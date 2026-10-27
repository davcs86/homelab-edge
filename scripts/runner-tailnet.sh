#!/usr/bin/env bash
# Join (up) or leave (down) the tailnet from a GitHub runner as an ephemeral node.
#   up [deploy]  tag:deploy; prints the edge's tailnet IPv4 to $GITHUB_OUTPUT as edge_ip
#   up ci [peer] tag:ci; prints the peer's tailnet IPv4 (default: the database host) as <peer>_ip and peer_ip
# Requires SOPS_AGE_SSH_PRIVATE_KEY_FILE.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
login_server="https://hs.$(awk '/^domain:/{print $2}' "$repo_root/inventories/prod/group_vars/all/main.yml")"

case "${1:?up|down}" in
  up)
    role="${2:-deploy}"
    case "$role" in
      deploy) keyfile=deploy-authkey.sops.yaml keyname=tailscale_deploy_authkey peer=edge ;;
      ci) keyfile=ci-authkey.sops.yaml keyname=tailscale_ci_authkey peer="${3:-db}" ;;
      *) echo "unknown role: $role (deploy|ci)" >&2; exit 2 ;;
    esac
    authkey="$(sops decrypt --extract "[\"$keyname\"]" "$repo_root/secrets/$keyfile")"
    echo "::add-mask::$authkey"
    sudo mkdir -p /run/tailscale
    # shellcheck disable=SC2024  # the log belongs to the runner user on purpose
    sudo tailscaled --state=mem: --socket=/run/tailscale/tailscaled.sock >"$RUNNER_TEMP/tailscaled.log" 2>&1 &
    for _ in $(seq 1 20); do sudo tailscale status >/dev/null 2>&1 && break; sleep 1; done
    sudo tailscale up --login-server "$login_server" --auth-key "$authkey" \
      --hostname "gha-${role}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" --accept-dns=false --timeout 60s
    peer_ip=""
    for _ in $(seq 1 15); do peer_ip="$(sudo tailscale ip -4 "$peer" 2>/dev/null || true)"; [ -n "$peer_ip" ] && break; sleep 2; done
    [ -n "$peer_ip" ] || { echo "::error::$peer is not visible from this $role node"; exit 1; }
    { echo "${peer}_ip=$peer_ip"; echo "peer_ip=$peer_ip"; } >>"$GITHUB_OUTPUT"
    echo "joined tailnet as tag:$role; $peer at $peer_ip"
    ;;
  down)
    # Ephemeral node: logout deletes it from headscale immediately.
    sudo tailscale logout 2>/dev/null || true
    sudo pkill tailscaled 2>/dev/null || true
    ;;
esac
