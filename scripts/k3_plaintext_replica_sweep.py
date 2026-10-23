#!/usr/bin/env python3
"""K3 明文记忆副本现场清理 —— SEC-05 的现场执行脚本。

SEC-05（明文迁移）验收口径是“生成加密库、暂存文件可清理、回滚副本默认保留且为
0600”。守护进程侧的三条清理入口（``--migrate-plaintext``、
``--cleanup-plaintext-backups``、``--cleanup-migration-temporaries``）只能清理**当前
数据库同目录**下自己命名的文件（``memory.db.plaintext-backup-*``、
``.memory.db.encrypted-*``），管不到历史上散落在开发目录里的明文库、明文回滚副本
和明文导出 —— 那些文件里存的是同一份记忆内容，只是一层 SQL/JSON 包装。

本脚本把“设备上还剩哪些明文记忆副本”变成一份可复核的清单，并在 ``--apply`` 时删掉：

* ``db-plaintext``：文件名属于 ``memory.db`` 家族（含 ``-wal``/``-shm``）且头 16 字节
  是 ``SQLite format 3`` —— 明文库。加密库（SQLCipher）没有这个头，会被判为
  ``db-encrypted`` 并保留；
* ``db-plaintext-backup``：``memory.db.plaintext-backup-*`` 迁移回滚副本；
* ``export-plaintext``：**任何**扩展名为 ``.json``/``.jsonl``/``.txt``/``.log``/``.csv``
  的文件（不限文件名，现场常见的 ``/tmp/*.json`` 转储与 ``*-readable.txt`` 都在此列），
  只要头部出现记忆集合键（``long_term_memories`` / ``session_events`` /
  ``session_summaries`` / ``memory_embeddings``），并且真能解析出这些集合。

永远不会被删的：现网库 ``/var/lib/bianbu-agent-memory/memory.db``、
``*.keypkg`` / ``*-secret`` / ``*.key``、以及任何**正被进程打开**的文件
（``/proc/*/fd`` 里有引用的一律跳过）。``capsules/`` 只在旧设备上存在，云备份退役后
不再创建，但历史目录仍按同一规则跳过。

用法（K3）：

    echo bianbu | sudo -S python3 scripts/k3_plaintext_replica_sweep.py            # 只报告
    echo bianbu | sudo -S python3 scripts/k3_plaintext_replica_sweep.py --apply     # 真正删除
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

DEFAULT_ROOTS = ("/home", "/var/lib/bianbu-agent-memory", "/tmp", "/var/tmp")
PROTECTED_PATHS = (
    "/var/lib/bianbu-agent-memory/memory.db",
    "/etc/bianbu-agent-memory",
)
PROTECTED_SUFFIXES = (".keypkg", ".secret", ".key", ".pem")
PRUNE_DIRECTORIES = {
    ".cache",
    ".cipd_bin",
    ".git",
    ".venv",
    "__pycache__",
    "depot_tools",
    "node_modules",
    "site-packages",
    "target",
}
SQLITE_HEADER = b"SQLite format 3\x00"
DB_FAMILY = re.compile(r"^memory\.db(-wal|-shm)?$")
PLAINTEXT_BACKUP = re.compile(r"^memory\.db\.plaintext-backup-")
MIGRATION_TEMPORARY = re.compile(r"^\.memory\.db\.encrypted-")
EXPORT_NAME = re.compile(r"export", re.IGNORECASE)
# 只有记忆特有的集合名才算命中：``instructions`` 太常见（SPIR-V 语法文件里就有
# ``instructions`` 列表），只作为附带计数上报，不参与判定。
DETECTION_COLLECTIONS = (
    "long_term_memories",
    "session_events",
    "session_summaries",
    "memory_embeddings",
)
REPORT_COLLECTIONS = DETECTION_COLLECTIONS + ("instructions",)
# 记忆集合键按导出字段名排序后 ``long_term_memories`` 总在前 200 字节内出现，因此看
# 文件头部就足以挑出候选，再用完整解析做最终判定（避免误伤普通 JSON 报告）。
EXPORT_KEYS = tuple(('"%s"' % key).encode() for key in DETECTION_COLLECTIONS)
TEXT_SUFFIXES = (".json", ".jsonl", ".ndjson", ".txt", ".log", ".csv")
HEAD_BYTES = 64 * 1024


def live_paths() -> set[str]:
    """``/proc/*/fd`` 里被打开的文件（读不到就当作空集，只影响“更保守”的判断）。"""

    opened: set[str] = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        descriptors = process / "fd"
        try:
            entries = list(descriptors.iterdir())
        except OSError:
            continue
        for descriptor in entries:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith("/"):
                opened.add(os.path.realpath(target))
    return opened


def is_protected(path: Path) -> bool:
    text = str(path)
    if text.endswith(PROTECTED_SUFFIXES):
        return True
    for protected in PROTECTED_PATHS:
        if text == protected or text.startswith(protected.rstrip("/") + "/"):
            return True
    return False


def digest(path: Path) -> str | None:
    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
    except OSError:
        return None
    return hasher.hexdigest()


def probe_sqlite(path: Path) -> dict:
    """把库（连同 -wal）拷到临时目录再读，绝不动原文件。"""

    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "memory.db"
        try:
            shutil.copy2(path, target)
            for suffix in ("-wal", "-shm"):
                sibling = Path(str(path) + suffix)
                if sibling.exists():
                    shutil.copy2(sibling, Path(str(target) + suffix))
        except OSError as error:
            return {"probe_error": f"{type(error).__name__}: {error}"}
        try:
            connection = sqlite3.connect(target, timeout=10)
        except sqlite3.Error as error:
            return {"probe_error": f"{type(error).__name__}: {error}"}
        try:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            probe: dict = {"tables": tables}
            for table in ("long_term_memories", "tombstones", "session_events"):
                if table in tables:
                    probe[f"{table}_rows"] = int(
                        connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    )
            return probe
        except sqlite3.Error as error:
            return {"probe_error": f"{type(error).__name__}: {error}"}
        finally:
            connection.close()


def classify_database_file(path: Path) -> dict:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(16)
    except OSError as error:
        return {"kind": "unreadable", "error": f"{type(error).__name__}: {error}"}
    name = path.name
    entry: dict = {"path": str(path), "size": size, "sha256": digest(path)}
    if header == SQLITE_HEADER:
        entry["kind"] = "db-plaintext"
    elif PLAINTEXT_BACKUP.match(name):
        entry["kind"] = "db-plaintext-backup"
    elif MIGRATION_TEMPORARY.match(name):
        entry["kind"] = "db-encrypted-temporary"
    elif name.endswith(("-wal", "-shm")):
        # 说明同目录下没有明文主库；单独的 wal 只有在主库仍是明文时才危险。
        entry["kind"] = "db-sidecar"
    else:
        entry["kind"] = "db-encrypted"
    if entry["kind"] in {"db-plaintext", "db-plaintext-backup"}:
        entry.update(probe_sqlite(path))
    return entry


def head_mentions_memory(path: Path) -> bool:
    """头部是否出现记忆集合键 —— 便宜的第一道筛选。"""

    try:
        with path.open("rb") as handle:
            head = handle.read(HEAD_BYTES)
    except OSError:
        return False
    return any(key in head for key in EXPORT_KEYS)


def export_collections(path: Path) -> dict | None:
    """返回导出文件里的记忆集合大小；不是记忆导出就返回 None。"""

    try:
        if path.stat().st_size > 64 * 1024 * 1024:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for candidate in (payload, payload.get("result") if isinstance(payload, dict) else None):
        if not isinstance(candidate, dict):
            continue
        found = {
            key: len(value)
            for key, value in candidate.items()
            if key in REPORT_COLLECTIONS and isinstance(value, list)
        }
        if any(key in found for key in DETECTION_COLLECTIONS):
            return found
    return None


def scan(roots: tuple[str, ...]) -> dict:
    opened = live_paths()
    databases: list[dict] = []
    exports: list[dict] = []
    skipped_live: list[str] = []
    for root in roots:
        for directory, subdirectories, files in os.walk(root, followlinks=False):
            subdirectories[:] = sorted(
                name
                for name in subdirectories
                if name not in PRUNE_DIRECTORIES
                and not os.path.islink(os.path.join(directory, name))
            )
            for name in sorted(files):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    continue
                relevant = bool(DB_FAMILY.match(name) or PLAINTEXT_BACKUP.match(name) or MIGRATION_TEMPORARY.match(name))
                # 文本类候选：文件名带 export，或头部出现记忆集合键（真正的判定
                # 在 export_collections 的完整解析里）。
                is_text_candidate = name.endswith(TEXT_SUFFIXES) and (
                    bool(EXPORT_NAME.search(name)) or head_mentions_memory(path)
                )
                if not relevant and not is_text_candidate:
                    continue
                if is_protected(path):
                    continue
                if os.path.realpath(path) in opened:
                    skipped_live.append(str(path))
                    continue
                if relevant:
                    databases.append(classify_database_file(path))
                else:
                    collections = export_collections(path)
                    if collections:
                        exports.append(
                            {
                                "path": str(path),
                                "size": path.stat().st_size,
                                "sha256": digest(path),
                                "collections": collections,
                            }
                        )
    return {"databases": databases, "exports": exports, "skipped_live": skipped_live}


def removable(entry: dict) -> bool:
    return entry.get("kind") in {"db-plaintext", "db-plaintext-backup"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", action="append", default=None, help="扫描根（可重复）")
    parser.add_argument("--apply", action="store_true", help="真正删除（默认只报告）")
    arguments = parser.parse_args()

    roots = tuple(arguments.root) if arguments.root else DEFAULT_ROOTS
    report: dict = {"roots": list(roots), "apply": arguments.apply}
    try:
        found = scan(roots)
        report.update(found)
        report["plaintext_databases"] = [item for item in found["databases"] if removable(item)]
        # JSON 导出永远是明文（导出接口就是给人看的），所以它属于“现场残留”，
        # 不属于“迁移副本”；两类都删，但分类计数分开记。
        report["plaintext_exports"] = found["exports"]
        removed: list[str] = []
        if arguments.apply:
            for entry in report["plaintext_databases"] + report["plaintext_exports"]:
                path = Path(entry["path"])
                try:
                    path.unlink()
                    removed.append(str(path))
                    if removable(entry):
                        for suffix in ("-wal", "-shm"):
                            sibling = Path(str(path) + suffix)
                            if sibling.exists() and os.path.realpath(sibling) not in found["skipped_live"]:
                                sibling.unlink()
                                removed.append(str(sibling))
                except OSError as error:
                    report.setdefault("remove_errors", []).append(
                        f"{path}: {type(error).__name__}: {error}"
                    )
            report["removed"] = removed
            report["after"] = scan(roots)
    except Exception as error:  # noqa: BLE001 - 现场脚本只报告，不隐藏
        report["error"] = f"{type(error).__name__}: {error}"

    print(json.dumps(report, ensure_ascii=False, indent=2))

    if "error" in report or report.get("remove_errors"):
        print("K3_PLAINTEXT_REPLICA_SWEEP_FAIL")
        return 1
    if arguments.apply:
        after = report.get("after") or {}
        if [item for item in after.get("databases", []) if removable(item)] or after.get("exports"):
            print("K3_PLAINTEXT_REPLICA_SWEEP_FAIL")
            return 1
        print("K3_PLAINTEXT_REPLICA_SWEEP_OK")
        return 0
    if report.get("plaintext_databases") or report.get("plaintext_exports"):
        print("K3_PLAINTEXT_REPLICA_SWEEP_REQUIRED")
        return 1
    print("K3_PLAINTEXT_REPLICA_SWEEP_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())