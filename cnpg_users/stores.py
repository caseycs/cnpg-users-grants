"""Where human passwords live: user_passwords_store.yaml picks one store.

    type: aws-ssm              (default) one SecureString parameter per role:
      aws_profile, aws_region, aws_ssm_prefix  <prefix><role>
    type: gcp-secret-manager   one secret per role: <gcp_secret_prefix><role>
      gcp_project, gcp_secret_prefix           (Application Default Credentials;
                                               needs the [gcp] extra)
    type: sops                 one encrypted YAML/JSON file mapping role: password
      sops_file                               (path relative to the config
                                               directory; any sops key type)

Every store lists role names (import: who is a human), reads passwords
(sync-users) and creates a missing one (sync-users --apply). Values are only
ever held in memory."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from ruamel.yaml import YAML

PASSWORDS_STORE = "user_passwords_store.yaml"
ROLE_NAME = re.compile(r"[a-z_][a-z0-9_]*")


class PasswordStore(Protocol):
    @property
    def description(self) -> str: ...  # for messages, e.g. "AWS SSM /cnpg-user/ (default, eu-central-1)"

    def names(self) -> set[str]: ...
    def passwords(self, names: list[str]) -> dict[str, str]: ...
    def create(self, name: str, password: str) -> str: ...  # returns where it was stored


# --- AWS SSM Parameter Store ---------------------------------------------------

@dataclass
class AwsSsmStore:
    aws_profile: str = "default"
    aws_region: str = "eu-central-1"
    aws_ssm_prefix: str = "/cnpg-user/"

    @property
    def prefix(self) -> str:
        return "/" + self.aws_ssm_prefix.strip("/") + "/"

    @property
    def description(self) -> str:
        return f"AWS SSM {self.prefix} ({self.aws_profile}, {self.aws_region})"

    def _client(self):
        import boto3
        return boto3.Session(profile_name=self.aws_profile, region_name=self.aws_region).client("ssm")

    def _fail(self, action: str, exc: Exception):
        sys.exit(f"error {action} in {self.description}: {exc}")

    def names(self) -> set[str]:
        from botocore.exceptions import BotoCoreError, ClientError
        try:
            names = set()
            for page in self._client().get_paginator("describe_parameters").paginate(
                ParameterFilters=[{"Key": "Path", "Option": "OneLevel", "Values": [self.prefix.rstrip("/")]}]
            ):
                names |= {p["Name"][len(self.prefix):] for p in page["Parameters"]}
            return names
        except (BotoCoreError, ClientError) as exc:
            self._fail("listing parameters", exc)

    def passwords(self, names: list[str]) -> dict[str, str]:
        from botocore.exceptions import BotoCoreError, ClientError
        out: dict[str, str] = {}
        try:
            ssm = self._client()
            for i in range(0, len(names), 10):  # GetParameters takes at most 10 names
                resp = ssm.get_parameters(Names=[self.prefix + n for n in names[i:i + 10]], WithDecryption=True)
                out |= {p["Name"][len(self.prefix):]: p["Value"] for p in resp["Parameters"]}
            return out
        except (BotoCoreError, ClientError) as exc:
            self._fail("reading parameters", exc)

    def create(self, name: str, password: str) -> str:
        from botocore.exceptions import BotoCoreError, ClientError
        param = self.prefix + name
        try:
            self._client().put_parameter(Name=param, Type="SecureString", Value=password, Overwrite=False)
            return param
        except (BotoCoreError, ClientError) as exc:
            self._fail(f"creating {param}", exc)


# --- GCP Secret Manager ----------------------------------------------------------

@dataclass
class GcpSecretManagerStore:
    gcp_project: str
    gcp_secret_prefix: str = "cnpg-user-"
    client_factory: Callable[[], Any] | None = None  # tests inject a fake

    @property
    def description(self) -> str:
        return f"GCP Secret Manager {self.gcp_secret_prefix}* (project {self.gcp_project})"

    def _client(self) -> Any:
        if self.client_factory is not None:
            return self.client_factory()
        try:
            from google.cloud import secretmanager  # type: ignore[attr-defined]
        except ImportError:
            sys.exit("the gcp-secret-manager store needs google-cloud-secret-manager: "
                     "install with the [gcp] extra, e.g. uvx --from 'cnpg-users-grants[gcp] @ git+…' cnpg-users")
        return secretmanager.SecretManagerServiceClient()

    def names(self) -> set[str]:
        client = self._client()
        parent = f"projects/{self.gcp_project}"
        names = set()
        for secret in client.list_secrets(request={"parent": parent, "filter": f"name:{self.gcp_secret_prefix}"}):
            secret_id = secret.name.rsplit("/", 1)[-1]
            if secret_id.startswith(self.gcp_secret_prefix):
                names.add(secret_id[len(self.gcp_secret_prefix):])
        return names

    def passwords(self, names: list[str]) -> dict[str, str]:
        client = self._client()
        out: dict[str, str] = {}
        for name in names:
            path = f"projects/{self.gcp_project}/secrets/{self.gcp_secret_prefix}{name}/versions/latest"
            try:
                out[name] = client.access_secret_version(request={"name": path}).payload.data.decode()
            except Exception as exc:  # google.api_core.exceptions.NotFound, without importing it
                if type(exc).__name__ != "NotFound":
                    raise
        return out

    def create(self, name: str, password: str) -> str:
        client = self._client()
        secret_id = self.gcp_secret_prefix + name
        secret = client.create_secret(request={
            "parent": f"projects/{self.gcp_project}", "secret_id": secret_id,
            "secret": {"replication": {"automatic": {}}},
        })
        client.add_secret_version(request={"parent": secret.name, "payload": {"data": password.encode()}})
        return secret.name


# --- sops-encrypted file ---------------------------------------------------------

@dataclass
class SopsStore:
    sops_file: Path

    def __post_init__(self):
        self._lock = threading.Lock()
        self._data: dict[str, str] | None = None

    @property
    def description(self) -> str:
        return f"sops file {self.sops_file}"

    def _run(self, args: list[str], stdin: str | None = None) -> str:
        # own session: Ctrl+C in the terminal reaches only this tool, not sops
        result = subprocess.run(["sops", *args], input=stdin, capture_output=True, text=True, start_new_session=True)
        if result.returncode:
            sys.exit(f"sops {' '.join(a for a in args if not a.startswith('['))} failed: {result.stderr.strip()}")
        return result.stdout

    def _load(self) -> dict[str, str]:
        with self._lock:
            if self._data is None:
                if not self.sops_file.is_file():
                    sys.exit(f"{self.description}: not found; create it with sops (e.g. an empty mapping, "
                             f"encrypted per your .sops.yaml)")
                data = json.loads(self._run(["-d", "--output-type", "json", str(self.sops_file)]) or "{}")
                if not isinstance(data, dict):
                    sys.exit(f"{self.description}: expected a mapping of role: password")
                self._data = {k: str(v) for k, v in data.items() if k != "sops" and isinstance(v, (str, int))}
            return self._data

    def names(self) -> set[str]:
        return set(self._load())

    def passwords(self, names: list[str]) -> dict[str, str]:
        data = self._load()
        return {n: data[n] for n in names if n in data}

    def create(self, name: str, password: str) -> str:
        if not ROLE_NAME.fullmatch(name):
            sys.exit(f"refusing to store a password for {name!r}: not a plain role name")
        if name in self._load():
            sys.exit(f"{self.description}: {name} already has a password")
        # value on stdin, not argv: stays out of process listings
        self._run(["set", "--value-stdin", str(self.sops_file), json.dumps([name])], stdin=json.dumps(password))
        with self._lock:
            self._data = None
        return f"{self.sops_file} [{name}]"


# --- loading -----------------------------------------------------------------------

STORE_TYPES = {"aws-ssm": AwsSsmStore, "gcp-secret-manager": GcpSecretManagerStore, "sops": SopsStore}
STORE_KEYS = {
    "aws-ssm": {"aws_profile", "aws_region", "aws_ssm_prefix"},
    "gcp-secret-manager": {"gcp_project", "gcp_secret_prefix"},
    "sops": {"sops_file"},
}
REQUIRED = {"gcp-secret-manager": {"gcp_project"}, "sops": {"sops_file"}}


def load_store(root: Path) -> PasswordStore:
    path = root / PASSWORDS_STORE
    if not path.is_file():
        sys.exit(f"passwords store settings not found: {path}")
    data = YAML(typ="safe").load(path) or {}
    kind = str(data.pop("type", "aws-ssm"))
    if kind not in STORE_TYPES:
        sys.exit(f"{path}: unknown type {kind!r} (one of: {', '.join(STORE_TYPES)})")
    unknown = set(data) - STORE_KEYS[kind]
    if unknown:
        sys.exit(f"{path}: unknown keys for type {kind}: {', '.join(sorted(unknown))}")
    missing = REQUIRED.get(kind, set()) - set(data)
    if missing:
        sys.exit(f"{path}: type {kind} needs: {', '.join(sorted(missing))}")
    settings = {k: str(v) for k, v in data.items()}
    if kind == "sops":
        return SopsStore(root / settings["sops_file"])
    return STORE_TYPES[kind](**settings)
