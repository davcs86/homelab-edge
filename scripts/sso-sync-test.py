#!/usr/bin/env python3
"""scripts/sso_sync.py against a fake lldap GraphQL server and a fake `docker` on PATH that records its argv
(SSO spec § 4.7, § 4.10; plan Review Focus 2 and 3)."""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/sso_sync.py"
sys.path.insert(0, str(ROOT / "scripts"))
import sso_sync  # noqa: E402

GROUPS = ["sso_people", "xstockstrat", "xstockstrat-staging", "webhooks-mcp", "commsgun", "happybabytracker"]
BIND = "authelia"
ADMIN_PW, BIND_PW = "-xAdminFakeSecret0123456789", "-xBindFakeSecret0123456789"
SUB_A, SUB_B = "0f1e2d3c-4b5a-4987-8a6b-5c4d3e2f1a0b", "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d"
SUB_C = "2c3d4e5f-6a7b-4c8d-9e0f-1a2b3c4d5e6f"
V1_UUID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
GOOD_ATTR = {"name": "sso-sub", "attributeType": "STRING", "isList": False, "isVisible": False, "isEditable": False}
BASE_ATTRS = [{"name": n, "attributeType": t, "isList": False, "isVisible": True, "isEditable": e}
              for n, t, e in (("mail", "STRING", True), ("display_name", "STRING", True), ("uuid", "STRING", False))]

FAKE_DOCKER = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_DOCKER_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
args = sys.argv[1:]
if "sh" in args and "-c" in args:
    text = os.environ.get("FAKE_EXPORT", "")
    if text:
        sys.stdout.write(text)
    else:
        sys.stderr.write("Error: no data to export\n")
elif "migrate" in args:
    sys.stderr.write("Error: schema already up to date\n")
    sys.exit(1)
'''


def user(uid, groups=(), sub=None, email=None):
    attrs = [{"name": "mail", "value": [email or f"{uid}@example.test"]}, {"name": "uuid", "value": ["b653b1c3-d9ef-3a6e-a6a2-6145eea8ecad"]}]
    if sub is not None:
        attrs.append({"name": "sso-sub", "value": [sub]})
    return {"id": uid, "groups": [{"displayName": g} for g in groups], "attributes": attrs}


def lldap_state(groups=(), attr=None, users=()):
    names = ["lldap_admin", "lldap_password_manager", "lldap_strict_readonly", *groups]
    return {"groups": [{"id": i + 1, "displayName": n} for i, n in enumerate(names)],
            "users": [user("admin", ["lldap_admin"]), *users],
            "schema": {"userSchema": {"attributes": BASE_ATTRS + ([attr] if attr else [])}}}


class FakeLldap:
    """Answers the login and the GraphQL calls sso_sync.py makes; mutations change the state and are recorded."""

    def __init__(self, state):
        self.state, self.mutations = state, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/auth/simple/login":
                    ok = body == {"username": "admin", "password": ADMIN_PW}
                    return self.reply({"token": "fake-jwt"} if ok else {}, 200 if ok else 401)
                if self.headers.get("Authorization") != "Bearer fake-jwt":
                    return self.reply({}, 401)
                self.reply(fake.graphql(body["query"], body.get("variables") or {}))

            def reply(self, obj, code=200):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def graphql(self, query, v):
        s = self.state
        op = re.search(r"\{\s*(\w+)", query.split("mutation", 1)[1]).group(1) if query.lstrip().startswith("mutation") else None
        if op is None:
            return {"data": copy.deepcopy(s)}
        self.mutations.append((op, v, query))
        if op == "createGroup":
            s["groups"].append({"id": len(s["groups"]) + 1, "displayName": v["name"]})
            return {"data": {op: {"id": len(s["groups"])}}}
        if op == "addUserAttribute":
            m = dict(re.findall(r"(attributeType|isList|isVisible|isEditable): (\w+)", query))
            s["schema"]["userSchema"]["attributes"].append({
                "name": v["name"], "attributeType": m["attributeType"],
                **{k: m[k] == "true" for k in ("isList", "isVisible", "isEditable")}})
        elif op == "createUser":
            s["users"].append(user(v["user"]["id"], email=v["user"]["email"]))
        elif op == "addUserToGroup":
            name = next(g["displayName"] for g in s["groups"] if g["id"] == v["group"])
            next(u for u in s["users"] if u["id"] == v["user"])["groups"].append({"displayName": name})
        elif op == "updateUser":
            u = next(u for u in s["users"] if u["id"] == v["user"]["id"])
            for a in v["user"]["insertAttributes"]:
                u["attributes"] = [x for x in u["attributes"] if x["name"] != a["name"]] + [a]
        return {"data": {op: {"ok": True}}}

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "bin").mkdir()
        docker = self.tmp / "bin/docker"
        docker.write_text(FAKE_DOCKER)
        docker.chmod(0o755)
        self.log = self.tmp / "docker.log"
        self.export = ""
        self.lldap = None

    def tearDown(self):
        if self.lldap:
            self.lldap.close()

    def serve(self, state):
        self.lldap = FakeLldap(state)
        return self.lldap

    def run_sync(self, *args):
        env = {**os.environ, "PATH": f"{self.tmp / 'bin'}:{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(self.log),
               "FAKE_EXPORT": self.export, "LLDAP_ADMIN_PASSWORD": ADMIN_PW, "LLDAP_BIND_PASSWORD": BIND_PW}
        common = ["--lldap-url", self.lldap.url, "--compose", "/srv/edge/compose.yml"]
        r = subprocess.run([sys.executable, str(SCRIPT), args[0], *common, *args[1:]], capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertNotIn("Traceback", r.stderr)
        return r

    def ensure(self):
        return self.run_sync("ensure", "--groups", *GROUPS, "--bind-user", BIND, "--bind-email", "authelia@ldap.invalid")

    def docker_calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def assertNoValues(self, r, *values):
        for text in (r.stdout, r.stderr):
            self.assertIsNone(UUID_RE.search(text), text)
            self.assertNotIn("@", text)
            for v in (ADMIN_PW, BIND_PW, *values):
                self.assertNotIn(v, text)


class Ensure(Base):
    def test_ensure_creates_only_the_missing_groups_and_the_attribute(self):
        fake = self.serve(lldap_state(groups=["sso_people", "commsgun"]))
        r = self.ensure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {
            "created_groups": ["xstockstrat", "xstockstrat-staging", "webhooks-mcp", "happybabytracker"],
            "created_attribute": True, "created_bind_user": True})
        self.assertEqual([m[0] for m in fake.mutations],
                         ["createGroup"] * 4 + ["addUserAttribute", "createUser", "addUserToGroup"])
        self.assertIn(GOOD_ATTR, fake.state["schema"]["userSchema"]["attributes"])
        bind = next(u for u in fake.state["users"] if u["id"] == BIND)
        self.assertEqual(bind["groups"], [{"displayName": "lldap_password_manager"}])

    def test_a_second_ensure_changes_nothing(self):
        fake = self.serve(lldap_state())
        self.assertEqual(self.ensure().returncode, 0)
        before, sent = copy.deepcopy(fake.state), len(fake.mutations)
        r = self.ensure()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"created_groups": [], "created_attribute": False, "created_bind_user": False})
        self.assertEqual(len(fake.mutations), sent)
        self.assertEqual(fake.state, before)

    def test_ensure_refuses_an_editable_visible_list_or_non_string_sub_attribute(self):
        for bad in ({"isEditable": True}, {"isVisible": True}, {"isList": True}, {"attributeType": "INTEGER"}):
            with self.subTest(bad=bad):
                fake = self.serve(lldap_state(attr=GOOD_ATTR | bad))
                r = self.ensure()
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(self.docker_calls(), [])
                fake.close()
                self.lldap = None

    def test_ensure_refuses_a_bind_user_in_any_sso_group(self):
        for group in GROUPS:
            with self.subTest(group=group):
                fake = self.serve(lldap_state(groups=GROUPS, attr=GOOD_ATTR,
                                              users=[user(BIND, ["lldap_password_manager", group])]))
                r = self.ensure()
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn(group, r.stderr)
                self.assertEqual(fake.mutations, [])
                self.assertEqual(self.docker_calls(), [])
                fake.close()
                self.lldap = None

    def test_ensure_never_mutates_another_user(self):
        fake = self.serve(lldap_state(users=[user("alice", ["sso_people"]), user("bob")]))
        people = copy.deepcopy([u for u in fake.state["users"] if u["id"] != BIND])
        self.assertEqual(self.ensure().returncode, 0)
        pm = next(g["id"] for g in fake.state["groups"] if g["displayName"] == "lldap_password_manager")
        for op, v, _ in fake.mutations:
            if op == "createGroup":
                self.assertIn(v["name"], GROUPS)
            elif op == "addUserAttribute":
                self.assertEqual(v["name"], "sso-sub")
            elif op == "createUser":
                self.assertEqual(v["user"]["id"], BIND)
            elif op == "addUserToGroup":
                self.assertEqual((v["user"], v["group"]), (BIND, pm))
            else:
                self.fail(f"unexpected mutation {op}")
        self.assertEqual([u for u in fake.state["users"] if u["id"] != BIND], people)

    def test_the_bind_password_reaches_lldap_set_password_and_nothing_is_printed(self):
        self.serve(lldap_state())
        r = self.ensure()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.docker_calls()
        self.assertEqual(calls, [["compose", "-f", "/srv/edge/compose.yml", "exec", "-T", "lldap", "/app/lldap_set_password",
                                  "--base-url", "http://localhost:17170", "--admin-username", "admin",
                                  f"--admin-password={ADMIN_PW}", "--username", BIND, f"--password={BIND_PW}"]])
        self.assertNoValues(r)

    def test_a_secret_starting_with_a_dash_is_passed_as_opt_equals_value(self):
        self.serve(lldap_state())
        self.assertEqual(self.ensure().returncode, 0)
        argv = self.docker_calls()[0]
        self.assertTrue(ADMIN_PW.startswith("-") and BIND_PW.startswith("-"))
        self.assertIn(f"--admin-password={ADMIN_PW}", argv)
        self.assertIn(f"--password={BIND_PW}", argv)
        self.assertNotIn(ADMIN_PW, argv)
        self.assertNotIn(BIND_PW, argv)


def export_yaml(*rows):
    return "identifiers:\n" + "".join(
        f"    - service: {s}\n      sector_id: \"{sec}\"\n      username: {u}\n      identifier: {i}\n" for s, sec, u, i in rows)


class Parse(unittest.TestCase):
    def test_parse_export_keeps_openid_without_sector_and_lowercases(self):
        text = "# yaml-language-server: $schema=https://www.authelia.com/schemas/v4.40/json-schema/export.identifiers.json\n\n" + export_yaml(
            ("openid", "", "Alice", SUB_A), ("openid", "example.com", "bob", SUB_B), ("other", "", "carol", V1_UUID))
        self.assertEqual(sso_sync.parse_export(text), {"alice": SUB_A})
        self.assertEqual(sso_sync.parse_export(""), {})
        with self.assertRaises(sso_sync.SyncError):
            sso_sync.parse_export(export_yaml(("openid", "", "Alice", SUB_A), ("openid", "", "alice", SUB_B)))

    def test_lldap_subs_reads_sso_people_only_and_lowercases(self):
        users = lldap_state(users=[user("Alice", ["sso_people"], SUB_A), user("bob", ["sso_people"]),
                                   user("carol", ["commsgun"], SUB_B)])["users"]
        self.assertEqual(sso_sync.lldap_subs(users), {"alice": SUB_A, "bob": None})


class Reconcile(Base):
    def reconcile(self, users, export_rows=()):
        fake = self.serve(lldap_state(groups=GROUPS, attr=GOOD_ATTR, users=users))
        self.export = export_yaml(*export_rows) if export_rows else ""
        before = copy.deepcopy(fake.state)
        r = self.run_sync("reconcile")
        return fake, r, before

    def adds(self):
        return [c for c in self.docker_calls() if "add" in c]

    def sub_of(self, fake, uid):
        u = next(u for u in fake.state["users"] if u["id"] == uid)
        return next((a["value"][0] for a in u["attributes"] if a["name"] == "sso-sub"), None)

    def test_reconcile_copies_a_first_login_sub_to_lldap(self):
        fake, r, _ = self.reconcile([user("alice", ["sso_people"])], [("openid", "", "alice", SUB_A)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"copied_to_lldap": ["alice"], "added_to_authelia": [], "mismatched": [], "unchanged": 0})
        self.assertEqual(self.sub_of(fake, "alice"), SUB_A)
        self.assertEqual([m[0] for m in fake.mutations], ["updateUser"])
        self.assertEqual(self.adds(), [])

    def test_reconcile_restores_an_authelia_sub_from_lldap(self):
        fake, r, _ = self.reconcile([user("alice", ["sso_people"], SUB_A)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"copied_to_lldap": [], "added_to_authelia": ["alice"], "mismatched": [], "unchanged": 0})
        self.assertEqual(self.adds(), [["compose", "-f", "/srv/edge/compose.yml", "run", "--rm", "--no-deps", "-T", "authelia",
                                        "authelia", "storage", "user", "identifiers", "add", "alice", "--identifier", SUB_A,
                                        "--service", "openid", "--config", "/config/configuration.yml"]])
        self.assertEqual(fake.mutations, [])
        migrate = ["compose", "-f", "/srv/edge/compose.yml", "run", "--rm", "--no-deps", "-T", "authelia",
                   "authelia", "storage", "migrate", "up"]
        self.assertEqual(self.docker_calls()[0], migrate)  # before the export, on the image the stack will run

    def test_reconcile_leaves_matching_subs_alone(self):
        fake, r, before = self.reconcile([user("alice", ["sso_people"], SUB_A), user("bob", ["sso_people"])],
                                         [("openid", "", "alice", SUB_A)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"copied_to_lldap": [], "added_to_authelia": [], "mismatched": [], "unchanged": 2})
        self.assertEqual((fake.mutations, self.adds(), fake.state), ([], [], before))

    def test_reconcile_matches_usernames_case_insensitively(self):
        fake, r, before = self.reconcile([user("alice", ["sso_people"], SUB_A), user("Bob", ["sso_people"])],
                                         [("openid", "", "Alice", SUB_A), ("openid", "", "bob", SUB_B)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"copied_to_lldap": ["bob"], "added_to_authelia": [], "mismatched": [], "unchanged": 1})
        self.assertEqual(self.sub_of(fake, "Bob"), SUB_B)
        self.assertEqual(self.adds(), [])

    def test_reconcile_ignores_users_outside_sso_people(self):
        fake, r, before = self.reconcile([user("carol", ["commsgun"], SUB_B), user("dave")], [("openid", "", "dave", SUB_A)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"copied_to_lldap": [], "added_to_authelia": [], "mismatched": [], "unchanged": 0})
        self.assertEqual((fake.mutations, self.adds(), fake.state), ([], [], before))

    def test_a_mismatch_or_a_non_v4_uuid_writes_nothing_and_exits_2(self):
        for bad in (SUB_B, "not-a-uuid", V1_UUID):
            with self.subTest(bad=bad):
                self.log.unlink(missing_ok=True)
                fake, r, before = self.reconcile(
                    [user("alice", ["sso_people"], bad), user("bob", ["sso_people"]), user("carol", ["sso_people"], SUB_C)],
                    [("openid", "", "alice", SUB_A), ("openid", "", "bob", "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e")])
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(json.loads(r.stdout)["mismatched"], ["alice"])
                self.assertIn("alice", r.stderr)
                self.assertEqual((fake.mutations, self.adds(), fake.state), ([], [], before))  # bob's copy, carol's add: neither
                self.assertNoValues(r, bad)
                fake.close()
                self.lldap = None

    def test_the_export_is_one_run_that_removes_a_stale_file_first(self):
        self.reconcile([user("alice", ["sso_people"])], [("openid", "", "alice", SUB_A)])
        runs = [c for c in self.docker_calls() if "sh" in c]
        self.assertEqual(len(runs), 1)
        argv = runs[0]
        self.assertEqual(argv[:argv.index("sh")], ["compose", "-f", "/srv/edge/compose.yml", "run", "--rm", "--no-deps", "-T", "authelia"])
        self.assertEqual(argv[argv.index("sh"):-1], ["sh", "-c"])
        steps = [s.strip() for s in argv[-1].split(";")]
        m = re.fullmatch(r"rm -f (/data/\S+)", steps[0])
        self.assertTrue(m, steps)
        self.assertEqual(steps[-1], f"rm -f {m.group(1)}")
        self.assertIn(f"authelia storage user identifiers export --file {m.group(1)} --config /config/configuration.yml >/dev/null",
                      steps)
        self.assertIn(f"cat {m.group(1)}", steps)

    def test_an_empty_authelia_store_is_no_data_not_an_error(self):
        _, r, _ = self.reconcile([user("alice", ["sso_people"])])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["unchanged"], 1)

    def test_the_summary_holds_usernames_and_counts_only(self):
        _, r, _ = self.reconcile([user("alice", ["sso_people"]), user("bob", ["sso_people"], SUB_B)], [("openid", "", "alice", SUB_A)])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNoValues(r, SUB_A, SUB_B)
        summary = json.loads(r.stdout)
        self.assertEqual(set(summary), {"copied_to_lldap", "added_to_authelia", "mismatched", "unchanged"})
        self.assertIsInstance(summary["unchanged"], int)
        self.log.unlink()
        self.lldap.close()
        self.serve(lldap_state())
        e = self.ensure()
        self.assertEqual(set(json.loads(e.stdout)), {"created_groups", "created_attribute", "created_bind_user"})
        self.assertNoValues(e)


if __name__ == "__main__":
    unittest.main()
