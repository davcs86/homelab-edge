# Alerts

Two independent paths. Set up both: the second one rides on the edge it is watching.

| Path | Fires on | Reaches you via | Survives the edge being down |
|---|---|---|---|
| healthchecks.io | edge stops pinging for > grace: host down, or any compose service not running / unhealthy | email | yes |
| ntfy | Gatus probes (headscale, public routes, the direct machine probes in group `machines`: `xstockstrat-ui-vm`/`-agent-vm`, `xstockstrat-staging-ui-vm`/`-agent-vm`, `proxmox-host`, `webhooks-mcp-portal-requires-auth`, `webhooks-ingest-private`, `commsgun-api-private`, `commsgun-backlog`, `commsgun-ui-private`, `happybabytracker-ui-private`, `happybabytracker-api-private`, and the SSO portal's `authelia-public`, `authelia-discovery` (exact issuer and `jwks_uri`), `authelia-jwks` (at least one key) and `lldap-private`), silent heartbeats (Proxmox, DB host, the edge backup `nightly-backup` (twice a day), pgBackRest backup), any failed edge unit | ntfy app on the phone | no (served by the edge, tailnet-only) |

## healthchecks.io (edge dead-man)

1. healthchecks.io → your project → the check whose ping URL is in `secrets/edge.sops.yaml` (`healthchecks_ping_url`,
   from secret `HC_PING_URL`). If it does not exist: create it, set the secret, run **init-secrets**, then **deploy**.
2. Period **5 min**, grace **10 min**.
3. **Integrations**: an email integration enabled on this check, address verified. Send a test from the integration.

`edge-deadman.timer` pings every 5 min only while every service in `compose.yml` is running and none is unhealthy,
so a stopped headscale emails within ~15 min.

## ntfy on the phone (everything else)

The phone must be on the tailnet (`ntfy.private.natanddavehouse.com` is tailnet-only). Use exactly that name: it is ntfy's
`base-url`, and an iOS wake-up only matches a subscription made under it.

1. Install **ntfy** (Play Store / F-Droid; iOS App Store).
2. Settings → **Manage users** → add server `https://ntfy.private.natanddavehouse.com`, user `david`, password
   `NTFY_DAVID_PASSWORD`.
3. **+** → topic `homelab-alerts`, *Use another server* → `https://ntfy.private.natanddavehouse.com`. Android: enable
   *Instant delivery* and exempt the app from battery optimisation. iOS wakes via ntfy.sh (`upstream-base-url`).
4. Test from an admin device:
   `curl -u david -H "Title: test" -d "hello" https://ntfy.private.natanddavehouse.com/homelab-alerts`

Current probe states: `https://status.private.natanddavehouse.com` (Gatus).
