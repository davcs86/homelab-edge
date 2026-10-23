#!/usr/bin/env python3
"""Verified TLS handshake against the local Caddy (127.0.0.1, port 443 unless NAME:PORT) per SNI name; exits 1 if any fails.

A handshake (chain + hostname checked against the system trust store) proves the certificate without an HTTP
request, so it also works for tailnet-only sites that abort requests from non-tailnet sources.
Usage: tls-check.py NAME[:PORT] [NAME[:PORT]...]
"""
import socket
import ssl
import sys


def check(target: str) -> bool:
    name, _, port = target.partition(":")
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", int(port or 443)), timeout=10) as raw, ctx.wrap_socket(raw, server_hostname=name) as tls:
            cert = tls.getpeercert()
    except (OSError, ssl.SSLError) as e:
        print(f"{name}: {type(e).__name__} {e}")
        return False
    issuer = dict(x[0] for x in cert["issuer"]).get("organizationName", "?")
    print(f"{name}: ok, issuer {issuer}, expires {cert['notAfter']}")
    return True


if __name__ == "__main__":
    results = [check(n) for n in sys.argv[1:]]
    sys.exit(0 if results and all(results) else 1)
