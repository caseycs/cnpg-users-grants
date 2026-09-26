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
    result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True)
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


def open_pr(repo: str, path: str, base_sha: str, text: str, branch: str, title: str, body: str) -> str:
    """Put `text` at `path` on `branch` (reset to the default branch) and
    open a PR for it, or update the open one; returns the PR url.
    base_sha is the blob the change was computed from: if the default
    branch has moved on for this file, nothing is pushed."""
    default = api("GET", f"repos/{repo}")["default_branch"]
    head = api("GET", f"repos/{repo}/git/ref/heads/{default}")["object"]["sha"]
    try:
        api("GET", f"repos/{repo}/git/ref/heads/{branch}")
        api("PATCH", f"repos/{repo}/git/refs/heads/{branch}", {"sha": head, "force": True})
    except GitHubError as exc:
        if exc.status != 404:
            raise
        api("POST", f"repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})

    current = get_file(repo, path, ref=branch)
    if current.sha != base_sha:
        raise GitHubError(f"{repo}:{path} changed on {default} since it was read; re-run")
    api("PUT", f"repos/{repo}/contents/{path}", {
        "message": title,
        "content": base64.b64encode(text.encode()).decode(),
        "sha": current.sha,
        "branch": branch,
    })

    owner = repo.split("/")[0]
    open_prs = api("GET", f"repos/{repo}/pulls?state=open&head={owner}:{branch}")
    if open_prs:
        pr = api("PATCH", f"repos/{repo}/pulls/{open_prs[0]['number']}", {"title": title, "body": body})
    else:
        pr = api("POST", f"repos/{repo}/pulls", {"title": title, "head": branch, "base": default, "body": body})
    return pr["html_url"]
