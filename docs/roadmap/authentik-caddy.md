# Authentik on the edge, integrated with Caddy

Status: planned. Nothing is installed. Resolve every `TODO(ask)` with the owner before building.

## Goal

Install and use Authentik on the edge droplet as the identity provider, with Caddy enforcing it on routes.

## Decided

- **Reachability:** outside the tailnet is preferred (browser sign-in from any device), provided it creates no
  dependency loop (see below). This must stay inside the public surface of 80/tcp, 443/tcp, 3478/udp: a new hostname
  on Caddy, no new port.
- **Database:** the shared DB host (`docs/runbooks/db-host.md`, `playbooks/db.yml`), not a container on the droplet.
- **Backups:** yes. Authentik's data joins the nightly age backups, and its database joins the pgBackRest repo.
- **Protected services:** out of scope for the first delivery. Authentik is installed and wired to Caddy; protecting
  services is done per service afterwards. Intended later targets: xstockstrat, Proxmox VE, and every future
  tailnet service.

## Dependency loops to rule out in the design

- Authentik reaches the shared DB host over the tailnet, and the tailnet is coordinated by headscale on this droplet.
  Headscale must not authenticate through Authentik, or a headscale or DB outage deadlocks recovery.
- Break glass (`docs/runbooks/recovery.md`), Gatus and ntfy must not sit behind Authentik, so alerts and recovery work
  while it is down.
- Caddy must keep serving its other routes when Authentik is down; only routes that opt in to it may fail closed.
- Restoring Authentik needs the DB host and its pgBackRest repo, not the other way round.

## TODO(ask)

- Hostname for the Authentik UI, and its TLS/ACME path (the deSEC DNS-01 flow in `docs/runbooks/acme-token.md`).
- Whether DB connections go through PgBouncer or straight to PostgreSQL, and the role/database naming.
- Whether admin and break-glass access to Authentik need a second path that does not depend on Authentik itself.
- Image version to pin (digest in `inventories/prod/group_vars/all/versions.yml`, rationale in `docs/versions.md`).
- Secret handling for its bootstrap admin and secret key (SOPS file under `secrets/`).
- Which of Proxmox VE, xstockstrat and future services use forward-auth versus native OIDC (each is its own item).
