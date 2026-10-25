#!/usr/bin/env python3
"""Generate secrets/edge.sops.yaml without plaintext ever touching disk.

Operator-chosen values come from the environment (GitHub environment secrets in CI):
  DESEC_TOKEN, HC_PING_URL, ADMIN_CONSOLE_PASSWORD, NTFY_DAVID_PASSWORD
Everything else (ntfy publisher credentials, Gatus heartbeat tokens, database role passwords, pgBackRest mTLS PKI,
the database host's TLS CA) is generated here.
Usage: init_secrets.py <edge-tls-hostname> <output.sops.yaml>   (needs: sops, openssl, python bcrypt)
       init_secrets.py --fill-missing <existing.sops.yaml>      adds only the generated credentials the file lacks; the
                                                                 file is decrypted in memory and nothing existing changes
"""
import os
import secrets
import string
import subprocess
import sys
import tempfile
from pathlib import Path

import bcrypt
import yaml

ROOT = Path(__file__).resolve().parent.parent

REQUIRED = ["DESEC_TOKEN", "HC_PING_URL", "ADMIN_CONSOLE_PASSWORD", "NTFY_DAVID_PASSWORD"]


def run(*cmd: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(cmd, input=stdin, check=True, capture_output=True).stdout


def ntfy_token() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "tk_" + "".join(secrets.choice(alphabet) for _ in range(29))


def bcrypt_hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


# Plain credentials that can be added to an existing file without touching anything else.
GENERATED = {
    "ntfy_alerts_password_bcrypt": lambda: bcrypt_hash(secrets.token_urlsafe(32)),
    "ntfy_alerts_token": ntfy_token,
    "gatus_heartbeat_pve_token": lambda: secrets.token_urlsafe(32),
    "gatus_heartbeat_backup_token": lambda: secrets.token_urlsafe(32),
    "gatus_heartbeat_db_token": lambda: secrets.token_urlsafe(32),
    "db_role_admin_password": lambda: secrets.token_urlsafe(32),
    "db_role_xstockstrat_production_password": lambda: secrets.token_urlsafe(32),
    "db_role_xstockstrat_staging_password": lambda: secrets.token_urlsafe(32),
}
OPERATOR_KEYS = ["desec_token", "healthchecks_ping_url", "admin_console_password_hash", "ntfy_david_password_bcrypt"]
PKI_KEYS = [f"pgbackrest_tls_{n}_{k}" for n in ("ca", "server", "client") for k in ("crt", "key")]
# Sets that are created whole, never completed: the TLS CA of the database host, and the certs the pull direction adds.
DB_TLS_KEYS = [f"db_tls_{n}_{k}" for n in ("ca", "server") for k in ("crt", "key")]
PGBACKREST_DB_KEYS = ["pgbackrest_db_server_crt", "pgbackrest_db_server_key", "pgbackrest_edge_client_crt", "pgbackrest_edge_client_key"]


def inventory_var(name: str) -> str:
    return str(yaml.safe_load((ROOT / "inventories/prod/group_vars/all/main.yml").read_text())[name])


def encrypt_to(doc: dict, out: str) -> None:
    plaintext = yaml.safe_dump(doc, sort_keys=True).encode()
    encrypted = run("sops", "encrypt", "--input-type", "yaml", "--output-type", "yaml",
                    "--filename-override", out, "/dev/stdin", stdin=plaintext)
    tmp = out + ".tmp"
    try:
        with open(tmp, "wb") as f:
            f.write(encrypted)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def fill_missing(path: str) -> None:
    current = yaml.safe_load(run("sops", "decrypt", "--output-type", "yaml", path)) or {}
    cannot = [k for k in OPERATOR_KEYS + PKI_KEYS if k not in current]
    if cannot:
        sys.exit(f"{path} lacks keys this mode cannot create: {', '.join(cannot)} "
                 "(operator values come from environment secrets and the PKI is generated as one set; regenerate with overwrite)")
    for group in (DB_TLS_KEYS, PGBACKREST_DB_KEYS):
        have = [k for k in group if k in current]
        if have and len(have) != len(group):
            sys.exit(f"{path} holds only part of the set {', '.join(group)}; a certificate set is created whole, so fix it by hand or regenerate with overwrite")
    new = {k: GENERATED[k]() for k in GENERATED if k not in current}
    if not all(k in current for k in DB_TLS_KEYS):
        new |= db_tls_pki()
    if not all(k in current for k in PGBACKREST_DB_KEYS):
        new |= pgbackrest_db_pki(current["pgbackrest_tls_ca_crt"], current["pgbackrest_tls_ca_key"])
    if not new:
        print("nothing to add")
        return
    encrypt_to({**current, **new}, path)
    print(f"added {', '.join(new)} to {path}")


def pki(edge_host: str, client_cn: str) -> dict:
    """Private CA plus server/client certs for pgBackRest mutual TLS (ECDSA P-256 for broad TLS 1.2 support; 10y CA, 3y leaves)."""
    with tempfile.TemporaryDirectory() as d:
        p = lambda n: os.path.join(d, n)  # noqa: E731
        run("openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", p("ca.key"))
        run("openssl", "req", "-x509", "-new", "-key", p("ca.key"), "-days", "3650",
            "-subj", "/CN=homelab-pgbackrest-ca", "-out", p("ca.crt"))
        for name, cn, ext in (
            ("server", edge_host, f"subjectAltName=DNS:{edge_host}\nextendedKeyUsage=serverAuth"),
            ("client", client_cn, "extendedKeyUsage=clientAuth"),
        ):
            run("openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", p(f"{name}.key"))
            run("openssl", "req", "-new", "-key", p(f"{name}.key"), "-subj", f"/CN={cn}", "-out", p(f"{name}.csr"))
            with open(p(f"{name}.ext"), "w") as f:
                f.write(ext + "\n")
            run("openssl", "x509", "-req", "-in", p(f"{name}.csr"), "-CA", p("ca.crt"), "-CAkey", p("ca.key"),
                "-CAcreateserial", "-days", "1095", "-extfile", p(f"{name}.ext"), "-out", p(f"{name}.crt"))
        read = lambda n: open(p(n)).read()  # noqa: E731
        return {
            "pgbackrest_tls_ca_crt": read("ca.crt"),
            "pgbackrest_tls_ca_key": read("ca.key"),
            "pgbackrest_tls_server_crt": read("server.crt"),
            "pgbackrest_tls_server_key": read("server.key"),
            "pgbackrest_tls_client_crt": read("client.crt"),
            "pgbackrest_tls_client_key": read("client.key"),
        }


def issue(ca_crt: str, ca_key: str, cn: str, ext: str) -> tuple[str, str]:
    """ECDSA P-256 leaf signed by a CA given as PEM text, with a random serial (3y)."""
    with tempfile.TemporaryDirectory() as d:
        p = lambda n: os.path.join(d, n)  # noqa: E731
        for name, pem in (("ca.crt", ca_crt), ("ca.key", ca_key)):
            fd = os.open(p(name), os.O_WRONLY | os.O_CREAT, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(pem)
        run("openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", p("leaf.key"))
        run("openssl", "req", "-new", "-key", p("leaf.key"), "-subj", f"/CN={cn}", "-out", p("leaf.csr"))
        with open(p("leaf.ext"), "w") as f:
            f.write(ext + "\n")
        run("openssl", "x509", "-req", "-in", p("leaf.csr"), "-CA", p("ca.crt"), "-CAkey", p("ca.key"),
            "-set_serial", "0x" + secrets.token_hex(8), "-days", "1095", "-extfile", p("leaf.ext"), "-out", p("leaf.crt"))
        return Path(p("leaf.crt")).read_text(), Path(p("leaf.key")).read_text()


def db_host() -> str:
    return f"db.{inventory_var('magicdns_base_domain').replace('{{ domain }}', inventory_var('domain'))}"


def db_tls_pki() -> dict:
    """Separate private CA (10y) and a server cert for PostgreSQL and PgBouncer; not shared with the pgBackRest PKI."""
    with tempfile.TemporaryDirectory() as d:
        run("openssl", "genpkey", "-algorithm", "EC", "-pkeyopt", "ec_paramgen_curve:P-256", "-out", os.path.join(d, "ca.key"))
        run("openssl", "req", "-x509", "-new", "-key", os.path.join(d, "ca.key"), "-days", "3650",
            "-subj", "/CN=homelab-db-tls-ca", "-out", os.path.join(d, "ca.crt"))
        ca_crt, ca_key = Path(d, "ca.crt").read_text(), Path(d, "ca.key").read_text()
    host = db_host()
    crt, key = issue(ca_crt, ca_key, host, f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth")
    return {"db_tls_ca_crt": ca_crt, "db_tls_ca_key": ca_key, "db_tls_server_crt": crt, "db_tls_server_key": key}


def pgbackrest_db_pki(ca_crt: str, ca_key: str) -> dict:
    """The database host's pgBackRest server cert and the edge's client cert, from the existing pgBackRest CA."""
    host = db_host()
    server = issue(ca_crt, ca_key, host, f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth")
    client = issue(ca_crt, ca_key, inventory_var("pgbackrest_edge_client_cn"), "extendedKeyUsage=clientAuth")
    return {"pgbackrest_db_server_crt": server[0], "pgbackrest_db_server_key": server[1],
            "pgbackrest_edge_client_crt": client[0], "pgbackrest_edge_client_key": client[1]}


def main() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--fill-missing":
        fill_missing(sys.argv[2])
        return
    edge_host, out, client_cn = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "db-pgbackrest")
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        sys.exit(f"missing environment: {', '.join(missing)}")
    if len(os.environ["ADMIN_CONSOLE_PASSWORD"]) < 16 or len(os.environ["NTFY_DAVID_PASSWORD"]) < 16:
        sys.exit("ADMIN_CONSOLE_PASSWORD and NTFY_DAVID_PASSWORD must be at least 16 characters")

    doc = {
        "desec_token": os.environ["DESEC_TOKEN"],
        "healthchecks_ping_url": os.environ["HC_PING_URL"],
        "admin_console_password_hash": run("openssl", "passwd", "-6", "-stdin",
                                           stdin=os.environ["ADMIN_CONSOLE_PASSWORD"].encode()).decode().strip(),
        "ntfy_david_password_bcrypt": bcrypt_hash(os.environ["NTFY_DAVID_PASSWORD"]),
        **{k: gen() for k, gen in GENERATED.items()},
        **(base := pki(edge_host, client_cn)),
        **db_tls_pki(),
        **pgbackrest_db_pki(base["pgbackrest_tls_ca_crt"], base["pgbackrest_tls_ca_key"]),
    }
    encrypt_to(doc, out)
    print(f"wrote {out} ({len(doc)} keys)")


if __name__ == "__main__":
    main()
