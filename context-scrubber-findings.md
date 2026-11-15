# Context Scrub — Findings

Low-signal context surfaced by `/context-scrubber` (mode `apply`) on 2026-11-15 from branch
`claude/context-constitution-parity-zggeq3` at commit `2f16154`, the repo state this audit reflects. Each row is a
line in the **auto-loaded** `CLAUDE.md` (with `AGENTS.md` a symlink to it), the generated constitution or findings, or
an opted-in doc (`README.md`, `docs/decisions.md`). Every row cites both the context line and the evidence it fails.

**How the rows were produced.** One read-only `context-auditor` pass covered the five files. It also grepped every
constitution anchor, including the cross-repo ones, and all of them resolve. The orchestrator re-opened the evidence
for every row carried here:

- the `8080` literals
- the pgbackrest lookup's missing `failed_when`
- the README repository map
- the decisions.md "image pin" and "DNS-01" entries

Line counts are exact; characters are the auditor's estimates.

## Summary

| Category | Failing lines | Lines | Characters | Tokens (only if measured) |
|---|---|---|---|---|
| Stale citations | 1 (already in findings) | — | — | — |
| Restated (agent reads for free) | 8 | ≈ 30 | ≈ 3,400 | — |
| Cross-file duplication | 6 | ≈ 12 | ≈ 1,300 | — |
| Contradicted by code | 5 (4 already in findings, 1 new) | — | — | — |
| Should be just-in-time | 1 (keep-but-verify) | — | — | — |
| Brittle / over-specified | 1 (keep-but-verify) | — | — | — |
| Bloat / low-value prose | 2 | ≈ 14 | ≈ 1,500 | — |
| **Removable total** (approved at the apply gate; see Phase 2 below) | see commit | — | — | — |
| Keep-but-verify (unconfirmed) | 6 | — | — | — |

## Stale citations

| Context line | Citation it makes | Reality | Suggested action |
|---|---|---|---|
| `CLAUDE.md#Context Guide`: "(cross-repo contracts C1–C8)" | webhooks-mcp `docs/contracts.md` | it has C9 | already a findings row; fix at the step-3 refresh |

## Restated facts (agent reads for free) — fails CF-N4

All of these are in `docs/decisions.md`, the owner's design and trap log. Each is restated in a code comment the agent
sees in the file it would edit. The scrubber proposes trimming each to its history/why; most are left for the owner.

| Context line | What restates it (free to read) | Why it fails | Suggested action |
|---|---|---|---|
| `#Known traps` (webhooks-mcp): "The image pin is by digest only; see `docs/versions.md`." | `inventories/prod/group_vars/all/versions.yml`, `docs/versions.md` | a pointer with no directive | remove |
| `#Known traps`: "DNS-01 needs `propagation_delay` …" | `roles/stack/templates/Caddyfile.j2#(acme)` comment | near-verbatim | trim to the incident (owner) |
| `#Known traps`: "`Automatic-Reboot-Time` follows the host clock (UTC) …" | `roles/base/tasks/main.yml` comment | restated | trim (owner) |
| `#Known traps`: "Archiving follows the stanza …" | `roles/db_host/tasks/archive.yml` comment and task name | restated | trim (owner) |
| `#Known traps` (commsgun): single-quoted env and `\'` escape | `roles/stack/tasks/main.yml` ("Commsgun API environment" comment), `docs/runbooks/commsgun.md` | restated | trim (owner) |
| `#Known traps`: body cap must equal the listener cap | `inventories/prod/group_vars/all/main.yml#body_limits` comment | restated | keep: it is the cross-repo contract home both repos cite |
| `#Design decisions`: "Policy tests run in CI only …" | `roles/stack/tasks/main.yml` ("Render policy without its tests block") | restated | trim (owner) |
| `#Design decisions`: bump-PR mechanics ("It keeps one branch and one open PR …") | `.github/workflows/bump-webhooks-mcp.yml` comments | mechanism, not rationale | trim to the token/approval tradeoff (owner) |

## Cross-file duplication — CF-N3

| Context line | Duplicate location(s) | Which copy to keep | Suggested action |
|---|---|---|---|
| constitution EDGE-04: "…and handlers are lost on a failed play" | `docs/decisions.md#Known traps` ("A failed play loses the handlers it had queued"), `docs/runbooks/recovery.md` | decisions.md and the runbook | trim EDGE-04 to the `env_file`-vs-handler choice plus a pointer |
| constitution EDGE-05: "A bind mount is root-owned and unwritable" | `docs/decisions.md#Known traps` (webhooks-mcp and commsgun bullets) | decisions.md | trim the clause to a pointer |
| constitution EDGE-06 Why (`--accept-dns=false`) | `docs/decisions.md` (design and trap entries) | decisions.md | **kept**: one clause, and it is the reason the pattern exists |
| constitution EDGE-12 Why ("Zone names are global") | `docs/decisions.md`, `Caddyfile.j2#(public_routes)` comment | — | **kept**: already a one-clause pointer |
| `docs/decisions.md`: owner-role timeouts | `docs/runbooks/db-host.md` ("Decided, and what is still open", "Constraints worth knowing") | decisions.md for the why | owner |
| `README.md#Documentation` ≈ 14 rows repeat `CLAUDE.md#Context Guide` | `CLAUDE.md` | both (human vs agent audience) | no action |

## Contradicted by code

Defects (CF-N9), all routed to `docs/context-constitution-findings.md`. Never `apply`-deleted.

| Context line | What the code does | Evidence | Suggested action |
|---|---|---|---|
| `README.md#Repository map` playbooks and `roles/stack/templates/` rows | `playbooks/app.yml`, `playbooks/db.yml`, `playbooks/diag-boot.yml`, `gatus-config.yaml.j2` and `commsgun-config.json.j2` exist but aren't listed | `playbooks/`, `roles/stack/templates/` | **new**: add at the step-3 refresh (extends the workflows-row finding) |
| `README.md` workflows row omits `app-host.yml`/`db-host.yml` | — | `.github/workflows/` | already in findings |
| `docs/decisions.md` zone prefixes `prod_`/`staging_` | prefix is `site.name` | `roles/stack/templates/Caddyfile.j2#(public_routes)` | already in findings |
| `docs/decisions.md` "two dead-man heartbeats" | 4 external endpoints | `roles/stack/templates/gatus-config.yaml.j2` | already in findings |
| `docs/decisions.md` "probes every managed SNI (hs, xstockstrat, `*.ts`)" | all `edge_sites` | `roles/stack/tasks/main.yml#_cert_check` | already in findings |

## Should be just-in-time (pre-loaded → pointer)

_None in auto-loaded files._ See keep-but-verify for decisions.md's home-side traps.

## Brittle / over-specified (anti-altitude)

_None confirmed._ See keep-but-verify ("Connection sizing").

## Bloat / low-value prose

| Context line(s) | Why it is filler | Suggested action |
|---|---|---|
| `docs/decisions.md#Known traps`: "The deploy's certificate-failure evidence printed no Caddy lines through a filtered `grep` …" | narrative of a fixed debugging step | trim (owner) |
| `docs/decisions.md#Known traps`: "Staging is a public name on 443 …" (~11 lines) | incident narrative mixed with live rules | shrink the narrative to a clause (owner) |

## Context budget (file-level)

| File | Measured lines | Measured characters | Over soft budget? |
|---|---|---|---|
| `docs/decisions.md` | 278 | 32,605 | yes (on demand; the largest) |
| `docs/context-constitution.md` | 83 | ≈ 14,000 | yes (on demand) |
| `README.md` | 133 | 9,228 | yes (opt-in human doc) |
| `docs/context-constitution-findings.md` | 58 | ≈ 6,500 | yes (on demand) |
| `CLAUDE.md` | 80 | ≈ 6,000 | yes (≈ 2,300 of that is the protected contract) |

## Silent skills (weak trigger surface)

_None._ Scanned: 0 · excluded as symlinked-out: 0.

## Keep-but-verify (unconfirmed — CF-1)

- `CLAUDE.md#Rules` 6 overlaps contract item 4. The contract cites Rule 6 as its enforcement, and Rule 6 adds the
  edge-specific checks ("a green lint is not a working edge"). Kept. Status: **owner preference**.
- constitution EDGE-13: headscale's `8080` literal is in compose, Caddyfile and `headscale-config.yaml.j2`, not
  Gatus. Re-scope at the step-3 refresh. Status: **confirmed drift**.
- constitution candidate "verify hardcodes …": `verify.yml` also probes `other.$d` (a negative probe). Add it for
  precision at the step-3 refresh.
- `docs/decisions.md`: about 15 home-side traps (Proxmox, DB host, app VM) could move under a `## Home-side traps`
  heading or to their runbooks. Status: **owner**.
- `docs/decisions.md` "Connection sizing …" is a long arithmetic chain. It is the cited derivation home
  (`roles/db_host/defaults/main.yml`) and asserted in `roles/db_host/tasks/postgres.yml`. Status: **keep**.
- constitution gotcha "the staging-VM lookup is the only fail-soft headscale query": confirmed (the
  `pgbackrest_repo` lookup has no `failed_when`). Status: **verified**.

## Protected blocks (reported, never trimmed)

| Block | Location | Marker |
|---|---|---|
| behavioral contract | `CLAUDE.md` (by marker) | `context-forge:behavioral-contract` |
| constitution pointer | `CLAUDE.md#Context Guide` row (host index style, no sentinel) | — |

---
_Surfaced by [context-forge](https://github.com/davcs86/agent-plugins). These are low-signal lines to trim, not
rules to keep — nothing grounded is dropped (**CF-N8**). Re-run `/context-scrubber` to re-audit._
