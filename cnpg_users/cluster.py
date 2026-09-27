"""Live CNPG cluster access: Cluster CR, CRs in its namespace, psql on the primary."""

from __future__ import annotations

import json
import sys

from kubernetes import client as k8s_client
from kubernetes import config as k8s_config
from kubernetes.stream import stream as k8s_stream
from kubernetes.stream.ws_client import ERROR_CHANNEL, STDERR_CHANNEL, STDOUT_CHANNEL

from . import eks, queries
from .config import DbConfig
from .grants import Catalog, Grant
from .grants import live_grants as grants_from_acl

eks.install()  # EKS tokens via boto3, not a process per connection

CNPG_GROUP = "postgresql.cnpg.io"
CNPG_VERSION = "v1"


class Cluster:
    def __init__(self, db: DbConfig):
        self.db = db
        # a client of its own: load_kube_config() would set a process-wide
        # default, racing between threads that read different clusters
        api_client = k8s_config.new_client_from_config(context=db.context)
        self.custom = k8s_client.CustomObjectsApi(api_client)
        self.core = k8s_client.CoreV1Api(api_client)
        try:
            self.cr = self.custom.get_namespaced_custom_object(
                CNPG_GROUP, CNPG_VERSION, db.namespace, "clusters", db.cluster
            )
        except k8s_client.ApiException as exc:
            sys.exit(f"error reading cluster {db.namespace}/{db.cluster}: {exc.reason}")
        self.primary = (self.cr.get("status") or {}).get("currentPrimary")
        if not self.primary:
            sys.exit(f"cluster {db.namespace}/{db.cluster} has no current primary pod")

    def list_crs(self, plural: str) -> list[dict]:
        try:
            return self.custom.list_namespaced_custom_object(
                CNPG_GROUP, CNPG_VERSION, self.db.namespace, plural
            ).get("items") or []
        except k8s_client.ApiException:
            return []

    def psql(self, sql: str, database: str | None = None) -> str:
        cmd = ["psql", "-U", "postgres"]
        if database:
            cmd += ["-d", database]
        cmd += ["-tAc", sql]
        where = f"pod {self.primary}{f', db {database}' if database else ''}"
        try:
            resp = k8s_stream(
                self.core.connect_get_namespaced_pod_exec,
                self.primary,
                self.db.namespace,
                command=cmd,
                container="postgres",
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
        except k8s_client.ApiException as exc:
            sys.exit(f"error execing into {where}: {exc.reason}")
        resp.run_forever(timeout=120)
        out = resp.read_channel(STDOUT_CHANNEL)
        status_raw = resp.read_channel(ERROR_CHANNEL)
        try:
            status = json.loads(status_raw) if status_raw else {}
        except json.JSONDecodeError:
            status = {}
        if status.get("status") != "Success":
            err = resp.read_channel(STDERR_CHANNEL)
            sys.exit(f"psql on {self.db.name} ({where}) failed: {(err or status_raw or '').strip()}")
        return out

    def execute(self, statements: list[str], database: str, batch_chars: int = 60_000) -> None:
        """Run statements in `database`, one transaction per batch (a batch is
        one psql -c, kept well under the exec argument size limit)."""
        batch: list[str] = []
        size = 0
        for s in statements + [None]:
            if s is None or (batch and size + len(s) > batch_chars):
                if batch:
                    self.psql("BEGIN;\n" + "\n".join(batch) + "\nCOMMIT;", database=database)
                batch, size = [], 0
            if s is not None:
                batch.append(s)
                size += len(s) + 1

    def sql(self, sql: str, database: str | None = None):
        out = self.psql(sql, database=database)
        try:
            return json.loads(out.replace("\r", "").strip())
        except json.JSONDecodeError:
            sys.exit(f"unexpected psql output (db={database or 'postgres'}):\n{out}")

    def databases(self) -> list[str]:
        return self.sql(queries.CLUSTER)["databases"]

    def live_grants(self, database: str) -> set[Grant]:
        return grants_from_acl(self.sql(queries.DB_ACL, database=database))

    def existing_roles(self, names: list[str]) -> set[str]:
        if not names:
            return set()
        in_list = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
        return set(self.sql(
            f"SELECT coalesce(jsonb_agg(rolname), '[]'::jsonb) FROM pg_roles WHERE rolname IN ({in_list})"
        ))

    def drop_blockers(self, names: list[str]) -> list[dict]:
        """What stops DROP ROLE for these roles: per role and database, objects
        owned and privileges held (pg_shdepend), with the database's owner."""
        if not names:
            return []
        in_list = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
        return self.sql(f"""
            SELECT coalesce(jsonb_agg(q ORDER BY q.role, q.database), '[]'::jsonb) FROM (
              SELECT r.rolname AS role, coalesce(d.datname, '') AS database,
                     coalesce(pg_get_userbyid(d.datdba), 'postgres') AS db_owner,
                     count(*) FILTER (WHERE s.deptype = 'o') AS owned,
                     count(*) FILTER (WHERE s.deptype IN ('a', 'i')) AS privileges
              FROM pg_shdepend s
              JOIN pg_roles r ON r.oid = s.refobjid
              LEFT JOIN pg_database d ON d.oid = s.dbid
              WHERE s.refclassid = 'pg_authid'::regclass AND r.rolname IN ({in_list})
              GROUP BY r.rolname, d.datname, d.datdba) q""")

    def set_passwords(self, statements: list[str]) -> None:
        """Run ALTER ROLE ... PASSWORD statements in one transaction, with
        statement logging off so even the verifiers stay out of the logs."""
        self.psql("SET log_statement = 'none'; " + " ".join(statements))

    def password_verifiers(self, names: list[str]) -> dict[str, str]:
        """role -> stored verifier ('' without a password) for the existing roles in names."""
        if not names:
            return {}
        in_list = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
        return self.sql(
            "SELECT coalesce(jsonb_object_agg(rolname, coalesce(rolpassword, '')), '{}'::jsonb)"
            f" FROM pg_authid WHERE rolname IN ({in_list})"
        )

    def catalog(self, database: str) -> Catalog:
        return Catalog.from_json(self.sql(queries.CATALOG, database=database))
