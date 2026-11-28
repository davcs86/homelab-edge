#!/usr/bin/env python3
"""Run by the deploy on the edge, after lldap is up and before Authelia serves (SSO spec § 4.7, § 4.10).

  ensure     lldap has the SSO groups, the hidden read-only `sso-sub` attribute and Authelia's bind account (in
             lldap_password_manager only), and that account's password is the one in SOPS.
  reconcile  each sso_people member's `sub` is the same in Authelia's storage and in lldap's `sso-sub`.

Usage: sso_sync.py ensure --lldap-url URL --compose FILE --groups G... --bind-user NAME --bind-email EMAIL
         (stdin: {"admin_password": ..., "bind_password": ...})
       sso_sync.py reconcile --lldap-url URL --compose FILE   (stdin: {"admin_password": ...})
The secrets come on stdin as one JSON object, never in argv or the environment.
Exit 0 done, 2 refused (unsafe state or mismatch; nothing written), 1 error. Prints one JSON object of usernames and
counts; deploy logs are public, so never a sub, an email, an attribute value or a secret.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
import uuid
from dataclasses import dataclass, field

import yaml

SUB_ATTRIBUTE = "sso-sub"
PEOPLE_GROUP = "sso_people"
PASSWORD_MANAGER_GROUP = "lldap_password_manager"
ADMIN_USER = "admin"
EXPORT_PATH = "/data/.sub-export.yml"
AUTHELIA_CONFIG = "/config/configuration.yml"
STATE_QUERY = ("{ groups { id displayName } users { id groups { displayName } attributes { name value } } "
               "schema { userSchema { attributes { name attributeType isList isVisible isEditable } } } }")


class Refused(Exception):
    """An unsafe state: nothing is written, exit 2. The message names usernames or settings only."""


class SyncError(Exception):
    """Exit 1. The message names a step, never a value."""


@dataclass
class EnsurePlan:
    create_groups: list = field(default_factory=list)
    create_attribute: bool = False
    create_bind_user: bool = False
    add_bind_to_password_manager: bool = False


@dataclass
class ReconcilePlan:
    copy_to_lldap: dict = field(default_factory=dict)
    add_to_authelia: dict = field(default_factory=dict)
    mismatched: list = field(default_factory=list)


def ensure_plan(state: dict, groups: list, bind_user: str) -> EnsurePlan:
    plan = EnsurePlan()
    existing = {g["displayName"] for g in state["groups"]}
    plan.create_groups = [g for g in groups if g not in existing]
    attrs = {a["name"]: a for a in state["schema"]["userSchema"]["attributes"]}
    attr = attrs.get(SUB_ATTRIBUTE)
    if attr is None:
        plan.create_attribute = True
    elif attr["attributeType"] != "STRING" or attr["isList"] or attr["isVisible"] or attr["isEditable"]:
        raise Refused(f"lldap attribute {SUB_ATTRIBUTE} exists but is not a single hidden read-only STRING")
    user = next((u for u in state["users"] if u["id"].lower() == bind_user.lower()), None)
    if user is None:
        plan.create_bind_user = plan.add_bind_to_password_manager = True
        return plan
    member_of = {g["displayName"].lower() for g in user["groups"]}
    in_sso = sorted(g for g in groups if g.lower() in member_of)
    if in_sso:
        raise Refused(f"bind user {bind_user} is in SSO group(s) {', '.join(in_sso)}")
    plan.add_bind_to_password_manager = PASSWORD_MANAGER_GROUP not in member_of
    return plan


def parse_export(text: str) -> dict:
    """Authelia's `identifiers export`: username lower-cased -> identifier, for the openid service without a sector."""
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        raise SyncError("Authelia's identifier export is not YAML") from None
    out = {}
    for row in doc.get("identifiers") or []:
        if row.get("service") != "openid" or row.get("sector_id"):
            continue
        name = str(row["username"]).lower()
        if name in out:
            raise SyncError(f"Authelia's export has more than one openid identifier for username {name}")
        out[name] = str(row["identifier"])
    return out


def lldap_subs(users: list) -> dict:
    """sso_people members only: id lower-cased -> their sso-sub, or None."""
    out = {}
    for u in users:
        if PEOPLE_GROUP not in {g["displayName"] for g in u["groups"]}:
            continue
        values = next((a["value"] for a in u["attributes"] if a["name"] == SUB_ATTRIBUTE), None)
        out[u["id"].lower()] = values[0] if values else None
    return out


def is_v4_uuid(value: str) -> bool:
    try:
        u = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError):
        return False
    return u.version == 4 and str(u) == value


def reconcile_plan(authelia: dict, lldap: dict) -> ReconcilePlan:
    plan = ReconcilePlan()
    for name, lsub in sorted(lldap.items()):
        asub = authelia.get(name)
        if lsub is not None and not is_v4_uuid(lsub):
            plan.mismatched.append(name)  # 4.39.28 accepts only version-4 identifiers
        elif asub and lsub:
            if asub != lsub:
                plan.mismatched.append(name)
        elif asub:
            plan.copy_to_lldap[name] = asub
        elif lsub:
            plan.add_to_authelia[name] = lsub
    holders = {}  # Authelia's identifier column is UNIQUE: one sub, one username
    for name, sub in [*authelia.items(), *((n, v) for n, v in lldap.items() if v is not None)]:
        holders.setdefault(sub, set()).add(name)
    shared = {n for names in holders.values() if len(names) > 1 for n in names}
    for name in shared:
        plan.copy_to_lldap.pop(name, None)
        plan.add_to_authelia.pop(name, None)
    plan.mismatched = sorted(set(plan.mismatched) | shared)
    return plan


class Lldap:
    def __init__(self, url: str, admin_password: str):
        self.url = url.rstrip("/")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.cookies = {}
        body = self._post("/auth/simple/login", {"username": ADMIN_USER, "password": admin_password}, "login")
        self.token = body["token"]

    def _post(self, path, payload, what, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(), headers=headers, method="POST")
        try:
            with self.opener.open(req, timeout=30) as r:
                for c in r.headers.get_all("Set-Cookie") or []:
                    name, _, value = c.split(";", 1)[0].partition("=")
                    self.cookies[name.strip()] = value
                return json.load(r)
        except Exception as e:
            raise SyncError(f"lldap {what} failed ({type(e).__name__})") from None

    def logout(self):
        """Revokes this session's token: it is in docker's exec events."""
        cookie = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        req = urllib.request.Request(self.url + "/auth/logout", headers={"Cookie": cookie})
        try:
            with self.opener.open(req, timeout=30):
                pass
        except Exception as e:
            raise SyncError(f"lldap logout failed ({type(e).__name__})") from None

    def gql(self, query, variables=None, what="query"):
        body = self._post("/api/graphql", {"query": query, "variables": variables or {}}, what, self.token)
        if body.get("errors") or body.get("data") is None:
            raise SyncError(f"lldap GraphQL {what} returned an error")
        return body["data"]

    def state(self):
        return self.gql(STATE_QUERY, what="state")


def compose(compose_file, *args, env=None):
    """Never raises; the caller names the step."""
    return subprocess.run(["docker", "compose", "-f", compose_file, *args], capture_output=True, text=True, timeout=300,
                          env=env)


def authelia_run(compose_file, *args):
    return compose(compose_file, "run", "--rm", "--no-deps", "-T", "authelia", *args)


def read_secrets(stream, *keys) -> list:
    try:
        doc = json.loads(stream.read())
    except ValueError:
        raise SyncError("stdin is not a JSON object") from None
    if not isinstance(doc, dict):
        raise SyncError("stdin is not a JSON object")
    for k in keys:
        if not isinstance(doc.get(k), str) or not doc[k]:
            raise SyncError(f"stdin lacks {k}")
    return [doc[k] for k in keys]


def ensure(args, admin_password, bind_password) -> dict:
    ld = Lldap(args.lldap_url, admin_password)
    state = ld.state()
    plan = ensure_plan(state, args.groups, args.bind_user)
    for name in plan.create_groups:
        ld.gql("mutation($name: String!) { createGroup(name: $name) { id } }", {"name": name}, "createGroup")
    if plan.create_attribute:
        ld.gql("mutation($name: String!) { addUserAttribute(name: $name, attributeType: STRING, isList: false, "
               "isVisible: false, isEditable: false) { ok } }", {"name": SUB_ATTRIBUTE}, "addUserAttribute")
    if plan.create_bind_user:
        ld.gql("mutation($user: CreateUserInput!) { createUser(user: $user) { id } }",
               {"user": {"id": args.bind_user, "email": args.bind_email}}, "createUser")
    if plan.add_bind_to_password_manager:
        gid = next((g["id"] for g in state["groups"] if g["displayName"] == PASSWORD_MANAGER_GROUP), None)
        if gid is None:
            raise SyncError(f"lldap has no {PASSWORD_MANAGER_GROUP} group")
        ld.gql("mutation($user: String!, $group: Int!) { addUserToGroup(userId: $user, groupId: $group) { ok } }",
               {"user": args.bind_user, "group": gid}, "addUserToGroup")
    # Docker's exec events record argv, not env: the password goes by env, and the token is revoked after.
    try:
        r = compose(args.compose, "exec", "-T", "-e", "LLDAP_USER_PASSWORD", "lldap", "/app/lldap_set_password",
                    "--base-url", "http://localhost:17170", f"--token={ld.token}", "--username", args.bind_user,
                    env={**os.environ, "LLDAP_USER_PASSWORD": bind_password})
    finally:
        ld.logout()
    if r.returncode != 0:
        raise SyncError(f"lldap_set_password for {args.bind_user} exited {r.returncode}")
    return {"created_groups": plan.create_groups, "created_attribute": plan.create_attribute,
            "created_bind_user": plan.create_bind_user}


def authelia_export(compose_file) -> dict:
    script = (f"rm -f {EXPORT_PATH}; authelia storage user identifiers export --file {EXPORT_PATH} "
              f"--config {AUTHELIA_CONFIG} >/dev/null && cat {EXPORT_PATH}; rc=$?; rm -f {EXPORT_PATH}; exit $rc")
    r = authelia_run(compose_file, "sh", "-c", script)
    if r.returncode != 0:
        if not r.stdout.strip() and "no data to export" in r.stderr:  # 4.39.28 exits 1 on an empty store
            return {}
        raise SyncError(f"Authelia's identifier export exited {r.returncode}")
    return parse_export(r.stdout)


def reconcile(args, admin_password) -> tuple:
    ld = Lldap(args.lldap_url, admin_password)
    users = ld.state()["users"]
    ids = {u["id"].lower(): u["id"] for u in users}
    in_lldap = lldap_subs(users)
    r = authelia_run(args.compose, "authelia", "storage", "migrate", "up", "--config", AUTHELIA_CONFIG)
    if r.returncode != 0 and "schema already up to date" not in r.stdout + r.stderr:
        raise SyncError(f"Authelia's storage migration exited {r.returncode}")
    plan = reconcile_plan(authelia_export(args.compose), in_lldap)
    acted = set(plan.copy_to_lldap) | set(plan.add_to_authelia) | set(plan.mismatched)
    summary = {"copied_to_lldap": [], "added_to_authelia": [], "mismatched": plan.mismatched,
               "unchanged": len(set(in_lldap) - acted)}
    if plan.mismatched:
        return summary, Refused("sub differs between Authelia and lldap, is not a version-4 UUID, or is held by two "
                                "usernames, for: " + ", ".join(plan.mismatched))
    try:
        for name, sub in sorted(plan.copy_to_lldap.items()):
            ld.gql("mutation($user: UpdateUserInput!) { updateUser(user: $user) { ok } }",
                   {"user": {"id": ids[name], "insertAttributes": [{"name": SUB_ATTRIBUTE, "value": [sub]}]}}, "updateUser")
            summary["copied_to_lldap"].append(name)
        for name, sub in sorted(plan.add_to_authelia.items()):
            r = authelia_run(args.compose, "authelia", "storage", "user", "identifiers", "add", ids[name],
                             "--identifier", sub, "--service", "openid", "--config", AUTHELIA_CONFIG)
            if r.returncode != 0:
                raise SyncError(f"Authelia's identifiers add for {name} exited {r.returncode}")
            summary["added_to_authelia"].append(name)
    except SyncError as x:  # the writes done so far still print
        return summary, x
    return summary, None


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("ensure")
    r = sub.add_parser("reconcile")
    for sp in (e, r):
        sp.add_argument("--lldap-url", required=True)
        sp.add_argument("--compose", required=True)
    e.add_argument("--groups", nargs="+", required=True)
    e.add_argument("--bind-user", required=True)
    e.add_argument("--bind-email", required=True)
    args = p.parse_args(argv)
    try:
        if args.cmd == "ensure":
            print(json.dumps(ensure(args, *read_secrets(sys.stdin, "admin_password", "bind_password"))))
            return 0
        summary, refused = reconcile(args, *read_secrets(sys.stdin, "admin_password"))
        print(json.dumps(summary))
        if refused:
            raise refused
        return 0
    except Refused as x:
        print(f"sso_sync {args.cmd}: refused: {x}", file=sys.stderr)
        return 2
    except SyncError as x:
        print(f"sso_sync {args.cmd}: error: {x}", file=sys.stderr)
        return 1
    except Exception as x:  # its message could hold a value: name the type only
        print(f"sso_sync {args.cmd}: error: {type(x).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
