#!/usr/bin/env python3
"""Generate secrets/edge.sops.yaml without plaintext ever touching disk.

Operator-chosen values come from the environment (GitHub environment secrets in CI):
  DESEC_TOKEN, HC_PING_URL, ADMIN_CONSOLE_PASSWORD, NTFY_DAVID_PASSWORD
Everything else (ntfy publisher credentials, Gatus heartbeat tokens, pgBackRest mTLS PKI) is generated here.
Usage: init_secrets.py <edge-tls-hostname> <output.sops.yaml>   (needs: sops, openssl, python bcrypt)
"""
import os
import secrets
import string
import subprocess
import sys
import tempfile

import bcrypt
import yaml

REQUIRED = ["DESEC_TOKEN", "HC_PING_URL", "ADMIN_CONSOLE_PASSWORD", "NTFY_DAVID_PASSWORD"]


def run(*cmd: str, stdin: bytes | None = None) -> bytes:
    return subprocess.run(cmd, input=stdin, check=True, capture_output=True).stdout


def ntfy_token() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "tk_" + "".join(secrets.choice(alphabet) for _ in range(29))


def bcrypt_hash(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=10)).decode()


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


def main() -> None:
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
        "ntfy_alerts_password_bcrypt": bcrypt_hash(secrets.token_urlsafe(32)),
        "ntfy_alerts_token": ntfy_token(),
        "gatus_heartbeat_pve_token": secrets.token_urlsafe(32),
        "gatus_heartbeat_backup_token": secrets.token_urlsafe(32),
        "gatus_heartbeat_db_token": secrets.token_urlsafe(32),
        **pki(edge_host, client_cn),
    }
    plaintext = yaml.safe_dump(doc, sort_keys=True).encode()
    encrypted = run("sops", "encrypt", "--input-type", "yaml", "--output-type", "yaml",
                    "--filename-override", out, "/dev/stdin", stdin=plaintext)
    with open(out, "wb") as f:
        f.write(encrypted)
    print(f"wrote {out} ({len(doc)} keys)")


if __name__ == "__main__":
    main()
