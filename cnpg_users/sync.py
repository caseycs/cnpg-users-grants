"""sync-grants: print the SQL that makes live grants match the config."""

from __future__ import annotations

import sys
from typing import TextIO

from .cluster import Cluster
from .config import DbConfig
from .grants import drop_ignored, format_statements, plan


def cmd_sync_grants(db: DbConfig, out: TextIO = sys.stdout) -> tuple[bool, None]:
    """Write the SQL per database to out; (True if live grants differ from the config, None)."""
    print(db.header, file=out)
    if not db.online:
        print("(offline — skipped)", file=out)
        return False, None

    cluster = Cluster(db)
    drift = False
    for database in cluster.databases():
        stmts = [s for per_user in (db.grants.get(database) or {}).values() for s in per_user or []]
        live = drop_ignored(cluster.live_grants(database), db.ignored_grantees)
        catalog = cluster.catalog(database)
        try:
            to_grant, to_revoke = plan(stmts, live, catalog)
        except ValueError as exc:
            sys.exit(f"{db.path}: grants.{database}: {exc}")

        print(f"  Database: {database}", file=out)
        if not (to_grant or to_revoke):
            print("    (in sync)", file=out)
            continue
        drift = True
        for title, lines in (
            ("To add", format_statements(to_grant, catalog)),
            ("To remove", format_statements(to_revoke, catalog, revoke=True)),
        ):
            if lines:
                print(f"    {title}:", file=out)
                for s in lines:
                    print(f"      {s}", file=out)
    return drift, None
