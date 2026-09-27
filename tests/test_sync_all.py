import io
from types import SimpleNamespace

from cnpg_users import sync_all
from cnpg_users.sync_all import SyncPlan, apply_sync, cmd_sync


def db(**kw):
    base = dict(online=True, repo="o/r", values_file="v.yaml", header="prod (cluster pg in ns x)", name="prod")
    return SimpleNamespace(**{**base, **kw})


def run(monkeypatch, users=("", False), grants=("", False), **db_kw):
    calls = []
    users_plan = SimpleNamespace(cluster="cluster-from-users")
    monkeypatch.setattr(sync_all, "users_body", lambda d, s, a: (calls.append("users"), (*users, users_plan))[1])
    monkeypatch.setattr(sync_all, "grants_body",
                        lambda d, c=None: (calls.append(("grants", c)), (*grants, SimpleNamespace(db=d, cluster=c)))[1])
    out = io.StringIO()
    drift, plan = cmd_sync(db(**db_kw), store=None, out=out)
    return drift, plan, out.getvalue(), calls


def test_in_sync_is_one_line(monkeypatch):
    drift, _, out, calls = run(monkeypatch)
    assert out == "prod (cluster pg in ns x): in sync\n" and not drift
    assert calls == ["users", ("grants", "cluster-from-users")]  # one connection for both parts


def test_both_parts_under_one_header(monkeypatch):
    drift, _, out, _ = run(monkeypatch, users=("  values.yaml (o/r):\n    diff\n", True),
                           grants=("  Database: app\n    To add:\n      GRANT …\n", True))
    assert drift
    assert out == ("prod (cluster pg in ns x)\n  values.yaml (o/r):\n    diff\n"
                   "  Database: app\n    To add:\n      GRANT …\n")


def test_db_without_values_file_still_checks_grants(monkeypatch):
    drift, plan, out, calls = run(monkeypatch, grants=("  Database: app\n", True), repo=None)
    assert calls == [("grants", None)] and plan.users is None and drift
    assert "users: skipped (no repo/values_file in the cluster file)" in out


def test_offline(monkeypatch):
    drift, plan, out, calls = run(monkeypatch, online=False)
    assert out == "prod (cluster pg in ns x): offline — skipped\n" and plan is None and calls == []


def test_apply_does_users_then_replans_grants(monkeypatch):
    order = []
    monkeypatch.setattr(sync_all, "apply_all", lambda users, store, timeout: order.append(("users", len(users))) or False)
    monkeypatch.setattr(sync_all, "grants_body", lambda d, c=None: (order.append(("replan", d)), ("", False, f"fresh-{d}"))[1])
    monkeypatch.setattr(sync_all, "apply_grants", lambda plans, yes: order.append(("grants", plans, yes)) or True)
    plans = [SyncPlan(users="u1", grants=SimpleNamespace(db="d1", cluster="c1")),
             SyncPlan(users=None, grants=SimpleNamespace(db="d2", cluster="c2"))]
    assert apply_sync(plans, store=None, timeout=5, assume_yes=True) is True
    assert order == [("users", 1), ("replan", "d1"), ("replan", "d2"), ("grants", ["fresh-d1", "fresh-d2"], True)]
