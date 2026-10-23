# Alerts

Two independent paths. Set up both: the second one rides on the edge it is watching.

| Path | Fires on | Reaches you via | Survives the edge being down |
|---|---|---|---|
| healthchecks.io | edge stops pinging for > grace: host down, or any compose service not running / unhealthy | email | yes |
| ntfy | Gatus probes (headscale, public routes, app, Proxmox), silent heartbeats (Proxmox, DB host, nightly backup), any failed edge unit | ntfy app on the phone | no (served by the edge, tailnet-only) |

## healthchecks.io (edge dead-man)

1. healthchecks.io → your project → the check whose ping URL is in `secrets/edge.sops.yaml` (`healthchecks_ping_url`,
   from secret `HC_PING_URL`). If it does not exist: create it, set the secret, run **init-secrets**, then **deploy**.
2. Period **5 min**, grace **10 min**.
3. **Integrations**: an email integration enabled on this check, address verified. Send a test from the integration.

`edge-deadman.timer` pings every 5 min only while every service in `compose.yml` is running and none is unhealthy,
so a stopped headscale emails within ~15 min.

## ntfy on the phone (everything else)

The phone must be on the tailnet (`ntfy.ts.natanddavehouse.com` is tailnet-only).

1. Install **ntfy** (Play Store / F-Droid; iOS App Store).
2. Settings → **Manage users** → add server `https://ntfy.ts.natanddavehouse.com`, user `david`, password
   `NTFY_DAVID_PASSWORD`.
3. **+** → topic `homelab-alerts`, *Use another server* → `https://ntfy.ts.natanddavehouse.com`. Android: enable
   *Instant delivery* and exempt the app from battery optimisation. iOS wakes via ntfy.sh (`upstream-base-url`).
4. Test from an admin device:
   `curl -u david -H "Title: test" -d "hello" https://ntfy.ts.natanddavehouse.com/homelab-alerts`

Current probe states: `https://status.ts.natanddavehouse.com` (Gatus).
