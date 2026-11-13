"""Which keys of secrets/edge.sops.yaml the roles read, and which the committed file holds (key names are plaintext in SOPS files)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DOTTED = re.compile(r"edge_secrets\.([a-z][a-z_0-9]*)")
SUBSCRIPT = re.compile(r"edge_secrets\[\s*['\"]([a-z][a-z_0-9]*)['\"]\s*\]")
INDIRECT = re.compile(r"edge_secrets\[\s*item\.key\s*\]")  # the loop items then carry `key: <name>`
LOOP_KEY = re.compile(r"\bkey:\s*([a-z][a-z_0-9]*)")
# The inventory names some SOPS keys indirectly: edge_secrets[item.password_key] (DB roles), edge_secrets[gate_sops_key] (app VMs).
PASSWORD_KEY = re.compile(r"""['"]?\b(?:password_key|gate_sops_key)['"]?:\s*['"]?([a-z][a-z_0-9]*)""")
# A template loop `{% for app in <registry> %}` reading edge_secrets[app.key]: the inventory list's items carry `key: <name>`.
REGISTRY_LOOP = re.compile(r"\{%-?\s*for\s+(\w+)\s+in\s+(\w+)")


def inventory_lists(root: Path) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in (root / "inventories").rglob("*.yml"):
        doc = yaml.safe_load(f.read_text())
        if isinstance(doc, dict):
            out |= {k: v for k, v in doc.items() if isinstance(v, list)}
    return out


def used_keys(root: Path = ROOT) -> set[str]:
    used: set[str] = set()
    registries = inventory_lists(root) if (root / "inventories").is_dir() else {}
    for base in (root / "roles", root / "playbooks", root / "inventories"):
        for f in base.rglob("*"):
            if not (f.is_file() and f.suffix in (".yml", ".yaml", ".j2")):
                continue
            text = f.read_text()
            used |= set(DOTTED.findall(text)) | set(SUBSCRIPT.findall(text)) | set(PASSWORD_KEY.findall(text))
            if INDIRECT.search(text):
                used |= set(LOOP_KEY.findall(text))
            for var, registry in REGISTRY_LOOP.findall(text):
                if re.search(rf"edge_secrets\[\s*{var}\.key\s*\]", text):
                    used |= {i["key"] for i in registries.get(registry, []) if isinstance(i, dict) and isinstance(i.get("key"), str)}
    return used


def file_keys(path: Path = ROOT / "secrets/edge.sops.yaml") -> set[str]:
    return set(yaml.safe_load(path.read_text())) - {"sops"}
