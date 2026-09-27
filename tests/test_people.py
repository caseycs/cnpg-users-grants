from argparse import Namespace
from pathlib import Path

import pytest

from cnpg_users.config import load_db_config
from cnpg_users.importer import merge_humans
from cnpg_users.people import cmd_user
from cnpg_users.users import drop_blocker_lines
from cnpg_users.values import sync_roles

DB = """\
context: ctx
namespace: ns
cluster: pg
humans:
  - name: alice
    superuser: false
    roles:
      - pg_read_all_data
apps:
  - webapp
grants:
  app:
    alice:
      - GRANT SELECT ON TABLE public.t TO alice;
    webapp:
      - GRANT SELECT ON TABLE public.t TO webapp;
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "dbs").mkdir()
    (tmp_path / "dbs" / "one.yaml").write_text(DB)
    (tmp_path / "dbs" / "two.yaml").write_text(DB.replace("pg\n", "pg2\n"))
    return tmp_path


def user(root, action, name=None, db=(), role=None, superuser=False):
    return cmd_user(Namespace(action=action, name=name, db=list(db), role=role, superuser=superuser), root)


def humans(root, db):
    return load_db_config(root, db).humans


def test_grant_new_human_default_role(root, capsys):
    user(root, "grant", "bob", ["one"])
    assert humans(root, "one") == [
        {"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]},
        {"name": "bob", "superuser": False, "roles": ["pg_read_all_data"]},
    ]
    assert humans(root, "two") == [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}]
    out = capsys.readouterr().out
    assert "+  - name: bob" in out and "sync-users --apply" in out


def test_grant_explicit_roles_and_superuser(root):
    user(root, "grant", "alice", ["one"], role=["pg_read_all_data", "pg_write_all_data"])
    assert humans(root, "one")[0] == {"name": "alice", "superuser": False,
                                      "roles": ["pg_read_all_data", "pg_write_all_data"]}
    user(root, "grant", "carol", ["one"], superuser=True)
    assert {"name": "carol", "superuser": True} in humans(root, "one")


def test_grant_is_idempotent(root, capsys):
    user(root, "grant", "alice", ["one"])
    assert "one: no changes" in capsys.readouterr().out
    assert (root / "dbs" / "one.yaml").read_text() == DB


def test_grant_refuses_apps_and_bad_names(root):
    with pytest.raises(SystemExit, match="apps"):
        user(root, "grant", "webapp", ["one"])
    with pytest.raises(SystemExit, match="invalid role name"):
        user(root, "grant", "Bob", ["one"])


def test_revoke_marks_absent_and_drops_grants(root):
    user(root, "revoke", "alice", ["one"])
    db = load_db_config(root, "one")
    assert db.humans == [{"name": "alice", "ensure": "absent"}]
    assert db.grants == {"app": {"webapp": ["GRANT SELECT ON TABLE public.t TO webapp;"]}}
    assert humans(root, "two")[0]["name"] == "alice" and "ensure" not in humans(root, "two")[0]


def test_offboard_everywhere_then_nothing_left(root, capsys):
    user(root, "offboard", "alice")
    for db in ("one", "two"):
        assert humans(root, db) == [{"name": "alice", "ensure": "absent"}]
    capsys.readouterr()
    user(root, "offboard", "alice")
    assert "in no db file" in capsys.readouterr().out


def test_list(root, capsys):
    user(root, "revoke", "alice", ["two"])
    capsys.readouterr()
    user(root, "list")
    assert capsys.readouterr().out == (
        "alice  one: pg_read_all_data\n"
        "alice  two: ensure: absent (being removed)\n"
    )


def test_merge_humans_keeps_pending_edits():
    config = [
        {"name": "alice", "superuser": False, "roles": ["pg_write_all_data"]},  # role change not applied
        {"name": "bob", "superuser": False},                                     # granted, not created yet
        {"name": "carol", "ensure": "absent"},                                   # revoked, still live
        {"name": "dave", "ensure": "absent"},                                    # revoked, dropped
    ]
    live = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]},
            {"name": "carol", "superuser": False}, {"name": "erin", "superuser": True}]
    out, notes = merge_humans(config, live, {"alice", "carol", "erin"})
    assert out == [config[0], config[1], config[2], {"name": "erin", "superuser": True}]
    assert [n.split(":")[0] for n in notes] == ["alice", "bob", "carol", "dave", "erin"]
    assert merge_humans(config, live, {"alice", "carol", "erin"}, prune=True)[0] == sorted(live, key=lambda h: h["name"])


def test_drop_blocker_lines():
    rows = [{"role": "alice", "database": "app", "db_owner": "app_owner", "owned": 2, "privileges": 5}]
    assert drop_blocker_lines(rows) == [
        "  before these roles can be dropped, run (as postgres, in each database):",
        "    app: alice owns 2, holds 5 privilege(s)",
        '      REASSIGN OWNED BY "alice" TO "app_owner"; DROP OWNED BY "alice";',
    ]
    assert drop_blocker_lines([]) == []


def test_absent_human_becomes_absent_in_values(root):
    user(root, "revoke", "alice", ["one"])
    db = load_db_config(root, "one")
    values = "roles:\n  - name: alice\n    ensure: present\n    login: true\n    superuser: false\n"
    change = sync_roles(values, db.humans, db.apps)
    assert change.absent == ["alice"]
    assert "ensure: absent" in change.text
