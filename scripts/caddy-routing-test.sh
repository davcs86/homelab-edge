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
' 'app_host={{ public_app_hostname }}' 'hs_host={{ headscale_hostname }}' 'staging_host={{ staging_public_hostname }}' 'wmcp_host={{ webhooks_mcp_public_hostname }}' 'cgpriv_host={{ commsgun_ui_private_hostname }}' 'cmpriv_host={{ commsgun_enqueue_private_hostname }}' 'stpriv_host={{ status_private_hostname }}' 'ntpriv_host={{ ntfy_private_hostname }}' 'whpriv_host={{ webhooks_ingest_private_hostname }}' 'hbpriv_host={{ happybabytracker_ui_private_hostname }}' 'llpriv_host={{ lldap_ui_private_hostname }}' 'auth_host={{ authelia_public_hostname }}' >"$work/hosts.j2"
(cd "$repo_root" && ansible localhost -c local -m ansible.builtin.template -a "src=$work/hosts.j2 dest=$work/hosts.env" >/dev/null)
app_host="" hs_host="" staging_host="" wmcp_host="" cgpriv_host="" cmpriv_host="" stpriv_host="" ntpriv_host="" whpriv_host="" hbpriv_host="" llpriv_host="" auth_host=""
# shellcheck disable=SC1091
. "$work/hosts.env"

# Mock upstreams echo "<name> <method> <path>" so routing and prefix stripping are observable;
# Test hooks: /echo-headers (or an X-Echo-Headers request header), /reset; commsgun-ui and authelia also send Location.
cat >"$work/mock.py" <<'EOF'
import http.server, socket, struct, sys
name, port = sys.argv[1], int(sys.argv[2])
class H(http.server.BaseHTTPRequestHandler):
    def _r(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n: self.rfile.read(n)
            if self.path.split("?")[0] == "/reset":
                self.send_response(200); self.send_header("Content-Length", "100000"); self.end_headers()
                self.wfile.write(b"x" * 1000); self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                self.connection.close(); return
            body = f"{name} {self.command} {self.path}"
            if self.path.startswith("/echo-headers") or self.headers.get("X-Echo-Headers"):
                body += "".join(f" {k}=[{self.headers.get(h, '')}]" for k, h in (("xfp", "X-Forwarded-Proto"), ("xfh", "X-Forwarded-Host"), ("xff", "X-Forwarded-For"), ("host", "Host")))
                body += f" cookie=[{'; '.join(self.headers.get_all('Cookie') or [])}]"
            body = body.encode()
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            if name in ("commsgun-ui", "authelia"): self.send_header("Location", self.path)
            self.end_headers(); self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
    do_GET = do_POST = do_DELETE = do_HEAD = _r
    def log_message(self, *a): pass
http.server.ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
EOF
for spec in ui:3000 agent:9000 headscale:8080 wmcp:9100 ingest:9101 commsgun-api:9200 commsgun-ui:9202 happybabytracker-ui:9212 lldap:17170 authelia:9091 gatus:8081 ntfy:2586; do
  python3 "$work/mock.py" "${spec%%:*}" "${spec##*:}" & pids+=($!)
  if [[ "$spec" == commsgun-ui:* ]]; then cgui_pid=$!; fi
  if [[ "$spec" == authelia:* ]]; then auth_pid=$!; fi
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
ck() { # same as req -> "<status> <upstream> cookie=[...]"
  local r; r="$(req "$@")"
  printf '%s %s' "$(cut -d' ' -f1-2 <<<"$r")" "$(grep -o ' cookie=\[.*\]$' <<<"$r" | cut -c2-)"
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
expect "UI health: POST reaches the UI"         "200 ui POST /trader/api/health"   "$(req "$app_host" -X POST "$u/trader/api/health")"
expect "UI health: sibling API paths reach the UI" "200 ui GET /trader/api/other"   "$(req "$app_host" "$u/trader/api/other")"
expect "static assets"                         "200 ui GET /_next/static/a.js"    "$(req "$app_host" "$u/_next/static/a.js")"
expect "UI root is public"                     "200 ui GET /"                     "$(req "$app_host" "$u/")"
expect "arbitrary path reaches the UI"         "200 ui GET /anything-else"        "$(req "$app_host" "$u/anything-else")"
expect "unauthenticated tool list is private"  "404"                              "$(req "$app_host" "$u/agent/api/tools")"
expect "login page for UI is public"           "200 ui GET /auth/login"           "$(req "$app_host" "$u/auth/login")"
expect "GET on login API reaches the UI"       "200 ui GET /api/auth/login"       "$(req "$app_host" "$u/api/auth/login")"
expect "agent prefix lookalike reaches the UI, not the agent" "200 ui GET /agentx"    "$(req "$app_host" "$u/agentx")"
expect "other agent paths are 404"             "404"                              "$(req "$app_host" "$u/agent/anything")"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(req "$app_host" -X POST -d '{}' "$u/api/auth/login" | cut -c1-3) "; done
expect "login rate limit (5/min) triggers 429" "200 200 200 200 200 429" "$codes"

expect "login body over cap"                   "413"                              "$(req2 "$app_host" -X POST --data-binary "@$work/big" "$u/api/auth/login")"

expect "headscale proxied"                     "200 headscale GET /health"        "$(req "$hs_host" "https://$hs_host/health")"
expect "headscale API blocked publicly"        "000"                              "$(req "$hs_host" "https://$hs_host/api/v1/node")"

# webhooks-mcp: public /mcp only on its own name; the ingest name is tailnet-only.
w="https://$wmcp_host"
expect "webhooks MCP endpoint"                 "200 wmcp POST /mcp"               "$(req "$wmcp_host" -X POST -d '{}' "$w/mcp")"
expect "webhooks MCP root is 404"              "404"                              "$(req "$wmcp_host" "$w/")"
expect "webhooks MCP /hooks is 404"            "404"                              "$(req "$wmcp_host" -X POST -d '{}' "$w/hooks")"
expect "webhooks MCP /healthz is 404"          "404"                              "$(req "$wmcp_host" "$w/healthz")"
expect "webhooks MCP from the tailnet"         "200 wmcp POST /mcp"               "$(req "$wmcp_host" --interface "$tailnet_ip" -X POST -d '{}' "$w/mcp")"
expect "webhooks MCP prefix lookalike /mcpx"   "404"                              "$(req "$wmcp_host" -X POST -d '{}' "$w/mcpx")"
expect "webhooks MCP sub-path /mcp/x"          "404"                              "$(req "$wmcp_host" -X POST -d '{}' "$w/mcp/x")"

# The portal: one upstream, Host and Cookie passed through; per-IP zones on the password and back-channel paths.
a="https://$auth_host"
mixed_auth="A${auth_host#a}"
expect "portal health"                         "200 authelia GET /api/health"     "$(req "$auth_host" "$a/api/health")"
expect "portal discovery"                      "200 authelia GET /.well-known/openid-configuration" "$(req "$auth_host" "$a/.well-known/openid-configuration")"
expect "portal gets Host unchanged and the client as the only X-Forwarded-For" \
  "200 authelia GET /echo-headers xfp=[https] xfh=[$auth_host] xff=[127.0.0.1] host=[$auth_host]" \
  "$(req "$auth_host" -H 'X-Forwarded-For: 1.2.3.4' "$a/echo-headers")"
expect "portal gets a mixed-case Host as sent" "200 authelia GET /echo-headers xfp=[https] xfh=[$mixed_auth] xff=[127.0.0.1] host=[$mixed_auth]" \
  "$(req "$mixed_auth" "https://$mixed_auth/echo-headers")"
expect "authelia_session reaches the portal"   "200 authelia cookie=[a=1; authelia_session=SECRET]" \
  "$(ck "$auth_host" -H 'Cookie: a=1; authelia_session=SECRET' "$a/echo-headers")"
expect "staging VM reaches the portal"         "200 authelia GET /api/health"     "$(req "$auth_host" --interface "$staging_vm_ip" "$a/api/health")"
codes=""
for _ in $(seq 1 11); do codes+="$(req "$auth_host" -X POST -d '{}' "$a/api/firstfactor" | cut -c1-3) "; done
expect "portal login limit (10/min) triggers 429" "200 200 200 200 200 200 200 200 200 200 429" "$codes"
expect "portal health is outside the login zone" "200 authelia GET /api/health"   "$(req "$auth_host" "$a/api/health")"
codes=""
for _ in $(seq 1 11); do codes+="$(req "$auth_host" -X POST -d '{}' "$a/api/oidc/token" | cut -c1-3) "; done
expect "portal token endpoint is outside the login zone" "200 200 200 200 200 200 200 200 200 200 200" "$codes"
n=20
for path in /api/firstfactor/reauthenticate /api/secondfactor/totp /api/reset-password /api/reset-password/identity/start \
  /api/user/session/elevation /api/user/session/elevation/abc; do
  n=$((n + 1))
  for _ in $(seq 1 10); do req "$auth_host" --interface "127.0.0.$n" -X POST -d '{}' "$a/api/firstfactor" >/dev/null; done
  expect "portal POST $path shares the login zone" "429" "$(req "$auth_host" --interface "127.0.0.$n" -X POST -d '{}' "$a$path")"
done

# authelia_session is scoped to the whole public zone: no app upstream may receive it, wherever it sits in the header.
for target in "ui|$app_host|$u/echo-headers|GET" "agent|$app_host|$u/agent|POST" "wmcp|$wmcp_host|$w/mcp|POST"; do
  IFS='|' read -r up host url method <<<"$target"
  for c in "authelia_session=SECRET|" "authelia_session=SECRET; a=1; b=2|a=1; b=2" "a=1; authelia_session=SECRET; b=2|a=1; b=2" \
    "a=1; b=2; authelia_session=SECRET|a=1; b=2"; do
    expect "$up drops authelia_session from '${c%%|*}'" "200 $up cookie=[${c#*|}]" \
      "$(ck "$host" -X "$method" -H 'X-Echo-Headers: 1' -H "Cookie: ${c%%|*}" "$url")"
  done
  expect "$up drops authelia_session from a second HTTP/2 cookie field" "200 $up cookie=[a=1; b=2]" \
    "$(ck "$host" --http2 -X "$method" -H 'X-Echo-Headers: 1' -H 'Cookie: a=1' -H 'Cookie: authelia_session=SECRET; b=2' "$url")"
  expect "$up drops authelia_session from a first HTTP/2 cookie field" "200 $up cookie=[a=1; b=2]" \
    "$(ck "$host" --http2 -X "$method" -H 'X-Echo-Headers: 1' -H 'Cookie: authelia_session=SECRET' -H 'Cookie: a=1; b=2' "$url")"
done

# commsgun: the dashboard and the enqueue API are tailnet-only *.private names; each reaches only its own upstream.
v="https://$cgpriv_host"
expect "private dashboard aborts publicly"     "000"                              "$(req "$cgpriv_host" "$v/login")"
expect "private dashboard from the tailnet"    "200 commsgun-ui GET /login"       "$(req "$cgpriv_host" --interface "$tailnet_ip" "$v/login")"
expect "staging VM denied on the private dashboard" "000"                         "$(req "$cgpriv_host" --interface "$staging_vm_ip" "$v/login")"
expect "unknown private-zone host is 404 on the tailnet" "404"                    "$(req "nope.${cgpriv_host#*.}" --interface "$tailnet_ip" "https://nope.${cgpriv_host#*.}/")"
expect "unknown private-zone host aborts publicly" "000"                          "$(req "nope.${cgpriv_host#*.}" "https://nope.${cgpriv_host#*.}/")"
expect "private dashboard gets Caddy's X-Forwarded-*, not the client's" \
  "200 commsgun-ui GET /echo-headers xfp=[https] xfh=[$cgpriv_host] xff=[$tailnet_ip]" \
  "$(req "$cgpriv_host" --interface "$tailnet_ip" -H 'X-Forwarded-For: 1.2.3.4' -H 'X-Forwarded-Host: evil.example' -H 'X-Forwarded-Proto: http' "$v/echo-headers")"
expect "private dashboard upstream keeps the query" "200 commsgun-ui GET /messages?recipient=canary@example.test" \
  "$(req "$cgpriv_host" --interface "$tailnet_ip" -H 'Referer: https://x/?recipient=canary@example.test' "$v/messages?recipient=canary@example.test")"
# happybabytracker: a tailnet-only *.private name with one upstream (the UI; the API is never published); no body cap.
hb="https://$hbpriv_host"
expect "happybaby aborts publicly"             "000"                              "$(req "$hbpriv_host" "$hb/healthz")"
expect "happybaby from the tailnet"            "200 happybabytracker-ui GET /healthz" "$(req "$hbpriv_host" --interface "$tailnet_ip" "$hb/healthz")"
expect "happybaby API health goes through the UI" "200 happybabytracker-ui GET /healthz/api" "$(req "$hbpriv_host" --interface "$tailnet_ip" "$hb/healthz/api")"
expect "staging VM denied on happybaby"        "000"                              "$(req "$hbpriv_host" --interface "$staging_vm_ip" "$hb/healthz")"
expect "happybaby gets Caddy's X-Forwarded-*, not the client's" \
  "200 happybabytracker-ui GET /echo-headers xfp=[https] xfh=[$hbpriv_host] xff=[$tailnet_ip]" \
  "$(req "$hbpriv_host" --interface "$tailnet_ip" -H 'X-Forwarded-For: 1.2.3.4' -H 'X-Forwarded-Host: evil.example' -H 'X-Forwarded-Proto: http' "$hb/echo-headers")"
expect "happybaby name reaches only its UI"    "200 happybabytracker-ui POST /v1/messages" "$(req "$hbpriv_host" --interface "$tailnet_ip" -X POST -d '{}' "$hb/v1/messages")"
# lldap's web UI: a tailnet-only *.private name; LDAP itself is never behind Caddy.
ll="https://$llpriv_host"
expect "lldap UI aborts publicly"              "000"                              "$(req "$llpriv_host" "$ll/health")"
expect "lldap UI from the tailnet"             "200 lldap GET /health"            "$(req "$llpriv_host" --interface "$tailnet_ip" "$ll/health")"
expect "staging VM denied on the lldap UI"     "000"                              "$(req "$llpriv_host" --interface "$staging_vm_ip" "$ll/health")"
# The enqueue API: its own body cap, the same isolation, its own upstream.
e="https://$cmpriv_host"
expect "private enqueue aborts publicly"       "000"                              "$(req "$cmpriv_host" -X POST -d '{}' "$e/v1/messages")"
expect "private enqueue from the tailnet"      "200 commsgun-api POST /v1/messages" "$(req "$cmpriv_host" --interface "$tailnet_ip" -X POST -d '{}' "$e/v1/messages")"
expect "private enqueue body at exactly 262144 bytes" "200 commsgun-api POST /v1/messages" "$(req "$cmpriv_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/at_cap_ingest" "$e/v1/messages")"
expect "private enqueue body over 262144 bytes" "413"                             "$(req "$cmpriv_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/big_ingest" "$e/v1/messages")"
expect "staging VM denied on the private enqueue" "000"                           "$(req "$cmpriv_host" --interface "$staging_vm_ip" -X POST -d '{}' "$e/v1/messages")"
expect "private enqueue name reaches only the API" "200 commsgun-api GET /login"  "$(req "$cmpriv_host" --interface "$tailnet_ip" "$e/login")"
expect "private dashboard name reaches only the UI" "200 commsgun-ui POST /v1/messages" "$(req "$cgpriv_host" --interface "$tailnet_ip" -X POST -d '{}' "$v/v1/messages")"
# The status page under *.private.
t="https://$stpriv_host"
expect "private status page aborts publicly"   "000"                              "$(req "$stpriv_host" "$t/")"
expect "private status page from the tailnet"  "200 gatus GET /"                  "$(req "$stpriv_host" --interface "$tailnet_ip" "$t/")"
expect "private status takes a heartbeat push" "200 gatus POST /api/v1/endpoints/home_x/external?success=true" "$(req "$stpriv_host" --interface "$tailnet_ip" -X POST "$t/api/v1/endpoints/home_x/external?success=true")"
expect "staging VM denied on the private status page" "000"                       "$(req "$stpriv_host" --interface "$staging_vm_ip" "$t/")"
# ntfy under *.private. ntfy accepts credentials in ?auth=, which the zone's logger must drop.
n="https://$ntpriv_host"
expect "private ntfy aborts publicly"          "000"                              "$(req "$ntpriv_host" "$n/v1/health")"
expect "private ntfy from the tailnet"         "200 ntfy GET /v1/health"          "$(req "$ntpriv_host" --interface "$tailnet_ip" "$n/v1/health")"
expect "private ntfy keeps the query upstream" "200 ntfy GET /homelab-alerts/json?poll=1&auth=canarycredential" "$(req "$ntpriv_host" --interface "$tailnet_ip" "$n/homelab-alerts/json?poll=1&auth=canarycredential")"
expect "staging VM denied on the private ntfy" "000"                              "$(req "$ntpriv_host" --interface "$staging_vm_ip" "$n/v1/health")"
# Caddy matches hosts case-insensitively but picks a per-host logger by exact name: the whole zone must redact.
mixed="C${cgpriv_host#c}"
expect "private dashboard, mixed-case host"   "200 commsgun-ui GET /messages?recipient=canary@example.test" \
  "$(req "$mixed" --interface "$tailnet_ip" -H 'Referer: https://x/?recipient=canary@example.test' "https://$mixed/messages?recipient=canary@example.test")"

# Staging app: under the *.public wildcard, one view for every client. Mock upstreams are shared, so only routing and limits are asserted.
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
expect "staging tool catalog is private from the tailnet too" "404"               "$(sreq --interface "$tailnet_ip" "$s/agent/api/tools")"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(sreq --interface "$tailnet_ip" -X POST -d '{}' "$s/api/auth/login" | cut -c1-3) "; done
expect "staging login limit applies to tailnet clients" "200 200 200 200 200 429" "$codes"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(sreq --interface 127.0.0.4 -X POST -d '{}' "$s/api/auth/login" | cut -c1-3) "; done
expect "staging login rate limit (5/min) 429"  "200 200 200 200 200 429"          "$codes"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(req "$app_host" --interface 127.0.0.7 -X POST -d '{}' "$u/api/auth/login" | cut -c1-3) "; done
expect "prod login limit trips for a fresh client" "200 200 200 200 200 429" "$codes"
expect "staging login limit is independent of prod" "200 ui POST /api/auth/login" "$(sreq --interface 127.0.0.7 -X POST -d '{}' "$s/api/auth/login")"
expect "unknown public-zone host is 404"       "404"                              "$(req "nope.${staging_host#*.}" "https://nope.${staging_host#*.}/")"
expect "unknown public-zone host is 404 from the tailnet" "404"                   "$(req "nope.${staging_host#*.}" --interface "$tailnet_ip" "https://nope.${staging_host#*.}/")"
expect "staging login body over cap"           "413"                              "$(sreq --interface 127.0.0.5 -X POST --data-binary "@$work/big" "$s/api/auth/login")"

expect "tailnet sees full UI"                  "200 ui GET /trader"               "$(req "$app_host" --interface "$tailnet_ip" "$u/trader")"
expect "prod tool catalog is private from the tailnet too" "404"                 "$(req "$app_host" --interface "$tailnet_ip" "$u/agent/api/tools")"
codes=""
for _ in 1 2 3 4 5 6; do codes+="$(req "$app_host" --interface "$tailnet_ip" -X POST -d '{}' "$u/api/auth/login" | cut -c1-3) "; done
expect "prod login limit applies to tailnet clients" "200 200 200 200 200 429"    "$codes"

# Isolation: tailnet grants are per port, so the staging VM may reach 443; Caddy keeps its address off every site but its own.
expect "staging VM denied on its own app over the tailnet" "000"                  "$(sreq --interface "$staging_vm_ip" "$s/trader")"
expect "staging VM denied on prod's site"      "000"                              "$(req "$app_host" --interface "$staging_vm_ip" "$u/trader")"
expect "staging VM denied on prod's agent"     "000"                              "$(req "$app_host" --interface "$staging_vm_ip" "$u/agent/api/tools")"
expect "staging VM denied on webhooks MCP"     "000"                              "$(req "$wmcp_host" --interface "$staging_vm_ip" -X POST -d '{}' "$w/mcp")"
expect "staging VM denied on headscale"        "000"                              "$(req "$hs_host" --interface "$staging_vm_ip" "https://$hs_host/health")"
# The ingest: one URL on 443 for every environment. The staging VM is admitted on this name only.
w2="https://$whpriv_host"
expect "private ingest aborts publicly"        "000"                              "$(req "$whpriv_host" -X POST -d '{}' "$w2/hooks")"
expect "private ingest from the tailnet"       "200 ingest POST /hooks/minted.x-1" "$(req "$whpriv_host" --interface "$tailnet_ip" -X POST -d '{}' "$w2/hooks/minted.x-1")"
expect "private ingest body over 256 KiB"      "413"                              "$(req "$whpriv_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/big_ingest" "$w2/hooks")"
expect "private ingest body at exactly 256 KiB" "200 ingest POST /hooks"          "$(req "$whpriv_host" --interface "$tailnet_ip" -X POST --data-binary "@$work/at_cap_ingest" "$w2/hooks")"
expect "staging VM reaches the private ingest on 443" "200 ingest POST /hooks"    "$(req "$whpriv_host" --interface "$staging_vm_ip" -X POST -d '{}' "$w2/hooks")"
expect "staging VM body over 256 KiB on the private ingest" "413"                 "$(req "$whpriv_host" --interface "$staging_vm_ip" -X POST --data-binary "@$work/big_ingest" "$w2/hooks")"
expect "staging VM still denied on an unknown private name" "000"                 "$(req "nope.${whpriv_host#*.}" --interface "$staging_vm_ip" "https://nope.${whpriv_host#*.}/")"
expect "private ingest keeps the query upstream" "200 ingest POST /hooks/n?token=canarytoken" "$(req "$whpriv_host" --interface "$tailnet_ip" -X POST -d '{}' "$w2/hooks/n?token=canarytoken")"
expect "other tailnet nodes still reach prod"  "200 ui GET /trader"               "$(req "$app_host" --interface "$tailnet_ip" "$u/trader")"

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
req "$cgpriv_host" --interface "$tailnet_ip" -H "$canary_hdr" "$v/reset?recipient=canary@example.test" >/dev/null
kill "$cgui_pid"; wait "$cgui_pid" 2>/dev/null || true
expect "dashboard upstream down"               "502" "$(req "$cgpriv_host" --interface "$tailnet_ip" -H "$canary_hdr" "$v/upstream-down?recipient=canary@example.test")"
wait_for_line "\"host\":\"$cgpriv_host\",\"uri\":\"/upstream-down\"" access_logs || true
expect "dashboard upstream down, mixed-case host" "502" "$(req "$mixed" --interface "$tailnet_ip" -H "$canary_hdr" "https://$mixed/upstream-down?recipient=canary@example.test")"
wait_for_line "\"host\":\"$mixed\",\"uri\":\"/upstream-down" access_logs || true
wait_for_line 'http.log.error.private_zone.*"uri":"/upstream-down"' docker logs caddy-routing-test || true
# The portal's reset links carry a token in the query; the *.public logger drops it, Referer and Location for every host.
auth_canary='Referer: https://x/?token=canaryreferer'
expect "portal reset link"                     "200 authelia GET /reset-password/step2?token=canarytoken" \
  "$(req "$auth_host" -H "$auth_canary" "$a/reset-password/step2?token=canarytoken")"
expect "portal reset link, mixed-case host"    "200 authelia GET /reset-password/step2?token=canarytoken" \
  "$(req "$mixed_auth" -H "$auth_canary" "https://$mixed_auth/reset-password/step2?token=canarytoken")"
kill "$auth_pid"; wait "$auth_pid" 2>/dev/null || true
expect "portal upstream down"                  "502" "$(req "$auth_host" -H "$auth_canary" "$a/upstream-down?token=canarytoken")"
expect "portal upstream down, mixed-case host" "502" "$(req "$mixed_auth" -H "$auth_canary" "https://$mixed_auth/upstream-down?token=canarytoken")"
wait_for_line "\"host\":\"$mixed_auth\",\"uri\":\"/upstream-down" access_logs || true
wait_for_line 'http.log.error.public_zone.*"uri":"/upstream-down"' docker logs caddy-routing-test || true
if ! docker logs caddy-routing-test >"$work/caddy.stderr" 2>&1 || [[ ! -s "$work/caddy.stderr" ]]; then
  echo "FAIL  cannot read Caddy's stderr"; fail=1; : >"$work/caddy.stderr"
fi
# The dashboard keeps recipient filters in its query; its log lines keep method, host, path and status only.
if read_log '*.access.log'; then
  if ! grep -q "\"host\":\"$cgpriv_host\",\"uri\":\"/upstream-down\"" "$work/log"; then
    echo "FAIL  the dashboard's 502 request was not logged at all"; fail=1
  elif ! grep -q "\"host\":\"$mixed\",\"uri\":\"/messages" "$work/log" || ! grep -q "\"host\":\"$cgpriv_host\",\"uri\":\"/messages" "$work/log"; then
    echo "FAIL  the dashboard's requests were not logged"; fail=1
  elif grep -q canary "$work/log"; then
    echo "FAIL  access log leaks a query, Referer or Location: $(grep -o '"[^"]*canary[^"]*"' "$work/log" | sort -u | tr '\n' ' ')"; fail=1
  else
    echo "PASS  *.private requests logged without query, Referer or Location (incl. reset and 502)"
  fi
fi
if read_log 'public.access.log'; then
  missing=""
  for want in "\"host\":\"$auth_host\",\"uri\":\"/reset-password/step2\"" "\"host\":\"$mixed_auth\",\"uri\":\"/reset-password/step2\"" \
    "\"host\":\"$auth_host\",\"uri\":\"/upstream-down\"" "\"host\":\"$mixed_auth\",\"uri\":\"/upstream-down\""; do
    grep -qF "$want" "$work/log" || missing+=" $want"
  done
  if [[ -n "$missing" ]]; then
    echo "FAIL  the portal's requests were not logged:$missing"; fail=1
  elif grep -q canary "$work/log"; then
    echo "FAIL  public access log leaks a query, Referer or Location: $(grep -o '"[^"]*canary[^"]*"' "$work/log" | sort -u | tr '\n' ' ')"; fail=1
  else
    echo "PASS  *.public requests logged without query, Referer or Location (incl. mixed case and 502)"
  fi
fi
if grep -q canary "$work/caddy.stderr"; then
  echo "FAIL  Caddy's stderr leaks a query or Referer: $(grep -o '"logger":"[^"]*"' <(grep canary "$work/caddy.stderr") | sort -u | tr '\n' ' ')"; fail=1
else
  echo "PASS  Caddy's stderr carries no query or Referer from *.private or *.public"
fi
for want in 'http.log.error.private_zone.*"uri":"/upstream-down"' 'http.log.error.public_zone.*"uri":"/upstream-down"' "http.handlers.reverse_proxy.*\"host\":\"$cgpriv_host\",\"uri\":\"/reset\""; do
  if grep -qE "$want" "$work/caddy.stderr"; then echo "PASS  stderr has a query-less line: $want"; else echo "FAIL  stderr lacks: $want"; fail=1; fi
done
# Zone names are global in caddy-ratelimit: a second site with the same `limits` must still get its own counters.
python3 - "$repo_root/inventories/prod/group_vars/all/main.yml" >"$work/twin.json" <<'EOF'
import json, sys, yaml
sites = yaml.safe_load(open(sys.argv[1]))["edge_sites"]
twin = next(s for s in sites if s["kind"] == "bearer_mcp")
sites.append({**twin, "name": twin["name"] + "-twin", "hostname": "twin-" + twin["hostname"]})
print(json.dumps({"edge_sites": sites}))
EOF
(cd "$repo_root" && ansible localhost -c local -m ansible.builtin.template \
  -a "src=roles/stack/templates/Caddyfile.j2 dest=$work/Caddyfile.twin" \
  -e caddy_test_mode=true -e app_tailnet_host=127.0.0.1 -e staging_tailnet_host=127.0.0.1 -e "@$work/twin.json" >/dev/null)
for f in Caddyfile Caddyfile.twin; do
  zones="$(docker run --rm --network none -v "$work/$f:/etc/caddy/Caddyfile:ro" "$image" caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null \
    | python3 -c 'import json, sys
def walk(o):
    if isinstance(o, dict):
        yield from o.get("rate_limits", {})
        for v in o.values(): yield from walk(v)
    elif isinstance(o, list):
        for v in o: yield from walk(v)
print(" ".join(walk(json.load(sys.stdin))))' || true)"
  dupes="$(tr ' ' '\n' <<<"$zones" | sed '/^$/d' | sort | uniq -d | paste -sd' ' -)"
  if [[ -z "$zones" ]]; then echo "FAIL  no rate-limit zones found in the adapted $f"; fail=1
  elif [[ -n "$dupes" ]]; then echo "FAIL  rate-limit zones shared across sites in $f: $dupes"; fail=1
  else echo "PASS  every rate-limit zone in $f is unique ($(wc -w <<<"$zones") zones)"; fi
done
if logs_written="$(docker exec caddy-routing-test sh -c 'ls /var/log/caddy/*.access.log')"; then
  echo "PASS  access logs written: $(echo "$logs_written" | xargs -n1 basename | tr '\n' ' ')"
else
  echo "FAIL  no access logs written"; fail=1
fi

exit "$fail"
