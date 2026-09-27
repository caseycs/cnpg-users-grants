import base64
from types import SimpleNamespace

import pytest

from cnpg_users import gitlab, repos, users
from cnpg_users.repos import PR_LABEL, FileChange, PullRequest

PROJECT = "grp%2Fsub%2Fr"  # grp/sub/r, URL-encoded


class FakeGitLab:
    """Records calls; answers like the REST API for one project (id 5)."""

    def __init__(self, file_shas=None, labeled_mrs=(), branch_mrs=()):
        self.file_shas = file_shas or {"a.yaml": "blobA", "dir%2Fb.yaml": "blobB"}
        self.labeled_mrs, self.branch_mrs = list(labeled_mrs), list(branch_mrs)
        self.calls = []

    def __call__(self, method, endpoint, payload=None, host=None):
        path, _, query = endpoint.partition("?")
        self.calls.append((method, path, payload, host))
        if (method, path) == ("GET", f"projects/{PROJECT}"):
            return {"id": 5, "default_branch": "main"}
        if (method, path) == ("GET", f"projects/{PROJECT}/repository/branches/main"):
            return {"commit": {"id": "head1"}}
        if method == "GET" and path.startswith(f"projects/{PROJECT}/repository/files/"):
            name = path.rsplit("/", 1)[1]
            return {"content": base64.b64encode(b"old\n").decode(), "blob_id": self.file_shas[name], "query": query}
        if (method, path) == ("GET", f"projects/{PROJECT}/merge_requests"):
            return self.labeled_mrs if "labels=" in query else self.branch_mrs
        if method in ("POST", "PUT") and "merge_requests" in path:
            return {"web_url": "https://gitlab.example.com/grp/sub/r/-/merge_requests/3", "iid": 3}
        return {}


CHANGES = [FileChange("a.yaml", "blobA", "new a\n"), FileChange("dir/b.yaml", "blobB", "new b\n")]


def run(fake, monkeypatch, changes=CHANGES):
    monkeypatch.setattr(gitlab, "api", fake)
    return gitlab.open_pr("grp/sub/r", changes, "cnpg-users/sync", "t", "b", host="gitlab.example.com")


def writes(fake):
    return [(m, e, p) for m, e, p, _ in fake.calls if m != "GET"]


def test_get_file_reads_the_default_branch(monkeypatch):
    fake = FakeGitLab()
    monkeypatch.setattr(gitlab, "api", fake)
    f = gitlab.get_file("grp/sub/r", "dir/b.yaml")
    assert (f.text, f.sha) == ("old\n", "blobB")
    assert fake.calls[-1][1] == f"projects/{PROJECT}/repository/files/dir%2Fb.yaml"


def test_open_mr_one_commit_for_all_files_and_label(monkeypatch):
    fake = FakeGitLab()
    assert run(fake, monkeypatch) == PullRequest("https://gitlab.example.com/grp/sub/r/-/merge_requests/3", "opened")
    assert writes(fake) == [
        ("POST", f"projects/{PROJECT}/repository/commits", {
            "branch": "cnpg-users/sync", "start_sha": "head1", "force": True, "commit_message": "t",
            "actions": [{"action": "update", "file_path": "a.yaml", "content": "new a\n"},
                        {"action": "update", "file_path": "dir/b.yaml", "content": "new b\n"}],
        }),
        ("POST", f"projects/{PROJECT}/merge_requests", {
            "source_branch": "cnpg-users/sync", "target_branch": "main", "title": "t", "description": "b",
            "labels": PR_LABEL, "remove_source_branch": True,
        }),
    ]
    assert {h for *_, h in fake.calls} == {"gitlab.example.com"}


def test_open_mr_rewrites_open_labeled_mr_on_its_branch(monkeypatch):
    fake = FakeGitLab(labeled_mrs=[{"iid": 9, "source_branch": "old-branch", "source_project_id": 5}])
    assert run(fake, monkeypatch).action == "updated"
    commit, update = writes(fake)
    assert commit[2]["branch"] == "old-branch"
    assert update == ("PUT", f"projects/{PROJECT}/merge_requests/9",
                      {"title": "t", "description": "b", "add_labels": PR_LABEL})


def test_open_mr_ignores_labeled_mrs_from_forks_and_reuses_branch_mr(monkeypatch):
    fork = [{"iid": 1, "source_branch": "x", "source_project_id": 99}]
    fake = FakeGitLab(labeled_mrs=fork, branch_mrs=[{"iid": 4, "source_project_id": 5}])
    assert run(fake, monkeypatch).action == "updated"
    commit, update = writes(fake)
    assert commit[2]["branch"] == "cnpg-users/sync" and update[1].endswith("/merge_requests/4")


def test_open_mr_refuses_when_any_file_moved(monkeypatch):
    fake = FakeGitLab(file_shas={"a.yaml": "blobA", "dir%2Fb.yaml": "moved"})
    with pytest.raises(gitlab.GitLabError, match="dir/b.yaml changed on main"):
        run(fake, monkeypatch)
    assert writes(fake) == []


def test_users_dispatch_by_provider(monkeypatch):
    seen = []
    monkeypatch.setattr(gitlab, "open_pr", lambda repo, changes, branch, title, body, host=None:
                        seen.append((repo, host)) or PullRequest("u", "opened"))
    db = SimpleNamespace(repo="grp/r", repo_provider="gitlab", gitlab_host="git.example.com")
    users.open_pr(db, [], "b", "t", "body")
    assert seen == [("grp/r", "git.example.com")]
    assert repos.provider("github").open_pr.__module__ == "cnpg_users.github"
