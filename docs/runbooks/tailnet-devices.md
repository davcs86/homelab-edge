# Tailnet devices

## Add a node

Every **node-admin** run prints its result and a device table (name, owner/tags, IPv4, online, last seen, key expiry)
on the run's **Summary** page.

1. On the device: `tailscale up --login-server https://hs.natanddavehouse.com` (Android: Tailscale app → ⋮ →
   *Change server*; iOS: Settings → Tailscale → *Alternate Coordination Server URL*). It shows a login URL ending in
   `hskey-authreq-…`.
2. **node-admin → `approve-device`**: paste that URL (or just the ID) into `registration`, pick `user`
   (`david` = edge only, `david_admin` = everything).
3. Servers: also fill `tags` (e.g. `xstockstrat`; the `tag:` prefix is optional) in the same run, or later with **`set-tags`**.
   Tagged nodes become owned by headscale's `tagged-devices`. For unattended servers prefer a single-use tagged key
   (`preauthkeys create --tags tag:x --expiration 1h`).

## Revoke a node

- **node-admin → `remove-device`** with the device name or ID: removes it and its key immediately; it must
  re-register to return.
- **node-admin → `force-reauth`**: forces re-authentication but keeps the record.
- Lost admin device: also remove its key from `admin_ssh_public_keys` and run **deploy**.

## Create an ephemeral tagged pre-auth key for CI

- From GitHub: **node-admin → `mint-ci-key`** mints a reusable, ephemeral `tag:ci` key (24 h) and commits it encrypted to
  `secrets/ci-authkey.sops.yaml`; the CI job decrypts it with its SOPS key and runs
  `tailscale up --login-server https://hs.natanddavehouse.com --auth-key "$KEY" --hostname ci-$RUN_ID`.
  Ephemeral nodes are deleted on logout, or 5 minutes after going offline.
- From an admin device (over the tailnet):
  `ssh david_admin@edge 'sudo docker compose -f /opt/edge/compose.yml exec -T headscale headscale preauthkeys create --tags tag:ci --ephemeral --expiration 1h'`

`tag:ci` can reach only `tag:xstockstrat:22` and `tag:db:22` (the `db-host` workflow; `docs/runbooks/db-host.md`).

## Same actions without GitHub

From an admin device on the tailnet, with the deploy key and SOPS key the workflow uses:

```sh
ansible-playbook playbooks/node-admin.yml -e '{"action":"list-devices"}'
ansible-playbook playbooks/node-admin.yml -e '{"action":"approve-device","registration":"<URL or ID>","user":"david","tags":"xstockstrat"}'
ansible-playbook playbooks/node-admin.yml -e '{"action":"set-tags","device":"<name|id>","tags":"xstockstrat,db"}'
ansible-playbook playbooks/node-admin.yml -e '{"action":"remove-device","device":"<name|id>"}'   # or force-reauth
ansible-playbook playbooks/node-admin.yml -e '{"action":"mint-ci-key","ci_key_hours":24}'
```

Pass vars as JSON: `-e key=value` splits on spaces. Add `"summary_file":"<path>"` to also write the Markdown table.
