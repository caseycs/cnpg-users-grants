"""The CNPG `roles:` list in an ArgoCD values.yaml, made to match a db file."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .config import make_write_yaml


def _pop_tail(node):
    """Detach the comment after node's last scalar: ruamel keeps the blank
    lines and comments that follow a list entry there."""
    if isinstance(node, CommentedMap) and node:
        key, slot = list(node)[-1], 2
    elif isinstance(node, CommentedSeq) and node:
        key, slot = len(node) - 1, 0
    else:
        return None
    if isinstance(node[key], (CommentedMap, CommentedSeq)) and node[key]:
        return _pop_tail(node[key])
    item = node.ca.items.get(key)
    token = item[slot] if item else None
    if token is not None:
        item[slot] = None
    return token


def _put_tail(node, token) -> None:
    if isinstance(node, CommentedMap):
        key, slot = list(node)[-1], 2
    else:
        key, slot = len(node) - 1, 0
    if isinstance(node[key], (CommentedMap, CommentedSeq)) and node[key]:
        _put_tail(node[key], token)
    else:
        node.ca.items.setdefault(key, [None, None, None, None])[slot] = token


def human_role(human: dict) -> dict:
    entry = CommentedMap(name=human["name"], ensure="present", login=True, superuser=bool(human["superuser"]))
    if human.get("roles"):
        entry["inRoles"] = CommentedSeq(human["roles"])
    return entry


@dataclass
class RolesChange:
    text: str                                          # the new values.yaml
    added: list[str] = field(default_factory=list)     # humans appended
    updated: list[str] = field(default_factory=list)   # humans whose entry changed
    absent: list[str] = field(default_factory=list)    # roles newly set ensure: absent

    @property
    def summary(self) -> list[str]:
        return ([f"add {n}" for n in self.added] + [f"update {n}" for n in self.updated]
                + [f"mark absent {n}" for n in self.absent])


def sync_roles(text: str, humans: list[dict], apps: list[str]) -> RolesChange:
    """values.yaml text with its roles: list matching the db file:
    - every human present, with login/superuser/inRoles as in the db file
      (other keys and comments kept; missing humans appended at the end)
    - every other role that isn't an app and has no passwordSecret gets
      ensure: absent, so CNPG drops it
    - apps, passwordSecret roles and already-absent roles are left alone."""
    yaml = make_write_yaml()
    doc = yaml.load(text)
    roles = doc.get("roles")
    if roles is None:
        raise ValueError("no roles: list")

    by_name = {r.get("name"): r for r in roles}
    wanted = {h["name"]: human_role(h) for h in humans}
    keep = set(apps) | set(wanted)
    change = RolesChange(text)
    for entry in roles:
        if entry.get("name") not in keep and "passwordSecret" not in entry and entry.get("ensure") != "absent":
            entry["ensure"] = "absent"
            change.absent.append(entry.get("name"))

    added = change.added
    for name, want in wanted.items():
        entry = by_name.get(name)
        if entry is None:
            roles.append(want)
            added.append(name)
            continue
        changes = {k: want[k] for k in ("ensure", "login", "superuser") if entry.get(k) != want[k]}
        drop_roles = "inRoles" in entry and "inRoles" not in want
        if "inRoles" in want and list(entry.get("inRoles") or []) != want["inRoles"]:
            changes["inRoles"] = want["inRoles"]
        if not (changes or drop_roles):
            continue
        change.updated.append(name)
        tail = _pop_tail(entry)  # re-attached to whatever ends the entry now
        entry.update(changes)
        if drop_roles:
            del entry["inRoles"]
        if tail is not None:
            _put_tail(entry, tail)

    buf = io.StringIO()
    yaml.dump(doc, buf)
    out = buf.getvalue()
    if added:
        # a blank line before each appended entry, as between the others
        for name in added:
            out = re.sub(rf"\n+(  - name: {re.escape(name)}\n)", r"\n\n\1", out, count=1)
        # ruamel moves the blank line that ended the list in front of the
        # first appended entry; put one back after the last
        if _blank_after_roles(text):
            last = re.escape(added[-1])
            out = re.sub(rf"(\n  - name: {last}\n(?:    [^\n]*\n)*)(?=\S)", r"\1\n", out, count=1)
    change.text = out
    return change


def _blank_after_roles(text: str) -> bool:
    """Whether the line before the top-level key following roles: is blank."""
    lines = text.splitlines()
    if "roles:" not in lines:
        return False
    for i in range(lines.index("roles:") + 1, len(lines)):
        if lines[i] and not lines[i][0].isspace() and not lines[i].startswith("#"):
            return lines[i - 1] == ""
    return False
