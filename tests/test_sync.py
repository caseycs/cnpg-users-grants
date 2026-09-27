import pytest

from cnpg_users import sync
from cnpg_users.cluster import Cluster
from cnpg_users.sync import DatabasePlan, GrantsPlan, apply_grants


class FakeCluster:
    def __init__(self):
        self.ran = []

    def execute(self, statements, database):
        self.ran.append((database, list(statements)))


def plans():
    c = FakeCluster()
    db = type("Db", (), {"name": "prod"})()
    return c, [GrantsPlan(db, c, [DatabasePlan("app", ["GRANT SELECT ON TABLE public.t TO x;",
                                                       "REVOKE SELECT ON TABLE public.u FROM y;"])])]


def test_apply_runs_after_yes_and_rechecks(monkeypatch, capsys):
    c, ps = plans()
    monkeypatch.setattr(sync, "plan_database", lambda db, cluster, database: ([], []))
    assert apply_grants(ps, assume_yes=True) is False
    assert c.ran == [("app", ["GRANT SELECT ON TABLE public.t TO x;", "REVOKE SELECT ON TABLE public.u FROM y;"])]
    assert "prod / app: applying 2 statement(s)" in capsys.readouterr().out


def test_apply_reports_what_still_differs(monkeypatch, capsys):
    c, ps = plans()
    monkeypatch.setattr(sync, "plan_database", lambda db, cluster, database: (["GRANT …"], []))
    assert apply_grants(ps, assume_yes=True) is True
    assert "still differs: 1 to add, 0 to remove" in capsys.readouterr().out


def test_apply_needs_a_terminal_or_yes(monkeypatch):
    c, ps = plans()
    monkeypatch.setattr(sync.sys.stdin, "isatty", lambda: False, raising=False)
    with pytest.raises(SystemExit, match="--yes"):
        apply_grants(ps)
    assert c.ran == []


def test_apply_declined(monkeypatch, capsys):
    c, ps = plans()
    monkeypatch.setattr(sync.sys.stdin, "isatty", lambda: True, raising=False)
    assert apply_grants(ps, ask=lambda q: "n") is True
    assert c.ran == [] and "not applied" in capsys.readouterr().out


def test_nothing_to_apply():
    c = FakeCluster()
    db = type("Db", (), {"name": "prod"})()
    assert apply_grants([GrantsPlan(db, c, [])]) is False and c.ran == []


def test_execute_batches_in_transactions():
    cluster = Cluster.__new__(Cluster)  # no kube connection
    sent = []
    cluster.psql = lambda sql, database=None: sent.append((database, sql))
    stmts = [f"GRANT SELECT ON TABLE public.t{i} TO x;" for i in range(10)]
    cluster.execute(stmts, "app", batch_chars=120)
    assert all(sql.startswith("BEGIN;\n") and sql.endswith("\nCOMMIT;") and db == "app" for db, sql in sent)
    assert len(sent) > 1
    assert [line for _, sql in sent for line in sql.splitlines()[1:-1]] == stmts
