"""Manage CNPG human users and app grants across clusters.

Config layout (read from the current directory):
    user_passwords_store.yaml
                      aws_profile, aws_region, aws_ssm_prefix of the SSM
                      parameters holding human passwords (used by import)
    dbs/<db>.yaml     one file per database: context, namespace, cluster,
                      repo, values_file, values_roles_path, ignored_grantees,
                      humans, apps,
                      grants

Commands:
    import <db>        Explore live roles and grants, classify every
                       non-system role as human or app, print a report and a
                       diff against dbs/<db>.yaml; --write updates it.
    sync-grants [<db>...] [--parallel N] [--apply [--yes]]
                       (default: every dbs/*.yaml; N at a time, default 4,
                       printed in order)
                       Print the GRANT/REVOKE SQL that makes live grants match
                       grants: in dbs/<db>.yaml; --apply runs it after a
                       confirmation (--yes skips it). Exit code:
                       0 in sync, 3 drift found, 1/2 error/usage.
    sync-users [<db>...] [--parallel N]
                       Print the diff that makes the CNPG roles list in the
                       repo's values.yaml (at values_roles_path) match
                       humans:/apps: (humans present with their
                       superuser/roles, every other non-app role ensure:
                       absent), and ALTER ROLE statements for humans whose
                       password differs from SSM (as a SCRAM verifier, never
                       the plaintext). Same exit codes as sync-grants.
                       --apply, after reading every db: one PR per repo with
                       all its values.yaml changes, labeled
                       cnpg-users-grants (an open PR with that label is
                       rewritten, else branch cnpg-users/sync gets a new
                       one); one wait of --apply-timeout seconds (default
                       180) for the roles to be created/dropped on every
                       cluster once merged and synced; then the password
                       statements db by db.

Table and sequence grants to a db file's ignored_grantees are left out of both
commands; their schema, database and default privileges are still managed.

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
import os
import sys
from pathlib import Path

from .config import list_db_names, load_db_config, load_ssm_settings
from .importer import cmd_import
from .sync import apply_grants, cmd_sync_grants
from .runner import MAX_WORKERS, run_in_order
from .people import cmd_user
from .users import apply_all, plan_users


def positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return n


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage CNPG human users and app grants")
    commands = parser.add_subparsers(dest="command", required=True)

    imp = commands.add_parser("import", help="explore live users/roles/grants for a database")
    imp.add_argument("db", help="database name (e.g. cloudnative-pg.my-app.prod) or its dbs/ file path")
    imp.add_argument("--write", action="store_true", help="update humans/apps/grants in the db file")
    imp.add_argument("--prune", action="store_true",
                     help="humans exactly as live: drop pending grants/revokes not applied yet")

    sync = commands.add_parser(
        "sync-grants",
        help="print SQL to sync live grants with config; --apply runs it; exit 3 on drift",
    )
    sync.add_argument("db", nargs="*", help="database names or dbs/ file paths (default: every dbs/*.yaml)")
    sync.add_argument("--parallel", type=positive_int, default=MAX_WORKERS, metavar="N",
                      help=f"how many dbs to read at once (default: {MAX_WORKERS})")
    sync.add_argument("--apply", action="store_true", help="run the SQL (asks for confirmation first)")
    sync.add_argument("--yes", action="store_true", help="with --apply: don't ask")

    users = commands.add_parser(
        "sync-users",
        help="print the values.yaml diff and ALTER ROLE statements that make users match the config; exit 3 on drift",
    )
    users.add_argument("db", nargs="*", help="database names or dbs/ file paths (default: every dbs/*.yaml)")
    users.add_argument("--parallel", type=positive_int, default=MAX_WORKERS, metavar="N",
                       help=f"how many dbs to read at once (default: {MAX_WORKERS})")
    users.add_argument("--apply", action="store_true",
                       help="open one PR per repo with the values.yaml changes, wait for the roles, then set passwords")
    users.add_argument("--apply-timeout", type=float, default=180, metavar="SECONDS",
                       help="how long to wait for the PR to be merged and synced (default: 180)")

    user = commands.add_parser("user", help="grant/revoke/offboard humans in the db files (config only; apply with sync-users)")
    user_cmds = user.add_subparsers(dest="action", required=True)
    g = user_cmds.add_parser("grant", help="make NAME a human in these db files")
    g.add_argument("name")
    g.add_argument("db", nargs="+", help="database names or dbs/ file paths")
    g.add_argument("--role", action="append", metavar="ROLE",
                   help="role to be a member of, repeatable (default: pg_read_all_data; none with --superuser)")
    g.add_argument("--superuser", action="store_true")
    r = user_cmds.add_parser("revoke", help="mark NAME ensure: absent in these db files and drop its grants")
    r.add_argument("name")
    r.add_argument("db", nargs="+", help="database names or dbs/ file paths")
    o = user_cmds.add_parser("offboard", help="revoke NAME in every db file it's in")
    o.add_argument("name")
    ls = user_cmds.add_parser("list", help="humans across all db files")
    ls.add_argument("name", nargs="?")

    args = parser.parse_args()
    root = Path.cwd()  # configs live in the directory the tool is run from
    if args.command == "user":
        sys.exit(cmd_user(args, root))
    if args.command == "import":
        cmd_import(load_db_config(root, args.db), load_ssm_settings(root), args.write, args.prune)
        return

    dbs = [load_db_config(root, name) for name in (args.db or list_db_names(root))]
    if args.command == "sync-grants":
        sys.exit(run_in_order(
            dbs, cmd_sync_grants,
            after=(lambda plans: apply_grants(plans, args.yes)) if args.apply else None,
            max_workers=args.parallel,
        ))
    ssm = load_ssm_settings(root)
    sys.exit(run_in_order(
        dbs,
        lambda db, out: plan_users(db, ssm, args.apply, out=out),
        after=(lambda plans: apply_all(plans, ssm, args.apply_timeout)) if args.apply else None,
        max_workers=args.parallel,
    ))

def run() -> None:
    """Console entry point (`cnpg-users`)."""
    try:
        main()
    except KeyboardInterrupt:
        sys.stdout.flush()
        print("\ninterrupted", file=sys.stderr, flush=True)
        # exit now: reads still in flight in worker threads (kubectl exec,
        # psql) can't be interrupted and would hold up a normal exit
        os._exit(130)
