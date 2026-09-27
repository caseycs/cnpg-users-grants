"""What sync-users needs from a Git host (GitHub via gh, GitLab via glab):
read a file, and put changes in one PR/MR. The host is picked per cluster
file with repo_provider."""

from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import Callable

PR_LABEL = "cnpg-users-grants"
PR_LABEL_COLOR = "1d76db"
PROVIDERS = ("github", "gitlab")


class RepoError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


@dataclass
class RepoFile:
    text: str
    sha: str  # blob sha, to detect the file changing before the PR is opened


@dataclass
class FileChange:
    path: str
    base_sha: str  # blob the change was computed from
    text: str      # new content


@dataclass
class PullRequest:
    url: str
    action: str  # "opened", or "updated" (an open PR with our label, rewritten)


@dataclass(frozen=True)
class Provider:
    get_file: Callable[..., RepoFile]     # (repo, path, ref=None)
    open_pr: Callable[..., PullRequest]   # (repo, changes, branch, title, body)


def provider(kind: str = "github", gitlab_host: str | None = None) -> Provider:
    if kind == "gitlab":
        from . import gitlab
        return Provider(partial(gitlab.get_file, host=gitlab_host), partial(gitlab.open_pr, host=gitlab_host))
    from . import github
    return Provider(github.get_file, github.open_pr)
