"""Shared setup for the integration tests: a throwaway namespace with a
1-instance CNPG Cluster, and the tool pointed at it through a db file."""

from __future__ import annotations

import base64
import io
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from ruamel.yaml import YAML

from cnpg_users.cluster import Cluster
from cnpg_users.config import load_db_config
from cnpg_users.sync import cmd_sync_grants

CONTEXT = os.environ.get("CNPG_IT_CONTEXT")
KEEP = os.environ.get("CNPG_IT_KEEP")  # keep the namespace for debugging
CLUSTER = "pg"
DATABASE = "app"  # CNPG's default initdb database
DB_NAME = "pg.it.kind"


def wait(what: str, check, timeout: float = 600, every: float = 3):
    deadline = time.monotonic() + timeout
    while True:
        result = check()
        if result:
            return result
        if time.monotonic() > deadline:
            pytest.fail(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(every)


@contextmanager
def cnpg_cluster(root: Path, managed_roles: list[dict], secrets: dict[str, tuple[str, str]], db_file_extra: str = ""):
    """Namespace + Cluster with these managed roles and basic-auth secrets
    ({secret name: (username, password)}); yields an env dict."""
    if not CONTEXT:
        pytest.skip("set CNPG_IT_CONTEXT (task test:integration does)")
    api = k8s_config.new_client_from_config(context=CONTEXT)
    core, custom = k8s_client.CoreV1Api(api), k8s_client.CustomObjectsApi(api)
    ns = f"cnpg-users-it-{uuid.uuid4().hex[:6]}"
    core.create_namespace({"metadata": {"name": ns}})
    try:
        for name, (user, password) in secrets.items():
            core.create_namespaced_secret(ns, {
                "metadata": {"name": name}, "type": "kubernetes.io/basic-auth",
                "data": {"username": base64.b64encode(user.encode()).decode(),
                         "password": base64.b64encode(password.encode()).decode()},
            })
        custom.create_namespaced_custom_object("postgresql.cnpg.io", "v1", ns, "clusters", {
            "apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "metadata": {"name": CLUSTER},
            "spec": {"instances": 1, "storage": {"size": "1Gi"}, "managed": {"roles": managed_roles}},
        })

        def ready():
            status = custom.get_namespaced_custom_object(
                "postgresql.cnpg.io", "v1", ns, "clusters", CLUSTER).get("status") or {}
            return status.get("readyInstances") == 1 and status.get("currentPrimary")
        wait("the CNPG cluster to be ready", ready)

        (root / "dbs").mkdir()
        (root / "dbs" / f"{DB_NAME}.yaml").write_text(
            f"context: {CONTEXT}\nnamespace: {ns}\ncluster: {CLUSTER}\n"
            f"repo: example/gitops\nvalues_file: it/values.yaml\n{db_file_extra}"
        )
        cluster = Cluster(load_db_config(root, DB_NAME))
        present = [r["name"] for r in managed_roles if r.get("ensure", "present") == "present"]
        wait("managed roles", lambda: cluster.existing_roles(present) == set(present), every=2)
        yield {"root": root, "ns": ns, "core": core, "custom": custom, "cluster": cluster}
    finally:
        if not KEEP:
            core.delete_namespace(ns)


def fake_store(passwords: dict[str, str]):
    """A password store in memory (the real ones are covered by test_stores.py)."""
    return SimpleNamespace(
        description="fake store",
        names=lambda: set(passwords),
        passwords=lambda names: {n: passwords[n] for n in names if n in passwords},
        create=lambda name, password: passwords.__setitem__(name, password) or f"fake[{name}]",
    )


def db(env):
    return load_db_config(env["root"], DB_NAME)


def doc(env) -> dict:
    return YAML(typ="safe").load(db(env).path)


def grants_of(env, grantee, database=DATABASE):
    return doc(env)["grants"][database][grantee]


def sync(env):
    out = io.StringIO()
    drift, plan = cmd_sync_grants(db(env), out=out)
    return drift, plan, out.getvalue()
