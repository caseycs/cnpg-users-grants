import pytest

from cnpg_users import cli
from cnpg_users.runner import FatalError


def test_fatal_error_printed_once_exit_1(monkeypatch, capsys):
    def main():
        raise FatalError("kubeconfig credentials (aws) failed: Token has expired")

    def exit_now(code):
        raise SystemExit(code)

    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setattr(cli.os, "_exit", exit_now)
    with pytest.raises(SystemExit) as exc:
        cli.run()
    assert exc.value.code == 1
    assert capsys.readouterr().err == "\nerror: kubeconfig credentials (aws) failed: Token has expired\n"
