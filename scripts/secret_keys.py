"""Which keys of secrets/edge.sops.yaml the roles read, and which the committed file holds (key names are plaintext in SOPS files)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOTTED = re.compile(r"edge_secrets\.([a-z][a-z_0-9]*)")
SUBSCRIPT = re.compile(r"edge_secrets\[\s*['\"]([a-z][a-z_0-9]*)['\"]\s*\]")
INDIRECT = re.compile(r"edge_secrets\[\s*item\.key\s*\]")  # the loop items then carry `key: <name>`
LOOP_KEY = re.compile(r"\bkey:\s*([a-z][a-z_0-9]*)")
# group_vars name the SOPS key of each workload's role password; the role reads it as edge_secrets[item.password_key].
PASSWORD_KEY = re.compile(r"""['"]?\bpassword_key['"]?:\s*['"]?([a-z][a-z_0-9]*)""")


def used_keys(root: Path = ROOT) -> set[str]:
    used: set[str] = set()
    for base in (root / "roles", root / "playbooks", root / "inventories"):
        for f in base.rglob("*"):
            if not (f.is_file() and f.suffix in (".yml", ".yaml", ".j2")):
                continue
            text = f.read_text()
            used |= set(DOTTED.findall(text)) | set(SUBSCRIPT.findall(text)) | set(PASSWORD_KEY.findall(text))
            if INDIRECT.search(text):
                used |= set(LOOP_KEY.findall(text))
    return used


def file_keys(path: Path = ROOT / "secrets/edge.sops.yaml") -> set[str]:
    return set(yaml.safe_load(path.read_text())) - {"sops"}
