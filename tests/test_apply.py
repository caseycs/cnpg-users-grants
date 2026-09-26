import base64

import pytest

from cnpg_users import github
from cnpg_users.users import waiting_line, wait_for_roles


class FakeGitHub:
    """Records calls; answers like the REST API for one repo and file."""

    def __init__(self, branch_exists=False, file_sha="blob1", open_prs=()):
        self.branch_exists, self.file_sha, self.open_prs = branch_exists, file_sha, list(open_prs)
        self.calls = []

    def __call__(self, method, endpoint, payload=None):
        self.calls.append((method, endpoint.split("?")[0], payload))
        if (method, endpoint) == ("GET", "repos/o/r"):
            return {"default_branch": "main"}
        if (method, endpoint) == ("GET", "repos/o/r/git/ref/heads/main"):
            return {"object": {"sha": "head1"}}
        if (method, endpoint) == ("GET", "repos/o/r/git/ref/heads/cnpg-users/db"):
            if not self.branch_exists:
                raise github.GitHubError("not found", 404)
            return {"object": {"sha": "old"}}
        if method == "GET" and endpoint.startswith("repos/o/r/contents/"):
            return {"content": base64.b64encode(b"old\n").decode(), "sha": self.file_sha}
        if method == "GET" and endpoint.startswith("repos/o/r/pulls"):
            return self.open_prs
        if method in ("POST", "PATCH") and "pulls" in endpoint:
            return {"html_url": "https://github.com/o/r/pull/7"}
        return {}


def run(fake, monkeypatch, base_sha="blob1"):
    monkeypatch.setattr(github, "api", fake)
    return github.open_pr("o/r", "v.yaml", base_sha, "new\n", "cnpg-users/db", "t", "b")


def test_open_pr_new_branch(monkeypatch):
    fake = FakeGitHub()
    assert run(fake, monkeypatch) == "https://github.com/o/r/pull/7"
    writes = [(m, e, p) for m, e, p in fake.calls if m != "GET"]
    assert writes == [
        ("POST", "repos/o/r/git/refs", {"ref": "refs/heads/cnpg-users/db", "sha": "head1"}),
        ("PUT", "repos/o/r/contents/v.yaml",
         {"message": "t", "content": base64.b64encode(b"new\n").decode(), "sha": "blob1", "branch": "cnpg-users/db"}),
        ("POST", "repos/o/r/pulls", {"title": "t", "head": "cnpg-users/db", "base": "main", "body": "b"}),
    ]


def test_open_pr_reuses_branch_and_pr(monkeypatch):
    fake = FakeGitHub(branch_exists=True, open_prs=[{"number": 7}])
    run(fake, monkeypatch)
    writes = [(m, e) for m, e, _ in fake.calls if m != "GET"]
    assert writes == [
        ("PATCH", "repos/o/r/git/refs/heads/cnpg-users/db"),  # reset to main
        ("PUT", "repos/o/r/contents/v.yaml"),
        ("PATCH", "repos/o/r/pulls/7"),
    ]
    reset = next(p for m, e, p in fake.calls if (m, e) == ("PATCH", "repos/o/r/git/refs/heads/cnpg-users/db"))
    assert reset == {"sha": "head1", "force": True}


def test_open_pr_refuses_when_file_moved(monkeypatch):
    fake = FakeGitHub(file_sha="blob2")
    with pytest.raises(github.GitHubError, match="changed on main"):
        run(fake, monkeypatch, base_sha="blob1")
    assert not [c for c in fake.calls if c[0] in ("PUT",) or "pulls" in c[1]]


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
