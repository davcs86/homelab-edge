# Wildcard portal zones (`*.public`, `*.private`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve `commsgun.private.<domain>` (tailnet-only) and `xstockstrat-staging.public.<domain>` (internet) from two new wildcard zones, in parallel with every existing name, so later portals need no Cloudflare or deSEC change.

**Architecture:** Two new Caddy sites, `*.public.<domain>` and `*.private.<domain>`, each with one wildcard certificate issued over the existing DNS-01 chain (same shape as `*.ts`). Public-zone services are `edge_sites` entries marked `zone: public` and rendered as host-matched handles inside the wildcard site; the private zone is hand-written like `*.ts`. `.private` names resolve only inside the tailnet: a headscale DNS route for the zone plus generated extra records.

**Tech Stack:** Ansible (Jinja2 templates), Caddy (custom build with `dns.providers.desec` and `caddy-ratelimit`), headscale v0.29.4, Gatus, bash/Python test scripts, GitHub Actions.

**Spec:** `docs/superpowers/specs/2026-11-18-wildcard-portal-zones-design.md` — read it first; this plan argues from it.

## Global Constraints

- Read `CLAUDE.md` (root) before any edit: list assumptions, minimum change, touch only what the task needs, comments state constraints only and are ≤ 2 lines.
- Nothing existing is renamed, removed or re-issued. No existing Gatus endpoint `name` or `group` changes (EDGE-10).
- No new secret, token or IaC tool. No change to `versions.yml`.
- Public surface stays 80/tcp, 443/tcp, 3478/udp.
- `commsgun` `ORIGIN` stays `https://{{ commsgun_ui_hostname }}`; staging's `APP_URL` is not touched (cutover is a later change).
- Every rate-limit zone name is unique across sites (EDGE-12).
- Every route to the commsgun dashboard logs through a redacting logger (no query, `Referer` or `Location`).
- Exact values: zones `public.{{ domain }}` / `private.{{ domain }}`; ACME labels `public` / `private`; DNS-route upstream `1.1.1.1`.
- There is no Docker in the authoring container. `scripts/caddy-routing-test.sh` and `caddy validate` run only in the CI `caddy` job: push and read that job's result. Python tests, `yamllint -s .`, `ansible-lint` and `ansible-playbook --syntax-check` run locally after `pip install -r requirements.txt && ansible-galaxy collection install -r requirements.yml -p collections`.
- Commits end with the session's attribution trailers; no model identifier in any pushed artifact.

## Review Focus

1. **Staging UI on a foreign Host.** Whether `/trader/api/health` answers 200 `{"status":"ok"}` when the Host is not `APP_URL` is unverified. Expected: it does. Pinned by Task 0's pre-check; if it fails, stop and ask the owner before Task 4 adds the probe.
2. **Missing Cloudflare/deSEC prerequisites.** The deploy would hang ~15 min and fail at the certificate check. Pinned by Task 0's `dig` checks, run before Task 6's deploy.
3. **An unknown `*.public` or `*.private` host.** Must answer 404 (private: only from the tailnet), never another service's content. Pinned in Task 3's routing test.
4. **Recipient addresses in logs via the new dashboard name.** Pinned by the canary request in Task 3's routing test.
5. **The staging VM reaching the private dashboard.** `deny_staging_vm` must apply to the private zone. Pinned in Task 3's routing test.

## File Structure

| File | Change |
|---|---|
| `inventories/prod/group_vars/all/main.yml` | zone and hostname vars; one `edge_sites` entry |
| `roles/stack/templates/headscale-config.yaml.j2` | DNS route for the private zone |
| `roles/tailscale/tasks/main.yml` | private dashboard name in `_names` |
| `roles/stack/templates/Caddyfile.j2` | site bodies as macros; `*.public` and `*.private` sites; second redacting logger |
| `roles/stack/tasks/main.yml` | private name in `_cert_check` |
| `roles/stack/templates/gatus-config.yaml.j2` | two probes |
| `playbooks/verify-tailnet.yml` | test node resolves the private name |
| `scripts/portal-zones-test.py` (new) + `.github/workflows/ci.yml` | inventory, headscale and wiring assertions |
| `scripts/caddy-routing-test.sh`, `scripts/gatus-config-test.py`, `scripts/commsgun-config-test.py` | new cases |
| `README.md`, `docs/runbooks/{onboard-service,acme-token,commsgun}.md`, `docs/decisions.md`, `docs/context-constitution.md`, `docs/context-constitution-findings.md`, `policy/policy.hujson` (comment only) | docs |

---

### Task 0: Owner prerequisites (no repo change)

These are the owner's actions; the implementer asks for them and records the outputs in the PR description.

- [ ] **Step 1: Cloudflare, all DNS-only**

| Name | Type | Value |
|---|---|---|
| `*.public` | A | `137.184.245.128` (`do_reserved_ipv4`) |
| `_acme-challenge.public` | CNAME | `public.acme.natanddavehouse.com` |
| `_acme-challenge.private` | CNAME | `private.acme.natanddavehouse.com` |

- [ ] **Step 2: deSEC** — add TXT write policies for subnames `public` and `private` to the existing Caddy token, using `docs/runbooks/acme-token.md` § Add a name (once per label).

- [ ] **Step 3: Verify from any machine**

```sh
dig +short anything.public.natanddavehouse.com A            # 137.184.245.128
dig +short _acme-challenge.public.natanddavehouse.com CNAME   # public.acme.natanddavehouse.com.
dig +short _acme-challenge.private.natanddavehouse.com CNAME  # private.acme.natanddavehouse.com.
dig +short commsgun.private.natanddavehouse.com               # empty
```

- [ ] **Step 4: Pre-check the staging UI on a foreign Host** (on the staging VM; `app_ui_port` is 3000)

```sh
curl -s -w '\n%{http_code}\n' -H 'Host: xstockstrat-staging.public.natanddavehouse.com' \
  http://127.0.0.1:3000/trader/api/health
```

Expected: `{"status":"ok"…}` with HTTP 200. Anything else: stop and ask the owner (Review Focus 1).

---

### Task 1: Inventory, private-zone resolution, wiring test

**Files:**
- Create: `scripts/portal-zones-test.py`
- Modify: `inventories/prod/group_vars/all/main.yml`, `roles/stack/templates/headscale-config.yaml.j2`, `roles/tailscale/tasks/main.yml` (`_names`), `roles/stack/tasks/main.yml` (`_cert_check`), `playbooks/verify-tailnet.yml`, `.github/workflows/ci.yml`, `scripts/commsgun-config-test.py`

**Interfaces — Produces** (later tasks use these exact names):
- vars `public_zone`, `private_zone`, `commsgun_ui_private_hostname` (`commsgun.{{ private_zone }}`), `staging_portal_hostname` (`xstockstrat-staging.{{ public_zone }}`)
- `edge_sites` entry `{name: xstockstrat-staging-portal, kind: app, hostname: "{{ staging_portal_hostname }}", upstream: "{{ staging_tailnet_host }}", ui_public: true, isolate_staging_vm: false, zone: public}`. `zone` is optional; absent means a standalone site with its own certificate.

- [ ] **Step 1: Write `scripts/portal-zones-test.py`** — reuse the `load_vars()` loader shape from `scripts/gatus-config-test.py`. Tests:

```python
def test_zone_and_tenant_names(self):
    d = V["domain"]
    self.assertEqual((V["public_zone"], V["private_zone"]), (f"public.{d}", f"private.{d}"))
    self.assertEqual(V["commsgun_ui_private_hostname"], f"commsgun.private.{d}")
    self.assertEqual(V["staging_portal_hostname"], f"xstockstrat-staging.public.{d}")

def test_staging_portal_is_a_public_zone_twin_of_staging(self):
    sites = {s["name"]: s for s in V["edge_sites"]}
    old, new = sites["xstockstrat-staging"], sites["xstockstrat-staging-portal"]
    self.assertEqual(new["zone"], "public")
    self.assertNotIn("zone", old)
    for k in ("kind", "upstream", "ui_public", "isolate_staging_vm"):
        self.assertEqual(new[k], old[k], k)
    self.assertEqual([s["name"] for s in V["edge_sites"] if s.get("zone")], ["xstockstrat-staging-portal"])

def test_existing_names_are_unchanged(self):
    d = V["domain"]
    self.assertEqual(V["staging_public_hostname"], f"xstockstrat-staging.{d}")
    self.assertEqual(V["commsgun_ui_hostname"], f"commsgun.ts.{d}")

def test_headscale_routes_the_private_zone_and_nothing_else(self):
    dns = yaml.safe_load(render("roles/stack/templates/headscale-config.yaml.j2"))["dns"]
    self.assertEqual(dns["nameservers"]["split"], {V["private_zone"]: ["1.1.1.1"]})
    self.assertEqual(dns["nameservers"]["global"], [])
    self.assertIs(dns["override_local_dns"], False)

def test_private_name_gets_a_record_and_a_certificate_probe(self):
    names = re.search(r"_names: >-\n(.*?)\n    _records", (ROOT / "roles/tailscale/tasks/main.yml").read_text(), re.S).group(1)
    self.assertIn("commsgun_ui_private_hostname", names)
    check = re.search(r"_cert_check: >-\n(.*?)\n  block:", (ROOT / "roles/stack/tasks/main.yml").read_text(), re.S).group(1)
    self.assertIn("commsgun_ui_private_hostname", check)
```

`render(path)` renders the template with `V` plus `ansible_managed="test"` under `StrictUndefined`.

- [ ] **Step 2: Run it** — `python3 scripts/portal-zones-test.py`. Expected: FAIL (`KeyError: 'public_zone'`).

- [ ] **Step 3: Implement.**
  - `main.yml`: add the four vars next to `staging_public_hostname` / `commsgun_ui_hostname`, and the `edge_sites` entry after the staging one. Update the comment above `edge_sites` (≤ 2 lines) to say a `zone: public` entry is served under the `*.public` wildcard certificate.
  - `headscale-config.yaml.j2`: replace `split: {}` with a mapping `{{ private_zone }}` → `[1.1.1.1]`.
  - `roles/tailscale/tasks/main.yml`: append `commsgun_ui_private_hostname` to the literal list in `_names`. (The staging portal name arrives through `edge_sites` — spec: parity with the existing staging name.)
  - `roles/stack/tasks/main.yml`: append `{{ commsgun_ui_private_hostname }}` to `_cert_check` and extend its comment to name the two new wildcards.
  - `playbooks/verify-tailnet.yml`: after "Extra record resolves on the test node via MagicDNS", add the same task for `commsgun_ui_private_hostname`; add that name to the rescue block's evidence.
  - `scripts/commsgun-config-test.py::test_both_names_get_split_horizon_records`: also assert `commsgun_ui_private_hostname`.
  - `.github/workflows/ci.yml`: add a step running `python3 scripts/portal-zones-test.py` after the Gatus config step.

- [ ] **Step 4: Verify**

```sh
python3 scripts/portal-zones-test.py && python3 scripts/commsgun-config-test.py && python3 scripts/gatus-config-test.py
yamllint -s . && ansible-lint && for p in playbooks/*.yml; do ansible-playbook --syntax-check "$p"; done
```

Expected: all pass. Then validate the rendered headscale config with the pinned binary (a bad config stops the control plane on restart):

```sh
S=<scratch dir>; curl -fsSL -o $S/headscale https://github.com/juanfont/headscale/releases/download/v0.29.4/headscale_0.29.4_linux_amd64; chmod +x $S/headscale
ansible localhost -c local -i inventories/prod -m template -a "src=roles/stack/templates/headscale-config.yaml.j2 dest=$S/config.yaml"
sed -i "s#/etc/headscale/#$S/#g; s#/var/lib/headscale/#$S/#g; s#/var/run/headscale/#$S/#g" $S/config.yaml
echo '[]' > $S/extra-records.json; echo '{}' > $S/policy.hujson; $S/headscale -c $S/config.yaml configtest; echo "exit=$?"
```

Expected: `exit=0`.

- [ ] **Step 5: Commit** — `feat(dns): public/private zone names and the tailnet route for the private zone`

---

### Task 2: Caddyfile — site bodies as macros (no behaviour change)

**Files:** Modify `roles/stack/templates/Caddyfile.j2` only.

**Interfaces — Produces:** Jinja macros `app_body(site)` and `bearer_mcp_body(site)`: everything a standalone site of that `kind` contains today **after** its `import acme` and `import access_log` lines. Task 3 calls them inside a host-matched handle.

- [ ] **Step 1: Capture the baseline** before editing:

```sh
ansible localhost -c local -m template -a "src=roles/stack/templates/Caddyfile.j2 dest=<scratch>/Caddyfile.before"
```

- [ ] **Step 2: Refactor.** Move the two `kind` bodies into the macros; the `{% for site in edge_sites %}` loop renders only entries without `zone`, as `{{ site.hostname }} { import acme …; import access_log …; {{ body }} }`. Entries with `zone` render nothing yet.

- [ ] **Step 3: Verify no behaviour change.** Render again to `Caddyfile.after`; then

```sh
diff <(sed 's/[[:space:]]\+/ /g; /^ *$/d' <scratch>/Caddyfile.before) <(sed 's/[[:space:]]\+/ /g; /^ *$/d' <scratch>/Caddyfile.after)
```

Expected: no output (identical apart from whitespace). `yamllint -s . && ansible-lint` pass.

- [ ] **Step 4: Commit** — `refactor(caddy): site bodies as macros`

---

### Task 3: Caddyfile — the two wildcard sites, with routing tests

**Files:** Modify `roles/stack/templates/Caddyfile.j2`, `scripts/caddy-routing-test.sh`.

**Interfaces — Consumes:** Task 1's vars and `zone: public` entry; Task 2's macros.

- [ ] **Step 1: Add the failing cases to `scripts/caddy-routing-test.sh`.** Add `portal_host={{ staging_portal_hostname }}`, `cgpriv_host={{ commsgun_ui_private_hostname }}` to the `hosts.j2` line and the variable initialisers. `p="https://$portal_host"`, `v="https://$cgpriv_host"`. Cases (place the private ones beside the existing dashboard cases, before the `commsgun-ui` mock is killed):

| Description | Expected | Request |
|---|---|---|
| portal public UI | `200 ui GET /trader` | `req "$portal_host" "$p/trader"` |
| portal tool catalog is private | `404` | `req "$portal_host" "$p/agent/api/tools"` |
| portal MCP endpoint | `200 agent POST /` | `req "$portal_host" -X POST "$p/agent"` |
| portal UI health probe path | `200 ui GET /trader/api/health` | `req "$portal_host" "$p/trader/api/health"` |
| portal tailnet agent | `200 agent GET /api/tools` | `req "$portal_host" --interface "$tailnet_ip" "$p/agent/api/tools"` |
| staging VM reaches the portal | `200 ui GET /trader` | `req "$portal_host" --interface "$staging_vm_ip" "$p/trader"` |
| portal login rate limit (5/min) 429 | `200 200 200 200 200 429` | six `POST $p/api/auth/login` from `--interface 127.0.0.8` |
| old staging name's limit is independent of the portal | `200 ui POST /api/auth/login` | `sreq --interface 127.0.0.8 -X POST -d '{}' "$s/api/auth/login"` |
| unknown public-zone host is 404 | `404` | `req "nope.${portal_host#*.}" "https://nope.${portal_host#*.}/"` |
| private dashboard aborts publicly | `000` | `req "$cgpriv_host" "$v/login"` |
| private dashboard from the tailnet | `200 commsgun-ui GET /login` | `req "$cgpriv_host" --interface "$tailnet_ip" "$v/login"` |
| staging VM denied on the private dashboard | `000` | `req "$cgpriv_host" --interface "$staging_vm_ip" "$v/login"` |
| unknown private-zone host is 404 on the tailnet | `404` | `req "nope.${cgpriv_host#*.}" --interface "$tailnet_ip" "https://nope.${cgpriv_host#*.}/"` |
| private dashboard gets Caddy's X-Forwarded-* | `200 commsgun-ui GET /echo-headers xfp=[https] xfh=[$cgpriv_host] xff=[$tailnet_ip]` | as the existing dashboard case, client headers spoofed |

  Log redaction: before the existing canary greps, send `req "$cgpriv_host" --interface "$tailnet_ip" -H "$canary_hdr" "$v/messages?recipient=canary@example.test"`, wait for and require an access-log line matching `"host":"$cgpriv_host","uri":"/messages"`. The existing `grep -q canary` checks on the access logs and on Caddy's stderr then cover the new name unchanged.

- [ ] **Step 2: Push; confirm the CI `caddy` job fails** on the new cases (portal and private hosts unreachable).

- [ ] **Step 3: Implement in `Caddyfile.j2`.**
  - `*.{{ public_zone }}` site: `import acme public.{{ acme_zone }}`, `import access_log public`; for each `edge_sites` entry with `zone == 'public'`: a matcher `@site_<name with - as _> host {{ site.hostname }}` and `handle` containing that entry's macro body; final `handle { respond "Not Found" 404 }`.
  - `*.{{ private_zone }}` site: `import acme private.{{ acme_zone }}`, `import access_log private`, `import deny_staging_vm`, `import tailnet_only`; a redacting logger named `commsgun_ui_private` for `hostnames {{ commsgun_ui_private_hostname }}` writing `/var/log/caddy/commsgun-ui-private.access.log` with the same roll settings and filter as `commsgun_ui`; `@commsgun_ui host {{ commsgun_ui_private_hostname }}` → `reverse_proxy 127.0.0.1:{{ commsgun_ui_port }}` (no header stripping — the dashboard needs Caddy's default `X-Forwarded-*`); final 404 handle.
  - Global options: add `http.log.error.commsgun_ui_private` to both the `exclude` list of `log default` and the `include` list of `log redacted_requests`.
  - Update the header comment of the `(acme)` snippet only if it becomes false; add no explanatory comments beyond constraints.

- [ ] **Step 4: Push; confirm the CI `caddy` job passes**, including "every rate-limit zone … is unique" for both `Caddyfile` and `Caddyfile.twin`, and `caddy validate`.

- [ ] **Step 5: Commit** — `feat(caddy): *.public and *.private wildcard sites`

---

### Task 4: Gatus probes

**Files:** Modify `roles/stack/templates/gatus-config.yaml.j2`, `scripts/gatus-config-test.py`.

- [ ] **Step 1: Add the failing tests**

```python
def test_the_private_dashboard_name_is_probed_over_the_tailnet(self):
    e = ENDPOINTS["commsgun-ui-private"]
    self.assertEqual((e["group"], e["url"], e["interval"]), ("edge", f"https://{V['commsgun_ui_private_hostname']}/healthz", "1m"))
    self.assertEqual(e["conditions"], ["[STATUS] == 200", "[CERTIFICATE_EXPIRATION] > 336h"])
    self.assertEqual(e["client"]["dns-resolver"], "udp://100.100.100.100:53")

def test_the_staging_portal_name_is_probed_from_the_internet_resolver(self):
    e = ENDPOINTS["xstockstrat-staging-portal-ui-public"]
    self.assertEqual((e["group"], e["url"]), ("public", f"https://{V['staging_portal_hostname']}/trader/api/health"))
    self.assertEqual(e["client"]["dns-resolver"], "tcp://1.1.1.1:53")
    self.assertNotIn("ignore-redirect", e["client"])
    self.assertEqual(e["conditions"], ["[STATUS] == 200", "[BODY].status == ok", "[CERTIFICATE_EXPIRATION] > 336h"])
```

- [ ] **Step 2: Run** `python3 scripts/gatus-config-test.py` — expected FAIL (`KeyError`).
- [ ] **Step 3: Implement** — two endpoints, placed after `commsgun-ui` and after `xstockstrat-staging-ui-public` respectively, copying those entries' shape (`<<: *tailnet` / `<<: *public`, `interval: 2m` for the public one, `alerts: [{type: ntfy}]`). No existing endpoint changes.
- [ ] **Step 4: Run** `python3 scripts/gatus-config-test.py` — expected PASS, including the pre-existing uniqueness and "every polled endpoint alerts" tests.
- [ ] **Step 5: Commit** — `feat(gatus): probe the private dashboard and staging portal names`

---

### Task 5: Docs

**Files:** as listed under File Structure. Re-read each touched doc against the code before committing (CLAUDE.md § Teardown).

- [ ] **`README.md`**: add the three Cloudflare rows and the two deSEC labels to § Rebuild from scratch; one sentence where it says every 443 hostname is an `edge_sites` entry.
- [ ] **`docs/runbooks/onboard-service.md` § 4**: the zero-registration path — a `.public` portal is an `edge_sites` entry with `zone: public`; a `.private` portal is a hostname var, a host handle in the `*.private` site, the name in `_names`, and a Gatus probe; the three external steps apply only to a hostname with its own certificate. State that an app holding a single origin (`ORIGIN`, `APP_URL`) works on one name at a time.
- [ ] **`docs/runbooks/acme-token.md`**: label lists and the rotate loop gain `public private`.
- [ ] **`docs/runbooks/commsgun.md`**: the second dashboard name; it answers only `/healthz` until `ORIGIN` is switched; the switch is one variable.
- [ ] **`docs/decisions.md`**: one design entry (wildcard zones on the existing chain; private zone via DNS route + extra records, with the rejected public-record alternative and why); one Known-traps entry (Linux nodes ignore extra records outside a DNS route — measured 2026-11-18).
- [ ] **`docs/context-constitution.md`**: EDGE-07 covers the `*.private` zone's four places. **`docs/context-constitution-findings.md`**: log the `policy.hujson` "split-horizon" comment as drift, and fix that comment (comment only, ≤ 2 lines: the app VMs reach their public hostname over the internet).
- [ ] **Verify:** `yamllint -s . && scripts/policy-test.sh` (the policy file changed, comment only). **Commit** — `docs: wildcard portal zones`

---

### Task 6: Deploy and prove (owner runs the workflows)

- [ ] **Step 1:** Confirm Task 0's `dig` outputs are in the PR description. Confirm CI is green on the branch head.
- [ ] **Step 2:** Owner runs **Actions → deploy**, branch `ccr-ff18dac8-k7iw33`, mode `tailnet`. Expected: green; "Every site presents a publicly trusted certificate" prints `ok` lines for `xstockstrat-staging.public.<domain>` and `commsgun.private.<domain>`. Headscale restarts once (config changed).
- [ ] **Step 3:** Owner runs **Actions → verify**. Expected: green; the tailnet check resolves the private name.
- [ ] **Step 4:** Spec success conditions 4–6, outputs pasted into the PR:

```sh
# Debian tailnet node, Mac, phone browser
curl -s -o /dev/null -w '%{http_code}\n' https://commsgun.private.natanddavehouse.com/healthz      # 200
# off the tailnet
nslookup commsgun.private.natanddavehouse.com                                                       # not found
curl -s https://xstockstrat-staging.public.natanddavehouse.com/trader/api/health                    # {"status":"ok"…}
curl -s -o /dev/null -w '%{http_code}\n' https://nonexistent.public.natanddavehouse.com/            # 404
```

  Gatus (`https://status.ts.<domain>`): `commsgun-ui-private` and `xstockstrat-staging-portal-ui-public` healthy; every pre-existing endpoint still healthy.
- [ ] **Step 5:** Mark PR #83 ready for review with the outputs in its description. Merging is the owner's decision. The cutovers (`ORIGIN`, `APP_URL`) and phase 2 are separate changes.
