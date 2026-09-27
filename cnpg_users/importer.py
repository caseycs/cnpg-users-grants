"""import: classify live roles, capture live grants, update dbs/<db>.yaml."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from typing import Callable

from . import queries
from .cluster import Cluster
from .config import DbConfig, make_write_yaml
from .diff import print_diff
from .grants import Catalog, Grant, drop_ignored, format_statements, plan
from .stores import PasswordStore

SYSTEM_ROLES = {"postgres", "streaming_replica", "cnpg_pooler_pgbouncer", "app"}


@dataclass
class Roles:
    humans: list[dict] = field(default_factory=list)
    apps: list[str] = field(default_factory=list)
    app_entries: dict[str, dict] = field(default_factory=dict)  # name -> {name, superuser, roles}
    app_reasons: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


def human_entry(role: dict) -> dict:
    """humans: item: superuser flag and live role memberships, as in the cluster."""
    entry = {"name": role["name"], "superuser": role["super"]}
    if role["in_roles"]:
        entry["roles"] = sorted(role["in_roles"])
    return entry


def classify_roles(
    roles: list[dict],
    cluster_cr: dict,
    crd_roles: set[str],
    store_users: set[str],
    say: Callable[[str], None] = print,
) -> Roles:
    """system/absent -> skipped; DatabaseRole CR or k8s password secret -> app;
    password in the store -> human; anything else -> app."""
    spec = cluster_cr.get("spec") or {}
    mrs = (cluster_cr.get("status") or {}).get("managedRolesStatus") or {}
    secret_synced = {n for n, st in (mrs.get("passwordStatus") or {}).items() if st.get("resourceVersion")}
    reserved = set((mrs.get("byStatus") or {}).get("reserved") or [])
    spec_roles = {r["name"]: r for r in (spec.get("managed") or {}).get("roles") or []}
    initdb = (spec.get("bootstrap") or {}).get("initdb") or {}
    bootstrap_owner = initdb.get("owner") if initdb.get("secret") else None

    out = Roles()

    def app(role: dict, reason: str) -> None:
        name = role["name"]
        out.apps.append(name)
        out.app_entries[name] = human_entry(role)
        out.app_reasons[name] = reason
        say(f"  {name}: app ({reason})")

    for role in roles:
        name = role["name"]
        entry = spec_roles.get(name) or {}
        if name.startswith(("pg_", "cnpg_")) or name in SYSTEM_ROLES or name in reserved:
            out.skipped.append(f"{name} (system)")
        elif name in crd_roles:
            app(role, "DatabaseRole CR, managed elsewhere")
        elif name in secret_synced or entry.get("passwordSecret") or name == bootstrap_owner:
            app(role, "password via k8s secret")
        elif entry.get("ensure") == "absent":
            out.skipped.append(f"{name} (ensure: absent)")
        elif name in store_users:
            out.humans.append(human_entry(role))
            say(f"  {name}: human (password in the store)")
        else:
            app(role, "no password in the store")

    out.humans.sort(key=lambda h: h["name"])
    out.apps.sort()
    return out


def merge_humans(
    config: list[dict], live: list[dict], live_roles: set[str], prune: bool = False
) -> tuple[list[dict], list[str]]:
    """humans: for the db file. With prune, exactly the live humans. Otherwise
    edits not applied yet survive: a human in the file but not live yet is
    kept (pending create), config wins over live for superuser/roles, and an
    ensure: absent entry stays until its role is gone. Live humans missing
    from the file are added. Returns (humans, notes)."""
    if prune:
        return sorted(live, key=lambda h: h["name"]), []
    live_by_name = {h["name"]: h for h in live}
    out, notes, seen = [], [], set()
    for c in config:
        name = c["name"]
        seen.add(name)
        if c.get("ensure") == "absent":
            if name in live_roles:
                out.append(dict(c))
                notes.append(f"{name}: ensure: absent, role still exists (pending drop)")
            else:
                notes.append(f"{name}: dropped, removed from humans")
            continue
        out.append(dict(c))
        if name not in live_by_name:
            notes.append(f"{name}: not created yet (pending)")
        elif {k: v for k, v in live_by_name[name].items()} != {k: v for k, v in c.items()}:
            notes.append(f"{name}: differs from live {live_by_name[name]} (pending; --prune takes live)")
    for h in live:
        if h["name"] not in seen:
            out.append(h)
            notes.append(f"{h['name']}: found live, added")
    return sorted(out, key=lambda h: h["name"]), notes


def build_grants_config(
    live: dict[str, set[Grant]], catalogs: dict[str, Catalog]
) -> dict[str, dict[str, list[str]]]:
    """database -> grantee -> compressed statements."""
    config: dict[str, dict[str, list[str]]] = {}
    for database in sorted(live):
        per_user = {
            grantee: format_statements({g for g in live[database] if g.grantee == grantee},
                                       catalogs[database], compress=True)
            for grantee in sorted({g.grantee for g in live[database]})
        }
        if per_user:
            config[database] = per_user
    return config


def update_config(db: DbConfig, humans: list[dict], apps: list, grants: dict) -> tuple[str, str]:
    """Apply import results to db.doc; returns (before, after) yaml text."""
    yaml = make_write_yaml()

    def dump() -> str:
        buf = io.StringIO()
        yaml.dump(db.doc, buf)
        return buf.getvalue()

    before = dump()
    doc = db.doc
    before_keys = set(doc.keys())
    for key, value in (("humans", humans), ("apps", apps), ("grants", grants)):
        if not value:
            doc.pop(key, None)
        elif doc.get(key) != value:
            doc[key] = value
    after = dump()

    # ruamel puts a blank line before keys appended after a block that ended
    # with one; drop it so new keys sit flush
    lines = after.splitlines(keepends=True)
    for key in [k for k in doc.keys() if k not in before_keys]:
        for i, line in enumerate(lines):
            if line.startswith(f"{key}:"):
                if i > 0 and lines[i - 1].strip() == "":
                    del lines[i - 1]
                break
    return before, "".join(lines)


def cmd_import(db: DbConfig, store: PasswordStore, write: bool, prune: bool = False) -> None:
    if not db.online:
        print(f"{db.header}: offline — skipped")
        return
    print(db.header)

    cluster = Cluster(db)
    info = cluster.sql(queries.CLUSTER)
    databases = info["databases"]
    crd_roles = {i.get("spec", {}).get("name") for i in cluster.list_crs("databaseroles")}
    crd_databases = {
        i["spec"]["name"]: i["spec"].get("owner")
        for i in cluster.list_crs("databases") if i.get("spec", {}).get("name")
    }
    all_live = {d: cluster.live_grants(d) for d in databases}
    live = {d: drop_ignored(g, db.ignored_grantees) for d, g in all_live.items()}
    catalogs = {d: cluster.catalog(d) for d in databases}

    print(f"\ndatabases: {', '.join(databases)}")
    for name, owner in sorted(crd_databases.items()):
        print(f"  Database CR: {name} (owner {owner})")

    roles = classify_roles(info["roles"], cluster.cr, crd_roles, store.names())

    humans, human_notes = merge_humans(db.humans, roles.humans, {r["name"] for r in info["roles"]}, prune)
    print("\nhumans:")
    for h in humans:
        if h.get("ensure") == "absent":
            print(f"  {h['name']}  ensure: absent")
            continue
        flags = ["superuser"] if h.get("superuser") else []
        print(f"  {h['name']}  {' '.join(flags + list(h.get('roles') or [])) or '-'}")
    for n in human_notes:
        print(f"  note: {n}")
    print("apps:")
    for a in roles.apps:
        e = roles.app_entries[a]
        flags = ["superuser"] if e.get("superuser") else []
        print(f"  {a}  {' '.join(flags + list(e.get('roles') or [])) or '-'}  ({roles.app_reasons[a]})")
    if not roles.apps:
        print("  (none)")
    print("skipped:")
    plain = sorted(s for s in roles.skipped if not s.startswith("pg_"))
    for s in plain:
        print(f"  {s}")
    if len(roles.skipped) > len(plain):
        print(f"  {len(roles.skipped) - len(plain)} pg_* builtin roles")

    grants = build_grants_config(live, catalogs)
    ignored = sum(len(all_live[d]) - len(live[d]) for d in databases)
    if grants:
        print("\ngrants sql for config:")
        for database, per_user in grants.items():
            print(
                f"  {database}: {sum(len(s) for s in per_user.values())} statements"
                f" ({', '.join(per_user)})"
            )
    if ignored:
        print(f"  ({ignored} live grants ignored via ignored_grantees)")

    # self-check: the generated statements must reproduce live grants exactly
    for database in databases:
        stmts = [s for per_user in grants.get(database, {}).values() for s in per_user]
        to_grant, to_revoke = plan(stmts, live[database], catalogs[database])
        if to_grant or to_revoke:
            print(f"\n  ! {database}: generated grants don't round-trip; sync-grants would run:")
            for s in format_statements(to_grant, catalogs[database])[:10]:
                print(f"      {s}")
            for s in format_statements(to_revoke, catalogs[database], revoke=True)[:10]:
                print(f"      {s}")

    apps = [a for a in roles.apps if a not in {h["name"] for h in humans}]
    before, after = update_config(db, humans, apps, grants)
    print("\nconfig diff:")
    print_diff(before, after, db.path.name, indent="  " if before == after else "")

    if write and before != after:
        db.path.write_text(after)
        print(f"\nupdated {db.path}")
