#!/usr/bin/env python3
"""K3 现网记忆空间残留自检与收口（MEM-L3-02 / MEM-L3-03 的现场证据）。

``k3_production_install_verify.py`` 回答“装到设备上的包是不是按新配置在跑”，
本脚本回答“设备里现在还存着什么不该留的东西”。两者都直连现网守护进程
（``/run/bianbu/agent-memoryd.sock``）、都不启动测试实例；区别是本脚本注册到
**用户 Runtime 的 memory space**，因为要收口的历史数据就写在那里。

为什么需要它：0.1.17 的写入闸门（``MemoryBridge`` / ``memory`` Tool）只拦新写入 ——
词典之外的 ``slot:`` 标签、验收脚本留下的探针记录、被当成偏好存下来的用户提问，
都是 0.1.17 之前写进生产库的，不会自己消失。规格 MEM-L3-02 要收口，就得把这批行
清掉并且留下现场证据。

判定标准（全部来自词典与规格，不做主观判断）：

* ``tests-residue``：文本/摘要/标签里带验收脚本的探针标记；
* ``slot-out-of-dictionary``：带 ``slot:`` 标签但槽位不在
  ``bianbu_agent_sdk.memory.ontology`` 词典内（MEM-L3-02）；
* ``question-as-preference``：带词典内槽位、但文本是疑问句 —— 一条带 ``slot`` 的
  记忆声明“这段文本就是该字段的值”（MEM-L3-03），值不该是问句。

用法（K3，仓库根目录）：

    echo bianbu | sudo -S python3 scripts/k3_memory_residue_report.py
    echo bianbu | sudo -S python3 scripts/k3_memory_residue_report.py --clean

``--clean`` 逐条 ``long_term.forget``（需要一个由
``/etc/bianbu-agent-memory/adapter-confirmation.secret`` 签发的确认令牌，因此要
root 或 ``bianbu-agent`` 组），然后重新导出复核。

本脚本只收口**用户 Runtime 空间**。清册里其它退役 space（验收脚本的临时 agent、
``runtime-context.*``）不在这里处理，它们的词典外槽位由
``scripts/k3_memory_namespace_cleanup.py`` 按同一判据收口。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bianbu_agent_sdk.memory.ontology import is_question_like  # noqa: E402
from k3_memory_admin import (  # noqa: E402
    ACCOUNT_ID,
    CLEANUP_CAPABILITIES,
    DEVICE_ID,
    SOCKET,
    CONFIRMATION_SECRET,
    confirmation_secret,
    declared_slots,
    forget_record,
    memory_session,
    out_of_dictionary_slots,
)

AGENT_ID = os.environ.get("MEMORY_AGENT_ID", "com.bianbu.runtime")
SPACE_ID = os.environ.get("MEMORY_SPACE_ID", f"{ACCOUNT_ID}.{AGENT_ID}.{DEVICE_ID}")
# 验收脚本写进用户空间的探针标记：RT0190 来自 runtime 0.1.90 升级 E2E
# （rt0190-e2e2.py 用 memory_record 写固定 marker），gateway-probe 来自
# k3_production_install_verify.py 的探针空间。探针空间不在本脚本的收口范围内，
# 列在用户空间的判据里是为了“用户空间出现探针文本”必然报错。
RESIDUE_MARKERS = ("RT0190_MEMORY_E2E_", "gateway-probe", "QCHECK")


def classify(item: dict) -> list[str]:
    """返回这条记忆命中的问题类型；空列表表示合规。"""

    text = str(item.get("text") or "")
    haystack = "\n".join(
        (text, str(item.get("summary") or ""), " ".join(map(str, item.get("tags") or [])))
    )
    reasons: list[str] = []
    if any(marker in haystack for marker in RESIDUE_MARKERS):
        reasons.append("tests-residue")
    if out_of_dictionary_slots(item):
        reasons.append("slot-out-of-dictionary")
    elif declared_slots(item) and is_question_like(text):
        reasons.append("question-as-preference")
    return reasons


def survey(client) -> dict:
    export = client.privacy.export_data(limit_per_collection=500, include_audit=False)
    memories = export.get("long_term_memories") or []
    findings = []
    for item in memories:
        reasons = classify(item)
        if reasons:
            findings.append(
                {
                    "memory_id": item.get("memory_id"),
                    "reasons": reasons,
                    "declared_slots": declared_slots(item),
                    "text": str(item.get("text") or "")[:160],
                }
            )
    return {
        "scope": export.get("scope"),
        "counts": {
            "long_term_memories": len(memories),
            "session_events": len(export.get("session_events") or []),
            "session_summaries": len(export.get("session_summaries") or []),
        },
        "declared_slots_in_use": sorted(
            {slot for item in memories for slot in declared_slots(item)}
        ),
        "findings": findings,
    }


def clean(client, findings: list[dict], report: dict) -> None:
    """逐条 forget 掉残留记录，并把 forget 结果写回报告。"""

    secret = confirmation_secret()
    if secret is None:
        report["clean"] = {
            "skipped": f"{CONFIRMATION_SECRET} is unreadable;"
            " re-run as root (or bianbu-agent)"
        }
        return
    forgotten = []
    for finding in findings:
        outcome = forget_record(client, str(finding["memory_id"]), secret)
        forgotten.append(outcome["memory_id"])
    report["clean"] = {"forgotten": forgotten, "count": len(forgotten)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--clean",
        action="store_true",
        help="forget 掉自查发现的残留记录（需要确认令牌 secret）",
    )
    arguments = parser.parse_args()

    report: dict = {"socket": SOCKET, "memory_space_id": SPACE_ID}
    try:
        with memory_session(
            account_id=ACCOUNT_ID,
            agent_id=AGENT_ID,
            space_id=SPACE_ID,
            capabilities=CLEANUP_CAPABILITIES,
            instance_prefix="residue-report",
        ) as client:
            report["stats_before"] = client.stats()
            report["survey"] = survey(client)
            if arguments.clean and report["survey"]["findings"]:
                clean(client, report["survey"]["findings"], report)
                report["survey_after_clean"] = survey(client)
    except Exception as error:  # noqa: BLE001 - 自查脚本只报告，不隐藏
        report["error"] = f"{type(error).__name__}: {error}"

    remaining = report.get("survey_after_clean", report.get("survey")) or {}
    findings = remaining.get("findings") or []
    report["residue_remaining"] = len(findings)
    # tombstone 是 forget 的同步记录，不是垃圾：它没有删除接口，也不该被删掉。
    report["tombstones_note"] = "tombstones are the sync record of a forget; they are expected"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if "error" in report or findings:
        print("K3_MEMORY_RESIDUE_FOUND")
        return 1
    print("K3_MEMORY_RESIDUE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
