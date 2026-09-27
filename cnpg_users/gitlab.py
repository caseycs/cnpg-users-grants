"""GitLab REST API via the glab CLI (uses its login; host is glab's default
unless the cluster file sets gitlab_host)."""

from __future__ import annotations

import base64
import json
import re
import subprocess
from urllib.parse import quote

from .repos import PR_LABEL, FileChange, PullRequest, RepoError, RepoFile


class GitLabError(RepoError):
    pass


def api(method: str, endpoint: str, payload: dict | None = None, host: str | None = None):
    cmd = ["glab", "api", "-X", method, endpoint]
    if host:
        cmd += ["--hostname", host]
    if payload is not None:
        # --input doesn't set a content type; JSON endpoints answer 415 without one
        cmd += ["--input", "-", "-H", "Content-Type: application/json"]
    # own session: Ctrl+C in the terminal reaches only this tool, not glab
    result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                            capture_output=True, text=True, start_new_session=True)
    if result.returncode:
        m = re.search(r"HTTP (\d{3})|\b(\d{3}) [A-Z][a-z]", result.stderr)
        raise GitLabError(f"{method} {endpoint}: {result.stderr.strip()}", int(m[1] or m[2]) if m else None)
    return json.loads(result.stdout) if result.stdout.strip() else None


def _enc(value: str) -> str:
    return quote(value, safe="")


def get_file(repo: str, path: str, ref: str | None = None, host: str | None = None) -> RepoFile:
    project = _enc(repo)
    if ref is None:  # GitLab needs a ref; GitHub defaults to the default branch
        ref = api("GET", f"projects/{project}", host=host)["default_branch"]
    data = api("GET", f"projects/{project}/repository/files/{_enc(path)}?ref={_enc(ref)}", host=host)
    return RepoFile(base64.b64decode(data["content"]).decode(), data["blob_id"])


def open_mrs(repo: str, project_id: int, query: str, host: str | None = None) -> list[dict]:
    """Open MRs matching query whose source branch is in the project itself (newest first)."""
    mrs = api("GET", f"projects/{_enc(repo)}/merge_requests?state=opened&per_page=100&{query}", host=host)
    return [mr for mr in mrs if mr.get("source_project_id") == project_id]


def open_pr(repo: str, changes: list[FileChange], branch: str, title: str, body: str,
            host: str | None = None) -> PullRequest:
    """Like github.open_pr, as a merge request: one commit on top of the
    default branch, on the branch of the open MR labeled PR_LABEL if there is
    one (force-pushed, title and description replaced), else on `branch` with
    a new labeled MR. If any file moved on the default branch since it was
    read, nothing is pushed. GitLab creates the label if it's missing."""
    project = _enc(repo)
    info = api("GET", f"projects/{project}", host=host)
    default = info["default_branch"]
    labeled = open_mrs(repo, info["id"], f"labels={_enc(PR_LABEL)}", host=host)
    reuse = labeled[0] if labeled else None
    branch = reuse["source_branch"] if reuse else branch

    head = api("GET", f"projects/{project}/repository/branches/{_enc(default)}", host=host)["commit"]["id"]
    for change in changes:
        if get_file(repo, change.path, ref=head, host=host).sha != change.base_sha:
            raise GitLabError(f"{repo}:{change.path} changed on {default} since it was read; re-run")

    api("POST", f"projects/{project}/repository/commits", {
        "branch": branch,
        "start_sha": head,
        "force": True,  # the branch becomes exactly head + this commit
        "commit_message": title,
        "actions": [{"action": "update", "file_path": c.path, "content": c.text} for c in changes],
    }, host=host)

    if reuse is None:  # an open unlabeled MR on the branch is reused too
        reuse = next(iter(open_mrs(repo, info["id"], f"source_branch={_enc(branch)}", host=host)), None)
    if reuse is not None:
        mr = api("PUT", f"projects/{project}/merge_requests/{reuse['iid']}",
                 {"title": title, "description": body, "add_labels": PR_LABEL}, host=host)
        action = "updated"
    else:
        mr = api("POST", f"projects/{project}/merge_requests", {
            "source_branch": branch, "target_branch": default, "title": title,
            "description": body, "labels": PR_LABEL, "remove_source_branch": True,
        }, host=host)
        action = "opened"
    return PullRequest(mr["web_url"], action)
