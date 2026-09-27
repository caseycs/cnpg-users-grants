import shutil
import subprocess
from types import SimpleNamespace

import pytest

from cnpg_users.stores import AwsSsmStore, GcpSecretManagerStore, SopsStore, load_store


def settings(tmp_path, text):
    (tmp_path / "passwords_store.yaml").write_text(text)
    return load_store(tmp_path)


def test_default_is_aws_ssm(tmp_path):
    store = settings(tmp_path, "aws_profile: p\naws_ssm_prefix: /x/\n")
    assert store == AwsSsmStore("p", "eu-central-1", "/x/")
    assert store.description == "AWS SSM /x/ (p, eu-central-1)"


def test_types_and_validation(tmp_path):
    assert settings(tmp_path, "type: gcp-secret-manager\ngcp_project: prj\n").description \
        == "GCP Secret Manager cnpg-user-* (project prj)"
    assert settings(tmp_path, "type: sops\nsops_file: secrets/pw.sops.yaml\n").sops_file == tmp_path / "secrets/pw.sops.yaml"
    for text, err in [
        ("type: vault\n", "unknown type 'vault'"),
        ("type: sops\n", "needs: sops_file"),
        ("type: gcp-secret-manager\n", "needs: gcp_project"),
        ("type: sops\nsops_file: x\naws_profile: p\n", "unknown keys for type sops: aws_profile"),
        ("profile: p\n", "unknown keys for type aws-ssm: profile"),
    ]:
        with pytest.raises(SystemExit, match=err):
            settings(tmp_path, text)


class FakeSecretManager:
    """The subset of SecretManagerServiceClient the store uses."""

    def __init__(self):
        self.secrets = {"projects/p/secrets/cnpg-user-alice": "pw-a", "projects/p/secrets/other": "x"}

    def list_secrets(self, request):
        assert request == {"parent": "projects/p", "filter": "name:cnpg-user-"}
        return [SimpleNamespace(name=n) for n in self.secrets]  # the server filter is substring: other gets through

    def access_secret_version(self, request):
        name = request["name"].removesuffix("/versions/latest")
        if name not in self.secrets:
            raise type("NotFound", (Exception,), {})()
        return SimpleNamespace(payload=SimpleNamespace(data=self.secrets[name].encode()))

    def create_secret(self, request):
        name = f"{request['parent']}/secrets/{request['secret_id']}"
        assert request["secret"] == {"replication": {"automatic": {}}}
        self.secrets[name] = None
        return SimpleNamespace(name=name)

    def add_secret_version(self, request):
        self.secrets[request["parent"]] = request["payload"]["data"].decode()


def test_gcp_secret_manager_store():
    fake = FakeSecretManager()
    store = GcpSecretManagerStore("p", client_factory=lambda: fake)
    assert store.names() == {"alice"}
    assert store.passwords(["alice", "bob"]) == {"alice": "pw-a"}
    assert store.create("bob", "pw-b") == "projects/p/secrets/cnpg-user-bob"
    assert store.passwords(["bob"]) == {"bob": "pw-b"}


@pytest.mark.skipif(not (shutil.which("sops") and shutil.which("age-keygen")), reason="needs sops and age")
def test_sops_store_with_real_sops(tmp_path, monkeypatch):
    key = tmp_path / "age.key"
    subprocess.run(["age-keygen", "-o", str(key)], check=True, capture_output=True)
    recipient = next(l.split(": ")[1] for l in key.read_text().splitlines() if l.startswith("# public key"))
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(key))
    plain = tmp_path / "pw.yaml"
    plain.write_text("alice: pw-a\n")
    enc = tmp_path / "pw.sops.yaml"
    enc.write_text(subprocess.run(["sops", "-e", "--age", recipient, str(plain)],
                                  check=True, capture_output=True, text=True).stdout)
    assert "pw-a" not in enc.read_text()

    store = SopsStore(enc)
    assert store.names() == {"alice"}
    assert store.passwords(["alice", "bob"]) == {"alice": "pw-a"}
    assert store.create("bob", "pw-b'\"$(x)") == f"{enc} [bob]"
    assert store.passwords(["alice", "bob"]) == {"alice": "pw-a", "bob": "pw-b'\"$(x)"}
    assert "pw-b" not in enc.read_text()  # stays encrypted
    with pytest.raises(SystemExit, match="already has a password"):
        store.create("bob", "again")
    with pytest.raises(SystemExit, match="not a plain role name"):
        store.create("../x", "pw")


def test_sops_store_missing_file(tmp_path):
    with pytest.raises(SystemExit, match="not found"):
        SopsStore(tmp_path / "nope.sops.yaml").names()
