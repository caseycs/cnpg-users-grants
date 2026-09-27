"""End-to-end against a real CNPG operator (task test:integration).

A throwaway namespace gets a 1-instance Cluster with two managed roles: an app
with a passwordSecret and a human without a password. The tool's commands run
in-process against it; only GitHub (values.yaml) and AWS SSM are faked."""

from __future__ import annotations

import base64
import io
import os
import time
import uuid

import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.stream import stream as k8s_stream
from ruamel.yaml import YAML

from cnpg_users import importer, users
from cnpg_users.cluster import Cluster
from cnpg_users.config import SsmSettings, load_db_config
from cnpg_users.github import RepoFile
from cnpg_users.passwords import password_matches
from cnpg_users.sync import apply_grants, cmd_sync_grants

pytestmark = pytest.mark.integration

CONTEXT = os.environ.get("CNPG_IT_CONTEXT")
KEEP = os.environ.get("CNPG_IT_KEEP")  # keep the namespace for debugging
CLUSTER = "pg"
DATABASE = "app"  # CNPG's default initdb database

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


def wait(what: str, check, timeout: float = 600, every: float = 3):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(every)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not CONTEXT:
        pytest.skip("set CNPG_IT_CONTEXT (task test:integration does)")
    api = k8s_config.new_client_from_config(context=CONTEXT)
    core, custom = k8s_client.CoreV1Api(api), k8s_client.CustomObjectsApi(api)
    ns = f"cnpg-users-it-{uuid.uuid4().hex[:6]}"
    core.create_namespace({"metadata": {"name": ns}})
    try:
        core.create_namespaced_secret(ns, {
            "metadata": {"name": "webapp-secret"}, "type": "kubernetes.io/basic-auth",
            "data": {"username": base64.b64encode(b"webapp").decode(), "password": base64.b64encode(b"app-pw").decode()},
        })
        custom.create_namespaced_custom_object("postgresql.cnpg.io", "v1", ns, "clusters", {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": CLUSTER},
            "spec": {
                "instances": 1,
                "storage": {"size": "1Gi"},
                "managed": {"roles": [
                    {"name": "webapp", "ensure": "present", "login": True, "superuser": False,
                     "passwordSecret": {"name": "webapp-secret"}},
                    {"name": "alice", "ensure": "present", "login": True, "superuser": False,
                     "inRoles": ["pg_read_all_data"]},
                ]},
            },
        })

        def ready():
            status = custom.get_namespaced_custom_object(
                "postgresql.cnpg.io", "v1", ns, "clusters", CLUSTER).get("status") or {}
            return status.get("readyInstances") == 1 and status.get("currentPrimary")
        wait("the CNPG cluster to be ready", ready)

        root = tmp_path_factory.mktemp("config")
        (root / "dbs").mkdir()
        (root / "dbs" / "pg.it.kind.yaml").write_text(
            f"context: {CONTEXT}\nnamespace: {ns}\ncluster: {CLUSTER}\n"
            f"repo: example/gitops\nvalues_file: it/values.yaml\n"
        )
        cluster = Cluster(load_db_config(root, "pg.it.kind"))
        wait("managed roles", lambda: cluster.existing_roles(["webapp", "alice"]) == {"webapp", "alice"}, every=2)
        cluster.psql(SETUP_SQL, database=DATABASE)
        yield {"root": root, "ns": ns, "core": core, "cluster": cluster}
    finally:
        if not KEEP:
            core.delete_namespace(ns)


def db(env):
    return load_db_config(env["root"], "pg.it.kind")


def grants_of(env, grantee):
    doc = YAML(typ="safe").load(db(env).path)
    return doc["grants"][DATABASE][grantee]


def sync(env):
    out = io.StringIO()
    drift, plan = cmd_sync_grants(db(env), out=out)
    return drift, plan, out.getvalue()


def test_import_classifies_roles_and_collapses_grants(env, monkeypatch):
    monkeypatch.setattr(importer, "ssm_users", lambda settings: {"alice"})
    importer.cmd_import(db(env), SsmSettings(), write=True)

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
    monkeypatch.setattr(users, "get_file", lambda repo, path: RepoFile(VALUES_YAML, "sha"))
    monkeypatch.setattr(users, "ssm_passwords", lambda settings, names: {"alice": "alice-pw-1"})
    out = io.StringIO()
    drift, plan = users.plan_users(db(env), SsmSettings(), out=out)
    assert drift
    assert "alice: no password set" in out.getvalue()
    assert "alice-pw-1" not in out.getvalue()

    assert users.set_passwords(plan, SsmSettings()) is False
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
    monkeypatch.setattr(users, "get_file", lambda repo, path: RepoFile(VALUES_YAML, "sha"))
    monkeypatch.setattr(users, "ssm_passwords", lambda settings, names: {})
    path = db(env).path
    path.write_text(path.read_text().replace(
        "  - name: alice\n    superuser: false\n    roles:\n      - pg_read_all_data\n",
        "  - name: alice\n    ensure: absent\n",
    ))
    out = io.StringIO()
    drift, plan = users.plan_users(db(env), SsmSettings(), out=out)
    assert drift and plan.change.absent == ["alice"]
    assert "+    ensure: absent" in out.getvalue()


def test_drop_blockers_are_reported(env):
    env["cluster"].psql("CREATE TABLE public.alice_notes (id int); ALTER TABLE public.alice_notes OWNER TO alice;",
                        database=DATABASE)
    rows = env["cluster"].drop_blockers(["alice"])
    by_db = {r["database"]: r for r in rows}
    assert by_db[DATABASE]["owned"] >= 1
    assert by_db[DATABASE]["db_owner"] == "app"
