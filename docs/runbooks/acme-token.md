# The ACME token

Caddy proves domain ownership through deSEC with one token. Caddy reads it from `desec_token` in
`secrets/edge.sops.yaml`; the GitHub secret `DESEC_TOKEN` only seeds that file when it is first generated, so
changing the GitHub secret alone changes nothing on the edge.

## Add a name (required before the first deploy of a new public hostname)

Each public hostname with its own certificate has its own label in `acme.natanddavehouse.com` (the `import acme
<label>.{{ acme_zone }}` line of its site in the Caddyfile), and the token may write only the labels it has a policy
for. Add the policy to the **existing** token: it is already on the edge, so no secret changes. In the deSEC web UI
create a temporary token with *Can manage tokens* (no subnet lock needed: it is the one your machine authenticates
with), then (`$T` = that temporary token, `$LABEL` = the new label, e.g. `webhooks-mcp`):

```sh
API=https://desec.io/api/v1/auth/tokens
curl -fsS $API/ -H "Authorization: Token $T" | jq -r '.[] | "\(.id) \(.name) \(.allowed_subnets)"'   # pick the Caddy token
ID=<its id>
curl -fsS -X POST $API/$ID/policies/rrsets/ -H "Authorization: Token $T" -H 'Content-Type: application/json' \
  -d "{\"domain\":\"acme.natanddavehouse.com\",\"subname\":\"$LABEL\",\"type\":\"TXT\",\"perm_write\":true}"
curl -fsS $API/$ID/policies/rrsets/ -H "Authorization: Token $T" | jq -c '.[]'   # the new label is listed
```

The subname is the bare label (not `_acme-challenge.<label>`, not the full hostname) and the domain is the delegated
zone, not `natanddavehouse.com`. Leave the Caddy token's `allowed_subnets` alone: they lock it to the edge, and the
edge is still the only caller. Delete the temporary token, then run **deploy** (`tailnet`).

Without the policy the deploy fails at "Every site presents a publicly trusted certificate" after about 15 minutes,
and the roles after `stack` do not run. Creating a new token instead does not help until `desec_token` is replaced
(below), and a new token needs every label, not only the new one.

## Rotate

The token lives in deSEC, can write only `TXT` at `hs`, `xstockstrat`, `xstockstrat-staging`, `webhooks-mcp`, `ts` in `acme.natanddavehouse.com`, and is
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
for s in hs xstockstrat xstockstrat-staging webhooks-mcp ts; do
  curl -fsS -X POST $P -H "Authorization: Token $T" -H 'Content-Type: application/json' \
    -d "{\"domain\":\"acme.natanddavehouse.com\",\"subname\":\"$s\",\"type\":\"TXT\",\"perm_write\":true}"
done
```

Then update secret `DESEC_TOKEN`, run **init-secrets** with `overwrite` (this also rotates ntfy publisher credentials
and the pgBackRest PKI — re-issue the DB host's client cert afterwards) — or, to rotate only this value, edit it in place:
`sops set secrets/edge.sops.yaml '["desec_token"]' '"<token>"'` — and run **deploy** (`tailnet`). Delete the old token
and the temporary token in deSEC. `max_unused_period` is 100 days because Caddy only touches it at renewal (~every 60).
