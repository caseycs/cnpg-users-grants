"""End-to-end against a real CNPG operator (task test:integration).

A throwaway namespace gets a 1-instance Cluster with two managed roles: an app
with a passwordSecret and a human without a password. The tool's commands run
in-process against it; only GitHub (values.yaml) and AWS SSM are faked."""

from __future__ import annotations

import io

import pytest
from kubernetes.stream import stream as k8s_stream

from cnpg_users import importer, users
from cnpg_users.github import RepoFile
from cnpg_users.passwords import password_matches
from cnpg_users.sync import apply_grants
from helpers import DATABASE, cnpg_cluster, db, fake_store, grants_of, sync

pytestmark = pytest.mark.integration

SETUP_SQL = """
CREATE SCHEMA reports;
CREATE TABLE public.events (id bigserial PRIMARY KEY, name text);
CREATE TABLE public.lookups (id bigserial PRIMARY KEY);
CREATE TABLE public.measurements (id bigserial, taken date NOT NULL) PARTITION BY RANGE (taken);
CREATE TABLE public.measurements_2026_01 PARTITION OF public.measurements FOR VALUES FROM ('2026-01-01') TO ('2026-02-01');
CREATE TABLE public.measurements_2026_02 PARTITION OF public.measurements FOR VALUES FROM ('2026-02-01') TO ('2026-03-01');
GRANT USAGE ON SCHEMA reports TO webapp;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO webapp;
GRANT INSERT ON TABLE public.events TO webapp;
GRANT USAGE ON SEQUENCE public.events_id_seq TO webapp;
GRANT UPDATE ON TABLE public.measurements, public.measurements_2026_01, public.measurements_2026_02 TO webapp;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA reports GRANT SELECT ON TABLES TO webapp;
"""

VALUES_YAML = """\
roles:

  - name: webapp
    ensure: present
    login: true
    superuser: false
    passwordSecret:
      name: webapp-secret

  - name: alice
    ensure: present
    login: true
    superuser: false
    inRoles:
      - pg_read_all_data
"""


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    roles = [
        {"name": "webapp", "ensure": "present", "login": True, "superuser": False,
         "passwordSecret": {"name": "webapp-secret"}},
        {"name": "alice", "ensure": "present", "login": True, "superuser": False, "inRoles": ["pg_read_all_data"]},
    ]
    with cnpg_cluster(tmp_path_factory.mktemp("config"), roles, {"webapp-secret": ("webapp", "app-pw")}) as env:
        env["cluster"].psql(SETUP_SQL, database=DATABASE)
        yield env


def test_import_classifies_roles_and_collapses_grants(env, monkeypatch):
    importer.cmd_import(db(env), fake_store({"alice": "unused"}), write=True)

    config = db(env)
    assert config.humans == [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}]
    assert "webapp" in config.apps
    assert grants_of(env, "webapp") == [
        "GRANT USAGE ON SCHEMA reports TO webapp;",
        "GRANT USAGE ON SEQUENCE public.events_id_seq TO webapp;",
        "GRANT SELECT ON ALL TABLES IN SCHEMA public TO webapp;",   # every table in public
        "GRANT INSERT ON TABLE public.events TO webapp;",
        "GRANT UPDATE ON TABLE public.measurements TO webapp;",     # covers both partitions
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA reports GRANT SELECT ON TABLES TO webapp;",
    ]


def test_imported_grants_are_in_sync(env):
    drift, _, out = sync(env)
    assert not drift, out
    assert out.strip().endswith(": in sync")


def test_config_change_is_planned_and_applied(env):
    path = db(env).path
    text = path.read_text()
    path.write_text(text.replace(
        "      - GRANT USAGE ON SCHEMA reports TO webapp;\n",
        "      - GRANT DELETE ON TABLE public.lookups TO webapp;\n",
    ))
    drift, plan, out = sync(env)
    assert drift
    assert "GRANT DELETE ON TABLE public.lookups TO webapp;" in out
    assert "REVOKE USAGE ON SCHEMA reports FROM webapp;" in out

    assert apply_grants([plan], assume_yes=True) is False
    assert not sync(env)[0]
    check = env["cluster"].sql(
        "SELECT to_jsonb(ARRAY[has_table_privilege('webapp', 'public.lookups', 'DELETE'),"
        " has_schema_privilege('webapp', 'reports', 'USAGE')])", database=DATABASE)
    assert check == [True, False]


def test_new_partition_gets_the_parents_grants(env):
    env["cluster"].psql("CREATE TABLE public.measurements_2026_03 PARTITION OF public.measurements"
                        " FOR VALUES FROM ('2026-03-01') TO ('2026-04-01');", database=DATABASE)
    drift, plan, out = sync(env)
    assert drift
    assert "GRANT SELECT, UPDATE ON TABLE public.measurements_2026_03 TO webapp;" in out
    assert apply_grants([plan], assume_yes=True) is False


def test_human_password_is_set_as_a_verifier_and_works(env, monkeypatch):
    monkeypatch.setattr(users, "get_file", lambda db, path: RepoFile(VALUES_YAML, "sha"))
    store = fake_store({"alice": "alice-pw-1"})
    out = io.StringIO()
    drift, plan = users.plan_users(db(env), store, out=out)
    assert drift
    assert "alice: no password set" in out.getvalue()
    assert "alice-pw-1" not in out.getvalue()

    assert users.set_passwords(plan, store) is False
    verifier = env["cluster"].password_verifiers(["alice"])["alice"]
    assert verifier.startswith("SCRAM-SHA-256$") and password_matches("alice-pw-1", "alice", verifier)

    # log in over TCP with the password, as a person would
    pod = env["cluster"].primary
    resp = k8s_stream(
        env["core"].connect_get_namespaced_pod_exec, pod, env["ns"], container="postgres",
        command=["sh", "-c", "PGPASSWORD=alice-pw-1 psql -h 127.0.0.1 -U alice -d app -tAc 'select current_user'"],
        stderr=True, stdin=False, stdout=True, tty=False,
    )
    assert resp.strip() == "alice"


def test_values_diff_marks_revoked_human_absent(env, monkeypatch):
    monkeypatch.setattr(users, "get_file", lambda db, path: RepoFile(VALUES_YAML, "sha"))

    path = db(env).path
    path.write_text(path.read_text().replace(
        "  - name: alice\n    superuser: false\n    roles:\n      - pg_read_all_data\n",
        "  - name: alice\n    ensure: absent\n",
    ))
    out = io.StringIO()
    drift, plan = users.plan_users(db(env), fake_store({}), out=out)
    assert drift and plan.change.absent == ["alice"]
    assert "+    ensure: absent" in out.getvalue()


def test_drop_blockers_are_reported(env):
    env["cluster"].psql("CREATE TABLE public.alice_notes (id int); ALTER TABLE public.alice_notes OWNER TO alice;",
                        database=DATABASE)
    rows = env["cluster"].drop_blockers(["alice"])
    by_db = {r["database"]: r for r in rows}
    assert by_db[DATABASE]["owned"] >= 1
    assert by_db[DATABASE]["db_owner"] == "app"


def test_sync_reports_users_and_grants_together(env, monkeypatch):
    from cnpg_users.sync_all import cmd_sync
    monkeypatch.setattr(users, "get_file", lambda db, path: RepoFile(VALUES_YAML, "sha"))
    out = io.StringIO()
    drift, plan = cmd_sync(db(env), fake_store({}), out=out)
    text = out.getvalue()
    assert drift and text.count(db(env).header) == 1
    assert "+    ensure: absent" in text                            # users part: alice is being removed
    assert "before these roles can be dropped" in text              # ... but still owns a table
    # grants part: the table the previous test created is missing webapp's ON ALL TABLES grant
    assert "Database: app\n    To add:\n      GRANT SELECT ON TABLE public.alice_notes TO webapp;" in text
    assert plan.users is not None and [d.database for d in plan.grants.databases] == ["app"]
