from __future__ import annotations

import json

import pytest

from bianbu_agent_sdk.memory import cli
from bianbu_agent_sdk.memory.errors import ErrorContext, PermissionDenied, Unauthenticated


def _error(error_type, message: str):
    return error_type(
        ErrorContext(
            code=error_type.code,
            message=message,
            request_id="req-1",
            trace_id="trace-1",
        )
    )


class _FakeClient:
    def __init__(self, payload: dict | None = None, failure: Exception | None = None) -> None:
        self._payload = payload or {}
        self._failure = failure

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def health(self) -> dict:
        if self._failure is not None:
            raise self._failure
        return self._payload


def test_cli_reports_unauthenticated_without_traceback(monkeypatch, capsys) -> None:
    def refuse(socket_path, *args, **kwargs):
        raise _error(Unauthenticated, "runtime registration was not found")

    monkeypatch.setattr(cli.MemoryClient, "connect", refuse)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["health"])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: Unauthenticated: runtime registration was not found" in captured.err
    assert "Traceback" not in captured.err


def test_cli_reports_unreachable_socket_without_traceback(monkeypatch, capsys) -> None:
    def refuse(socket_path, *args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", str(socket_path))

    monkeypatch.setattr(cli.MemoryClient, "connect", refuse)
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["health"])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ServerUnavailable: ")
    assert "Traceback" not in captured.err


def test_cli_reports_command_failure_without_traceback(monkeypatch, capsys) -> None:
    failure = _error(PermissionDenied, "audit.read is not granted")
    monkeypatch.setattr(
        cli.MemoryClient,
        "connect",
        lambda socket_path, *args, **kwargs: _FakeClient(failure=failure),
    )
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["health"])
    assert exit_info.value.code == 2
    captured = capsys.readouterr()
    assert "error: PermissionDenied: audit.read is not granted" in captured.err
    assert "Traceback" not in captured.err


def test_cli_prints_json_on_success(monkeypatch, capsys) -> None:
    payload = {"status": "ok", "components": {"store": "ok"}}
    monkeypatch.setattr(
        cli.MemoryClient,
        "connect",
        lambda socket_path, *args, **kwargs: _FakeClient(payload=payload),
    )
    cli.main(["health"])
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload
    assert captured.err == ""


def test_cli_import_reads_the_document_and_passes_the_confirmation_token(
    monkeypatch, tmp_path, capsys
) -> None:
    document = {
        "format": "bianbu-agent-memory.export.v1",
        "session_events": [],
        "session_summaries": [],
        "long_term_memories": [],
    }
    source = tmp_path / "export.json"
    source.write_text(json.dumps(document), encoding="utf-8")
    captured: dict = {}

    class _Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        @classmethod
        def connect(cls, *args, **kwargs):
            return cls()

        def __enter__(self):
            return self

        def __exit__(self, *exc_info) -> bool:
            return False

        @property
        def privacy(self):
            class _Privacy:
                def import_data(self, payload, *, confirm_token):
                    captured["payload"] = payload
                    captured["token"] = confirm_token
                    return {"imported": {"long_term_memories": 2}}

            return _Privacy()

    monkeypatch.setattr(cli, "MemoryClient", _Client)
    cli.main(["import", str(source), "--confirm-token", "token"])

    assert captured["payload"] == document
    assert captured["token"] == "token"
    assert '"long_term_memories": 2' in capsys.readouterr().out
