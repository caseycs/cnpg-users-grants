"""Files in the directory the tool is run from: dbs/<db>.yaml (one per
database) and user_passwords_store.yaml (where human passwords live).
A db given as a .yaml path is also looked up relative to that directory."""

from __future__ import annotations

import io
import sys
from dataclasses import dataclass, field, fields
from pathlib import Path

from ruamel.yaml import YAML


PASSWORDS_STORE = "user_passwords_store.yaml"


@dataclass
class SsmSettings:
    aws_profile: str = "default"
    aws_region: str = "eu-central-1"
    aws_ssm_prefix: str = "/cnpg-user/"


@dataclass
class DbConfig:
    name: str
    context: str
    namespace: str
    cluster: str
    path: Path
    doc: dict                  # round-trip yaml document of dbs/<db>.yaml, edited by import
    online: bool = True
    repo: str | None = None         # GitHub repo holding the ArgoCD values.yaml
    values_file: str | None = None  # path of values.yaml (CNPG roles list) in repo
    values_roles_path: str = "roles"  # dotted path of the roles list in values_file, e.g. cluster.roles
    humans: list[dict] = field(default_factory=list)  # name, superuser, roles; ensure: absent = being removed
    apps: list[str] = field(default_factory=list)  # app role names, for reference only
    grants: dict = field(default_factory=dict)  # database -> grantee -> [statement]
    ignored_grantees: list[str] = field(default_factory=list)

    @property
    def present_humans(self) -> list[dict]:
        return [h for h in self.humans if h.get("ensure") != "absent"]

    @property
    def absent_humans(self) -> list[str]:
        """Humans marked ensure: absent (revoked, role not dropped yet)."""
        return [h["name"] for h in self.humans if h.get("ensure") == "absent"]

    @property
    def header(self) -> str:
        """First line of every per-db report."""
        return f"{self.name} (cluster {self.cluster} in ns {self.namespace})"


def load_ssm_settings(root: Path) -> SsmSettings:
    path = root / PASSWORDS_STORE
    if not path.is_file():
        sys.exit(f"passwords store settings not found: {path}")
    data = YAML(typ="safe").load(path) or {}
    unknown = set(data) - {f.name for f in fields(SsmSettings)}
    if unknown:
        sys.exit(f"{path}: unknown keys: {', '.join(sorted(unknown))}")
    return SsmSettings(**{k: str(v) for k, v in data.items()})


def list_db_names(root: Path) -> list[str]:
    return sorted(p.stem for p in (root / "dbs").glob("*.yaml"))


def resolve_db_path(root: Path, name: str) -> Path:
    if name.endswith(".yaml"):
        candidates = [Path(name), root / name]
        return next((p for p in candidates if p.is_file()), candidates[0])
    return root / "dbs" / f"{name}.yaml"


def load_db_config(root: Path, name: str) -> DbConfig:
    db_path = resolve_db_path(root, name)
    if not db_path.is_file():
        sys.exit(f"database '{db_path.stem}' not found: {db_path} missing")
    doc = YAML(typ="rt").load(db_path)
    missing = [k for k in ("context", "namespace", "cluster") if k not in doc]
    if missing:
        sys.exit(f"{db_path}: missing required keys: {', '.join(missing)}")
    online = doc.get("online", True)
    if not isinstance(online, bool):
        sys.exit(f"{db_path}: 'online' must be true or false (got {online!r})")
    return DbConfig(
        name=db_path.stem,
        context=doc["context"],
        namespace=doc["namespace"],
        cluster=doc["cluster"],
        path=db_path,
        doc=doc,
        online=online,
        repo=doc.get("repo"),
        values_file=doc.get("values_file"),
        values_roles_path=str(doc.get("values_roles_path") or "roles"),
        humans=[dict(h) for h in doc.get("humans") or []],
        apps=[str(a) if isinstance(a, str) else str(a["name"]) for a in doc.get("apps") or []],
        grants=doc.get("grants") or {},
        ignored_grantees=[str(r) for r in doc.get("ignored_grantees") or []],
    )


def dump_doc(doc) -> str:
    buf = io.StringIO()
    make_write_yaml().dump(doc, buf)
    return buf.getvalue()


def flush_new_keys(text: str, new_keys: list[str]) -> str:
    """ruamel puts a blank line before keys appended after a block that ended
    with one; drop it so new keys sit flush."""
    lines = text.splitlines(keepends=True)
    for key in new_keys:
        for i, line in enumerate(lines):
            if line.startswith(f"{key}:"):
                if i > 0 and lines[i - 1].strip() == "":
                    del lines[i - 1]
                break
    return "".join(lines)


def make_write_yaml() -> YAML:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    return yaml
