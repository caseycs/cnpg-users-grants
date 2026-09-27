"""import against a real CNPG cluster: every kind of role, ignored grantees, a
second database, the round-trip warning, pending edits and --prune."""

from __future__ import annotations

import pytest

from cnpg_users import importer
from helpers import DATABASE, cnpg_cluster, db, doc, fake_store, grants_of, sync, wait

pytestmark = pytest.mark.integration

APP_SQL = """
CREATE ROLE leaver LOGIN;
CREATE TABLE public.leaver_notes (id int);
ALTER TABLE public.leaver_notes OWNER TO leaver;
CREATE ROLE metrics;
CREATE SCHEMA reports;
CREATE TABLE public.events (id bigserial PRIMARY KEY);
GRANT USAGE ON SCHEMA reports TO metrics;
GRANT SELECT ON TABLE public.events TO metrics;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA reports GRANT SELECT ON TABLES TO metrics;
GRANT SELECT ON TABLE public.events TO webapp;
"""

ANALYTICS_SQL = """
CREATE TABLE public.facts (id int);
CREATE TABLE public.logs (id int, at date NOT NULL) PARTITION BY RANGE (at);
CREATE TABLE public.logs_2026_01 PARTITION OF public.logs FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
GRANT SELECT ON TABLE public.facts TO webapp;
GRANT UPDATE ON TABLE public.logs TO webapp;
"""
# Postgres doesn't copy a parent's grants to its partitions, so webapp holds
# UPDATE on logs but not on logs_2026_01: a state the file format (a parent
# grant covers its partitions) can't express exactly.


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    roles = [
        {"name": "webapp", "ensure": "present", "login": True, "superuser": False,
         "passwordSecret": {"name": "webapp-secret"}},
        {"name": "alice", "ensure": "present", "login": True, "superuser": False, "inRoles": ["pg_read_all_data"]},
        # created below owning a table, so CNPG's DROP ROLE fails and it stays live
        {"name": "leaver", "ensure": "absent"},
    ]
    with cnpg_cluster(tmp_path_factory.mktemp("config"), roles, {"webapp-secret": ("webapp", "app-pw")},
                      db_file_extra="ignored_grantees: [metrics]\n") as env:
        cluster = env["cluster"]
        cluster.psql(APP_SQL, database=DATABASE)
        cluster.psql("CREATE DATABASE analytics;")  # can't run inside a transaction with others
        cluster.psql("GRANT CONNECT ON DATABASE analytics TO webapp;")
        cluster.psql(ANALYTICS_SQL, database="analytics")
        env["custom"].create_namespaced_custom_object("postgresql.cnpg.io", "v1", env["ns"], "databaseroles", {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "DatabaseRole", "metadata": {"name": "reporter"},
            "spec": {"cluster": {"name": "pg"}, "name": "reporter", "login": True},
        })
        wait("the DatabaseRole", lambda: cluster.existing_roles(["reporter"]) == {"reporter"}, every=2)
        yield env


def run_import(env, capsys, **kwargs) -> str:
    capsys.readouterr()
    importer.cmd_import(db(env), fake_store({"alice": "pw"}), **kwargs)
    return capsys.readouterr().out


def test_every_kind_of_role_is_classified(env, capsys):
    out = run_import(env, capsys, write=True)
    config = db(env)
    assert config.humans == [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}]
    assert {"webapp", "reporter", "metrics"} <= set(config.apps)
    assert "webapp: app (password via k8s secret)" in out
    assert "reporter: app (DatabaseRole CR, managed elsewhere)" in out
    assert "metrics: app (no password in the store)" in out
    # CNPG's bootstrap owner and a role being dropped are neither humans nor apps
    for name, why in (("app", "system"), ("leaver", "ensure: absent")):
        assert f"  {name} ({why})" in out
        assert name not in config.apps and name not in {h["name"] for h in config.humans}
    assert cnpg_live(env, "leaver")  # still there: CNPG couldn't drop it


def cnpg_live(env, role: str) -> bool:
    return env["cluster"].existing_roles([role]) == {role}


def test_ignored_grantee_keeps_schema_and_default_privileges(env):
    assert grants_of(env, "metrics") == [
        "GRANT USAGE ON SCHEMA reports TO metrics;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA reports GRANT SELECT ON TABLES TO metrics;",
    ]
    _, _, out = sync(env)
    assert "metrics" not in out  # its live table grant is neither re-granted nor revoked


def test_grants_in_a_second_database(env):
    assert set(doc(env)["grants"]) == {DATABASE, "analytics"}
    assert grants_of(env, "webapp", "analytics") == [
        "GRANT SELECT ON TABLE public.facts TO webapp;",
        "GRANT UPDATE ON TABLE public.logs TO webapp;",
        "GRANT CONNECT ON DATABASE analytics TO webapp;",
    ]


def test_round_trip_warning_and_the_sql_it_implies(env, capsys):
    out = run_import(env, capsys, write=False)
    assert "! analytics: generated grants don't round-trip; sync-grants would run:" in out
    assert "GRANT UPDATE ON TABLE public.logs_2026_01 TO webapp;" in out
    drift, _, sync_out = sync(env)
    assert drift
    assert "Database: analytics" in sync_out and f"Database: {DATABASE}" not in sync_out
    assert "GRANT UPDATE ON TABLE public.logs_2026_01 TO webapp;" in sync_out


def test_pending_edits_survive_import_and_prune_drops_them(env, capsys):
    path = db(env).path
    path.write_text(path.read_text().replace(
        "humans:\n  - name: alice\n    superuser: false\n    roles:\n      - pg_read_all_data\n",
        "humans:\n  - name: alice\n    ensure: absent\n  - name: bob\n    superuser: false\n",
    ))
    out = run_import(env, capsys, write=True)
    assert db(env).humans == [{"name": "alice", "ensure": "absent"}, {"name": "bob", "superuser": False}]
    assert "alice: ensure: absent, role still exists (pending drop)" in out
    assert "bob: not created yet (pending)" in out

    run_import(env, capsys, write=True, prune=True)
    assert db(env).humans == [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}]
