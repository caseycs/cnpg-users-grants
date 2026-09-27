"""user grant / revoke / offboard / list: edit humans: (and grants:) in the
cluster files. Config only; `sync-users --apply` and `sync-grants` apply it."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from .config import DbConfig, dump_doc, flush_new_keys, list_db_names, load_db_config
from .diff import print_diff

DEFAULT_ROLES = ["pg_read_all_data"]
ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]*")


def check_name(name: str) -> None:
    if not ROLE_NAME.fullmatch(name):
        sys.exit(f"invalid role name {name!r}: lowercase letters, digits and _ only")


def _humans(doc) -> CommentedSeq:
    if not doc.get("humans"):
        doc["humans"] = CommentedSeq()
    return doc["humans"]


def grant(db: DbConfig, name: str, roles: list[str], superuser: bool) -> None:
    """Make `name` a present human with exactly these roles/superuser."""
    if name in db.apps:
        sys.exit(f"{db.path}: {name} is listed under apps:, not a human")
    entry = CommentedMap(name=name, superuser=superuser)
    if roles:
        entry["roles"] = CommentedSeq(roles)
    humans = _humans(db.doc)
    for i, h in enumerate(humans):
        if h.get("name") == name:
            humans[i] = entry
            return
    humans.append(entry)
    humans.sort(key=lambda h: h.get("name") or "")


def revoke(db: DbConfig, name: str) -> list[str]:
    """Mark `name` ensure: absent and drop its grants: statements. Returns notes."""
    notes = []
    humans = _humans(db.doc)
    entry = next((h for h in humans if h.get("name") == name), None)
    if entry is None:
        notes.append(f"{name} is not a human in this cluster file")
    elif entry.get("ensure") != "absent":
        humans[humans.index(entry)] = CommentedMap(name=name, ensure="absent")
    grants = db.doc.get("grants") or {}
    for database in list(grants):
        per_user = grants[database] or {}
        if name in per_user:
            del per_user[name]
            notes.append(f"removed {name}'s grants in {database}")
        if not per_user:
            del grants[database]
        for grantee, stmts in per_user.items():
            for s in stmts or []:
                if re.search(rf"FOR ROLE \"?{re.escape(name)}\"?\s", s):
                    notes.append(f"{database}: {grantee} has default privileges FOR ROLE {name}; edit by hand: {s}")
    if "grants" in db.doc and not grants:
        del db.doc["grants"]
    return notes


def involves(db: DbConfig, name: str) -> bool:
    """Whether `name` still has anything in this cluster file to revoke."""
    present = any(h["name"] == name for h in db.present_humans)
    has_grants = any(name in (per_user or {}) for per_user in (db.doc.get("grants") or {}).values())
    return present or has_grants


def save(db: DbConfig, before: str, before_keys: set[str]) -> bool:
    after = flush_new_keys(dump_doc(db.doc), [k for k in db.doc if k not in before_keys])
    if after == before:
        print(f"{db.name}: no changes")
        return False
    print(db.name)
    print_diff(before, after, db.path.name, indent="  ")
    db.path.write_text(after)
    return True


def edit(dbs: list[DbConfig], change) -> int:
    """Apply change(db) -> notes to each cluster file, print diffs, write them."""
    changed = 0
    for db in dbs:
        before, keys = dump_doc(db.doc), set(db.doc)
        notes = change(db) or []
        changed += save(db, before, keys)
        for n in notes:
            print(f"  note: {n}")
    if changed:
        print(f"\n{changed} cluster file(s) changed. Apply with: cnpg-users sync-users --apply "
              f"(and sync-grants for grant changes)")
    return 0


def cmd_user(args, root: Path) -> int:
    if args.action == "list":
        return list_users(root, args.name)
    check_name(args.name)
    if args.action == "grant":
        roles = args.role if args.role is not None else ([] if args.superuser else DEFAULT_ROLES)
        for r in roles:
            check_name(r)
        dbs = [load_db_config(root, d) for d in args.db]
        return edit(dbs, lambda db: grant(db, args.name, roles, args.superuser))
    if args.action == "revoke":
        dbs = [load_db_config(root, d) for d in args.db]
    else:  # offboard: every cluster file the user is still in
        dbs = [db for db in (load_db_config(root, d) for d in list_db_names(root)) if involves(db, args.name)]
        if not dbs:
            print(f"{args.name} is in no cluster file (or already marked ensure: absent everywhere)")
            return 0
    return edit(dbs, lambda db: revoke(db, args.name))


def list_users(root: Path, only: str | None) -> int:
    rows = []
    for name in list_db_names(root):
        db = load_db_config(root, name)
        for h in db.humans:
            if only and h["name"] != only:
                continue
            if h.get("ensure") == "absent":
                what = "ensure: absent (being removed)"
            else:
                what = ", ".join((["superuser"] if h.get("superuser") else []) + list(h.get("roles") or [])) or "-"
            rows.append((h["name"], db.name, what))
    if not rows:
        print(f"no humans{f' named {only}' if only else ''}")
    width = max((len(r[0]) for r in rows), default=0)
    for user, db, what in sorted(rows):
        print(f"{user:<{width}}  {db}: {what}")
    return 0
