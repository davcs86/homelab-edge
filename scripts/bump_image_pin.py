#!/usr/bin/env python3
"""Pins the newest published image digest in versions.yml and docs/versions.md; the bump-webhooks-mcp workflow runs it."""
import argparse
import datetime
import json
import os
import re
import sys
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple

DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
REVISION_LABEL = "org.opencontainers.image.revision"
ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])

Response = namedtuple("Response", "status headers body")


class BumpError(Exception):
    pass


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses non-https redirects and drops the registry token when the redirect leaves the host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise BumpError(f"refusing redirect to non-https URL {newurl}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.headers.pop("Authorization", None)
        return new


def http_fetch(method, url, headers):
    opener = urllib.request.build_opener(SafeRedirect)
    try:
        with opener.open(urllib.request.Request(url, method=method, headers=headers), timeout=30) as r:
            return Response(r.status, {k.title(): v for k, v in r.headers.items()}, r.read())
    except urllib.error.HTTPError as e:
        return Response(e.code, {k.title(): v for k, v in e.headers.items()}, e.read())
    except urllib.error.URLError as e:
        raise BumpError(f"cannot reach {url}: {e.reason}") from e


class Registry:
    def __init__(self, image, fetch=http_fetch):
        self.host, _, self.repo = image.partition("/")
        self.fetch = fetch
        self._token = None

    def _get(self, method, path, accept=ACCEPT):
        if self._token is None:
            r = self.fetch("GET", f"https://{self.host}/token?scope=repository:{self.repo}:pull", {})
            if r.status != 200:
                raise BumpError(f"registry token request failed with HTTP {r.status}")
            self._token = json.loads(r.body)["token"]
        return self.fetch(method, f"https://{self.host}/v2/{self.repo}/{path}",
                          {"Authorization": f"Bearer {self._token}", "Accept": accept})

    def _json(self, path):
        r = self._get("GET", path)
        if r.status != 200:
            raise BumpError(f"registry returned HTTP {r.status} for {path}")
        return json.loads(r.body)

    def tag_digest(self, tag):
        r = self._get("HEAD", f"manifests/{tag}")
        if r.status == 404:
            raise BumpError(f"tag '{tag}' not found in {self.host}/{self.repo} (not published yet?)")
        if r.status != 200:
            raise BumpError(f"registry returned HTTP {r.status} for tag '{tag}'")
        digest = r.headers.get("Docker-Content-Digest", "")
        if not DIGEST_RE.match(digest):
            raise BumpError(f"registry sent no usable Docker-Content-Digest for tag '{tag}'")
        return digest

    def verify_digest(self, digest):
        if not DIGEST_RE.match(digest):
            raise BumpError(f"digest must look like sha256:<64 hex>, got '{digest}'")
        if self._get("HEAD", f"manifests/{digest}").status != 200:
            raise BumpError(f"{digest} is not in the registry for {self.host}/{self.repo}")
        return digest

    def revision(self, digest):
        manifest = self._json(f"manifests/{digest}")
        if "manifests" in manifest:
            amd64 = [m for m in manifest["manifests"]
                     if m.get("platform", {}).get("os") == "linux" and m.get("platform", {}).get("architecture") == "amd64"]
            if not amd64:
                return "unknown"
            manifest = self._json(f"manifests/{amd64[0]['digest']}")
        config = self._json(f"blobs/{manifest['config']['digest']}")
        label = ((config.get("config") or {}).get("Labels") or {}).get(REVISION_LABEL, "")
        return label if re.fullmatch(r"[0-9a-f]{40}", label) else "unknown"


def _pin_lines(text, var):
    return re.findall(rf"^{re.escape(var)}:.*$", text, re.M)


def current_pin(text, var, image):
    lines = _pin_lines(text, var)
    if len(lines) != 1:
        raise BumpError(f"expected exactly one '{var}:' line in the versions file, found {len(lines)}")
    m = re.fullmatch(rf'{re.escape(var)}: "{re.escape(image)}@(sha256:[0-9a-f]{{64}})"', lines[0])
    if not m:
        raise BumpError(f"the '{var}' line is not '{image}@sha256:<64 hex>' in quotes")
    return m.group(1)


def replace_pin(text, var, image, digest):
    old = current_pin(text, var, image)
    return text.replace(f'{var}: "{image}@{old}"', f'{var}: "{image}@{digest}"', 1)


def replace_docs_row(text, var, image, digest, revision, date):
    rows = [l for l in text.splitlines() if f"`{var}`" in l]
    if len(rows) != 1:
        raise BumpError(f"expected exactly one docs row mentioning `{var}`, found {len(rows)}")
    pat = re.compile(rf"`{re.escape(image)}@sha256:[0-9a-f]{{8}}…`, digest only \(commit `(?:[0-9a-f]{{7}}|unknown)`, \d{{4}}-\d{{2}}-\d{{2}}\)")
    if len(pat.findall(rows[0])) != 1:
        raise BumpError(f"the docs row for `{var}` has no pin cell like `{image}@sha256:xxxxxxxx…`, digest only (commit `…`, date)")
    new = f"`{image}@{digest[:15]}…`, digest only (commit `{revision[:7] if revision != 'unknown' else 'unknown'}`, {date})"
    return text.replace(rows[0], pat.sub(lambda _: new, rows[0]), 1)


def write_outputs(**kv):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.writelines(f"{k}={v}\n" for k, v in kv.items())


def main(argv=None, fetch=http_fetch, today=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--var", required=True)
    ap.add_argument("--image", required=True)
    ap.add_argument("--tag", default="main")
    ap.add_argument("--digest")
    ap.add_argument("--versions", default="inventories/prod/group_vars/all/versions.yml")
    ap.add_argument("--docs", default="docs/versions.md")
    a = ap.parse_args(argv)
    try:
        versions = pathlib.Path(a.versions).read_text()
        docs = pathlib.Path(a.docs).read_text()
        previous = current_pin(versions, a.var, a.image)
        reg = Registry(a.image, fetch)
        target = reg.verify_digest(a.digest) if a.digest else reg.tag_digest(a.tag)
        if target == previous:
            print("up to date")
            write_outputs(changed="false")
            return 0
        revision = reg.revision(target)
        date = today or datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        new_versions = replace_pin(versions, a.var, a.image, target)
        new_docs = replace_docs_row(docs, a.var, a.image, target, revision, date)
    except BumpError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    pathlib.Path(a.versions).write_text(new_versions)
    pathlib.Path(a.docs).write_text(new_docs)
    print(f"{previous} -> {target} (revision {revision})")
    write_outputs(changed="true", digest=target, short=target[7:19], previous=previous, revision=revision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
