"""sync-grants: print (and with --apply, run) the SQL that makes live grants
match the config."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable, TextIO

from .cluster import Cluster
from .config import DbConfig
from .grants import drop_ignored, format_statements, plan


@dataclass
class DatabasePlan:
    database: str
    statements: list[str]  # GRANTs, then REVOKEs


@dataclass
class GrantsPlan:
    db: DbConfig
    cluster: Cluster
    databases: list[DatabasePlan] = field(default_factory=list)

    @property
    def count(self) -> int:
        return sum(len(d.statements) for d in self.databases)


def plan_database(db: DbConfig, cluster: Cluster, database: str) -> tuple[list[str], list[str]]:
    """(GRANT statements, REVOKE statements) for one database."""
    stmts = [s for per_user in (db.grants.get(database) or {}).values() for s in per_user or []]
    live = drop_ignored(cluster.live_grants(database), db.ignored_grantees)
    catalog = cluster.catalog(database)
    try:
        to_grant, to_revoke = plan(stmts, live, catalog, db.ignored_grantees)
    except ValueError as exc:
        sys.exit(f"{db.path}: grants.{database}: {exc}")
    return format_statements(to_grant, catalog), format_statements(to_revoke, catalog, revoke=True)


def cmd_sync_grants(db: DbConfig, out: TextIO = sys.stdout) -> tuple[bool, GrantsPlan | None]:
    """Write the SQL for the databases that aren't in sync to out (one line if
    all are); (True if live grants differ from the config, plan for apply_grants)."""
    if not db.online:
        print(f"{db.header}: offline — skipped", file=out)
        return False, None

    cluster = Cluster(db)
    result = GrantsPlan(db, cluster)
    lines: list[str] = []
    for database in cluster.databases():
        grants, revokes = plan_database(db, cluster, database)
        if not (grants or revokes):
            continue
        result.databases.append(DatabasePlan(database, grants + revokes))
        lines.append(f"  Database: {database}")
        for title, stmts in (("To add", grants), ("To remove", revokes)):
            if stmts:
                lines.append(f"    {title}:")
                lines += [f"      {s}" for s in stmts]
    if not lines:
        print(f"{db.header}: in sync", file=out)
        return False, result
    print(db.header, file=out)
    print("\n".join(lines), file=out)
    return True, result


def confirm(question: str, assume_yes: bool, ask: Callable[[str], str] = input) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        sys.exit(f"{question} — not a terminal; pass --yes to apply without asking")
    try:
        return ask(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def apply_grants(plans: list[GrantsPlan], assume_yes: bool = False,
                 ask: Callable[[str], str] = input) -> bool:
    """Run each database's statements (one transaction per batch), then read it
    again. True if anything still differs (or nothing was applied)."""
    pending = [(p, d) for p in plans for d in p.databases]
    if not pending:
        return False
    total = sum(len(d.statements) for _, d in pending)
    print()
    if not confirm(f"Apply {total} statement(s) to {len(pending)} database(s)?", assume_yes, ask):
        print("not applied")
        return True
    drift = False
    for p, d in pending:
        print(f"{p.db.name} / {d.database}: applying {len(d.statements)} statement(s)...", flush=True)
        p.cluster.execute(d.statements, database=d.database)
        grants, revokes = plan_database(p.db, p.cluster, d.database)
        if grants or revokes:
            drift = True
            print(f"  still differs: {len(grants)} to add, {len(revokes)} to remove")
        else:
            print("  in sync")
    return drift
