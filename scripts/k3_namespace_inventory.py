#!/usr/bin/env python3
"""K3 命名空间清册与槽位合规审计（MEM-L3-02 收口的全库证据）。

``bianbu-agent-memoryd --report-namespaces`` 是设备上唯一能列出**全部** memory
namespace 的口子：协议里每个方法的作用域都是调用者自己的
``(account_id, agent_id, memory_space_id)``，所以退役的测试空间、探针空间、某次
联调留下的 runtime-context 空间，对任何身份都不可见，却会永久留在库里。本脚本
把那份只读清册取回来，并逐条检查每个空间里实际用到的 ``slot:`` 标签是否都在
``bianbu_agent_sdk.memory.ontology`` 词典内 —— 这正是 MEM-L3-02 要收口的东西：
0.1.17 的写入闸门只拦新写入，拦不住 0.1.17 之前落库的槽位。

用法（K3，仓库根目录）：

    echo bianbu | sudo -S python3 scripts/k3_namespace_inventory.py

    # 也可以直接吃一份已保存的报告（离线复核 / 留证）
    python3 scripts/k3_namespace_inventory.py --report /home/example/namespace-report.json

读库密钥来自 ``--account-secret-file``，因此默认路径需要 root（或能读
``/etc/bianbu-agent-memory/account-secret``）；脚本本身不会再调用 sudo。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from bianbu_agent_sdk.memory.ontology import known_slots, resolve_slot

DAEMON = os.environ.get("MEMORYD", "/usr/libexec/bianbu-agent-memoryd")
DATABASE = os.environ.get("MEMORY_DATABASE", "/var/lib/bianbu-agent-memory/memory.db")
KEY_PACKAGE = os.environ.get(
    "MEMORY_KEY_PACKAGE", "/var/lib/bianbu-agent-memory/memory.keypkg"
)
ACCOUNT_SECRET = os.environ.get(
    "MEMORY_ACCOUNT_SECRET", "/etc/bianbu-agent-memory/account-secret"
)


def daemon_report() -> dict:
    """向设备上的守护进程二进制要一份只读命名空间清册。"""

    command = [
        DAEMON,
        "--report-namespaces",
        "--database",
        DATABASE,
        "--key-package",
        KEY_PACKAGE,
        "--account-secret-file",
        ACCOUNT_SECRET,
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            " ".join(command)
            + " failed (%d): %s" % (result.returncode, (result.stderr or "").strip()[-400:])
        )
    return json.loads(result.stdout)


def audit(report: dict) -> dict:
    dictionary = set(known_slots())
    namespaces = []
    violations = []
    empty = []
    for namespace in report.get("namespaces") or []:
        declared = [str(slot) for slot in (namespace.get("declared_slots") or [])]
        unknown = sorted({slot for slot in declared if resolve_slot(slot) is None})
        counts = namespace.get("counts") or {}
        entry = {
            "account_id": namespace.get("account_id"),
            "agent_id": namespace.get("agent_id"),
            "memory_space_id": namespace.get("memory_space_id"),
            "counts": counts,
            "declared_slots": declared,
            "unknown_slots": unknown,
        }
        namespaces.append(entry)
        for slot in unknown:
            violations.append(
                {
                    "account_id": entry["account_id"],
                    "agent_id": entry["agent_id"],
                    "memory_space_id": entry["memory_space_id"],
                    "slot": slot,
                }
            )
        if all(
            int(counts.get(key) or 0) == 0
            for key in (
                "long_term_memories",
                "memory_embeddings",
                "session_events",
                "session_summaries",
                "instructions",
            )
        ):
            empty.append(entry)
    return {
        "dictionary_size": len(dictionary),
        "database": report.get("database"),
        "storage_encryption": report.get("storage_encryption"),
        "namespace_count": report.get("namespace_count"),
        "long_term_memories": report.get("long_term_memories"),
        "namespaces": namespaces,
        "empty_namespaces": [
            {
                "account_id": entry["account_id"],
                "agent_id": entry["agent_id"],
                "memory_space_id": entry["memory_space_id"],
                "tombstones": int(entry["counts"].get("tombstones") or 0),
            }
            for entry in empty
        ],
        "unknown_slots": violations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        type=Path,
        help="已保存的 --report-namespaces JSON；不给就现场调用守护进程二进制",
    )
    arguments = parser.parse_args()

    attempt: dict = {"daemon": DAEMON, "database": DATABASE}
    try:
        report = (
            json.loads(arguments.report.read_text(encoding="utf-8"))
            if arguments.report is not None
            else daemon_report()
        )
        attempt["audit"] = audit(report)
    except Exception as error:  # noqa: BLE001 - 审计脚本只报告，不隐藏
        attempt["error"] = f"{type(error).__name__}: {error}"

    audit_result = attempt.get("audit") or {}
    print(json.dumps(attempt, ensure_ascii=False, indent=2))
    print(
        "namespaces=%s long_term_memories=%s unknown_slots=%s"
        % (
            audit_result.get("namespace_count"),
            audit_result.get("long_term_memories"),
            len(audit_result.get("unknown_slots") or []),
        )
    )
    if "error" in attempt or audit_result.get("unknown_slots"):
        print("K3_NAMESPACE_INVENTORY_FAIL")
        return 1
    print("K3_NAMESPACE_INVENTORY_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
