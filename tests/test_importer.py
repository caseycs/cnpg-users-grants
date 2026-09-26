from pathlib import Path

import pytest

from cnpg_users.config import SsmSettings, list_db_names, load_db_config, load_ssm_settings
from cnpg_users.grants import Catalog, drop_ignored, live_grants, plan
from cnpg_users.importer import build_grants_config, classify_roles, update_config

DB_YAML = """\
online: true
context: ctx
namespace: ns
cluster: pg
ignored_grantees:
  - pg_monitor
humans:
  - name: known_human
    superuser: false
    roles:
      - pg_read_all_data
apps:
  - known_app
"""


def write_config(tmp_path: Path, db_yaml: str = DB_YAML):
    (tmp_path / "dbs").mkdir()
    (tmp_path / "dbs" / "pg.ns.env.yaml").write_text(db_yaml)
    return load_db_config(tmp_path, "pg.ns.env")


def acl(**rows):
    base = dict(schema_grants=[], object_grants=[],
                default_acls=[], db_grants=[])
    return {**base, **rows}


def obj(grantee, schema, name, *privs, kind="table"):
    return [dict(grantee=grantee, schema=schema, object=name, kind=kind, privilege=p) for p in privs]


def role(name, super=False, in_roles=()):
    return dict(name=name, super=super, in_roles=list(in_roles))


def test_load_db_config(tmp_path):
    db = write_config(tmp_path)
    assert (db.name, db.context, db.namespace, db.cluster) == ("pg.ns.env", "ctx", "ns", "pg")
    assert db.ignored_grantees == ["pg_monitor"]


def test_list_db_names(tmp_path):
    write_config(tmp_path)
    (tmp_path / "dbs" / "a.yaml").write_text(DB_YAML)
    (tmp_path / "dbs" / "notes.txt").write_text("")
    assert list_db_names(tmp_path) == ["a", "pg.ns.env"]


def test_classify_roles():
    cluster_cr = {
        "spec": {
            "managed": {"roles": [
                {"name": "declared_no_ssm"},
                {"name": "secret_app", "passwordSecret": {"name": "s"}},
                {"name": "gone", "ensure": "absent"},
            ]},
            "bootstrap": {"initdb": {"owner": "owner_app", "secret": {"name": "s"}}},
        },
        "status": {"managedRolesStatus": {
            "passwordStatus": {"synced_app": {"resourceVersion": "1"}},
            "byStatus": {"reserved": ["reserved_role"]},
        }},
    }
    roles = [role(n) for n in (
        "postgres", "pg_read_all_data", "reserved_role", "crd_app", "synced_app", "secret_app",
        "owner_app", "gone", "declared_no_ssm", "reader", "other",
    )] + [role("admin", super=True), role("writer", in_roles=["pg_write_all_data", "pg_read_all_data"])]
    # an SSM password never overrides a k8s secret
    ssm = {"admin", "reader", "writer", "secret_app", "gone"}
    got = classify_roles(roles, cluster_cr, {"crd_app"}, ssm, say=lambda _: None)
    assert got.humans == [
        {"name": "admin", "superuser": True},
        {"name": "reader", "superuser": False},
        {"name": "writer", "superuser": False, "roles": ["pg_read_all_data", "pg_write_all_data"]},
    ]
    assert got.apps == ["crd_app", "declared_no_ssm", "other", "owner_app", "secret_app", "synced_app"]
    assert got.app_reasons["other"] == "no password in SSM"
    assert sorted(got.skipped) == [
        "gone (ensure: absent)", "pg_read_all_data (system)", "postgres (system)", "reserved_role (system)",
    ]


def test_ssm_settings(tmp_path):
    (tmp_path / "user_passwords_store.yaml").write_text("aws_profile: p\naws_ssm_prefix: /x/\n")
    assert load_ssm_settings(tmp_path) == SsmSettings("p", "eu-central-1", "/x/")
    (tmp_path / "user_passwords_store.yaml").write_text("profile: p\n")
    with pytest.raises(SystemExit, match="unknown keys: profile"):
        load_ssm_settings(tmp_path)


def test_build_grants_config_roundtrips_and_ignores():
    acls = {"app": acl(
        schema_grants=[dict(schema="auth", grantee="auth", privilege="USAGE")],
        object_grants=obj("auth", "auth", "devices", "SELECT") + obj("pg_monitor", "auth", "devices", "SELECT")
        + obj("g", "public", "a", "SELECT") + obj("g", "public", "b", "SELECT"),
        default_acls=[dict(grantor="postgres", schema="public", kind="TABLES", grantee="g", privilege="SELECT")],
        db_grants=[dict(database="app", grantee="auth", privilege="CONNECT")],
    ), "empty": acl()}
    catalogs = {
        "app": Catalog(objects={("table", "auth"): ["devices"], ("table", "public"): ["a", "b"]}),
        "empty": Catalog(),
    }
    live = {d: drop_ignored(live_grants(a), ["pg_monitor"]) for d, a in acls.items()}
    config = build_grants_config(live, catalogs)
    assert config == {"app": {
        "auth": [
            "GRANT USAGE ON SCHEMA auth TO auth;",
            "GRANT SELECT ON TABLE auth.devices TO auth;",
            "GRANT CONNECT ON DATABASE app TO auth;",
        ],
        "g": [
            "GRANT SELECT ON ALL TABLES IN SCHEMA public TO g;",
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO g;",
        ],
    }}
    stmts = [s for per_user in config["app"].values() for s in per_user]
    assert plan(stmts, live["app"], catalogs["app"]) == (set(), set())


def test_update_config(tmp_path):
    db = write_config(tmp_path)
    grants = {"app": {"auth": ["GRANT CONNECT ON DATABASE app TO auth;"]}}
    humans = [{"name": "known_human", "superuser": False, "roles": ["pg_read_all_data"]}]
    before, after = update_config(db, humans, ["a", "known_app"], grants)
    assert before == DB_YAML
    assert after == DB_YAML.replace("  - known_app\n", "  - a\n  - known_app\n") + (
        "grants:\n  app:\n    auth:\n      - GRANT CONNECT ON DATABASE app TO auth;\n"
    )
    # idempotent
    assert update_config(db, humans, ["a", "known_app"], grants) == (after, after)
