"""The CNPG `roles:` list in an ArgoCD values.yaml, made to match a cluster file."""

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
    unlisted: list[str] = field(default_factory=list)  # roles in values.yaml that aren't in the cluster file

    @property
    def summary(self) -> list[str]:
        return ([f"add {n}" for n in self.added] + [f"update {n}" for n in self.updated]
                + [f"mark absent {n}" for n in self.absent])


def _update_entry(entry, want: dict, keys: tuple[str, ...]) -> bool:
    """Set keys and inRoles of a roles-list entry to want; True if it changed."""
    changes = {k: want[k] for k in keys if entry.get(k) != want[k]}
    drop_roles = "inRoles" in entry and "inRoles" not in want
    if "inRoles" in want and list(entry.get("inRoles") or []) != want["inRoles"]:
        changes["inRoles"] = want["inRoles"]
    if not (changes or drop_roles):
        return False
    tail = _pop_tail(entry)  # re-attached to whatever ends the entry now
    entry.update(changes)
    if drop_roles:
        del entry["inRoles"]
    if tail is not None:
        _put_tail(entry, tail)
    return True


def sync_roles(text: str, humans: list[dict], apps: list[str], roles_path: str = "roles") -> RolesChange:
    """values.yaml text with its CNPG roles list (at dotted `roles_path`,
    e.g. "roles" or "cluster.roles") matching the cluster file's humans:
    - every present human with login/superuser/inRoles as in the cluster file
      (other keys and comments kept; missing humans appended at the end)
    - every human marked ensure: absent gets ensure: absent, so CNPG drops it
    - apps and roles listed nowhere in the cluster file are never changed; the
      latter are reported in change.unlisted."""
    yaml = make_write_yaml()
    doc = yaml.load(text)
    keys = roles_path.split(".")
    roles = doc
    for key in keys:
        roles = roles.get(key) if isinstance(roles, dict) else None
    if not isinstance(roles, list):
        raise ValueError(f"no list at {roles_path}")
    # block-style indentation of the list (make_write_yaml: 2 per mapping level)
    key_indent = 2 * (len(keys) - 1)
    item = " " * (key_indent + 2) + "- name: "
    field = " " * (key_indent + 4)

    by_name = {r.get("name"): r for r in roles}
    wanted = {h["name"]: human_role(h) for h in humans if h.get("ensure") != "absent"}
    absent = {h["name"] for h in humans if h.get("ensure") == "absent"}
    change = RolesChange(text)
    for entry in roles:
        name = entry.get("name")
        if entry.get("ensure") == "absent":
            continue
        if name in absent:
            entry["ensure"] = "absent"
            change.absent.append(name)
        elif name not in wanted and name not in apps:
            change.unlisted.append(name)

    added = change.added
    for name, want in wanted.items():
        entry = by_name.get(name)
        if entry is None:
            roles.append(want)
            added.append(name)
            continue
        if _update_entry(entry, want, ("ensure", "login", "superuser")):
            change.updated.append(name)

    buf = io.StringIO()
    yaml.dump(doc, buf)
    out = buf.getvalue()
    if added:
        # a blank line before each appended entry, as between the others
        for name in added:
            out = re.sub(rf"\n+({item}{re.escape(name)}\n)", r"\n\n\1", out, count=1)
        # ruamel moves the blank line that ended the list in front of the
        # first appended entry; put one back after the last
        if _blank_after_list(text, keys):
            last = re.escape(added[-1])
            out = re.sub(rf"(\n{item}{last}\n(?:{field}[^\n]*\n)*)(?=[ ]{{0,{key_indent}}}[^ \n])",
                         r"\1\n", out, count=1)
    change.text = out
    return change


def _blank_after_list(text: str, keys: list[str]) -> bool:
    """Whether the list under keys (a: b: ... list:) is followed by a blank line."""
    lines = text.splitlines()
    i = -1
    for depth, key in enumerate(keys):  # find each key line inside its parent
        head = " " * (2 * depth) + key + ":"
        i = next((j for j in range(i + 1, len(lines)) if lines[j].rstrip() == head), None)
        if i is None:
            return False
    indent = 2 * (len(keys) - 1)
    for j in range(i + 1, len(lines)):
        line = lines[j]
        if line.strip() and not line.lstrip().startswith("#") and len(line) - len(line.lstrip()) <= indent:
            return lines[j - 1] == ""
    return False
