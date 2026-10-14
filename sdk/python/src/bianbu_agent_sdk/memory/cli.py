from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .client import DEFAULT_SOCKET, MemoryClient
from .errors import ErrorContext, MemoryError, NotFound


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bianbu-agent-memory")
    parser.add_argument(
        "--socket",
        default=os.environ.get("BIANBU_AGENT_MEMORY_SOCKET", DEFAULT_SOCKET),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health")
    subparsers.add_parser("stats")
    subparsers.add_parser("settings")
    settings = subparsers.add_parser("settings-update")
    settings.add_argument("--memory-enabled", choices=("true", "false"))
    settings.add_argument("--retention-days", type=int, choices=(0, 7, 30, 90))
    settings.add_argument("--audit-enabled", choices=("true", "false"))
    audit = subparsers.add_parser("audit")
    audit.add_argument("--limit", type=int, default=100)
    audit_export = subparsers.add_parser("audit-export")
    audit_export.add_argument("destination", type=Path)
    audit_export.add_argument("--limit", type=int, default=500)
    export = subparsers.add_parser("export")
    export.add_argument("destination", type=Path)
    export.add_argument("--limit", type=int, default=500)
    export.add_argument("--without-audit", action="store_true")
    checkpoint = subparsers.add_parser("checkpoint")
    checkpoint.add_argument("session_id")
    checkpoints = subparsers.add_parser("checkpoints")
    checkpoints.add_argument("--limit", type=int, default=10)
    restore = subparsers.add_parser("restore-checkpoint")
    restore.add_argument("checkpoint_ref")
    maintenance = subparsers.add_parser("maintenance")
    maintenance.add_argument("--keep-expired", action="store_true")
    clear_history = subparsers.add_parser("clear-history")
    clear_history.add_argument("--confirm-token", required=True)
    clear_all = subparsers.add_parser("clear-all")
    clear_all.add_argument("--confirm-token", required=True)
    mark = subparsers.add_parser(
        "mark-important",
        help="set the user-designated important mark on a MEM-L3 record",
    )
    mark.add_argument("memory_id")
    unmark = subparsers.add_parser(
        "unmark-important",
        help="clear the user-designated important mark on a MEM-L3 record",
    )
    unmark.add_argument("memory_id")
    return parser


def _boolean(value: str | None) -> bool | None:
    return None if value is None else value == "true"


def _write_json(path: Path, value: Any) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    return {"written": str(path), "format": value.get("format")}


def _set_important(
    client: MemoryClient, memory_id: str, important: bool
) -> dict[str, Any]:
    """Toggle the reserved important mark through the daemon revise path.

    ``long_term.revise`` replaces the whole tag list, so the record is read
    back first: the SDK helper keeps every other tag — including the field
    slot — and the mark is the only thing that changes.
    """
    records = client.long_term.get_many([memory_id])
    if not records:
        raise NotFound(
            ErrorContext(
                code="NotFound",
                message=f"memory {memory_id} was not found in this memory space",
                request_id="unknown",
                trace_id="unknown",
            )
        )
    receipt = client.long_term.mark_important(records[0], important=important)
    return {
        "memory_id": receipt.record_id,
        "revision": receipt.revision,
        "important": important,
        "tags": list(receipt.raw.get("tags") or []),
        "unchanged": bool(receipt.raw.get("unchanged")),
    }


def _execute(client: MemoryClient, arguments: argparse.Namespace) -> Any:
    if arguments.command == "health":
        return client.health()
    if arguments.command == "stats":
        return client.stats()
    if arguments.command == "settings":
        return client.privacy.settings()
    if arguments.command == "settings-update":
        return client.privacy.update_settings(
            memory_enabled=_boolean(arguments.memory_enabled),
            retention_days=arguments.retention_days,
            audit_enabled=_boolean(arguments.audit_enabled),
        )
    if arguments.command == "audit":
        return client.audit.list(limit=arguments.limit)
    if arguments.command == "audit-export":
        return _write_json(
            arguments.destination,
            client.audit.export(limit=arguments.limit),
        )
    if arguments.command == "export":
        return _write_json(
            arguments.destination,
            client.privacy.export_data(
                limit_per_collection=arguments.limit,
                include_audit=not arguments.without_audit,
            ),
        )
    if arguments.command == "checkpoint":
        return client.runtime.prepare_checkpoint(arguments.session_id)
    if arguments.command == "checkpoints":
        return {"checkpoints": client.runtime.list_checkpoints(limit=arguments.limit)}
    if arguments.command == "restore-checkpoint":
        return client.runtime.restore_checkpoint(arguments.checkpoint_ref)
    if arguments.command == "maintenance":
        return client.privacy.run_maintenance(
            remove_expired=not arguments.keep_expired
        )
    if arguments.command == "clear-history":
        return client.privacy.clear_history(confirm_token=arguments.confirm_token)
    if arguments.command == "clear-all":
        return client.privacy.clear_all(confirm_token=arguments.confirm_token)
    if arguments.command in {"mark-important", "unmark-important"}:
        return _set_important(
            client, arguments.memory_id, arguments.command == "mark-important"
        )
    raise AssertionError(arguments.command)


def main(argv: list[str] | None = None) -> None:
    arguments = _parser().parse_args(argv)
    try:
        with MemoryClient.connect(arguments.socket) as client:
            result = _execute(client, arguments)
    except MemoryError as error:
        # B7②: the CLI is used by registered Agents, but a denied or refused
        # caller must still get one actionable line instead of a raw traceback.
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    except OSError as error:
        print(f"error: ServerUnavailable: {error}", file=sys.stderr)
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
