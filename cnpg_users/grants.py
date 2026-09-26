"""Grant model shared by import and sync-grants.

Both sides are normalized into sets of atomic `Grant`s (one privilege on one
object for one grantee, identifiers unquoted):

    live ACLs (QUERY_DB json)  --live_grants-->        set[Grant]
    config statements          --parse+expand-->       set[Grant]

`format_statements` turns a set of Grants back into SQL; with compress=True
it emits the shortest statement list whose expansion is the same set (ALL
TABLES IN SCHEMA, parent grant covering its partitions), which is what import
writes to the config.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Iterable, NamedTuple

PRIV_ORDER = [
    "SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES",
    "TRIGGER", "MAINTAIN", "USAGE", "CREATE", "CONNECT", "TEMPORARY", "EXECUTE",
]
PRIV_ALIASES = {"TEMP": "TEMPORARY"}
DEFAULT_OBJTYPES = ("TABLES", "SEQUENCES", "FUNCTIONS", "TYPES", "SCHEMAS", "LARGE OBJECTS")
DEFAULT_ALIASES = {"ROUTINES": "FUNCTIONS"}

# statement order in the config: schema, sequences, tables, defaults, database
_KIND_ORDER = {
    "schema": 0, "all_sequences": 1, "sequence": 1, "all_tables": 2, "table": 2,
    "default": 3, "database": 4,
}


class Grant(NamedTuple):
    kind: str          # database | schema | table | sequence | default
    schema: str        # table/sequence: its schema; default: IN SCHEMA or '' (global)
    name: str          # object/schema/database name; default: object type (TABLES, ...)
    grantee: str       # role name or PUBLIC
    priv: str
    grantor: str = ""  # default only: FOR ROLE


class Stmt(NamedTuple):
    """One GRANT statement: like Grant but with a privilege set, and two
    extra kinds, all_tables / all_sequences (name is '')."""
    kind: str
    schema: str
    name: str
    grantee: str
    privs: frozenset[str]
    grantor: str = ""


@dataclass
class Catalog:
    """Relations of one database, needed to expand ALL ... IN SCHEMA and
    parent-table grants, plus the server's keywords for identifier quoting."""
    objects: dict[tuple[str, str], list[str]] = field(default_factory=dict)  # (kind, schema) -> names
    parents: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)  # partition -> parent
    keywords: frozenset[str] = frozenset()  # keywords quote_ident() would quote

    @classmethod
    def from_json(cls, data: dict) -> Catalog:
        cat = cls(keywords=frozenset(data.get("keywords") or []))
        for o in data["objects"]:
            cat.objects.setdefault((o["kind"], o["schema"]), []).append(o["name"])
            if o.get("parent"):
                cat.parents[(o["schema"], o["name"])] = (o["parent_schema"], o["parent"])
        for names in cat.objects.values():
            names.sort()
        return cat

    @cached_property
    def _children(self) -> dict[tuple[str, str], list[tuple[str, str]]]:
        out: dict[tuple[str, str], list[tuple[str, str]]] = {}
        for child, parent in self.parents.items():
            out.setdefault(parent, []).append(child)
        return out

    def descendants(self, schema: str, name: str) -> list[tuple[str, str]]:
        out, todo = [], list(self._children.get((schema, name), []))
        while todo:
            obj = todo.pop()
            out.append(obj)
            todo.extend(self._children.get(obj, []))
        return out

    def ancestors(self, schema: str, name: str) -> list[tuple[str, str]]:
        out, obj = [], (schema, name)
        while obj in self.parents:
            obj = self.parents[obj]
            out.append(obj)
        return out

    def qi(self, ident: str) -> str:
        """quote_ident(): bare only for lowercase names that aren't keywords."""
        if re.fullmatch(r"[a-z_][a-z0-9_$]*", ident) and ident not in self.keywords:
            return ident
        return '"' + ident.replace('"', '""') + '"'


def fmt_privs(privs: Iterable[str]) -> str:
    return ", ".join(sorted(privs, key=_priv_rank))


def _priv_rank(p: str) -> int:
    return PRIV_ORDER.index(p) if p in PRIV_ORDER else len(PRIV_ORDER)


# --- live side ---------------------------------------------------------------

def live_grants(acl: dict) -> set[Grant]:
    """QUERY_DB json -> atomic grants."""
    out: set[Grant] = set()
    for r in acl["schema_grants"]:
        out.add(Grant("schema", "", r["schema"], r["grantee"], r["privilege"]))
    for r in acl["object_grants"]:
        out.add(Grant(r["kind"], r["schema"], r["object"], r["grantee"], r["privilege"]))
    for r in acl["default_acls"]:
        out.add(Grant("default", r["schema"], r["kind"], r["grantee"], r["privilege"], r["grantor"]))
    for r in acl["db_grants"]:
        out.add(Grant("database", "", r["database"], r["grantee"], r["privilege"]))
    return out


def drop_ignored(grants: Iterable[Grant], ignored_grantees: Iterable[str]) -> set[Grant]:
    ignored = set(ignored_grantees)
    return {g for g in grants if g.grantee not in ignored}


# --- config side -------------------------------------------------------------

_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_PRIVS = r"[A-Za-z]+(?:\s*,\s*[A-Za-z]+)*"
_GRANT_RE = re.compile(
    rf"GRANT\s+(?P<privs>{_PRIVS})\s+ON\s+(?P<target>.+?)\s+TO\s+(?P<grantee>{_IDENT})",
    re.I | re.S,
)
_DEFAULT_RE = re.compile(
    rf"ALTER\s+DEFAULT\s+PRIVILEGES\s+FOR\s+ROLE\s+(?P<grantor>{_IDENT})"
    rf"(?:\s+IN\s+SCHEMA\s+(?P<schema>{_IDENT}))?"
    rf"\s+GRANT\s+(?P<privs>{_PRIVS})\s+ON\s+(?P<objtype>[A-Za-z]+(?:\s+OBJECTS)?)"
    rf"\s+TO\s+(?P<grantee>{_IDENT})",
    re.I | re.S,
)
_TARGETS = [
    ("all_tables", re.compile(rf"ALL\s+TABLES\s+IN\s+SCHEMA\s+(?P<schema>{_IDENT})", re.I)),
    ("all_sequences", re.compile(rf"ALL\s+SEQUENCES\s+IN\s+SCHEMA\s+(?P<schema>{_IDENT})", re.I)),
    ("table", re.compile(rf"TABLE\s+(?P<schema>{_IDENT})\.(?P<name>{_IDENT})", re.I)),
    ("sequence", re.compile(rf"SEQUENCE\s+(?P<schema>{_IDENT})\.(?P<name>{_IDENT})", re.I)),
    ("schema", re.compile(rf"SCHEMA\s+(?P<name>{_IDENT})", re.I)),
    ("database", re.compile(rf"DATABASE\s+(?P<name>{_IDENT})", re.I)),
]


def unquote(ident: str) -> str:
    if ident.startswith('"'):
        return ident[1:-1].replace('""', '"')
    return ident.lower()


def _grantee(ident: str) -> str:
    return "PUBLIC" if ident.upper() == "PUBLIC" else unquote(ident)


def _privs(text: str, stmt: str) -> frozenset[str]:
    privs = set()
    for p in re.split(r"\s*,\s*", text.strip().upper()):
        p = PRIV_ALIASES.get(p, p)
        if p not in PRIV_ORDER:
            raise ValueError(f"unknown privilege {p!r} in: {stmt}")
        privs.add(p)
    return frozenset(privs)


def parse_statement(stmt: str) -> Stmt:
    """Parse one config statement; raises ValueError if it isn't one of the
    forms format_statements produces."""
    s = stmt.strip().rstrip(";").strip()
    m = _DEFAULT_RE.fullmatch(s)
    if m:
        objtype = " ".join(m["objtype"].upper().split())
        objtype = DEFAULT_ALIASES.get(objtype, objtype)
        if objtype not in DEFAULT_OBJTYPES:
            raise ValueError(f"unknown default privileges object type {objtype!r} in: {stmt}")
        return Stmt(
            "default", unquote(m["schema"]) if m["schema"] else "", objtype,
            _grantee(m["grantee"]), _privs(m["privs"], stmt), unquote(m["grantor"]),
        )
    m = _GRANT_RE.fullmatch(s)
    if m:
        for kind, rx in _TARGETS:
            t = rx.fullmatch(m["target"])
            if t:
                groups = t.groupdict()
                schema = unquote(groups["schema"]) if groups.get("schema") else ""
                name = unquote(groups["name"]) if groups.get("name") else ""
                return Stmt(kind, schema, name, _grantee(m["grantee"]), _privs(m["privs"], stmt))
    raise ValueError(f"unsupported grant statement: {stmt}")


def expand(stmts: Iterable[Stmt], catalog: Catalog) -> set[Grant]:
    """Statements -> atomic grants. ALL ... IN SCHEMA expands to the schema's
    current relations; a table grant also covers all its partitions (at any
    depth), since import collapses partitions into their parent."""
    out: set[Grant] = set()
    for st in stmts:
        if st.kind in ("all_tables", "all_sequences"):
            kind = st.kind[4:-1]
            targets = [(kind, st.schema, n) for n in catalog.objects.get((kind, st.schema), [])]
        elif st.kind == "table":
            targets = [("table", st.schema, st.name)]
            targets += [("table", s, n) for s, n in catalog.descendants(st.schema, st.name)]
        else:
            targets = [(st.kind, st.schema, st.name)]
        for kind, schema, name in targets:
            for p in st.privs:
                out.add(Grant(kind, schema, name, st.grantee, p, st.grantor))
    return out


# --- formatting ----------------------------------------------------------------

def format_statements(
    grants: Iterable[Grant], catalog: Catalog, *, revoke: bool = False, compress: bool = False
) -> list[str]:
    by_target: dict[tuple, set[str]] = {}
    for g in grants:
        by_target.setdefault((g.kind, g.schema, g.name, g.grantee, g.grantor), set()).add(g.priv)
    if compress:
        stmts = _compress(by_target, catalog)
    else:
        stmts = [Stmt(k, s, n, ge, frozenset(p), gr) for (k, s, n, ge, gr), p in by_target.items()]
    stmts.sort(key=lambda st: (
        st.grantee, _KIND_ORDER[st.kind], st.grantor, st.schema,
        len(st.privs), sorted(map(_priv_rank, st.privs)), st.name,
    ))
    return [render(st, catalog, revoke=revoke) for st in stmts]


def _compress(by_target: dict[tuple, set[str]], catalog: Catalog) -> list[Stmt]:
    out: list[Stmt] = []
    rel_privs: dict[tuple[str, str], dict[tuple[str, str], set[str]]] = {}  # (grantee, kind) -> obj -> privs
    for (kind, schema, name, grantee, grantor), privs in by_target.items():
        if kind in ("table", "sequence"):
            rel_privs.setdefault((grantee, kind), {})[(schema, name)] = set(privs)
        else:
            out.append(Stmt(kind, schema, name, grantee, frozenset(privs), grantor))

    for (grantee, kind), objs in rel_privs.items():
        # privileges held on every relation of a schema -> ALL ... IN SCHEMA
        for schema in sorted({s for s, _ in objs}):
            names = catalog.objects.get((kind, schema), [])
            if len(names) < 2 or any((schema, n) not in objs for n in names):
                continue
            common = set.intersection(*(objs[(schema, n)] for n in names))
            if common:
                out.append(Stmt(f"all_{kind}s", schema, "", grantee, frozenset(common)))
                for n in names:
                    objs[(schema, n)] -= common
        # a parent's grant covers its partitions: emit only what ancestors lack
        if kind == "table":
            objs = {
                obj: privs - set().union(*(objs.get(a, set()) for a in catalog.ancestors(*obj)))
                for obj, privs in objs.items()
            }
        out.extend(
            Stmt(kind, schema, name, grantee, frozenset(privs))
            for (schema, name), privs in objs.items() if privs
        )
    return out


def render(st: Stmt, catalog: Catalog, *, revoke: bool = False) -> str:
    q = catalog.qi
    verb, prep = ("REVOKE", "FROM") if revoke else ("GRANT", "TO")
    grantee = "PUBLIC" if st.grantee == "PUBLIC" else q(st.grantee)
    privs = fmt_privs(st.privs)
    if st.kind == "default":
        scope = f" IN SCHEMA {q(st.schema)}" if st.schema else ""
        return (
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {q(st.grantor)}{scope}"
            f" {verb} {privs} ON {st.name} {prep} {grantee};"
        )
    target = {
        "database": lambda: f"DATABASE {q(st.name)}",
        "schema": lambda: f"SCHEMA {q(st.name)}",
        "table": lambda: f"TABLE {q(st.schema)}.{q(st.name)}",
        "sequence": lambda: f"SEQUENCE {q(st.schema)}.{q(st.name)}",
        "all_tables": lambda: f"ALL TABLES IN SCHEMA {q(st.schema)}",
        "all_sequences": lambda: f"ALL SEQUENCES IN SCHEMA {q(st.schema)}",
    }[st.kind]()
    return f"{verb} {privs} ON {target} {prep} {grantee};"


def plan(config_stmts: Iterable[str], live: set[Grant], catalog: Catalog) -> tuple[set[Grant], set[Grant]]:
    """(to_grant, to_revoke) that make `live` match the config statements.
    `live` should already have ignored grantees dropped."""
    desired = expand((parse_statement(s) for s in config_stmts), catalog)
    return desired - live, live - desired
