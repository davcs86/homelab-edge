#!/usr/bin/env bash
# Offline test of the rendered Caddyfile against local mock upstreams (Caddy internal CA).
# Usage: scripts/caddy-routing-test.sh <caddy-image>    (requires docker, ansible, python3, curl, sudo for an lo alias)
set -euo pipefail

image="${1:?caddy image}"
repo_root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
tailnet_ip=100.64.0.250
staging_vm_ip=100.64.0.251
pids=()
# shellcheck disable=SC2317,SC2329  # invoked via trap
cleanup() {
  docker rm -f caddy-routing-test >/dev/null 2>&1 || true
  for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done
  sudo ip addr del "$tailnet_ip/32" dev lo 2>/dev/null || true
  sudo ip addr del "$staging_vm_ip/32" dev lo 2>/dev/null || true
  rm -rf "$work"
}
trap cleanup EXIT

(cd "$repo_root" && ansible localhost -c local -m ansible.builtin.template \
  -a "src=roles/stack/templates/Caddyfile.j2 dest=$work/Caddyfile" \
  -e caddy_test_mode=true -e app_tailnet_host=127.0.0.1 -e staging_tailnet_host=127.0.0.1 \
  -e "{\"staging_vm_ips\": [\"$staging_vm_ip\"]}" >/dev/null)

printf '%s
' 'app_host={{ public_app_hostname }}' 'hs_host={{ headscale_hostname }}' 'status_host={{ status_hostname }}' 'staging_host={{ staging_public_hostname }}' 'staging_port={{ staging_edge_port }}' 'wmcp_host={{ webhooks_mcp_hostname }}' 'ingest_host={{ webhooks_ingest_hostname }}' 'comms_host={{ commsgun_enqueue_hostname }}' 'cgui_host={{ commsgun_ui_hostname }}' >"$work/hosts.j2"
(cd "$repo_root" && ansible localhost -c local -m ansible.builtin.template -a "src=$work/hosts.j2 dest=$work/hosts.env" >/dev/null)
app_host="" hs_host="" status_host="" staging_host="" staging_port="" wmcp_host="" ingest_host="" comms_host="" cgui_host=""
# shellcheck disable=SC1091
. "$work/hosts.env"

# Mock upstreams echo "<name> <method> <path>" so routing and prefix stripping are observable;
# Test hooks: /echo-headers, /reset; commsgun-ui also sends Location.
cat >"$work/mock.py" <<'EOF'
import http.server, socket, struct, sys
name, port = sys.argv[1], int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def _r(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n: self.rfile.read(n)
            if self.path.startswith("/reset"):
                self.send_response(200); self.send_header("Content-Length", "100000"); self.end_headers()
                self.wfile.write(b"x" * 1000); self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close(); return
            body = f"{name} {self.command} {self.path}"
            if self.path.startswith("/echo-headers"):
                body += "".join(f" {k}=[{self.headers.get(h, '')}]" for k, h in (("xfp", "X-Forwarded-Proto"), ("xfh", "X-Forwarded-Host"), ("xff", "X-Forwarded-For")))
            body = body.encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            if name == "commsgun-ui": self.send_header("Location", self.path)
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    do_GET = do_POST = do_DELETE = do_HEAD = _r
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOF
for spec in ui:3000 agent:9000 headscale:8080 wmcp:9100 ingest:9101 commsgun-api:9200 commsgun-ui:9202; do
  python3 "$work/mock.py" "${spec%%:*}" "${spec##*:}" & pids+=($!)
  if [[ "$spec" == commsgun-ui:* ]]; then cgui_pid=$!; fi
done

sudo ip addr replace "$tailnet_ip/32" dev lo
sudo ip addr replace "$staging_vm_ip/32" dev lo
mkdir -p "$work/logs"
docker run -d --name caddy-routing-test --network host \
  -v "$work/Caddyfile:/etc/caddy/Caddyfile:ro" -v "$work/logs:/var/log/caddy" "$image" >/dev/null
head -c 20000 /dev/zero >"$work/big"
head -c 262145 /dev/zero >"$work/big_ingest"
head -c 262144 /dev/zero >"$work/at_cap_ingest"
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
# Caddy writes its logs as root with a private mode, so read them through the container; an unreadable or empty log is a FAIL.
read_log() { # <file glob in /var/log/caddy> -> $work/log; non-zero if unreadable or empty
  if docker exec caddy-routing-test sh -c "cat /var/log/caddy/$1" >"$work/log" 2>"$work/log.err" && [[ -s "$work/log" ]]; then return 0; fi
  echo "FAIL  cannot read Caddy log $1: $(head -c 300 "$work/log.err")"; fail=1; return 1
}
# Caddy writes its logs asynchronously; a timeout is not a FAIL here, the check that follows reports it.
wait_for_line() { # <ERE> <command printing the log>...; polls for up to ~10 s
  local want="$1"; shift
  for _ in $(seq 1 50); do
    "$@" >"$work/poll" 2>&1 || true
    if grep -qE "$want" "$work/poll"; then return 0; fi
    sleep 0.2
  done
  return 1
}
# shellcheck disable=SC2317,SC2329  # invoked through wait_for_line
access_logs() { docker exec caddy-routing-test sh -c 'cat /var/log/caddy/*.access.log'; }

u="https://$app_host"
expect "MCP endpoint, prefix stripped"         "200 agent POST /"                 "$(req "$app_host" -X POST "$u/agent")"
expect "MCP endpoint trailing slash"           "200 agent GET /"                  "$(req "$app_host" "$u/agent/")"
expect "agent RFC9728 metadata"                "200 agent GET /.well-known/oauth-protected-resource" "$(req "$app_host" "$u/agent/.well-known/oauth-protected-resource")"
expect "root-inserted metadata goes to UI"     "200 ui GET /.well-known/oauth-protected-resource/agent" "$(req "$app_host" "$u/.well-known/oauth-protected-resource/agent")"
expect "token endpoint"                        "200 agent POST /oauth/token"      "$(req "$app_host" -X POST "$u/agent/oauth/token")"
expect "authorize keeps query"                 "200 agent GET /oauth/authorize?x=1" "$(req "$app_host" "$u/agent/oauth/authorize?x=1")"
expect "login page"                            "200 ui GET /auth/oauth-login"     "$(req "$app_host" "$u/auth/oauth-login")"
expect "UI health probe path is public"        "200 ui GET /trader/api/health"    "$(req "$app_host" "$u/trader/api/health")"
expect "UI health: POST is 404"                "404"                              "$(req "$app_host" -X POST "$u/trader/api/health")"
expect "UI health: sibling API paths are 404"  "404"                              "$(req "$app_host" "$u/trader/api/other")"
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

# webhooks-mcp: public /mcp only on its own name; the ingest name is tailnet-only.
w="https://$wmcp_host"
expect "webhooks MCP endpoint"                 "200 wmcp POST /mcp"               "$(req "$wmcp_host" -X POST -d '{}' "$w/mcp")"
expect "webhooks MCP root is 404"              "404"                              "$(req "$wmcp_host" "$w/")"
expect "webhooks MCP /hooks is 404"            "404"                              "$(req "$wmcp_host" -X POST -d '{}' "$w/hooks")"
expect "webhooks MCP /healthz is 404"          "404"                              "$(req "$wmcp_host" "$w/healthz")"
expect "webhooks ingest aborts publicly"       "000"                              "$(req "$ingest_host" -X POST -d '{}' "https://$ingest_host/hooks")"
expect "webhooks ingest from the tailnet"      "200 ingest POST /hooks"           "$(req "$ingest_host" --interface "$tailnet_ip" -X POST -d '{}' "https://$ingest_host/hooks")"
expect "webhooks ingest body over 256 KiB"     "413"                              "$(req "$ingest_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/big_ingest" "https://$ingest_host/hooks")"
expect "webhooks ingest body at exactly 256 KiB" "200 ingest POST /hooks"        "$(req "$ingest_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/at_cap_ingest" "https://$ingest_host/hooks")"
expect "webhooks MCP prefix lookalike /mcpx"   "404"                              "$(req "$wmcp_host" -X POST -d '{}' "$w/mcpx")"
expect "webhooks MCP sub-path /mcp/x"          "404"                              "$(req "$wmcp_host" -X POST -d '{}' "$w/mcp/x")"

# commsgun: both names are tailnet-only *.ts names; each reaches only its own upstream.
c="https://$comms_host"
g="https://$cgui_host"
expect "enqueue aborts publicly"               "000"                              "$(req "$comms_host" -X POST -d '{}' "$c/v1/messages")"
expect "enqueue from the tailnet"              "200 commsgun-api POST /v1/messages" "$(req "$comms_host" --interface "$tailnet_ip" -X POST -d '{}' "$c/v1/messages")"
expect "enqueue body at exactly 262144 bytes"  "200 commsgun-api POST /v1/messages" "$(req "$comms_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/at_cap_ingest" "$c/v1/messages")"
expect "enqueue body over 262144 bytes"        "413"                              "$(req "$comms_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/big_ingest" "$c/v1/messages")"
expect "dashboard aborts publicly"             "000"                              "$(req "$cgui_host" "$g/login")"
expect "dashboard from the tailnet"            "200 commsgun-ui GET /login"       "$(req "$cgui_host" --interface "$tailnet_ip" "$g/login")"
expect "staging VM denied on enqueue"          "000"                              "$(req "$comms_host" --interface "$staging_vm_ip" -X POST -d '{}' "$c/v1/messages")"
expect "staging VM denied on dashboard"        "000"                              "$(req "$cgui_host" --interface "$staging_vm_ip" "$g/login")"
expect "enqueue name reaches only the API"     "200 commsgun-api GET /login"      "$(req "$comms_host" --interface "$tailnet_ip" "$c/login")"
expect "dashboard name reaches only the UI"    "200 commsgun-ui POST /v1/messages" "$(req "$cgui_host" --interface "$tailnet_ip" -X POST -d '{}' "$g/v1/messages")"
expect "dashboard gets Caddy's X-Forwarded-*, not the client's" \
  "200 commsgun-ui GET /echo-headers xfp=[https] xfh=[$cgui_host] xff=[$tailnet_ip]" \
  "$(req "$cgui_host" --interface "$tailnet_ip" -H 'X-Forwarded-For: 1.2.3.4' -H 'X-Forwarded-Host: evil.example' -H 'X-Forwarded-Proto: http' "$g/echo-headers")"
expect "dashboard upstream keeps the query"    "200 commsgun-ui GET /messages?recipient=canary@example.test" \
  "$(req "$cgui_host" --interface "$tailnet_ip" -H 'Referer: https://x/?recipient=canary@example.test' "$g/messages?recipient=canary@example.test")"
expect "other *.ts name, with a query"         "200 ingest GET /hooks?keep=visible" "$(req "$ingest_host" --interface "$tailnet_ip" "https://$ingest_host/hooks?keep=visible")"

# Staging app: its own name on 443. Mock upstreams are shared, so only routing and limits are asserted.
# wreq reaches the tailnet-only listener on staging_port (the staging webhooks ingest).
wreq() { # <host> <curl args...>: any name on the staging port
  local host="$1"; shift
  local code
  code="$(curl -sk --noproxy "*" --max-time 5 --resolve "$host:$staging_port:127.0.0.1" -o "$work/body" -w '%{http_code}' "$@")" || code="000"
  printf '%s' "$code"
  printf ' %s' "$(cat "$work/body" 2>/dev/null)"; : >"$work/body"
}
sreq() { req "$staging_host" "$@"; } # <curl args...>
s="https://$staging_host"
expect "staging public UI"                     "200 ui GET /trader"               "$(sreq "$s/trader")"
expect "staging public UI root"                "200 ui GET /"                     "$(sreq "$s/")"
expect "staging public tool catalog is private" "404"                             "$(sreq "$s/agent/api/tools")"
expect "staging public other agent paths 404"  "404"                              "$(sreq "$s/agent/anything")"
expect "staging OAuth callback"                 "200 agent GET /oauth/callback"   "$(sreq "$s/agent/oauth/callback")"
expect "staging login page"                     "200 ui GET /auth/oauth-login"    "$(sreq "$s/auth/oauth-login")"
expect "staging UI health probe path"           "200 ui GET /trader/api/health"   "$(sreq "$s/trader/api/health")"
expect "staging MCP endpoint"                  "200 agent POST /"                 "$(sreq -X POST "$s/agent")"
expect "staging root-inserted metadata"        "200 ui GET /.well-known/oauth-protected-resource/agent" "$(sreq "$s/.well-known/oauth-protected-resource/agent")"
expect "staging token endpoint"                "200 agent POST /oauth/token"      "$(sreq -X POST "$s/agent/oauth/token")"
expect "staging UI from the tailnet"           "200 ui GET /trader"               "$(sreq --interface "$tailnet_ip" "$s/trader")"
expect "staging tailnet agent"                 "200 agent GET /api/tools"         "$(sreq --interface "$tailnet_ip" "$s/agent/api/tools")"
expect "staging tailnet bypasses login limit"  "200 ui POST /api/auth/login"     "$(sreq --interface "$tailnet_ip" -X POST -d '{}' "$s/api/auth/login")"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(sreq --interface 127.0.0.4 -X POST -d '{}' "$s/api/auth/login" | cut -c1-3) "; done
expect "staging login rate limit (5/min) 429"  "200 200 200 200 200 429"          "$codes"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(req "$app_host" --interface 127.0.0.7 -X POST -d '{}' "$u/api/auth/login" | cut -c1-3) "; done
expect "prod login limit trips for a fresh client" "200 200 200 200 200 429" "$codes"
expect "staging login limit is independent of prod" "200 ui POST /api/auth/login" "$(sreq --interface 127.0.0.7 -X POST -d '{}' "$s/api/auth/login")"
expect "staging login body over cap"           "413"                              "$(sreq --interface 127.0.0.5 -X POST --data-binary "@$work/big" "$s/api/auth/login")"
expect "tailnet listener 404s other names"      "404"                              "$(curl -sk --noproxy "*" --max-time 5 --resolve "other.${status_host#*.}:$staging_port:127.0.0.1" -o /dev/null -w '%{http_code}' "https://other.${status_host#*.}:$staging_port/")"
expect "tailnet listener does not serve status"   "404"                              "$(curl -sk --noproxy "*" --max-time 5 --resolve "$status_host:$staging_port:127.0.0.1" -o /dev/null -w '%{http_code}' "https://$status_host:$staging_port/")"
expect "staging ingest aborts publicly"        "000"                              "$(wreq "$ingest_host" -X POST -d '{}' "https://$ingest_host:$staging_port/hooks")"
expect "staging ingest from the tailnet"       "200 ingest POST /hooks"           "$(wreq "$ingest_host" --interface "$tailnet_ip" -X POST -d '{}' "https://$ingest_host:$staging_port/hooks")"
expect "staging ingest body over 256 KiB"      "413"                              "$(wreq "$ingest_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/big_ingest" "https://$ingest_host:$staging_port/hooks")"
expect "staging ingest body at exactly 256 KiB" "200 ingest POST /hooks"          "$(wreq "$ingest_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/at_cap_ingest" "https://$ingest_host:$staging_port/hooks")"
wmcp_on_staging="$(curl -sk --noproxy "*" --max-time 5 --interface "$tailnet_ip" --resolve "$wmcp_host:$staging_port:127.0.0.1" -X POST -d '{}' "https://$wmcp_host:$staging_port/mcp" || true)"
if [[ "$wmcp_on_staging" == *wmcp* ]]; then
  echo "FAIL  webhooks MCP name reached its upstream on the staging port: $wmcp_on_staging"; fail=1
else
  echo "PASS  webhooks MCP name reaches no upstream on the staging port"
fi
prod_on_staging="$(curl -sk --noproxy "*" --max-time 5 --interface "$tailnet_ip" --resolve "$app_host:$staging_port:127.0.0.1" "https://$app_host:$staging_port/trader" || true)"
if [[ "$prod_on_staging" == *ui* || "$prod_on_staging" == *agent* ]]; then
  echo "FAIL  prod name reached an upstream on the staging port: $prod_on_staging"; fail=1
else
  echo "PASS  prod name reaches no upstream on the staging port"
fi

expect "tailnet sees full UI"                  "200 ui GET /trader"               "$(req "$app_host" --interface "$tailnet_ip" "$u/trader")"
expect "tailnet reaches agent tools"           "200 agent GET /api/tools"         "$(req "$app_host" --interface "$tailnet_ip" "$u/agent/api/tools")"
expect "tailnet bypasses login rate limit"     "200 ui POST /api/auth/login"      "$(req "$app_host" --interface "$tailnet_ip" -X POST -d '{}' "$u/api/auth/login")"

# Isolation: tailnet grants are per port, so the staging VM may reach 443; Caddy keeps its address off every site but its own.
expect "staging VM reaches its own site"       "200 ui GET /trader"               "$(sreq --interface "$staging_vm_ip" "$s/trader")"
expect "staging VM denied on prod's site"      "000"                              "$(req "$app_host" --interface "$staging_vm_ip" "$u/trader")"
expect "staging VM denied on prod's agent"     "000"                              "$(req "$app_host" --interface "$staging_vm_ip" "$u/agent/api/tools")"
expect "staging VM denied on webhooks MCP"     "000"                              "$(req "$wmcp_host" --interface "$staging_vm_ip" -X POST -d '{}' "$w/mcp")"
expect "staging VM denied on the ingest (443)" "000"                              "$(req "$ingest_host" --interface "$staging_vm_ip" -X POST -d '{}' "https://$ingest_host/hooks")"
expect "staging VM denied on status"           "000"                              "$(req "$status_host" --interface "$staging_vm_ip" "https://$status_host/")"
expect "staging VM denied on headscale"        "000"                              "$(req "$hs_host" --interface "$staging_vm_ip" "https://$hs_host/health")"
expect "staging VM reaches the ingest on 8443" "200 ingest POST /hooks"           "$(wreq "$ingest_host" --interface "$staging_vm_ip" -X POST -d '{}' "https://$ingest_host:$staging_port/hooks")"
expect "other tailnet nodes still reach prod"  "200 ui GET /trader"               "$(req "$app_host" --interface "$tailnet_ip" "$u/trader")"
staging_on_8443="$(curl -sk --noproxy "*" --max-time 5 --interface "$tailnet_ip" --resolve "$staging_host:$staging_port:127.0.0.1" "https://$staging_host:$staging_port/trader" || true)"
if [[ "$staging_on_8443" =~ ^(ui|agent|ingest|wmcp)\  ]]; then
  echo "FAIL  the staging app name reached an upstream on the tailnet-only port: $staging_on_8443"; fail=1
else
  echo "PASS  the staging app name reaches no upstream on the tailnet-only port"
fi

expect "OAuth callback routed"                 "200 agent GET /oauth/callback"   "$(req "$app_host" "$u/agent/oauth/callback?code=SECRETCODE123&state=STATE456")"
wait_for_line "\"host\":\"$app_host\",\"uri\":\"/agent/oauth/callback" access_logs || true
if read_log '*.access.log'; then
  if ! grep -q "\"host\":\"$app_host\",\"uri\":\"/agent/oauth/callback" "$work/log"; then
    echo "FAIL  the OAuth callback request was not logged"; fail=1
  elif grep -qE 'SECRETCODE123|STATE456' "$work/log"; then
    echo "FAIL  access log leaks OAuth code/state"; fail=1
  else
    echo "PASS  access log redacts OAuth code/state"
  fi
fi
canary_hdr='Referer: https://x/?recipient=canary@example.test'
req "$cgui_host" --interface "$tailnet_ip" -H "$canary_hdr" "$g/reset?recipient=canary@example.test" >/dev/null
req "$ingest_host" --interface "$tailnet_ip" "https://$ingest_host/reset?keep=visible" >/dev/null
kill "$cgui_pid"; wait "$cgui_pid" 2>/dev/null || true
expect "dashboard upstream down"               "502" "$(req "$cgui_host" --interface "$tailnet_ip" -H "$canary_hdr" "$g/upstream-down?recipient=canary@example.test")"
wait_for_line "\"host\":\"$cgui_host\",\"uri\":\"/upstream-down\"" access_logs || true
wait_for_line 'http.log.error.commsgun_ui.*"uri":"/upstream-down"' docker logs caddy-routing-test || true
if ! docker logs caddy-routing-test >"$work/caddy.stderr" 2>&1 || [[ ! -s "$work/caddy.stderr" ]]; then
  echo "FAIL  cannot read Caddy's stderr"; fail=1; : >"$work/caddy.stderr"
fi
# The dashboard keeps recipient filters in its query; its log lines keep method, host, path and status only.
if read_log '*.access.log'; then
  if ! grep -q "\"host\":\"$cgui_host\",\"uri\":\"/upstream-down\"" "$work/log"; then
    echo "FAIL  the dashboard's 502 request was not logged at all"; fail=1
  elif grep -q canary "$work/log"; then
    echo "FAIL  access log leaks the dashboard's query, Referer or Location: $(grep -o '"[^"]*canary[^"]*"' "$work/log" | sort -u | tr '\n' ' ')"; fail=1
  else
    echo "PASS  dashboard logged without query, Referer or Location (incl. reset and 502)"
  fi
fi
if read_log tailnet.access.log; then
  if grep -q '"uri":"/hooks?keep=visible"' "$work/log"; then
    echo "PASS  other *.ts names are still logged with their query"
  else
    echo "FAIL  the ingest request lost its query in tailnet.access.log"; fail=1
  fi
fi
if grep -q canary "$work/caddy.stderr"; then
  echo "FAIL  Caddy's stderr leaks the dashboard's query or Referer: $(grep -o '"logger":"[^"]*"' <(grep canary "$work/caddy.stderr") | sort -u | tr '\n' ' ')"; fail=1
else
  echo "PASS  Caddy's stderr carries no dashboard query or Referer"
fi
for want in 'http.log.error.commsgun_ui.*"uri":"/upstream-down"' "http.handlers.reverse_proxy.*\"host\":\"$cgui_host\",\"uri\":\"/reset\"" \
            "http.handlers.reverse_proxy.*\"host\":\"$ingest_host\",\"uri\":\"/reset\""; do
  if grep -qE "$want" "$work/caddy.stderr"; then echo "PASS  stderr has a query-less line: $want"; else echo "FAIL  stderr lacks: $want"; fail=1; fi
done
for n in comms cgui; do
  h="${n}_host"
  on8443="$(curl -sk --noproxy "*" --max-time 5 --interface "$tailnet_ip" --resolve "${!h}:$staging_port:127.0.0.1" -X POST -d '{}' "https://${!h}:$staging_port/v1/messages" -w ' %{http_code}' || true)"
  if [[ "$on8443" == "Not Found 404" ]]; then echo "PASS  $n name is 404 on the staging port"; else echo "FAIL  $n name on the staging port: $on8443"; fail=1; fi
done
if logs_written="$(docker exec caddy-routing-test sh -c 'ls /var/log/caddy/*.access.log')"; then
  echo "PASS  access logs written: $(echo "$logs_written" | xargs -n1 basename | tr '\n' ' ')"
else
  echo "FAIL  no access logs written"; fail=1
fi

exit "$fail"
