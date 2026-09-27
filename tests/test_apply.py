import base64

import pytest

from cnpg_users import github
from cnpg_users.users import waiting_line, wait_for_roles


class FakeGitHub:
    """Records calls; answers like the REST API for one repo."""

    def __init__(self, branch_exists=False, file_shas=None, open_prs=(), labeled_prs=(), label_exists=True):
        self.branch_exists, self.label_exists = branch_exists, label_exists
        self.open_prs, self.labeled_prs = list(open_prs), list(labeled_prs)
        self.file_shas = file_shas or {"a.yaml": "blobA", "b.yaml": "blobB"}
        self.calls = []

    def __call__(self, method, endpoint, payload=None):
        path, _, query = endpoint.partition("?")
        self.calls.append((method, path, payload))
        if (method, path) == ("GET", "repos/o/r"):
            return {"default_branch": "main"}
        if (method, path) == ("GET", "repos/o/r/git/ref/heads/main"):
            return {"object": {"sha": "head1"}}
        if (method, path) == ("GET", "repos/o/r/git/commits/head1"):
            return {"tree": {"sha": "tree0"}}
        if method == "GET" and path.startswith("repos/o/r/git/ref/heads/"):
            if not self.branch_exists:
                raise github.GitHubError("not found", 404)
            return {"object": {"sha": "old"}}
        if method == "GET" and path.startswith("repos/o/r/contents/"):
            assert query == "ref=head1"
            name = path.rsplit("/", 1)[1]
            return {"content": base64.b64encode(b"old\n").decode(), "sha": self.file_shas[name]}
        if (method, path) == ("POST", "repos/o/r/git/trees"):
            return {"sha": "tree1"}
        if (method, path) == ("POST", "repos/o/r/git/commits"):
            return {"sha": "commit1"}
        if (method, path) == ("GET", "repos/o/r/pulls"):
            return self.labeled_prs if "head=" not in query else self.open_prs
        if (method, path) == ("GET", f"repos/o/r/labels/{github.PR_LABEL}"):
            if not self.label_exists:
                raise github.GitHubError("not found", 404)
            return {}
        if method in ("POST", "PATCH") and "pulls" in path:
            return {"html_url": "https://github.com/o/r/pull/7", "number": 7}
        return {}


CHANGES = [github.FileChange("a.yaml", "blobA", "new a\n"), github.FileChange("b.yaml", "blobB", "new b\n")]
LABEL = [{"name": github.PR_LABEL}]


def run(fake, monkeypatch, changes=CHANGES):
    monkeypatch.setattr(github, "api", fake)
    return github.open_pr("o/r", changes, "cnpg-users/sync", "t", "b")


def writes(fake):
    return [(m, e, p) for m, e, p in fake.calls if m != "GET"]


def test_open_pr_one_commit_for_all_files_and_label(monkeypatch):
    fake = FakeGitHub(label_exists=False)
    assert run(fake, monkeypatch) == github.PullRequest("https://github.com/o/r/pull/7", "opened")
    assert writes(fake) == [
        ("POST", "repos/o/r/git/trees", {"base_tree": "tree0", "tree": [
            {"path": "a.yaml", "mode": "100644", "type": "blob", "content": "new a\n"},
            {"path": "b.yaml", "mode": "100644", "type": "blob", "content": "new b\n"},
        ]}),
        ("POST", "repos/o/r/git/commits", {"message": "t", "tree": "tree1", "parents": ["head1"]}),
        ("POST", "repos/o/r/git/refs", {"ref": "refs/heads/cnpg-users/sync", "sha": "commit1"}),
        ("POST", "repos/o/r/pulls", {"title": "t", "head": "cnpg-users/sync", "base": "main", "body": "b"}),
        ("POST", "repos/o/r/labels", {"name": github.PR_LABEL, "color": github.PR_LABEL_COLOR,
                                      "description": "Opened by cnpg-users.py sync-users (cnpg-users-grants-cli)"}),
        ("POST", "repos/o/r/issues/7/labels", {"labels": [github.PR_LABEL]}),
    ]


def test_open_pr_rewrites_open_labeled_pr_on_its_branch(monkeypatch):
    labeled = [{"number": 5, "labels": LABEL, "head": {"ref": "someone/tweak", "repo": {"full_name": "o/r"}}}]
    fake = FakeGitHub(branch_exists=True, labeled_prs=labeled)
    assert run(fake, monkeypatch).action == "updated"
    assert [(m, e) for m, e, _ in writes(fake)] == [
        ("POST", "repos/o/r/git/trees"),
        ("POST", "repos/o/r/git/commits"),
        ("PATCH", "repos/o/r/git/refs/heads/someone/tweak"),  # the labeled PR's own branch
        ("PATCH", "repos/o/r/pulls/5"),
        ("POST", "repos/o/r/issues/7/labels"),
    ]
    reset = next(p for m, e, p in fake.calls if (m, e) == ("PATCH", "repos/o/r/git/refs/heads/someone/tweak"))
    assert reset == {"sha": "commit1", "force": True}


def test_open_pr_ignores_labeled_prs_from_forks_and_reuses_branch_pr(monkeypatch):
    fork = [{"number": 9, "labels": LABEL, "head": {"ref": "x", "repo": {"full_name": "fork/r"}}}]
    fake = FakeGitHub(branch_exists=True, labeled_prs=fork, open_prs=[{"number": 7}])
    assert run(fake, monkeypatch).action == "updated"
    assert ("PATCH", "repos/o/r/git/refs/heads/cnpg-users/sync") in [(m, e) for m, e, _ in writes(fake)]
    assert ("PATCH", "repos/o/r/pulls/7") in [(m, e) for m, e, _ in writes(fake)]


def test_open_pr_refuses_when_any_file_moved(monkeypatch):
    fake = FakeGitHub(file_shas={"a.yaml": "blobA", "b.yaml": "moved"})
    with pytest.raises(github.GitHubError, match="b.yaml changed on main"):
        run(fake, monkeypatch)
    assert writes(fake) == []


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_wait_for_roles_until_applied():
    clock = Clock()
    states = iter([{"gone"}, {"gone", "new"}, {"new"}])
    got = wait_for_roles(lambda names: next(states), ["new"], ["gone"], 180, clock.sleep, clock)
    assert got == (set(), set())
    assert clock.t == 20


def test_wait_for_roles_times_out():
    clock = Clock()
    got = wait_for_roles(lambda names: {"gone"}, ["new"], ["gone"], 25, clock.sleep, clock)
    assert got == ({"new"}, {"gone"})
    assert clock.t == 25


def test_wait_for_roles_reports_each_poll():
    clock, lines = Clock(), []
    states = iter([{"gone"}, {"gone"}, {"gone", "new"}, {"new"}])
    wait_for_roles(lambda names: next(states), ["new"], ["gone"], 180, clock.sleep, clock,
                   report=lambda *args: lines.append(args))
    assert lines == [(0, {"new"}, {"gone"}), (10, {"new"}, {"gone"}), (20, set(), {"gone"})]


def test_waiting_line():
    initial = ({"new"}, {"gone"})
    assert waiting_line(0, {"new"}, {"gone"}, initial) == "    0s: no changes detected yet — waiting to create new; drop gone"
    assert waiting_line(20, set(), {"gone"}, initial) == "    20s: partly applied — waiting to drop gone"


def test_apply_all_one_pr_per_repo_then_one_wait(monkeypatch, capsys):
    from types import SimpleNamespace

    from cnpg_users import users
    from cnpg_users.github import RepoFile
    from cnpg_users.passwords import scram_verifier
    from cnpg_users.values import RolesChange

    class FakeCluster:
        def __init__(self, roles):
            self.roles = set(roles)

        def existing_roles(self, names):
            return self.roles & set(names)

        def password_verifiers(self, names):
            return {n: scram_verifier("pw") for n in names if n in self.roles}

        def set_passwords(self, statements):
            raise AssertionError("passwords already in sync")

    def plan(name, repo, path, changed, humans, absent=()):
        db = SimpleNamespace(name=name, repo=repo, values_file=path)
        change = RolesChange("new" if changed else "old", absent=list(absent))
        return users.UsersPlan(db, FakeCluster(humans), RepoFile("old", f"sha-{name}"), change, list(humans))

    plans = [
        plan("devel", "o/argocd", "devel/values.yaml", True, ["erin"], absent=["frank"]),
        plan("ai", "o/ai-argocd", "ai/values.yaml", False, ["grace"]),
        plan("prod", "o/argocd", "prod/values.yaml", True, ["heidi"]),
    ]
    prs = []
    monkeypatch.setattr(users, "open_pr", lambda repo, changes, branch, title, body:
                        prs.append((repo, [c.path for c in changes], branch, title))
                        or github.PullRequest(f"https://pr/{repo}", "opened"))
    monkeypatch.setattr(users, "ssm_passwords", lambda ssm, names: {n: "pw" for n in names})
    waits = []
    monkeypatch.setattr(users, "wait_for_roles", lambda existing, present, absent, timeout, report:
                        waits.append((sorted(present), sorted(absent), sorted(existing(present + absent)))) or (set(), set()))

    assert users.apply_all(plans, ssm=None, timeout=5) is False
    assert prs == [("o/argocd", ["devel/values.yaml", "prod/values.yaml"], "cnpg-users/sync", "cnpg users: sync devel, prod")]
    assert waits == [(["devel/erin", "prod/heidi"], ["devel/frank"], ["devel/erin", "prod/heidi"])]
    out = capsys.readouterr().out
    assert out.index("PR (opened): https://pr/o/argocd") < out.index("waiting up to 5s") < out.index("devel: passwords")
    assert "\nai: passwords\n" in out  # unchanged values.yaml, passwords still checked
