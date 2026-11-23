# Versions

All pins were at least two weeks old on 2026-10-14 (except the Caddy, Gatus and ntfy pins raised on 2026-11-22 for known CVEs, and the webhooks-mcp, commsgun and happybabytracker images, the owner's own, pinned as soon as they are published; the `bump-webhooks-mcp` workflow proposes the webhooks-mcp pin, `bump-commsgun` the two commsgun pins and `bump-happybabytracker` the two happybabytracker pins); container images are pinned by digest, binaries by checksum.

| Component | Pin | Why this one |
|---|---|---|
| Ubuntu | 26.04 LTS (`ubuntu-26-04-x64`) | your choice; Docker, Tailscale and PGDG all publish `resolute` packages |
| Droplet | `s-1vcpu-2gb-amd`, sfo3 | 2 GB RAM headroom, 50 GB disk (30 GB budget incl. ~15 GB pgBackRest); sfo3 is closest to Hayward |
| headscale | v0.29.4 (`headscale_image`, tag + digest) | v0.29.5 was one day old; 0.29.5's fixes only touch `via` grants, which this policy does not use. The image is the only pin: a separate `headscale_version` was read by nothing and could drift from the tag, so it was removed (2026-11-15) |
| tailscale (edge + runners) | 1.98.10 | newest client inside headscale 0.29.4's generated capability table (≤1.98); 0.29.4 rejects < 1.80 |
| Caddy | 2.11.9 (runtime and builder, tag + digest) | raised on 2026-11-22 over the two-week rule: 2.11.6's runtime image carried a critical `brotli-libs` CVE-2026-111497 and Go stdlib / `x/net` HIGH findings (Trivy scan of the pinned digests); 2.11.9 clears the critical and leaves 2 HIGH (`go-jose` v3 has no upstream fix, `x/net` 0.60) |
| Go toolchain for the Caddy build (`caddy_go_toolchain`) | go1.26.11 | the 2.11.9 builder image still bundles Go 1.26.5 (6 HIGH stdlib findings fixed in 1.26.10); `GOTOOLCHAIN` makes `xcaddy` compile with 1.26.11, fetched through the Go module proxy and verified against the checksum database |
| caddy-dns/desec | v1.1.0 | latest release |
| mholt/caddy-ratelimit | commit `5625512f` (2026-06-12) | only tag (v0.1.0) is from 2024; HEAD has been quiet for 4 months |
| Gatus | v5.38.0 | raised on 2026-11-22 over the two-week rule: v5.37.0 had 12 HIGH findings in its Go dependencies (`x/net`, `grpc`, `go-jose`, stdlib); v5.38.0 leaves 5 HIGH (Trivy). Checks, heartbeats and ntfy alerts are declared in the repo |
| ntfy | v2.38.1 | raised on 2026-11-22 over the two-week rule: v2.28.0 had 12 HIGH findings (openssl, musl, zlib, Go stdlib, `x/net`); v2.38.1 has none (Trivy) and is a security release for message templating. No config change needed: the declarative users, ACLs and tokens and the `/v1/health` check are unchanged since 2.28. The database schema moves 16 → 17, so a downgrade needs a manual `schema_version` reset |
| webhooks-mcp (`webhooks_mcp_image`) | `ghcr.io/davcs86/webhooks-mcp@sha256:449c955b…`, digest only (commit `c9be704`, 2026-11-20) | the image is tagged by commit SHA and has no semver tag, so the digest is the pin. Built, smoke-tested and pushed by that repo's CI on merge to `main`; the package is public, so the edge pulls without a credential. The `bump-webhooks-mcp` workflow opens the bump PR daily or on manual run (optionally with a chosen digest); editing `versions.yml` by hand still works, but keep the pin cell exactly `…@sha256:xxxxxxxx…`, digest only (commit `xxxxxxx`, YYYY-MM-DD) or the workflow fails. It keeps one branch, `bump/webhooks-mcp`, and one open PR: a newer digest, or a merge to `main` that the branch lacks, rebuilds the branch from `main` and updates that PR, so commits added to it by hand are lost, and a manually chosen older digest lasts only until the next daily run proposes the newest again (merge it first, or close the PR for the newest to reject it). To reject a digest, close its PR without merging; a closed PR with that title stops the workflow proposing the digest again. |
| commsgun API (`commsgun_api_image`) | `ghcr.io/davcs86/commsgun-api@sha256:3ceb1f89…`, digest only (commit `017db3e`, 2026-11-22) | the owner's own image, tagged by commit SHA with no semver tag, so the digest is the pin; built, smoke-tested and pushed together with the dashboard by that repo's CI on merge to `main`, public on GHCR. The `bump-commsgun` workflow opens one bump PR for both images daily or on manual run (optionally with chosen digests) and proposes nothing when their revision labels differ or are missing; editing `versions.yml` by hand still works. Pin both commsgun images from the same commit. Keep the pin cell in exactly this form (`…@sha256:xxxxxxxx…`, digest only (commit `xxxxxxx`, YYYY-MM-DD)); `scripts/bump_image_pin.py` rewrites it |
| commsgun dashboard (`commsgun_ui_image`) | `ghcr.io/davcs86/commsgun-ui@sha256:485e1877…`, digest only (commit `017db3e`, 2026-11-22) | built from the same commit as the API image; same reasons and the same pin-cell format; bumped together with the API image by the `bump-commsgun` workflow |
| happybabytracker API (`happybabytracker_api_image`) | `ghcr.io/davcs86/happybabytracker-api@sha256:84620a24…`, digest only (commit `d3820ab`, 2026-11-23) | the owner's own image, tagged by commit SHA with no semver tag, so the digest is the pin; built and pushed together with the UI by that repo's CI on merge to `main` (first publication: run 44886063955), public on GHCR (anonymous pull verified). The `bump-happybabytracker` workflow opens one bump PR for both images daily or on manual run (optionally with chosen digests) and proposes nothing when their revision labels differ or are missing; editing `versions.yml` by hand still works. Pin both happybabytracker images from the same commit. Keep the pin cell in exactly this form (`…@sha256:xxxxxxxx…`, digest only (commit `xxxxxxx`, YYYY-MM-DD)); `scripts/bump_image_pin.py` rewrites it |
| happybabytracker UI (`happybabytracker_ui_image`) | `ghcr.io/davcs86/happybabytracker-ui@sha256:5840d405…`, digest only (commit `d3820ab`, 2026-11-23) | built from the same commit as the API image; same reasons and the same pin-cell format; bumped together with the API image by the `bump-happybabytracker` workflow |
| Docker CE / CLI | 29.8.2 | 29.9.x under two weeks |
| containerd.io / compose / buildx | 2.3.6 / 5.5.1 / 0.37.1 | newest ≥ 2 weeks for 26.04 |
| Docker on the app VMs (Debian 13) | the same five releases, `~debian.13~trixie` builds (`docker_packages_debian13`) | one Docker version across edge and app hosts; all five are in Docker's trixie repository |
| pgBackRest | 2.59.2 (PGDG) | 2.59.3 under two weeks; **must equal the DB host's version** |
| PostgreSQL (DB host) | 18.6-1.pgdg13+2 (PGDG, Debian 13) | the newest 18 in PGDG's trixie index on 2026-10-25 and older than two weeks (published 2026-08-13); DigitalOcean runs 18, xstockstrat's CI still runs 16 |
| `postgres` image for the app-VM DB probe (`db_probe_image`, runbook only) | 18.6-alpine, digest `10a5c69a…` | the DB host's major and minor (18.6, released 2026-08-13); Docker Hub lists only the current digest, the 2026-10-26 rebuild (base-image refresh), so the release is ≥ two weeks old but this digest is not |
| TimescaleDB (DB host) | 2.30.2, Apache build (`timescaledb-2-oss`) | 2.31.0 was five days old; 2.30.2 is 2026-09-29 and needs PostgreSQL ≥ 18.6. The Apache build matches DigitalOcean's edition; compression waits for the cutover. DigitalOcean production runs 2.30.2 (confirmed by the owner on 2026-10-25), which a restore requires |
| PgBouncer (DB host) | 1.26.0-4.pgdg13+1 | 1.26.1 was 11 days old; ≥ 1.21 is needed for `max_prepared_statements` |
| pgBackRest (DB host) | 2.59.2-1.pgdg13+1 | the same upstream release as the edge's Ubuntu build; repo-host mode needs both sides equal |
| Debian (DB host) | 13 (trixie) | what Proxmox VE 9 is built on; the pins above are the `pgdg13` and `debian13` builds |
| ansible-core | 2.22.0 | released 2026-11-02, newest ≥ 2 weeks |
| collections | general 13.4.0, docker 5.3.0, posix 2.2.2, sops 2.4.0, crypto 3.4.0 | newest ≥ 2 weeks |
| sops | 3.13.3 | SSH-key age recipients need ≥ 3.10 |
| age (on edge) | Ubuntu archive 1.2.1 | supports ssh-ed25519 recipients |
| typer (`tools/pve-setup.py`) | 0.28.0 | released 2026-11-06, newest ≥ 2 weeks; transitive pins in `tools/pve-setup.requirements.txt` |
| tailscale in `templates/app-deploy/deploy.yml` | 1.98.10 | same pin and checksum as the edge; update both together |
| 1password/load-secrets-action (`templates/app-deploy`) | v5.0.1, commit `70062d7a876d3eb6334754fa26efd2fbd90c32f2` | released 2026-08-18; v5 is newer than the v4 in 1Password's docs examples. Its Workload Identity mode is public preview and needs a GitHub secret too, so service accounts are used. |
| 1Password CLI (`templates/app-deploy`) | 2.40.0, zip sha256 `74277219e8da60958c00f9aee9d2023225e98fdda8bfd2156a5d9e85e0edaab3` | released 2026-10-02; 2.41.x under two weeks. The zip's `op.sig` verified against 1Password's code-signing key `3FEF 9748 469A DBE1 5DA7 CA80 AC2D 6274 2012 EA22` when pinned. The template installs it first, because the action otherwise downloads an unverified "latest". |
| actions/checkout, setup-python | v7.0.1, v7.0.0 by commit | newest ≥ 2 weeks |
| age and smartmontools (Proxmox host, used by `tools/pve-setup.py`) | Debian archive packages | not pinned: the toolkit calls only `age -r/-o/-d/-i`, `age-keygen` and `smartctl -H`, stable across releases, and the host gets security updates from its own apt. The `tools` CI job installs `age` from the Ubuntu archive for the same reason. |

Ubuntu-archive packages (fail2ban, chrony, sqlite3, age, …) are pinned by the release and receive security updates
through unattended-upgrades. Third-party packages (Docker, Tailscale, pgBackRest) are pinned and `apt-mark hold`, and
unattended-upgrades is told not to touch them.

## Dependabot

`.github/dependabot.yml` proposes weekly bumps, each at least 14 days after release (`cooldown`), for the GitHub Actions
pins (`.github/workflows`, `.github/actions/setup`), `requirements.txt` and `tools/pve-setup.requirements.txt`. It does
not cover what it cannot parse: `versions.yml` (digests, checksums, apt versions), `requirements.yml` (Galaxy
collections), the Caddy Dockerfile (its images arrive as build args from `versions.yml`), the Jinja compose templates and
`templates/app-deploy/` (not a workflow path). Merge a Dependabot PR only after updating the matching row above in the
same PR; the `webhooks-mcp` digest stays with `bump-webhooks-mcp`, the two commsgun digests with `bump-commsgun`, and the two
happybabytracker digests with `bump-happybabytracker`.
