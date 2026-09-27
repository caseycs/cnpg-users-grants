"""GitHub REST API via the gh CLI (uses its login)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from dataclasses import dataclass


class GitHubError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def api(method: str, endpoint: str, payload: dict | None = None):
    cmd = ["gh", "api", "-X", method, endpoint]
    if payload is not None:
        cmd += ["--input", "-"]
    # own session: Ctrl+C in the terminal reaches only this tool, not gh
    result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, start_new_session=True)
    if result.returncode:
        m = re.search(r"HTTP (\d{3})", result.stderr)
        raise GitHubError(f"{method} {endpoint}: {result.stderr.strip()}", int(m[1]) if m else None)
    return json.loads(result.stdout) if result.stdout.strip() else None


@dataclass
class RepoFile:
    text: str
    sha: str  # blob sha, to detect the file changing before the PR is opened


def get_file(repo: str, path: str, ref: str | None = None) -> RepoFile:
    data = api("GET", f"repos/{repo}/contents/{path}" + (f"?ref={ref}" if ref else ""))
    return RepoFile(base64.b64decode(data["content"]).decode(), data["sha"])


@dataclass
class FileChange:
    path: str
    base_sha: str  # blob the change was computed from
    text: str      # new content


PR_LABEL = "cnpg-users-grants"
PR_LABEL_COLOR = "1d76db"


@dataclass
class PullRequest:
    url: str
    action: str  # "opened", or "updated" (an open PR with our label, rewritten)


def labeled_open_prs(repo: str, label: str = PR_LABEL) -> list[dict]:
    """Open PRs with the label whose branch is in the repo itself (newest first)."""
    prs = api("GET", f"repos/{repo}/pulls?state=open&per_page=100")
    return [pr for pr in prs
            if label in {lbl["name"] for lbl in pr.get("labels") or []}
            and (pr["head"].get("repo") or {}).get("full_name") == repo]


def ensure_label(repo: str, label: str = PR_LABEL) -> None:
    try:
        api("GET", f"repos/{repo}/labels/{label}")
    except GitHubError as exc:
        if exc.status != 404:
            raise
        api("POST", f"repos/{repo}/labels", {
            "name": label, "color": PR_LABEL_COLOR, "description": "Opened by cnpg-users.py sync-users (cnpg-users-grants)",
        })


def open_pr(repo: str, changes: list[FileChange], branch: str, title: str, body: str) -> PullRequest:
    """Put the changes in one commit on top of the default branch and point a
    PR at it: the open PR labeled PR_LABEL if there is one (its branch is
    rewritten, title and body replaced), else `branch` with a new PR, labeled
    PR_LABEL. If any file moved on the default branch since it was read,
    nothing is pushed."""
    labeled = labeled_open_prs(repo)
    reuse = labeled[0] if labeled else None
    branch = reuse["head"]["ref"] if reuse else branch

    default = api("GET", f"repos/{repo}")["default_branch"]
    head = api("GET", f"repos/{repo}/git/ref/heads/{default}")["object"]["sha"]
    for change in changes:
        if get_file(repo, change.path, ref=head).sha != change.base_sha:
            raise GitHubError(f"{repo}:{change.path} changed on {default} since it was read; re-run")

    base_tree = api("GET", f"repos/{repo}/git/commits/{head}")["tree"]["sha"]
    tree = api("POST", f"repos/{repo}/git/trees", {
        "base_tree": base_tree,
        "tree": [{"path": c.path, "mode": "100644", "type": "blob", "content": c.text} for c in changes],
    })["sha"]
    commit = api("POST", f"repos/{repo}/git/commits", {"message": title, "tree": tree, "parents": [head]})["sha"]
    try:
        api("GET", f"repos/{repo}/git/ref/heads/{branch}")
        api("PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": commit, "force": True})
    except GitHubError as exc:
        if exc.status != 404:
            raise
        api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": commit})

    if reuse is None:  # an open unlabeled PR on the branch (from before labels) is reused too
        owner = repo.split("/")[0]
        reuse = next(iter(api("GET", f"repos/{repo}/pulls?state=open&head={owner}:{branch}")), None)
    if reuse is not None:
        pr = api("PATCH", f"repos/{repo}/pulls/{reuse['number']}", {"title": title, "body": body})
        action = "updated"
    else:
        pr = api("POST", f"repos/{repo}/pulls", {"title": title, "head": branch, "base": default, "body": body})
        action = "opened"
    ensure_label(repo)
    api("POST", f"repos/{repo}/issues/{pr['number']}/labels", {"labels": [PR_LABEL]})
    return PullRequest(pr["html_url"], action)
