"""sync: users and grants for every db at once. The read phase reports both
parts per db; --apply runs the users flow first (PR per GitOps repo, wait
for the merge, passwords), since grants may name roles that only exist
after it, then applies grants after one confirmation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO

from .config import DbConfig
from .stores import PasswordStore
from .sync import GrantsPlan, apply_grants, grants_body
from .users import UsersPlan, apply_all, users_body


@dataclass
class SyncPlan:
    users: UsersPlan | None   # None when the db has no repo/values_file
    grants: GrantsPlan


def cmd_sync(db: DbConfig, store: PasswordStore, apply: bool = False,
             out: TextIO = sys.stdout) -> tuple[bool, SyncPlan | None]:
    """One report per db: what differs in users and in grants, or one line."""
    if not db.online:
        print(f"{db.header}: offline — skipped", file=out)
        return False, None
    parts, drift, users = [], False, None
    if db.repo and db.values_file:
        body, users_drift, users = users_body(db, store, apply)
        parts.append(body)
        drift |= users_drift
    else:
        parts.append("  users: skipped (no repo/values_file in the db file)\n")
    body, grants_drift, grants = grants_body(db, users.cluster if users else None)
    parts.append(body)
    drift |= grants_drift
    text = "".join(parts)
    if not text:
        print(f"{db.header}: in sync", file=out)
    else:
        print(db.header, file=out)
        print(text, end="", file=out)
    return drift, SyncPlan(users, grants)


def apply_sync(plans: list[SyncPlan], store: PasswordStore, timeout: float = 180,
               assume_yes: bool = False) -> bool:
    """Users first (PRs, wait, passwords), then grants. True if anything still differs."""
    users = [p.users for p in plans if p.users is not None]
    drift = apply_all(users, store, timeout) if users else False
    # grants were planned before the users step; re-plan now that roles may exist
    fresh = []
    for p in plans:
        _, _, g = grants_body(p.grants.db, p.grants.cluster)
        fresh.append(g)
    return apply_grants(fresh, assume_yes) or drift
