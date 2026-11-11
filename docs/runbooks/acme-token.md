# Rotate the ACME token

The token lives in deSEC, can write only `TXT` at `hs`, `xstockstrat`, `webhooks-mcp`, `ts` in `acme.natanddavehouse.com`, and is
accepted only from the droplet's **egress** addresses. These are not the reserved IP: DigitalOcean sends outbound
traffic from the droplet's own public IPv4 and IPv6 (the droplet page in the DO console, or `networks` in the DO API;
not the reserved IP), and desec.io has an AAAA record, so Caddy reaches it over IPv6. Both must be allowed. In the deSEC web UI
create a temporary token with *Can manage tokens*, then (`$T` = that temporary token, `$IP4`/`$IP6` = the egress
addresses):

```sh
API=https://desec.io/api/v1/auth/tokens
NEW=$(curl -fsS -X POST $API/ -H "Authorization: Token $T" -H 'Content-Type: application/json' \
  -d "{\"name\":\"caddy-edge-$(date +%Y%m%d)\",\"allowed_subnets\":[\"$IP4/32\",\"$IP6/128\"],\"max_unused_period\":\"100 00:00:00\"}")
ID=$(echo "$NEW" | jq -r .id); echo "$NEW" | jq -r .token   # -> GitHub secret DESEC_TOKEN (shown once)
P="$API/$ID/policies/rrsets/"
curl -fsS -X POST $P -H "Authorization: Token $T" -H 'Content-Type: application/json' \
  -d '{"domain":null,"subname":null,"type":null,"perm_write":false}'
for s in hs xstockstrat webhooks-mcp ts; do
  curl -fsS -X POST $P -H "Authorization: Token $T" -H 'Content-Type: application/json' \
    -d "{\"domain\":\"acme.natanddavehouse.com\",\"subname\":\"$s\",\"type\":\"TXT\",\"perm_write\":true}"
done
```

Then update secret `DESEC_TOKEN`, run **init-secrets** with `overwrite` (this also rotates ntfy publisher credentials
and the pgBackRest PKI — re-issue the DB host's client cert afterwards) — or, to rotate only this value, edit it in place:
`sops set secrets/edge.sops.yaml '["desec_token"]' '"<token>"'` — and run **deploy** (`tailnet`). Delete the old token
and the temporary token in deSEC. `max_unused_period` is 100 days because Caddy only touches it at renewal (~every 60).
