import base64
import hashlib

import pytest

from cnpg_users.passwords import generate_password, password_matches, scram_verifier
from cnpg_users.users import password_plan
from cnpg_users.values import sync_roles

VALUES = """\
instances: 1
roles:

  # apps

  - name: webapp
    ensure: present
    login: true
    superuser: false
    passwordSecret:
      name: webapp-secret

  - name: webapp_owner
    ensure: present
    login: true
    superuser: true

  # humans

  - name: alice
    ensure: present
    login: true
    superuser: false
    inRoles:
      - pg_read_all_data

  - name: bob
    ensure: present
    login: true
    superuser: true

  - name: gone
    ensure: absent
    login: true
    superuser: false

backup:
  enabled: true
"""


def test_scram_verifier_matches_postgres_format():
    v = scram_verifier("secret", salt=b"0123456789abcdef")
    assert v.startswith("SCRAM-SHA-256$4096:" + base64.b64encode(b"0123456789abcdef").decode() + "$")
    assert password_matches("secret", "u", v) is True
    assert password_matches("other", "u", v) is False


def test_password_matches_md5_and_unknown():
    md5 = "md5" + hashlib.md5(b"secretu").hexdigest()
    assert password_matches("secret", "u", md5) is True
    assert password_matches("secret", "v", md5) is False
    assert password_matches("secret", "u", "") is False
    assert password_matches("secret", "u", "plain") is None


def test_sync_roles_unchanged():
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]},
              {"name": "bob", "superuser": True}]
    change = sync_roles(VALUES, humans, ["webapp", "webapp_owner"])
    assert change.text == VALUES and change.summary == []


def test_sync_roles_absent_human_is_dropped():
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}, {"name": "bob", "ensure": "absent"}]
    change = sync_roles(VALUES, humans, ["webapp", "webapp_owner"])
    assert change.absent == ["bob"] and change.unlisted == []
    assert change.text == VALUES.replace(
        "  - name: bob\n    ensure: present\n", "  - name: bob\n    ensure: absent\n")


def test_sync_roles_unlisted_roles_are_only_reported():
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}]
    # bob isn't a human any more but wasn't marked absent; webapp_owner isn't in apps
    change = sync_roles(VALUES, humans, ["webapp"])
    assert change.text == VALUES
    assert change.unlisted == ["webapp_owner", "bob"] and change.absent == []


def test_sync_roles_updates_and_appends():
    humans = [
        {"name": "alice", "superuser": True},                                          # promoted, roles dropped
        {"name": "bob", "superuser": True},
        {"name": "gone", "superuser": False, "roles": ["pg_read_all_data"]},           # re-added
        {"name": "carol", "superuser": False, "roles": ["pg_read_all_data", "pg_write_all_data"]},
    ]
    change = sync_roles(VALUES, humans, ["webapp", "webapp_owner"])
    assert change.summary == ["add carol", "update alice", "update gone"]
    assert change.text == VALUES.replace(
        "  - name: alice\n    ensure: present\n    login: true\n    superuser: false\n    inRoles:\n      - pg_read_all_data\n",
        "  - name: alice\n    ensure: present\n    login: true\n    superuser: true\n",
    ).replace(
        "  - name: gone\n    ensure: absent\n    login: true\n    superuser: false\n",
        "  - name: gone\n    ensure: present\n    login: true\n    superuser: false\n    inRoles:\n      - pg_read_all_data\n",
    ).replace(
        "\nbackup:",
        "\n  - name: carol\n    ensure: present\n    login: true\n    superuser: false\n"
        "    inRoles:\n      - pg_read_all_data\n      - pg_write_all_data\n\nbackup:",
    )


def test_sync_roles_requires_roles_list():
    with pytest.raises(ValueError):
        sync_roles("instances: 1\n", [], [])


def test_password_plan():
    verifiers = {"ok": scram_verifier("pw-ok"), "old": scram_verifier("previous"), "nopw": "", "nossm": ""}
    passwords = {"ok": "pw-ok", "old": "pw-new", "nopw": "pw", "new": "pw"}
    statements, status = password_plan(["ok", "old", "nopw", "nossm", "new"], passwords, verifiers)
    assert status == [
        "ok: in sync",
        "old: differs",
        "nopw: no password set",
        "nossm: no password in SSM (--apply generates one)",
        "new: role not created yet — deploy values.yaml, then re-run",
    ]
    assert [s.split(" PASSWORD ")[0] for s in statements] == ['ALTER ROLE "old"', 'ALTER ROLE "nopw"']
    for stmt, pw in zip(statements, ["pw-new", "pw"]):
        verifier = stmt.split("'")[1]
        assert f"'{pw}'" not in stmt
        assert password_matches(pw, "", verifier) is True


def test_sync_roles_keeps_group_comment_when_dropping_roles():
    text = VALUES.replace("      - pg_read_all_data\n\n  - name: bob", "      - pg_read_all_data\n\n  # superusers\n\n  - name: bob")
    humans = [{"name": "alice", "superuser": False}, {"name": "bob", "superuser": True}]
    out = sync_roles(text, humans, ["webapp", "webapp_owner"]).text
    assert out == text.replace("    superuser: false\n    inRoles:\n      - pg_read_all_data\n\n  # superusers",
                               "    superuser: false\n\n  # superusers")


def test_generate_password():
    pw = generate_password()
    assert len(pw) == 32 and pw.isalnum()
    assert any(c.isupper() for c in pw) and any(c.isdigit() for c in pw)
    assert generate_password() != pw


def nested(text: str) -> str:
    """VALUES with its roles: list moved under cluster: (one level deeper)."""
    head, rest = text.split("roles:\n", 1)
    roles_block, tail = rest.split("\nbackup:", 1)
    indented = "\n".join(("  " + line) if line else line for line in roles_block.split("\n"))
    return f"{head}cluster:\n  instances: 3\n  roles:\n{indented}\nbackup:{tail}"


def test_sync_roles_nested_path_unchanged_and_absent():
    text = nested(VALUES)
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}, {"name": "bob", "superuser": True}]
    assert sync_roles(text, humans, ["webapp", "webapp_owner"], "cluster.roles").text == text
    change = sync_roles(text, [humans[0], {"name": "bob", "ensure": "absent"}], ["webapp", "webapp_owner"], "cluster.roles")
    assert change.absent == ["bob"]
    assert change.text == text.replace("    - name: bob\n      ensure: present\n", "    - name: bob\n      ensure: absent\n")


def test_sync_roles_nested_path_appends_with_blank_lines():
    text = nested(VALUES)
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}, {"name": "bob", "superuser": True},
              {"name": "carol", "superuser": False}]
    out = sync_roles(text, humans, ["webapp", "webapp_owner"], "cluster.roles").text
    assert out == text.replace(
        "\nbackup:",
        "\n    - name: carol\n      ensure: present\n      login: true\n      superuser: false\n\nbackup:",
    )


def test_sync_roles_missing_path():
    with pytest.raises(ValueError, match="no list at cluster.roles"):
        sync_roles(VALUES, [], [], "cluster.roles")


def test_sync_roles_bare_app_names_untouched():
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}, {"name": "bob", "superuser": True}]
    assert sync_roles(VALUES, humans, ["webapp", "webapp_owner"]).text == VALUES


def test_sync_roles_never_touches_apps():
    humans = [{"name": "alice", "superuser": False, "roles": ["pg_read_all_data"]}, {"name": "bob", "superuser": True}]
    # listing an app that has no values entry adds nothing; its entries stay as they are
    assert sync_roles(VALUES, humans, ["webapp", "webapp_owner", "not_in_values"]).text == VALUES
