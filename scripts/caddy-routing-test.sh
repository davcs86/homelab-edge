#!/usr/bin/env bash
# Offline test of the rendered Caddyfile against local mock upstreams (Caddy internal CA).
# Usage: scripts/caddy-routing-test.sh <caddy-image>    (requires docker, ansible, python3, curl, sudo for an lo alias)
set -euo pipefail

image="${1:?caddy image}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
tailnet_ip=100.64.0.250
pids=()
# shellcheck disable=SC2317  # invoked via trap
cleanup() {
  docker rm -f caddy-routing-test >/dev/null 2>&1 || true
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  sudo ip addr del "$tailnet_ip/32" dev lo 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

(cd "$repo_root" && ansible localhost -c local -m ansible.builtin.template \
  -a "src=roles/stack/templates/Caddyfile.j2 dest=$work/Caddyfile" \
  -e caddy_test_mode=true -e app_tailnet_host=127.0.0.1 -e staging_tailnet_host=127.0.0.1 >/dev/null)

printf '%s
' 'app_host={{ public_app_hostname }}' 'hs_host={{ headscale_hostname }}' 'status_host={{ status_hostname }}' 'staging_host={{ staging_ui_hostname }}' 'staging_port={{ staging_edge_port }}' >"$work/hosts.j2"
(cd "$repo_root" && ansible localhost -c local -m ansible.builtin.template -a "src=$work/hosts.j2 dest=$work/hosts.env" >/dev/null)
app_host="" hs_host="" status_host="" staging_host="" staging_port=""
# shellcheck disable=SC1091
. "$work/hosts.env"

# Mock upstreams echo "<name> <method> <path>" so routing and prefix stripping are observable.
cat >"$work/mock.py" <<'EOF'
import http.server, sys
name, port = sys.argv[1], int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def _r(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n: self.rfile.read(n)
            body = f"{name} {self.command} {self.path}".encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    do_GET = do_POST = do_DELETE = do_HEAD = _r
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOF
for spec in ui:3000 agent:9000 headscale:8080; do
  python3 "$work/mock.py" "${spec%%:*}" "${spec##*:}" & pids+=($!)
done

sudo ip addr replace "$tailnet_ip/32" dev lo
mkdir -p "$work/logs"
docker run -d --name caddy-routing-test --network host \
  -v "$work/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$work/logs:/var/log/caddy" "$image" >/dev/null
head -c 20000 /dev/zero >"$work/big"
for _ in $(seq 1 30); do curl -sk --noproxy "*" -o /dev/null --resolve "$app_host:443:127.0.0.1" "https://$app_host/" && break; sleep 1; done

fail=0
req() { # <host> [curl args...] -> "<status> <body>"
  local host="$1"; shift
  local code
  code="$(curl -sk --noproxy "*" --max-time 5 --resolve "$host:443:127.0.0.1" -o "$work/body" -w '%{http_code}' "$@")" || code="000"
  printf '%s' "$code"
  printf ' %s' "$(cat "$work/body" 2>/dev/null)"; : >"$work/body"
}
req2() { # same as req, but as a different client (source 127.0.0.3) so rate-limit state is fresh
  local host="$1"; shift
  local code
  code="$(curl -sk --noproxy "*" --max-time 5 --interface 127.0.0.3 --resolve "$host:443:127.0.0.1" -o "$work/body" -w '%{http_code}' "$@")" || code="000"
  printf '%s' "$code"
  printf ' %s' "$(cat "$work/body" 2>/dev/null)"; : >"$work/body"
}
expect() { # <description> <expected-prefix> <actual>
  if [[ "$3" == "$2"* ]]; then echo "PASS  $1 -> $3"; else echo "FAIL  $1: expected '$2', got '$3'"; fail=1; fi
}

u="https://$app_host"
expect "MCP endpoint, prefix stripped"         "200 agent POST /"                 "$(req "$app_host" -X POST "$u/agent")"
expect "MCP endpoint trailing slash"           "200 agent GET /"                  "$(req "$app_host" "$u/agent/")"
expect "agent RFC9728 metadata"                "200 agent GET /.well-known/oauth-protected-resource" "$(req "$app_host" "$u/agent/.well-known/oauth-protected-resource")"
expect "root-inserted metadata goes to UI"     "200 ui GET /.well-known/oauth-protected-resource/agent" "$(req "$app_host" "$u/.well-known/oauth-protected-resource/agent")"
expect "token endpoint"                        "200 agent POST /oauth/token"      "$(req "$app_host" -X POST "$u/agent/oauth/token")"
expect "authorize keeps query"                 "200 agent GET /oauth/authorize?x=1" "$(req "$app_host" "$u/agent/oauth/authorize?x=1")"
expect "login page"                            "200 ui GET /auth/oauth-login"     "$(req "$app_host" "$u/auth/oauth-login")"
expect "static assets"                         "200 ui GET /_next/static/a.js"    "$(req "$app_host" "$u/_next/static/a.js")"
expect "UI root is not public"                 "404"                              "$(req "$app_host" "$u/")"
expect "arbitrary path is 404"                 "404"                              "$(req "$app_host" "$u/anything-else")"
expect "unauthenticated tool list is private"  "404"                              "$(req "$app_host" "$u/agent/api/tools")"
expect "login page for UI is private"          "404"                              "$(req "$app_host" "$u/auth/login")"
expect "GET on login API is 404"               "404"                              "$(req "$app_host" "$u/api/auth/login")"
expect "prefix lookalike is 404"               "404"                              "$(req "$app_host" "$u/agentx")"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(req "$app_host" -X POST -d '{}' "$u/api/auth/login" | cut -c1-3) "; done
expect "login rate limit (5/min) triggers 429" "200 200 200 200 200 429" "$codes"

expect "login body over cap"                   "413"                              "$(req2 "$app_host" -X POST --data-binary "@$work/big" "$u/api/auth/login")"

expect "headscale proxied"                     "200 headscale GET /health"        "$(req "$hs_host" "https://$hs_host/health")"
expect "headscale API blocked publicly"        "000"                              "$(req "$hs_host" "https://$hs_host/api/v1/node")"
expect "tailnet-only wildcard aborts publicly" "000"                              "$(req "$status_host" "https://$status_host/")"

# Staging listener: mock upstreams are shared, so only routing and the tailnet gate are asserted.
sreq() { # <curl args...>
  local code
  code="$(curl -sk --noproxy "*" --max-time 5 --resolve "$staging_host:$staging_port:127.0.0.1" -o "$work/body" -w '%{http_code}' "$@")" || code="000"
  printf '%s' "$code"
  printf ' %s' "$(cat "$work/body" 2>/dev/null)"; : >"$work/body"
}
s="https://$staging_host:$staging_port"
expect "staging aborts outside the tailnet"    "000"                              "$(sreq "$s/")"
expect "staging UI from the tailnet"           "200 ui GET /trader"               "$(sreq --interface "$tailnet_ip" "$s/trader")"
expect "staging agent, prefix stripped"        "200 agent GET /api/tools"         "$(sreq --interface "$tailnet_ip" "$s/agent/api/tools")"
expect "staging listener 404s other names"     "404"                              "$(curl -sk --noproxy "*" --max-time 5 --interface "$tailnet_ip" --resolve "other.${status_host#*.}:$staging_port:127.0.0.1" -o /dev/null -w '%{http_code}' "https://other.${status_host#*.}:$staging_port/")"
expect "prod name not served on staging port"  "404"                              "$(curl -sk --noproxy "*" --max-time 5 --interface "$tailnet_ip" --resolve "$app_host:$staging_port:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$app_host:$staging_port/")"

expect "tailnet sees full UI"                  "200 ui GET /trader"               "$(req "$app_host" --interface "$tailnet_ip" "$u/trader")"
expect "tailnet reaches agent tools"           "200 agent GET /api/tools"         "$(req "$app_host" --interface "$tailnet_ip" "$u/agent/api/tools")"
expect "tailnet bypasses login rate limit"     "200 ui POST /api/auth/login"      "$(req "$app_host" --interface "$tailnet_ip" -X POST -d '{}' "$u/api/auth/login")"

expect "OAuth callback routed"                 "200 agent GET /oauth/callback"   "$(req "$app_host" "$u/agent/oauth/callback?code=SECRETCODE123&state=STATE456")"
sleep 1
if grep -qE 'SECRETCODE123|STATE456' "$work"/logs/*.access.log; then
  echo "FAIL  access log leaks OAuth code/state"; fail=1
else
  echo "PASS  access log redacts OAuth code/state"
fi
ls "$work"/logs/*.access.log >/dev/null && echo "PASS  access logs written: $(cd "$work/logs" && ls)"

exit "$fail"
