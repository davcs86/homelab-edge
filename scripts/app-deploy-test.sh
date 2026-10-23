#!/usr/bin/env bash
# Offline test of templates/app-deploy/app-deploy.sh against a fake docker: releases, pruning, rollback, input checks.
set -euo pipefail

gate_script="$(cd "$(dirname "$0")/.." && pwd)/templates/app-deploy/app-deploy.sh"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
app="$work/opt/myapp"
mkdir -p "$work/bin" "$app"
fail=0

cat >"$work/bin/docker" <<EOT
#!/usr/bin/env bash
echo "docker \$* [cfg=\${DOCKER_CONFIG:-none}]" >>"$work/log"
[[ "\$*" == *login* ]] && cat >/dev/null
[[ -n "\${FAIL_ON:-}" && "\$*" == *"\$FAIL_ON"*"up -d"* ]] && exit 1
exit 0
EOT
chmod +x "$work/bin/docker"
export PATH="$work/bin:$PATH"

gate() { SSH_ORIGINAL_COMMAND="$*" bash "$gate_script" "$app" myapp; }
check() { if [[ "$2" == "$3" ]]; then echo "PASS  $1"; else echo "FAIL  $1: expected '$2', got '$3'"; fail=1; fi; }
release() { printf 'c%011d-%d' "$1" "$1"; }

for n in 1 2 3 4 5 6 7 8; do
  r="$(release "$n")"
  echo "name: x" | gate put "$r" compose.yml
  echo "APP_IMAGE=i$n" | gate put "$r" .env
  echo tok | gate deploy "$r" octo >/dev/null
  sleep 0.05
done
check "keeps the newest 5 releases" 5 "$(find "$app/releases" -mindepth 1 -maxdepth 1 | wc -l)"
check "current is the newest release" "$(release 8)" "$(basename "$(readlink "$app/current")")"
check ".env is owner-only" 600 "$(stat -c %a "$app/releases/$(release 8)/.env")"

r9="$(release 9)"
echo x | gate put "$r9" compose.yml
echo y | gate put "$r9" .env
rc=0
echo tok | FAIL_ON="$r9" gate deploy "$r9" octo 2>/dev/null || rc=$?
check "unhealthy release fails the deploy" 1 "$rc"
check "unhealthy release is rolled back" "$(release 8)" "$(basename "$(readlink "$app/current")")"

check "registry token never reaches docker argv or env" 0 "$(grep -c tok "$work/log" || true)"
check "every up avoids pulling" 0 "$(grep 'up -d' "$work/log" | grep -vc -- '--pull never' || true)"

rc=0; gate put ../etc/passwd compose.yml </dev/null 2>/dev/null || rc=$?
check "path-like release is rejected" 2 "$rc"
rc=0; gate put "$(release 1)" evil </dev/null 2>/dev/null || rc=$?
check "unknown file name is rejected" 2 "$rc"
rc=0; gate "rm -rf /" </dev/null 2>/dev/null || rc=$?
check "unknown action is rejected" 2 "$rc"

exit "$fail"
