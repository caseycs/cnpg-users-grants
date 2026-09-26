#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["ruamel.yaml", "pygments", "kubernetes", "boto3"]
# ///
"""Manage CNPG human users and app grants across clusters.

Config layout:
    user_passwords_store.yaml
                      aws_profile, aws_region, aws_ssm_prefix of the SSM
                      parameters holding human passwords (used by import)
    dbs/<db>.yaml     one file per database: context, namespace, cluster,
                      repo, values_file, ignored_grantees, humans, apps,
                      grants

Commands:
    import <db>        Explore live roles and grants, classify every
                       non-system role as human or app, print a report and a
                       diff against dbs/<db>.yaml; --write updates it.
    sync-grants <db>...|--all
                       Print the GRANT/REVOKE SQL that makes live grants match
                       grants: in dbs/<db>.yaml. Never executes it. Exit code:
                       0 in sync, 3 drift found, 1/2 error/usage.
    sync-users <db>...|--all
                       Print the diff that makes the CNPG roles: list in the
                       repo's values.yaml match humans:/apps: (humans present
                       with their superuser/roles, every other non-app role
                       ensure: absent), and ALTER ROLE statements for humans
                       whose password differs from SSM (as a SCRAM verifier,
                       never the plaintext). Same exit codes as sync-grants.
                       --apply: open (or update) a PR per values.yaml, wait
                       --apply-timeout seconds (default 180) for the roles to
                       be created/dropped once it's merged and synced, then
                       run the password statements.

Grants to a db file's ignored_grantees are left out of both commands.

Classification (live cluster object + SSM parameter names):
    - system roles (pg_*/cnpg_* builtins, Cluster status reserved) → skipped
    - DatabaseRole CRs → app, managed elsewhere, never touched
    - password via k8s secret: secret-synced (Cluster status passwordStatus
      resourceVersion), a passwordSecret reference in the Cluster spec, or
      the bootstrap owner with its initdb secret → app
    - ensure: absent in the Cluster's managed roles → skipped
    - password parameter <prefix><role> in SSM (user_passwords_store.yaml)
      → human (superuser flag and role memberships from the live role)
    - everything else → app

Library code lives in cnpg_users/; tests: uv run --with pytest --with ruamel.yaml pytest tests
"""

import argparse
import sys
from pathlib import Path

from cnpg_users.config import list_db_names, load_db_config, load_ssm_settings
from cnpg_users.importer import cmd_import
from cnpg_users.sync import cmd_sync_grants
from cnpg_users.users import cmd_sync_users


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage CNPG human users and app grants")
    commands = parser.add_subparsers(dest="command", required=True)

    imp = commands.add_parser("import", help="explore live users/roles/grants for a database")
    imp.add_argument("db", help="database name (e.g. cloudnative-pg.my-app.prod) or its dbs/ file path")
    imp.add_argument("--write", action="store_true", help="update humans/apps/grants in the db file")

    sync = commands.add_parser(
        "sync-grants",
        help="print SQL to sync live grants with config (does not execute); exit 3 on drift",
    )
    sync.add_argument("db", nargs="*", help="database names or dbs/ file paths")
    sync.add_argument("--all", action="store_true", help="every dbs/*.yaml")

    users = commands.add_parser(
        "sync-users",
        help="print the values.yaml diff and ALTER ROLE statements that make users match the config; exit 3 on drift",
    )
    users.add_argument("db", nargs="*", help="database names or dbs/ file paths")
    users.add_argument("--all", action="store_true", help="every dbs/*.yaml")
    users.add_argument("--apply", action="store_true",
                       help="open a PR per values.yaml, wait for the roles, then set passwords")
    users.add_argument("--apply-timeout", type=float, default=180, metavar="SECONDS",
                       help="how long to wait for the PR to be merged and synced (default: 180)")

    args = parser.parse_args()
    root = Path(__file__).parent
    if args.command == "import":
        cmd_import(load_db_config(root, args.db), load_ssm_settings(root), args.write)
        return

    if args.all == bool(args.db):
        (sync if args.command == "sync-grants" else users).error("give database names or --all")
    if args.command == "sync-grants":
        run = cmd_sync_grants
    else:
        ssm = load_ssm_settings(root)
        run = lambda db: cmd_sync_users(db, ssm, args.apply, args.apply_timeout)  # noqa: E731
    drift = False
    for i, name in enumerate(list_db_names(root) if args.all else args.db):
        if i:
            print()
        drift |= run(load_db_config(root, name))
    sys.exit(3 if drift else 0)


if __name__ == "__main__":
    main()
