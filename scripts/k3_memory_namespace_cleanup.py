#!/usr/bin/env python3
"""K3 全库历史槽位收口 —— MEM-L3-02 的现场执行脚本。

0.1.17 起写入闸门只接受 L3 词典内的槽位（``MemoryBridge`` / ``memory`` Tool 在写入
前 ``resolve_slot``），但闸门拦不住 0.1.17 之前落库的行：某次验收/联调留下的
``slot:test.marker``、``slot:workflow.deploy_action`` 会一直挂在库里，而且对任何
身份都不可见 —— 协议里每个方法只作用于调用者自己的 space，所以“退役的测试空间”
只能靠全库清册（``--report-namespaces``）看到，也只能以那个空间的身份清理。

为什么不直接改 SQL：canonical 库是 SQLCipher，且 forget 必须留下 Tombstone 与
outbox 同步语义（``long_term.forget``），否则云端旧备份会把删掉的行“复活”。

本脚本只执行 MEM-L3-02 这一条判据 —— 带词典外 ``slot:`` 标签的行 —— 不动其它任何
数据。判定口径与 ``k3_namespace_inventory.py`` 一致，两者应同时为 OK。

用法（K3，仓库根目录）：

    echo bianbu | sudo -S python3 scripts/k3_memory_namespace_cleanup.py          # 只报告
    echo bianbu | sudo -S python3 scripts/k3_memory_namespace_cleanup.py --apply  # 实际清理

清理走 ``long_term.forget``，``reason_code=l3_slot_gate_cleanup``；执行需要
``/etc/bianbu-agent-memory/adapter-confirmation.secret``，因此必须 root（或
``bianbu-agent`` 组）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bianbu_agent_sdk.memory.ontology import resolve_slot  # noqa: E402
from k3_memory_admin import (  # noqa: E402
    CLEANUP_CAPABILITIES,
    DEVICE_ID,
    REGISTRY,
    confirmation_secret,
    export_records,
    forget_record,
    memory_session,
    out_of_dictionary_slots,
    process_start_time_ticks,
)

DAEMON = os.environ.get("MEMORYD", "/usr/libexec/bianbu-agent-memoryd")
DATABASE = os.environ.get("MEMORY_DATABASE", "/var/lib/bianbu-agent-memory/memory.db")
KEY_PACKAGE = os.environ.get(
    "MEMORY_KEY_PACKAGE", "/var/lib/bianbu-agent-memory/memory.keypkg"
)
ACCOUNT_SECRET = os.environ.get(
    "MEMORY_ACCOUNT_SECRET", "/etc/bianbu-agent-memory/account-secret"
)
REASON_CODE = "l3_slot_gate_cleanup"


def namespace_report() -> dict:
    """取一份只读全库命名空间清册（root 专属：要读 key package 与 account secret）。"""

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
            + " failed (%d): %s"
            % (result.returncode, (result.stderr or "").strip()[-400:])
        )
    return json.loads(result.stdout)


def unknown_slots(report: dict) -> list[dict]:
    """摊平清册里所有“声明了词典外槽位”的命名空间。"""

    findings = []
    for namespace in report.get("namespaces") or []:
        for slot in sorted(
            {
                str(slot)
                for slot in (namespace.get("declared_slots") or [])
                if resolve_slot(str(slot)) is None
            }
        ):
            findings.append(
                {
                    "account_id": namespace.get("account_id"),
                    "agent_id": namespace.get("agent_id"),
                    "memory_space_id": namespace.get("memory_space_id"),
                    "slot": slot,
                }
            )
    return findings


def device_id_of(namespace: dict) -> str:
    """从 ``<account>.<agent>.<device>`` 里切回设备号。"""

    account_id = str(namespace.get("account_id") or "")
    agent_id = str(namespace.get("agent_id") or "")
    space_id = str(namespace.get("memory_space_id") or "")
    prefix = f"{account_id}.{agent_id}."
    return space_id[len(prefix) :] if space_id.startswith(prefix) else DEVICE_ID


def sweep_namespace(namespace: dict, secret: bytes | None, apply: bool) -> dict:
    """以该命名空间的身份导出、定位、清理带词典外槽位的行。"""

    account_id = str(namespace["account_id"])
    agent_id = str(namespace["agent_id"])
    space_id = str(namespace["memory_space_id"])
    outcome: dict = {
        "account_id": account_id,
        "agent_id": agent_id,
        "memory_space_id": space_id,
        "reported_slots": namespace.get("slots") or [],
        "rows": [],
        "forgotten": [],
        "remaining": [],
    }
    with memory_session(
        account_id=account_id,
        agent_id=agent_id,
        space_id=space_id,
        device_id=device_id_of(namespace),
        capabilities=CLEANUP_CAPABILITIES,
        instance_prefix="slot-cleanup",
    ) as client:
        rows = [
            item
            for item in (export_records(client).get("long_term_memories") or [])
            if out_of_dictionary_slots(item)
        ]
        for item in rows:
            # 现场证据不含记忆正文：全库口径的脚本不该把别人的记忆内容抄进
            # JSON，只留 id、槽位与正文指纹（可复核、不可逆读）。
            text = str(item.get("text") or "")
            outcome["rows"].append(
                {
                    "memory_id": item.get("memory_id"),
                    "slots": out_of_dictionary_slots(item),
                    "kind": item.get("kind"),
                    "text_length": len(text),
                    "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            )
        if apply and rows:
            if secret is None:
                raise RuntimeError(
                    "confirmation secret is unreadable; re-run as root (or bianbu-agent)"
                )
            for item in rows:
                forget_record(
                    client, str(item["memory_id"]), secret, reason_code=REASON_CODE
                )
                outcome["forgotten"].append(item["memory_id"])
        outcome["remaining"] = [
            item.get("memory_id")
            for item in (export_records(client).get("long_term_memories") or [])
            if out_of_dictionary_slots(item)
        ]
    return outcome


def stale_registrations() -> list[dict]:
    """列出身份注册目录里的死条目：进程已退出，或 pid 启动时刻不匹配。

    memoryd 的身份来自 ``memory-registry/<pid>.json``。脚本正常退出会删掉自己的
    注册，但被 kill 的现场进程会留下文件 —— 它们既不是记忆数据也不是凭据（启动
    时刻绑定使其无法被冒用），只是垃圾；顺手一起收掉，避免目录越长越难读。
    """

    findings: list[dict] = []
    for path in sorted(REGISTRY.glob("*.json")):
        entry: dict = {"path": str(path)}
        try:
            pid: int | None = int(path.stem)
        except ValueError:
            pid = None
        record: dict = {}
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            entry["reason"] = f"unreadable: {type(error).__name__}"
        if pid is None:
            entry.setdefault("reason", "filename is not a pid")
        else:
            try:
                live_ticks: int | None = process_start_time_ticks(pid)
            except OSError:
                live_ticks = None
            if live_ticks is None:
                entry.setdefault("reason", "process is gone")
            elif record.get("pid_start_time_ticks") != live_ticks:
                entry.setdefault("reason", "pid start time mismatched")
        if "reason" in entry:
            entry["agent_id"] = record.get("agent_id")
            entry["memory_space_id"] = record.get("memory_space_id")
            findings.append(entry)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="真正执行 forget（默认只报告）"
    )
    parser.add_argument(
        "--prune-registrations",
        action="store_true",
        help="顺手删掉 memory-registry 里已死进程留下的注册文件",
    )
    arguments = parser.parse_args()

    report: dict = {
        "database": DATABASE,
        "daemon": DAEMON,
        "apply": arguments.apply,
        "reason_code": REASON_CODE,
    }
    try:
        findings = unknown_slots(namespace_report())
        report["unknown_slots_before"] = findings
        # 一个命名空间可能命中多个槽位，按空间去重，只连一次、只扫一次。
        targets: dict[str, dict] = {}
        for finding in findings:
            entry = targets.setdefault(
                str(finding["memory_space_id"]),
                {
                    "account_id": finding["account_id"],
                    "agent_id": finding["agent_id"],
                    "memory_space_id": finding["memory_space_id"],
                    "slots": [],
                },
            )
            entry["slots"].append(finding["slot"])
        report["namespaces"] = []
        secret = confirmation_secret()
        for target in targets.values():
            try:
                report["namespaces"].append(sweep_namespace(target, secret, arguments.apply))
            except Exception as error:  # noqa: BLE001 - 现场脚本逐空间报告失败
                report["namespaces"].append(
                    {
                        "account_id": target.get("account_id"),
                        "agent_id": target.get("agent_id"),
                        "memory_space_id": target.get("memory_space_id"),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
        report["unknown_slots_after"] = unknown_slots(namespace_report())
        report["registrations"] = {"stale": stale_registrations(), "pruned": []}
        if arguments.prune_registrations:
            for entry in report["registrations"]["stale"]:
                try:
                    Path(entry["path"]).unlink()
                    report["registrations"]["pruned"].append(entry["path"])
                except OSError as error:
                    report.setdefault("remove_errors", []).append(
                        f"{entry['path']}: {type(error).__name__}: {error}"
                    )
            report["registrations"]["stale"] = stale_registrations()
    except Exception as error:  # noqa: BLE001
        report["error"] = f"{type(error).__name__}: {error}"

    print(json.dumps(report, ensure_ascii=False, indent=2))

    after = report.get("unknown_slots_after")
    unresolved = [
        entry
        for entry in report.get("namespaces") or []
        if entry.get("error") or entry.get("remaining")
    ]
    # 注册文件的死条目只影响“顺手收掉”这项，不改变 MEM-L3-02 的结论：
    # 只有显式传了 --prune-registrations 才会因为收不掉而判 FAIL。
    stale_after = (report.get("registrations") or {}).get("stale") or []
    if (
        "error" in report
        or after is None
        or after
        or unresolved
        or (stale_after and arguments.prune_registrations)
    ):
        if not arguments.apply and report.get("unknown_slots_before"):
            print("K3_NAMESPACE_CLEANUP_REQUIRED")
        else:
            print("K3_NAMESPACE_CLEANUP_FAIL")
        return 1
    print("K3_NAMESPACE_CLEANUP_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())