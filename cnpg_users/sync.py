"""sync-grants: print the SQL that makes live grants match the config."""

from __future__ import annotations

import sys
from typing import TextIO

from .cluster import Cluster
from .config import DbConfig
from .grants import drop_ignored, format_statements, plan


def cmd_sync_grants(db: DbConfig, out: TextIO = sys.stdout) -> tuple[bool, None]:
    """Write the SQL for the databases that aren't in sync to out (one line if
    all are); (True if live grants differ from the config, None)."""
    if not db.online:
        print(f"{db.header}: offline — skipped", file=out)
        return False, None

    cluster = Cluster(db)
    lines: list[str] = []
    for database in cluster.databases():
        stmts = [s for per_user in (db.grants.get(database) or {}).values() for s in per_user or []]
        live = drop_ignored(cluster.live_grants(database), db.ignored_grantees)
        catalog = cluster.catalog(database)
        try:
            to_grant, to_revoke = plan(stmts, live, catalog)
        except ValueError as exc:
            sys.exit(f"{db.path}: grants.{database}: {exc}")

        if not (to_grant or to_revoke):
            continue
        lines.append(f"  Database: {database}")
        for title, stmts in (
            ("To add", format_statements(to_grant, catalog)),
            ("To remove", format_statements(to_revoke, catalog, revoke=True)),
        ):
            if stmts:
                lines.append(f"    {title}:")
                lines += [f"      {s}" for s in stmts]
    if not lines:
        print(f"{db.header}: in sync", file=out)
        return False, None
    print(db.header, file=out)
    print("\n".join(lines), file=out)
    return True, None
