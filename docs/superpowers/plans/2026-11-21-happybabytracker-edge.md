# happybabytracker on the edge Implementation Plan (plan 2 of 7)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the happybabytracker app (API + UI images, already published) at `happybaby.private.<domain>` for the tailnet, with its secrets, twice-daily encrypted backup, Gatus probes, digest pins and a bump workflow, deployed through the existing pipeline.

**Architecture:** Two compose services on their own network, modelled on commsgun: the API is never published, the UI binds `127.0.0.1:9212` and Caddy's `*.private` site proxies to it. The app's SQLite file is snapshotted by the existing `edge-backup` script into the same `age` archive as headscale's, as the app's own uid. Nothing in `policy/`, no new tool, no change to the public surface.

**Tech Stack:** Ansible (Jinja2), docker compose, Caddy, Gatus, systemd timer, SOPS, GitHub Actions, Python/bash test scripts.

**Spec:** `davcs86/happybabytracker` at `main` (merge commit `f66ecae`): `docs/superpowers/specs/2026-11-20-happybabytracker-design.md` sections 13, 15, 16 and `docs/superpowers/HANDOFF.md`; its `README.md` states what is actually built. This plan argues from them and from `CLAUDE.md` here. Read the spec's section 15 first.

## Global Constraints

- Root `CLAUDE.md` binds every task: list assumptions, minimum change, touch only what the task needs, comments state constraints only and are ≤ 2 lines. Rule 1: never assume a value the owner has not stated (see **Open values**). Rule 2: public surface stays 80/tcp, 443/tcp, 3478/udp. Rule 3: no secret in the repo, a log or a commit message; any task handling one sets `no_log: true`. Rule 4: pins by digest, with their `docs/versions.md` row in the same change. Rule 5: Ansible only. Rule 6: success is shown by output. Rule 8: the name is `happybaby.private.<domain>`, never under `*.ts` or `*.public`.
- Edge seams: EDGE-01 (never reformat a pin line), EDGE-02 (no `header_up`, no `trusted_proxies` in the `*.private` site), EDGE-03/04 (restart only via `docker compose -f …`; env-file changes get no `notify:`), EDGE-07 (a `*.private` name is wired in four places), EDGE-08 (secret keys appear as literals in roles), EDGE-10 (the Gatus heartbeat `nightly-backup`, group `edge`, keeps its name), EDGE-12 (unique rate-limit zone names).
- Contract with the app (do not change one side alone): API container port `9210` never published; UI container port `9212` published only as `127.0.0.1:<host>:9212`; health paths `/healthz` and `/healthz/api` (served by the UI; `/healthz/reminders` does not exist until plan 6 and must not be probed); data path `/data` on a named volume; uid `10001`; read-only root; image names `ghcr.io/davcs86/happybabytracker-api` and `-ui`. API env: `ADMIN_USERNAME` (`^[a-z0-9._-]{3,32}$`), `ADMIN_PASSWORD` (≥ 16), `INTERNAL_API_TOKEN` (≥ 32), `PUBLIC_ORIGIN` (`https://…`); UI env: `INTERNAL_API_TOKEN`, `API_BASE_URL=http://happybabytracker-api:9210`, `ORIGIN`. The UI image already sets `PORT` and `PROTOCOL_HEADER`/`HOST_HEADER`/`ADDRESS_HEADER`/`XFF_DEPTH`; the API image sets `DATA_DIR=/data` and `GOMEMLIMIT=40MiB`. Do not render any of those.
- Owner decisions for this plan (2026-11-21): who may reach the app is accepted as for the commsgun dashboard (every identity holding `tag:edge:443`; no policy or Caddy change; record it in `docs/decisions.md`); the SQLite snapshot runs as uid 10001 with the host's `sqlite3` via `setpriv`; the secret declarations land in the same PR and `fill_missing` is run on the PR branch; the VAPID key is generated and stored in SOPS only, not rendered (plan 6 adds the env line and its assert).
- Invite and reset tokens travel in the URL path (`/invite/*`, `/reset/*`). Caddy's own private access log may keep them (owner-accepted). Tokens must never reach OTEL: no log shipper exists in this repo today (grep of `roles/`, `inventories/`, `playbooks/` on 2026-11-21 found none); any future one must scrub those paths. State this in the runbook and `decisions.md`.
- `ansible`, `ansible-lint`, `yamllint` and `sops` may be absent in the authoring container (`pip install -r requirements.txt` gives the first three); `bcrypt` is needed by `scripts/init-secrets-test.py`. `scripts/caddy-routing-test.sh` and the real `init-secrets` run only in CI. Push and read the CI jobs for what cannot run locally. Docker may be present for `docker compose config`.
- Commits end with the session's attribution trailers. One PR for this plan: the plan document travels in it (the PR opened for the plan carries the plan and its implementation).

## Open values (confirmed by the owner on 2026-11-21: every row as proposed, except row 7, which is deferred)

| # | Value | Proposal | Used in |
|---|---|---|---|
| 1 | Base domain and name | read from `inventories/prod/group_vars/all/main.yml`: `domain: natanddavehouse.com`, so `happybaby.private.natanddavehouse.com` (design: owner) | `ORIGIN`, `PUBLIC_ORIGIN`, Caddy, Gatus |
| 2 | `happybaby` is free in `*.private` | none of this repo uses it; confirm nothing at home does | Task 3 |
| 3 | Admin username | the owner sets it with `sops set`; nothing in the repo carries it | Task 1, 4 |
| 4 | Backup times and jitter | `OnCalendar=*-*-* 00,12:00:00 UTC` (design: owner), keep `RandomizedDelaySec=10m` | Task 5 |
| 5 | Heartbeat interval for `edge_nightly-backup` | `14h` (12 h cycle + 2 h slack), name unchanged | Task 5 |
| 6 | Archive retention | local `backup_local_retention_days: 7` unchanged (14 archives); remote retention on the backup host is the owner's policy and is not set by this repo | Task 5 |
| 7 | `backup_host_known_hosts` (`TODO(provide)`) | **deferred**: no value was provided, so the `TODO(provide)` marker stays and the rsync keeps `StrictHostKeyChecking=accept-new`; the PR and the runbook say so. The rsync now carries health data: the owner supplies the `ssh-keyscan` output later | Task 5 |
| 8 | Data volume ownership | rely on the image (`COPY --chown=10001:10001` + `VOLUME /data`), as commsgun does; no pre-chown task | Task 4 |
| 9 | Compose limits | API `96m`, UI `192m`, API `stop_grace_period: 30s`, no `request_body` cap on the Caddy handle | Task 3, 4 |
| 10 | Gatus probes | `happybabytracker-ui-private` (`/healthz`) and `happybabytracker-api-private` (`/healthz/api`), group `edge`, `1m`, ntfy alerts, `[CERTIFICATE_EXPIRATION] > 336h` on the UI probe only | Task 3 |
| 11 | Memory headroom | declared limits grow 736m → 1024m on a 2 GB droplet; measure `free -m` and `docker stats --no-stream` on the droplet first and proceed if at least 400 MiB is available after adding. **Measured 2026-11-21** (DigitalOcean monitoring, last 7 days, ~25-minute samples): 1962 MiB total, 1237 MiB available at the worst sample, so about 949 MiB would remain with both new limits fully used; threshold met | Task 0 |
| 12 | Bump cron | `"59 15 * * *"` (after webhooks-mcp 15:23 and commsgun 15:41) | Task 2 |
| 13 | Names | `happybabytracker_ui_private_hostname`, `happybabytracker_ui_port: 9212`, `happybabytracker_api_image`, `happybabytracker_ui_image`; services `happybabytracker-api` / `-ui`; network `happybabytracker`; volume `happybabytracker-data`; env dir `{{ edge_root }}/happybabytracker/` | all |
| 14 | Timer re-arm | an explicit `systemctl restart edge-backup.timer` when the unit changes (not determined whether `daemon_reload` alone re-arms a running timer) | Task 5 |
| 15 | Initial pin | from the app's first publication (CI run `44886063955`, merge commit `f66ecae`): API `sha256:be93ee9d1447994601d1dfb32d9da2b196b17b5a735caac515886556aef73040`, UI `sha256:4fa06e06d8c950128e51c4f63a1ab5931c171ce1c8eeec9f05d227ca8b24dc74`, commit `f66ecae`, date `2026-11-21`. Anonymous pull verified (HTTP 200) on 2026-11-21, so the packages are public | Task 2 |

## Review Focus

1. **A snapshot taken while the API is writing (WAL) must leave no root-owned files and must be a valid database.** A root `sqlite3` that creates `-wal`/`-shm` owned by root can stop the API (uid 10001, `cap_drop: ALL`) from opening its database. Docker's data root is `0710 root:root` by default (this repo does not change it), so uid 10001 alone cannot even traverse `/var/lib/docker`: the snapshot runs with the root group as a supplementary group. Pinned by Task 5's functional test with a live writer inside a temp tree that reproduces those modes.
2. **A bad or colliding admin username or a short secret crash-loops the API and fails the deploy at `--wait`.** The pre-write asserts must mirror the app's start-up checks exactly. Pinned by Task 4's `first_failure` tests with fake secrets. (The app also refuses to start if `ADMIN_USERNAME` equals an existing parent's username: a runbook line, Task 6.)
3. **Changing `OnCalendar` of a running timer.** Pinned by Task 5's restart-on-change test and the post-deploy `systemctl list-timers` check.
4. **Restoring the database over stale `-wal`/`-shm` files or with the wrong owner.** Pinned by the restore drill (integrity check of the restored copy) and the recovery runbook's removal and `chown` steps, Task 5 and 6.
5. **A bump PR with a mismatched or unknown pair.** Both images are pinned in one PR; a pair whose revision labels differ or are missing is refused. Pinned by Task 2's guard tests.

## File Structure

| File | Change |
|---|---|
| `scripts/init_secrets.py`, `scripts/init-secrets-test.py`, `secrets/README.md` | four keys; VAPID generator |
| `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md` | two pins and rows |
| `.github/workflows/bump-happybabytracker.yml` (new), `scripts/bump-image-pin-test.py`, `docs/runbooks/github-push-token.md` | bump workflow, tests, workflow list |
| `inventories/prod/group_vars/all/main.yml` | hostname and port vars, backup schedule, heartbeat interval |
| `roles/tailscale/tasks/main.yml`, `roles/stack/templates/Caddyfile.j2`, `roles/stack/templates/gatus-config.yaml.j2` | name wired in the other three places |
| `scripts/portal-zones-test.py`, `scripts/gatus-config-test.py`, `scripts/caddy-routing-test.sh` | new cases |
| `roles/stack/templates/compose.yml.j2`, `roles/stack/tasks/main.yml` | services, network, volume, secrets block, env files |
| `scripts/happybabytracker-config-test.py` (new), `scripts/commsgun-config-test.py`, `.github/workflows/ci.yml` | config test; relaxed network test; CI wiring |
| `roles/backup/templates/edge-backup.sh.j2`, `edge-backup-verify.sh.j2`, `roles/backup/tasks/main.yml` | second database, drill, timer |
| `scripts/edge-backup-test.py` (new) | backup structure and live-writer test |
| `docs/runbooks/{happybabytracker (new),recovery,alerts}.md`, `docs/decisions.md`, `CLAUDE.md`, `README.md` | docs |

---

### Task 0: Owner prerequisites (no repo change)

The owner does these; the implementer asks for the output and pastes it in the PR description.

- [x] **Step 1: Confirm every row of Open values** — done by the owner on 2026-11-21 (row 7 deferred).
- [x] **Step 2: Measure memory on the droplet** (row 11) — done via the DigitalOcean monitoring API (see row 11); a `free -m` during a backup run is optional.
- [x] **Step 3: `backup_host_known_hosts`** (row 7) — deferred; say "deferred" in the PR description.
- [x] **Step 4: Confirm the packages stay public** — verified on 2026-11-21 (anonymous token, HTTP 200 on both manifests); nothing to do unless that changes.

---

### Task 1: Secrets declarations

**Files:** Modify `scripts/init_secrets.py`, `scripts/init-secrets-test.py`, `secrets/README.md`; check `scripts/secrets-keys-test.py`.

**Interfaces — Produces:**
- `GENERATED` gains `happybabytracker_admin_password` and `happybabytracker_internal_api_token` (both `secrets.token_urlsafe(36)`) and `happybabytracker_vapid_private_key`.
- `generate_vapid_private_key() -> str`: stdlib only; 32 random bytes accepted only when `1 <= d < n` with `n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551`, else retried; returned as unpadded base64url (43 characters of `[A-Za-z0-9_-]`).
- `HAND_KEYS` gains `happybabytracker_admin_username` (owner-provided with `sops set`; never generated).

- [ ] **Step 1: Write the failing tests** in `scripts/init-secrets-test.py`, in the style of the commsgun ones: `test_happybabytracker_owner_key_is_never_generated_or_required_by_fill_missing_but_is_known`; `test_happybabytracker_generated_keys_are_filled_when_missing_and_kept_when_present` (an existing VAPID key is never replaced: "never rotated casually"); `test_the_vapid_key_is_43_url_safe_characters_of_a_valid_p256_scalar` (regex, decodes to 32 bytes, `1 <= d < n`); `test_the_vapid_generator_rejects_zero_and_n` (monkeypatch `secrets.token_bytes` to yield 32 zero bytes, then the bytes of `n`, then a valid value; the result is the valid one); `test_no_happybabytracker_secret_contains_a_quote_backslash_or_line_break`.
- [ ] **Step 2: Run** `python3 scripts/init-secrets-test.py` (install `bcrypt` into a venv first; sops-dependent cases run in CI only). Expected: the new tests FAIL.
- [ ] **Step 3: Implement** the keys and `generate_vapid_private_key()` above.
- [ ] **Step 4: Run** `python3 scripts/secrets-keys-test.py` and the init-secrets test. If `secrets-keys-test.py` requires every generated key to be read by a role, allow-list `happybabytracker_vapid_private_key` with a one-line comment naming plan 6; no role reads it in this plan. Expected: PASS (the "key exists in the committed file" test stays red until Task 7's owner steps; say so in the PR).
- [ ] **Step 5: `secrets/README.md`**: add the four keys to the `edge.sops.yaml` row; one paragraph: which are generated, which is the owner's, and that a changed VAPID key invalidates every push subscription.
- [ ] **Step 6: Commit** `feat: declare the happybabytracker secrets`.

---

### Task 2: Pins and the bump workflow

**Files:** Modify `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md`, `docs/runbooks/github-push-token.md`, `scripts/bump-image-pin-test.py`; create `.github/workflows/bump-happybabytracker.yml`.

**Interfaces — Produces:** vars `happybabytracker_api_image`, `happybabytracker_ui_image`; workflow branch `bump/happybabytracker`, concurrency group `bump-happybabytracker`, steps `api` then `ui`.

- [ ] **Step 1: Write the failing tests** in `scripts/bump-image-pin-test.py`: parametrise the commsgun workflow classes (or add a mirrored class) over `happybabytracker`: triggers are `workflow_dispatch` with `api_digest`/`ui_digest` plus the cron `59 15 * * *`; workflow permissions `contents: read`, job permissions as commsgun's; the only secret is `GITHUB_TOKEN`; both pins change in one PR; the guard refuses a pair whose revision labels differ or are unknown; the PR body links `https://github.com/davcs86/happybabytracker/commit/…` and no `commsgun` URL appears (a copy-paste guard). Add: each new `versions.yml` line matches `^<var>: ".*@(sha256:[0-9a-f]{64})"$` exactly once, and exactly one `docs/versions.md` row mentions each var.
- [ ] **Step 2: Run** `python3 scripts/bump-image-pin-test.py`. Expected: FAIL (no workflow, no pins).
- [ ] **Step 3: Pins** (EDGE-01: copy the shape, double quotes, no trailing comment) beside the commsgun lines with the values of Open values row 15; two `docs/versions.md` rows in the exact pin-cell form (`` `ghcr.io/davcs86/happybabytracker-api@sha256:xxxxxxxx…`, digest only (commit `f66ecae`, 2026-11-21) ``); update the header sentences that name the exempt bots and the Dependabot paragraph. Before committing, confirm both images' `org.opencontainers.image.revision` label is `f66ecae84ceffe9e9655ed8626040b27b938d6a7` (the label read `bump_image_pin.py` uses, or `docker buildx imagetools inspect`).
- [ ] **Step 3: Create** `bump-happybabytracker.yml` by copying `bump-commsgun.yml` and changing only: vars, images, title prefix, branch, concurrency group, commit-URL repo, step-summary wording, cron.
- [ ] **Step 4: `docs/runbooks/github-push-token.md`**: add the workflow and branch to its list.
- [ ] **Step 5: Run** the bump test and `yamllint -s .github inventories` → PASS.
- [ ] **Step 6: Commit** `feat: pin the happybabytracker images and add their bump workflow`.

---

### Task 3: The name, in the other three places (EDGE-07)

**Files:** Modify `inventories/prod/group_vars/all/main.yml`, `roles/tailscale/tasks/main.yml` (`_names`), `roles/stack/templates/Caddyfile.j2`, `roles/stack/templates/gatus-config.yaml.j2`, `scripts/portal-zones-test.py`, `scripts/gatus-config-test.py`, `scripts/caddy-routing-test.sh`.

**Interfaces — Produces:** `happybabytracker_ui_private_hostname: "happybaby.{{ private_zone }}"`, `happybabytracker_ui_port: 9212` (a host port, EDGE-05), Caddy matcher `@happybabytracker_ui`, probes `happybabytracker-ui-private` and `happybabytracker-api-private`.

- [ ] **Step 1: Failing tests first.** Extend `portal-zones-test.py` (`test_zone_and_tenant_names` with the new name; `test_private_name_gets_a_record_and_a_certificate_probe` with the var in `_names`). Extend `gatus-config-test.py`: `test_the_happybabytracker_name_is_probed_over_the_tailnet` (names, group `edge`, interval `1m`, the `*tailnet` anchor, urls `https://<host>/healthz` and `/healthz/api`, conditions `[STATUS] == 200`, `[CERTIFICATE_EXPIRATION] > 336h` on the UI probe only, `alerts: [{type: ntfy}]`) and `test_no_probe_targets_healthz_reminders`. Extend `caddy-routing-test.sh` (CI `caddy` job only): the host in the generated hosts file, a `happybabytracker-ui:9212` mock upstream, and expectations mirroring the commsgun UI's: aborts publicly (`000`), 200 over the tailnet interface, the staging VM gets `000`, and the `/echo-headers` check shows `X-Forwarded-Proto: https`, `X-Forwarded-Host: <the name>` and a single-entry `X-Forwarded-For` even when the client spoofs them.
- [ ] **Step 2: Red order:** add the vars (place 1), run `python3 scripts/portal-zones-test.py` and see `test_caddy_serves_nothing_under_the_machine_zone` FAIL; then add `_names` (place 2) → PASS.
- [ ] **Step 3: Caddy handle** inside the `*.{{ private_zone }}` site after `import deny_staging_vm`, mirroring the commsgun UI handle: `reverse_proxy 127.0.0.1:{{ happybabytracker_ui_port }}`; no `header_up`, no `trusted_proxies` (EDGE-02); no `request_body` cap (row 9). **Gatus probes** as in the interface, beside `commsgun-ui-private`.
- [ ] **Step 4: Run** `python3 scripts/gatus-config-test.py`, `python3 scripts/portal-zones-test.py`, `yamllint -s .` → PASS. Run `caddy-routing-test.sh` only where Docker, Ansible and sudo exist; otherwise read the CI `caddy` job.
- [ ] **Step 5: Commit** `feat: wire the happybaby private name (EDGE-07)`.

---

### Task 4: Compose services, secrets block, env files

**Files:** Modify `roles/stack/templates/compose.yml.j2`, `roles/stack/tasks/main.yml`, `scripts/commsgun-config-test.py`, `.github/workflows/ci.yml`; create `scripts/happybabytracker-config-test.py`.

**Interfaces — Consumes:** Task 1 keys, Task 2 image vars, Task 3 hostname and port. **Produces:** services `happybabytracker-api` and `happybabytracker-ui`, network `happybabytracker`, volume `happybabytracker-data`, `{{ edge_root }}/happybabytracker/api.env` and `ui.env` (mode `0600`).

- [ ] **Step 1: Write `scripts/happybabytracker-config-test.py`** mirroring `commsgun-config-test.py` (render the stack tasks, `parse_env`, `first_failure`, compose read-back). Tests: `test_api_is_never_published_and_ui_binds_loopback_only` (API has no `ports`; UI exactly `127.0.0.1:{{ port }}:9212`); `test_services_are_hardened_read_only_and_on_their_own_network` (merged `*hardening`, `read_only`, only network `happybabytracker`, `mem_limit` 96m and 192m, API `stop_grace_period: 30s`, UI `depends_on: [happybabytracker-api]` with no condition, UI healthcheck on `127.0.0.1:9212/healthz`, the data volume mounted only on the API at `/data`); `test_assertions_run_before_any_file_is_written`; `test_each_secret_is_present_a_string_and_free_of_quote_backslash_cr_lf`; `test_minimum_lengths_and_username_shape_mirror_the_apps_startup_checks` (password ≥ 16, token ≥ 32, username `^[a-z0-9._-]{3,32}$`; with fake secrets, an uppercase, a 2-character and a quote-bearing username each fail before any file is written; the failure message names keys only); `test_env_files_hold_exactly_the_contracted_variables` (`api.env`: `ADMIN_USERNAME`, `ADMIN_PASSWORD`, `INTERNAL_API_TOKEN`, `PUBLIC_ORIGIN=https://<host>`; `ui.env`: `INTERNAL_API_TOKEN`, `API_BASE_URL=http://happybabytracker-api:9210`, `ORIGIN=https://<host>`; mode `0600`, `no_log: true`, no `notify:`; no `VAPID_*`, no `DATA_DIR`, `PORT` or proxy-header variable); `test_vapid_is_not_rendered_in_plan_2`; `test_docker_compose_config_reads_back` (when Docker exists).
- [ ] **Step 2: Relax** `commsgun-config-test.py::test_own_network_shared_with_nobody_else` (it asserts the compose networks are exactly `{"commsgun": {}}`) to "the `commsgun` network is used only by `commsgun-*` services"; add the new test to `ci.yml` `lint` after the commsgun one.
- [ ] **Step 3: Run** the new test and see it FAIL; implement: the two services (mirroring commsgun's, UI `ports`, `env_file`, the Task 3 port var), the network and volume entries, the `Stack directories` item `{path: "{{ edge_root }}/happybabytracker", mode: "0750"}`, and a block "Happybabytracker secrets and configuration" (`no_log: true`) placed after commsgun's and before "Compose file": asserts first, then the two env copies with single-quoted values. Secret keys as literals (EDGE-08). Run the new and the commsgun config tests, `secrets-keys-test.py`, `yamllint -s .` → PASS.
- [ ] **Step 4: Commit** `feat: run happybabytracker on the edge (compose, secrets, env files)`.

---

### Task 5: Twice-daily backup of the database

**Files:** Modify `roles/backup/templates/edge-backup.sh.j2`, `roles/backup/templates/edge-backup-verify.sh.j2`, `roles/backup/tasks/main.yml`, `inventories/prod/group_vars/all/main.yml` (`backup_schedule`, `heartbeat_backup_interval`), `.github/workflows/ci.yml`; create `scripts/edge-backup-test.py`.

**Interfaces — Produces:** archive layout gains `happybabytracker/happybabytracker.db`; `backup_schedule: "*-*-* 00,12:00:00 UTC"`; `heartbeat_backup_interval: 14h`; the Gatus external endpoint `nightly-backup` (group `edge`) keeps its name (EDGE-10) — the runbook says it now runs twice a day.

- [ ] **Step 1: Write `scripts/edge-backup-test.py`** (new). Structure tests on both rendered templates (`jinja2.StrictUndefined`, inventory vars): `test_the_database_is_snapshotted_as_the_apps_uid` (the `.backup` of the live file runs under `setpriv --reuid=10001 --regid=10001 --groups=0 sqlite3` (`--groups=0` is what lets uid 10001 traverse `/var/lib/docker`, which is `0710 root:root`) into a `0700` directory owned by 10001; no bare-root `sqlite3` opens the live path; no `docker exec`); `test_the_copy_is_integrity_checked_and_manifested` (`PRAGMA integrity_check` on the copy; `MANIFEST.sha256` covers both `headscale` and `happybabytracker`); `test_the_drill_checks_the_restored_copy_and_prints_no_rows` (integrity check only; no `SELECT` or `.dump`, so no counts of kids are shown); `test_rendered_scripts_pass_shellcheck`; `test_the_timer_follows_the_schedule` (`OnCalendar={{ backup_schedule }}`, `Persistent=true`, `RandomizedDelaySec=10m`; `systemd-analyze calendar` shows 00:00 and 12:00 UTC when available); `test_changing_the_unit_restarts_the_timer` (a register-and-restart task conditioned on the unit change); `test_the_backup_heartbeat_keeps_its_name_and_covers_the_schedule` (the Gatus external endpoint `nightly-backup`, group `edge`, interval ≥ 13h and ≤ 24h). Functional test `test_a_snapshot_during_writes_is_valid_and_leaves_nothing_owned_by_root`: build a WAL-mode database owned by 10001 inside a temp tree that mimics Docker's modes (`<root>/docker` `0710 root:root`, `<root>/docker/volumes` `0701`, the volume `_data` dir owned by 10001), with a background writer process; first assert that plain `setpriv --reuid=10001 --regid=10001 --clear-groups` FAILS to read it (the trap is real in the test), then, run the rendered snapshot step (script with `vol_root` pointed at the temp tree and `age`/`rsync` stubbed on `PATH`), then assert the copy passes `integrity_check`, contains rows the writer committed, and **no file in the source directory is owned by root**; needs root and `setpriv`, otherwise it prints "SKIPPED (not root)"; wire it into a CI job that has root (`sudo -n`, as the `caddy` job does).
- [ ] **Step 2: Run** `python3 scripts/edge-backup-test.py`; expected: FAIL.
- [ ] **Step 3: Implement** the snapshot after the headscale copies (source `/var/lib/docker/volumes/edge_happybabytracker-data/_data/happybabytracker.db`; root then moves the checked copy into `$work/happybabytracker/`), extend the MANIFEST `find` and the header comment, add the drill check, change the two vars, add the timer restart task; update the unit `Description=` wording. `scripts/gatus-config-test.py::test_heartbeats_are_unchanged` must stay green.
- [ ] **Step 4: Run** the new test, `gatus-config-test.py`, `yamllint -s .`, `ansible-playbook --syntax-check` for every playbook (CI list) → PASS.
- [ ] **Step 5: Commit** `feat: back up the happybabytracker database twice a day`.

---

### Task 6: Documentation

**Files:** Create `docs/runbooks/happybabytracker.md`; modify `docs/runbooks/recovery.md`, `docs/runbooks/alerts.md`, `docs/decisions.md`, `CLAUDE.md` (Context Guide row), `README.md` (diagram, zone list, workflows, bump note, runbook table); `docs/runbooks/onboard-service.md` only if it names the apps.

- [ ] **Step 1: Runbook**, modelled on `commsgun.md`: intro table (name, containers, host port, use); first deploy (owner steps of Task 7, checks from a tailnet device, `000` from outside, Gatus); sign in as admin and add a parent (invite link; the link is shown once); reset a password; rotate the internal token or admin password (admin sessions end at the next start); an admin username must not equal a parent's username (the API refuses to start); the data restore; what a VAPID change costs; image updates through `bump-happybabytracker` then a deploy; logs hold ids only and the Caddy private log keeps `/invite/*` and `/reset/*` paths (720 h); no log shipper exists and tokens must never reach OTEL; the heartbeat named `nightly-backup` runs twice a day.
- [ ] **Step 2: `recovery.md`**: new "Restore happybabytracker": decrypt with the offline key; stop `happybabytracker-ui` and `-api` with `docker compose -f … stop`; copy `happybabytracker.db` into the volume's `_data`; **remove stale `-wal`/`-shm`; `chown 10001:10001`**; start both; check `/healthz/api`.
- [ ] **Step 3: `decisions.md`** — one Known-traps entry: reach accepted as commsgun's; the database IS in the nightly archive (unlike commsgun's by decision), snapshotted as the app's uid and why; the VAPID key is generated now and rendered only by plan 6; tokens in the Caddy path log. Context Guide row in `CLAUDE.md`; `alerts.md` probe list; `README.md`.
- [ ] **Step 4: Re-read every touched doc against the code** (Teardown rule) and fix drift. Run `yamllint -s .`.
- [ ] **Step 5: Commit** `docs: happybabytracker runbook, recovery and decisions`.

---

### Task 7: PR, owner steps and verification

Owner or CI only; the implementer opens the PR and reports each output.

- [ ] **Step 1: Push and open the PR.** CI will be red on `secrets-keys-test.py` (keys not yet in `secrets/edge.sops.yaml`) until Steps 2-3.
- [ ] **Step 2 (owner): allow this branch in the `production` environment, run Actions → `init-secrets` with `fill_missing` on it** (the three generated keys land in `secrets/edge.sops.yaml`; the branch restriction is described in `docs/runbooks/commsgun.md` §2.3); revert the environment setting afterwards.
- [ ] **Step 3 (owner): set the admin username** with `sops set secrets/edge.sops.yaml '["happybabytracker_admin_username"]' '"<name>"'`, commit and push to the branch.
- [ ] **Step 4: CI Gate green on the PR head** (all jobs, including `caddy`, `secrets` and `lint`).
- [ ] **Step 5 (owner): deploy** (`deploy` workflow, `tailnet` mode, `production` approval). The API starts under `--wait`; a bad username or secret fails there.
- [ ] **Step 6: Verify and report** — on the droplet `stat -c '%a %U:%G %n' /var/lib/docker /var/lib/docker/volumes` (expected `710 root:root` and `701 root:root`; if different, tell the implementer: the `--groups=0` choice is still safe); from a tailnet device `https://happybaby.private.<domain>` shows the login page (200); from the internet the name does not answer; Gatus `happybabytracker-ui-private` and `-api-private` green; `systemctl list-timers edge-backup.timer` shows the next run at 00:00 or 12:00 UTC (plus jitter); `playbooks/backup-now.yml` (via the `verify` workflow's tailnet job) journal shows both databases archived, "restore drill OK" and the heartbeat; sign in as the admin and create a test parent from the runbook.

## Done when

CI Gate is green on the PR head; the deploy run log is attached; the Gatus probes are green; the `backup-now` journal shows both databases and the drill passing; the login page loads from the tailnet and the name does not answer from the internet. No repository state other than this one is changed; the app's images are unchanged.

## Not in this plan

ntfy (plan 6), reminders and Web Push, `/healthz/reminders` probe (plan 6), rendering `VAPID_PRIVATE_KEY` (plan 6), any `policy/` change, a Caddy source-address restriction.
