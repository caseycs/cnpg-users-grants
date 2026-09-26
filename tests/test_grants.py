import pytest

from cnpg_users.grants import (
    Catalog,
    Grant,
    Stmt,
    drop_ignored,
    expand,
    format_statements,
    live_grants,
    parse_statement,
    plan,
    render,
)

KEYWORDS = frozenset({"order", "user", "select"})


def catalog(objects=None, parents=None):
    return Catalog(objects=objects or {}, parents=parents or {}, keywords=KEYWORDS)


def acl(**rows):
    base = dict(schema_grants=[], object_grants=[],
                default_acls=[], db_grants=[])
    return {**base, **rows}


def obj(grantee, schema, name, *privs, kind="table"):
    return [dict(grantee=grantee, schema=schema, object=name, kind=kind, privilege=p) for p in privs]


def roundtrip(live: set[Grant], cat: Catalog) -> tuple[list[str], set, set]:
    stmts = format_statements(live, cat, compress=True)
    to_grant, to_revoke = plan(stmts, live, cat)
    return stmts, to_grant, to_revoke


# --- quoting / parsing ---------------------------------------------------------

@pytest.mark.parametrize("ident, quoted", [
    ("accounts", "accounts"),
    ("order", '"order"'),
    ("Foo", '"Foo"'),
    ("a-b", '"a-b"'),
    ('we"ird', '"we""ird"'),
    ("x$1", "x$1"),
])
def test_qi(ident, quoted):
    assert catalog().qi(ident) == quoted


@pytest.mark.parametrize("st", [
    Stmt("schema", "", "auth", "auth", frozenset({"USAGE"})),
    Stmt("database", "", "webapp", "dashboards", frozenset({"CONNECT"})),
    Stmt("table", "public", "order", "PUBLIC", frozenset({"SELECT"})),
    Stmt("table", "My Schema", "Foo", "Some-User", frozenset({"SELECT", "INSERT"})),
    Stmt("sequence", "public", "a_id_seq", "x", frozenset({"SELECT", "USAGE"})),
    Stmt("all_tables", "dashboards", "", "dashboards", frozenset({"SELECT"})),
    Stmt("all_sequences", "public", "", "x", frozenset({"USAGE"})),
    Stmt("default", "public", "SEQUENCES", "workers", frozenset({"SELECT", "USAGE"}), "postgres"),
    Stmt("default", "", "TABLES", "user", frozenset({"SELECT"}), "webapp"),
    Stmt("default", "", "LARGE OBJECTS", "x", frozenset({"SELECT"}), "webapp"),
])
def test_render_parse_roundtrip(st):
    assert parse_statement(render(st, catalog())) == st


def test_render_revoke_uses_from():
    cat = catalog()
    assert render(Stmt("table", "public", "t", "x", frozenset({"SELECT"})), cat, revoke=True) \
        == "REVOKE SELECT ON TABLE public.t FROM x;"
    assert render(Stmt("default", "", "TABLES", "PUBLIC", frozenset({"SELECT"}), "r"), cat, revoke=True) \
        == "ALTER DEFAULT PRIVILEGES FOR ROLE r REVOKE SELECT ON TABLES FROM PUBLIC;"


def test_public_is_not_quoted():
    assert render(Stmt("table", "public", "t", "PUBLIC", frozenset({"SELECT"})), catalog()) \
        == "GRANT SELECT ON TABLE public.t TO PUBLIC;"


def test_parse_normalizes():
    assert parse_statement("grant select, temp on database App to public") \
        == Stmt("database", "", "app", "PUBLIC", frozenset({"SELECT", "TEMPORARY"}))
    assert parse_statement("ALTER DEFAULT PRIVILEGES FOR ROLE r GRANT EXECUTE ON ROUTINES TO x;").name \
        == "FUNCTIONS"


@pytest.mark.parametrize("bad", [
    "GRANT SELECT ON t TO x;",                       # unqualified table
    "GRANT ALL ON TABLE public.t TO x;",             # ALL PRIVILEGES not supported
    "GRANT SELECT ON FUNCTION public.f() TO x;",
    "REVOKE SELECT ON TABLE public.t FROM x;",
    "GRANT r1 TO x;",
])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_statement(bad)


def test_plan_rejects_bad_statement():
    with pytest.raises(ValueError):
        plan(["GRANT SELECT ON t TO x;"], set(), catalog())


# --- live side -----------------------------------------------------------------

def test_live_grants():
    live = live_grants(acl(
        schema_grants=[dict(schema="auth", grantee="auth", privilege="USAGE")],
        object_grants=obj("auth", "auth", "devices", "SELECT", "UPDATE"),
        default_acls=[dict(grantor="postgres", schema="", kind="TABLES", grantee="x", privilege="SELECT")],
        db_grants=[dict(database="app", grantee="auth", privilege="CONNECT")],
    ))
    assert live == {
        Grant("schema", "", "auth", "auth", "USAGE"),
        Grant("table", "auth", "devices", "auth", "SELECT"),
        Grant("table", "auth", "devices", "auth", "UPDATE"),
        Grant("default", "", "TABLES", "x", "SELECT", "postgres"),
        Grant("database", "", "app", "auth", "CONNECT"),
    }


def test_drop_ignored():
    live = {Grant("table", "public", "t", "pg_monitor", "SELECT"), Grant("table", "public", "t", "x", "SELECT")}
    assert drop_ignored(live, ["pg_monitor"]) == {Grant("table", "public", "t", "x", "SELECT")}


# --- expansion -------------------------------------------------------------------

PARTS = {("public", "m_2024"): ("public", "m"), ("public", "m_2024_01"): ("public", "m_2024")}


def test_expand_all_tables():
    cat = catalog({("table", "s"): ["a", "b"]})
    assert expand([parse_statement("GRANT SELECT ON ALL TABLES IN SCHEMA s TO x;")], cat) == {
        Grant("table", "s", "a", "x", "SELECT"), Grant("table", "s", "b", "x", "SELECT"),
    }


def test_expand_parent_covers_nested_partitions():
    got = expand([parse_statement("GRANT SELECT ON TABLE public.m TO x;")], catalog(parents=PARTS))
    assert {g.name for g in got} == {"m", "m_2024", "m_2024_01"}


# --- compress + round-trip ---------------------------------------------------------

def test_default_acls_roundtrip():
    live = live_grants(acl(default_acls=[
        dict(grantor="postgres", schema="public", kind="SEQUENCES", grantee="workers", privilege="SELECT"),
        dict(grantor="postgres", schema="public", kind="SEQUENCES", grantee="workers", privilege="USAGE"),
        dict(grantor="webapp", schema="", kind="TABLES", grantee="x", privilege="SELECT"),
    ]))
    stmts, to_grant, to_revoke = roundtrip(live, catalog())
    assert stmts == [
        "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT, USAGE ON SEQUENCES TO workers;",
        "ALTER DEFAULT PRIVILEGES FOR ROLE webapp GRANT SELECT ON TABLES TO x;",
    ]
    assert not to_grant and not to_revoke


def test_quoted_identifiers_roundtrip():
    live = live_grants(acl(object_grants=obj("PUBLIC", "public", "Foo", "SELECT") + obj("x", "public", "order", "SELECT")))
    stmts, to_grant, to_revoke = roundtrip(live, catalog())
    assert stmts == ['GRANT SELECT ON TABLE public."Foo" TO PUBLIC;', 'GRANT SELECT ON TABLE public."order" TO x;']
    assert not to_grant and not to_revoke


def test_all_tables_uses_common_privileges():
    cat = catalog({("table", "s"): ["a", "b", "c"]})
    live = live_grants(acl(object_grants=obj("x", "s", "a", "SELECT") + obj("x", "s", "b", "SELECT")
                           + obj("x", "s", "c", "SELECT", "INSERT")))
    stmts, to_grant, to_revoke = roundtrip(live, cat)
    assert stmts == ["GRANT SELECT ON ALL TABLES IN SCHEMA s TO x;", "GRANT INSERT ON TABLE s.c TO x;"]
    assert not to_grant and not to_revoke


def test_no_all_tables_when_an_object_lacks_grants():
    cat = catalog({("table", "s"): ["a", "b", "c"]})
    live = live_grants(acl(object_grants=obj("x", "s", "a", "SELECT") + obj("x", "s", "b", "SELECT")))
    stmts, _, _ = roundtrip(live, cat)
    assert stmts == ["GRANT SELECT ON TABLE s.a TO x;", "GRANT SELECT ON TABLE s.b TO x;"]


def test_nested_partitions_collapse_into_parent():
    cat = catalog({("table", "public"): ["m", "m_2024", "m_2024_01", "other"]}, PARTS)
    live = live_grants(acl(object_grants=obj("x", "public", "m", "SELECT") + obj("x", "public", "m_2024", "SELECT")
                           + obj("x", "public", "m_2024_01", "SELECT", "INSERT")))
    stmts, to_grant, to_revoke = roundtrip(live, cat)
    assert stmts == ["GRANT SELECT ON TABLE public.m TO x;", "GRANT INSERT ON TABLE public.m_2024_01 TO x;"]
    assert not to_grant and not to_revoke


def test_partition_missing_parent_grant_is_added_by_sync():
    cat = catalog({("table", "public"): ["m", "m_2024", "m_2024_01"]}, PARTS)
    live = live_grants(acl(object_grants=obj("x", "public", "m", "SELECT")))
    _, to_grant, to_revoke = roundtrip(live, cat)
    assert {g.name for g in to_grant} == {"m_2024", "m_2024_01"}
    assert not to_revoke


def test_plan_diff_and_output():
    cat = catalog({("table", "public"): ["a", "b"]})
    live = live_grants(acl(object_grants=obj("x", "public", "a", "SELECT") + obj("y", "public", "a", "SELECT")))
    to_grant, to_revoke = plan(["GRANT SELECT ON ALL TABLES IN SCHEMA public TO x;"], live, cat)
    assert format_statements(to_grant, cat) == ["GRANT SELECT ON TABLE public.b TO x;"]
    assert format_statements(to_revoke, cat, revoke=True) == ["REVOKE SELECT ON TABLE public.a FROM y;"]
