#!/usr/bin/env python3
"""断电注入的采集与校验探针（REL-06 / MEM-NFR-04-02）。

断电等效操作是 `echo b > /proc/sysrq-trigger`（内核直接重启、不做 sync，页缓存里的
脏数据全部丢失，与硬掉电的表现一致）。触发由 `scripts/k3_power_loss_test.py` 在
开发机上完成；本文件只负责**掉电前采集**与**重启后校验**两半，因为校验必须用
掉电前就记下来的锚点：

- `pre`：记录 boot_id / uptime / 版本 / unit 状态 / 库与 WAL 体积 / `system.health` /
  命名空间行数清单（`--report-namespaces`），并写入一批**不带过期时间**的标记记忆
  （每条记下 memory_id 与文本 sha256），最后再写一次让 WAL 保持非空。
- `post`：重启后重新采集同一组指标，并用 `get_many` 逐条取回标记记忆比对文本
  sha256；同时盘点临时文件残留、扫描 journal 里的恢复/报错行，输出门禁结论。

另有 `health` 子命令，不落盘、不建标记，只报一次 `system.health`，供驱动在重启后等
守护进程自己的库自检出结论（见 `scripts/k3_power_loss_test.py`）。

用法（K3，root）：

    python3 scripts/k3_power_loss_probe.py health
    python3 scripts/k3_power_loss_probe.py pre  --json /tmp/power-pre.json --markers 20
    python3 scripts/k3_power_loss_probe.py post --json /tmp/power-post.json --pre /tmp/power-pre.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import tempfile
from pathlib import Path

SOCKET = os.environ.get("MEMORY_SOCKET", "/run/bianbu/agent-memoryd.sock")
REGISTRY = Path("/run/bianbu/agent-runtime/memory-registry")
DATA_DIR = Path("/var/lib/bianbu-agent-memory")
DATABASE = DATA_DIR / "memory.db"
KEY_PACKAGE = DATA_DIR / "memory.keypkg"
ACCOUNT_SECRET = Path("/etc/bianbu-agent-memory/account-secret")
DAEMON = Path("/usr/libexec/bianbu-agent-memoryd")
PROBE_AGENT = "com.bianbu.memory.powerprobe"
PROBE_CAPABILITIES = [
    "memory.long_term.read",
    "memory.long_term.write",
    "memory.forget",
    "memory.audit.status",
    "memory.privacy.read",
]
REGISTRATION_TTL_MS = 24 * 60 * 60 * 1000
TEMP_PATTERNS = (".tmp", ".journal", "-journal", ".staging", ".partial", ".part")


def run(command: list[str], timeout: int = 120) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:  # pragma: no cover - device only
        return 127, f"{type(error).__name__}: {error}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def start_time_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    return int(fields.split()[19])


def local_device_id() -> str:
    base = Path("/sys/class/net")
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if entry.name == "lo":
                continue
            address = (entry / "address").read_text(encoding="ascii").strip()
            if address and address != "00:00:00:00:00:00":
                return address
    return "powerprobe"


SPACE = f"power-loss.{PROBE_AGENT}.{local_device_id()}"


def ensure_registration() -> Path:
    pid = os.getpid()
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": start_time_ticks(pid),
        "account_id": "default",
        "device_id": local_device_id(),
        "agent_id": PROBE_AGENT,
        "agent_name": PROBE_AGENT,
        "agent_instance_id": f"power-probe.{pid}.{start_time_ticks(pid)}",
        "memory_space_id": SPACE,
        "capabilities": PROBE_CAPABILITIES,
        "expires_at_ms": int(time.time() * 1000) + REGISTRATION_TTL_MS,
    }
    path = REGISTRY / f"{pid}.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def memory_client():
    from bianbu_agent_sdk.memory import MemoryClient

    return MemoryClient.connect(
        SOCKET, timeout=30.0, account_id="default", agent_name=PROBE_AGENT
    )


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    except OSError:
        return "unknown"


def uptime_seconds() -> float:
    return float(Path("/proc/uptime").read_text().split()[0])


def storage_snapshot() -> dict[str, object]:
    snapshot: dict[str, object] = {}
    for name, path in (
        ("database_bytes", DATABASE),
        ("wal_bytes", Path(f"{DATABASE}-wal")),
        ("shm_bytes", Path(f"{DATABASE}-shm")),
        ("key_package_bytes", KEY_PACKAGE),
    ):
        try:
            snapshot[name] = path.stat().st_size
        except OSError:
            snapshot[name] = None
    try:
        snapshot["free_bytes"] = os.statvfs(str(DATA_DIR)).f_bavail * os.statvfs(
            str(DATA_DIR)
        ).f_frsize
    except OSError:
        snapshot["free_bytes"] = None
    return snapshot


def temp_files() -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for entry in sorted(DATA_DIR.rglob("*")):
        name = entry.name
        if not any(pattern in name for pattern in TEMP_PATTERNS):
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        found.append(
            {
                "path": str(entry),
                "bytes": stat.st_size,
                "mtime_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(stat.st_mtime)),
            }
        )
    return found


def unit_properties() -> dict[str, str]:
    code, output = run(
        [
            "systemctl", "show", "bianbu-agent-memoryd",
            "-p", "ActiveState,SubState,NRestarts,MainPID,MemoryMax",
        ]
    )
    if code != 0:
        return {"error": output}
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        properties[key] = value
    return properties


def namespace_report() -> dict[str, object]:
    """只读打开生产库，列出每个命名空间的行数（`--report-namespaces`）。"""
    code, output = run(
        [
            str(DAEMON), "--database", str(DATABASE), "--key-package", str(KEY_PACKAGE),
            "--account-secret-file", str(ACCOUNT_SECRET), "--report-namespaces",
        ]
    )
    start = output.find("{")
    end = output.rfind("}")
    if code != 0 or start < 0 or end <= start:
        return {"error": output[:400]}
    try:
        report = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return {"error": output[:400]}
    rows = report.get("namespaces") or []
    counted = (
        "instructions", "long_term_memories", "memory_embeddings",
        "session_events", "session_summaries", "tombstones",
    )
    totals = {
        key: sum(int((row.get("counts") or {}).get(key) or 0) for row in rows)
        for key in counted
    }
    totals["namespaces"] = report.get("namespace_count") or len(rows)
    return {
        "database": report.get("database"),
        "namespace_count": report.get("namespace_count"),
        "totals": totals,
        "namespaces": rows,
    }


def health_snapshot() -> dict[str, object]:
    try:
        with memory_client() as client:
            health = client.health()
            return {
                "status": health.get("status"),
                "database": health.get("database"),
                "schema_version": health.get("schema_version"),
                "storage_encryption": health.get("storage_encryption"),
                "database_check": health.get("database_check"),
                "embedding": {
                    key: (health.get("embedding") or {}).get(key)
                    for key in ("enabled", "model", "base_url")
                },
            }
    except Exception as error:  # noqa: BLE001 - 探针只报告
        return {"error": f"{type(error).__name__}: {error}"}


def versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in ("bianbu-agent-memoryd", "bianbu-agent-memory", "spacemit-ai-gateway"):
        code, output = run(["dpkg-query", "-W", "-f=${Version}", package])
        result[package] = output if code == 0 else "not-installed"
    result["kernel"] = os.uname().release
    result["host"] = os.uname().nodename
    return result


def journal_since(since_epoch: float, pattern: str) -> list[str]:
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(since_epoch))
    code, output = run(
        ["journalctl", "-u", "bianbu-agent-memoryd", "--since", since, "-g", pattern,
         "-o", "short-iso", "--no-pager"],
        timeout=300,
    )
    if code != 0:
        return [f"journalctl failed: {output[:200]}"]
    return [line for line in output.splitlines() if line.strip() and not line.startswith("-- ")]


def write_markers(run_id: str, count: int) -> list[dict[str, object]]:
    from bianbu_agent_sdk.memory import MemoryKind, SyncPolicy

    markers: list[dict[str, object]] = []
    with memory_client() as client:
        for index in range(count):
            text = f"断电标记 {run_id} #{index}：掉电重启后这条记录必须仍然可读"
            receipt = client.long_term.put(
                kind=MemoryKind.FACT,
                text=text,
                tags=("power-loss",),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                idempotency_key=f"power-loss:{run_id}:{index}",
            )
            markers.append(
                {
                    "index": index,
                    "memory_id": receipt.memory_id,
                    "text": text,
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    "idempotency_key": f"power-loss:{run_id}:{index}",
                }
            )
        # 让 WAL 保持非空：掉电恢复要真的走一遍 WAL 重放。
        client.long_term.search_headers("断电标记", top_k=5)
        stats = client.stats()
    print(json.dumps({"markers_written": len(markers), "stats_ok": isinstance(stats, dict)},
                     ensure_ascii=False))
    return markers


def confirmation_token(secret: bytes, method: str, ttl_ms: int = 120_000) -> str:
    import secrets as secrets_module

    expiry = int(time.time() * 1000) + ttl_ms
    nonce = secrets_module.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    return f"v1:{expiry}:{nonce}:{hashlib.sha256(secret + payload).hexdigest()}"


def command_pre(arguments: argparse.Namespace) -> int:
    ensure_registration()
    run_id = arguments.run_id or time.strftime("%Y%m%d-%H%M%S")
    report: dict[str, object] = {
        "phase": "pre",
        "run_id": run_id,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "collected_epoch": time.time(),
        "boot_id": boot_id(),
        "uptime_s": uptime_seconds(),
        "versions": versions(),
        "unit": unit_properties(),
        "storage": storage_snapshot(),
        "health": health_snapshot(),
        "namespaces": namespace_report(),
        "temp_files": temp_files(),
    }
    if (report["unit"].get("ActiveState") != "active"
            or report["health"].get("status") != "ok"
            or report["health"].get("database") != "ok"
            or not report["namespaces"].get("totals")
            or arguments.markers <= 0):
        report["verdict"] = "FAIL"
        durable_report(arguments.json, report)
        return 1
    report["markers"] = write_markers(run_id, arguments.markers)
    report["storage_after_markers"] = storage_snapshot()
    durable_report(arguments.json, report)
    print(json.dumps({key: report[key] for key in ("run_id", "boot_id", "storage_after_markers")},
                     ensure_ascii=False, indent=2))
    return 0


def command_health(arguments: argparse.Namespace) -> int:
    """只读地报一次健康状态，供驱动在重启后等「库自检已出结论」。

    不带 `--json`、不写报告文件、不建标记记忆：它会被轮询调用十几次，任何落盘都是
    给设备添垃圾（注册记录用完即删）。退出码 0 = 守护进程已发布判词（`ok` 或
    `degraded`，`database_check.verdict` 非空），1 = 还没验（`unknown` / `busy`）
    或读不到 —— 调用方据此继续等。注意 0 不等于「库没问题」：真出缺陷时判词是
    `degraded`，同样算「已出结论」，由 post 的门禁去判 FAIL。
    """
    health: dict[str, object] = {}
    registration: Path | None = None
    try:
        registration = ensure_registration()
        health = health_snapshot()
    except Exception as error:  # noqa: BLE001 - 探针只报告
        health = {"error": f"{type(error).__name__}: {error}"}
    finally:
        if registration is not None:
            registration.unlink(missing_ok=True)
    print(json.dumps({
        "collected_epoch": time.time(),
        "conclusive": health.get("status") in {"ok", "degraded"},
        "health": health,
    }, ensure_ascii=False))
    return 0 if health.get("status") in {"ok", "degraded"} else 1


def durable_report(path: str, report: dict) -> None:
    destination = Path(path).resolve()
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=destination.parent,
                                     delete=False) as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
        temporary = stream.name
    os.replace(temporary, destination)
    directory = os.open(destination.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def command_post(arguments: argparse.Namespace) -> int:
    ensure_registration()
    pre = json.loads(Path(arguments.pre).read_text(encoding="utf-8"))
    run_id = pre["run_id"]
    markers = pre["markers"]
    # 服务可能还在启动：等它 active，最多 180 s。
    deadline = time.time() + 180
    while time.time() < deadline:
        if unit_properties().get("ActiveState") == "active":
            break
        time.sleep(3)
    recovered: list[dict[str, object]] = []
    try:
        with memory_client() as client:
            found = {item.memory_id: item for item in client.long_term.get_many(
                [marker["memory_id"] for marker in markers])}
            for marker in markers:
                item = found.get(marker["memory_id"])
                text = getattr(item, "text", None) if item is not None else None
                recovered.append(
                    {
                        "index": marker["index"],
                        "memory_id": marker["memory_id"],
                        "present": item is not None,
                        "text_matches": text == marker["text"]
                        and hashlib.sha256(text.encode("utf-8")).hexdigest() == marker["sha256"],
                    }
                )
    except Exception as error:  # noqa: BLE001
        recovered.append({"error": f"{type(error).__name__}: {error}"})
    report: dict[str, object] = {
        "phase": "post",
        "run_id": run_id,
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "collected_epoch": time.time(),
        "boot_id": boot_id(),
        "uptime_s": uptime_seconds(),
        "versions": versions(),
        "unit": unit_properties(),
        "storage": storage_snapshot(),
        "health": health_snapshot(),
        "namespaces": namespace_report(),
        "temp_files": temp_files(),
        "markers_expected": len(markers),
        "markers_recovered": recovered,
        "journal_recovery": journal_since(
            float(pre["collected_epoch"]) + 1, "WAL|recover|checkpoint"
        )[:20],
        "journal_problems": journal_since(
            float(pre["collected_epoch"]) + 1, "panic|corrupt|malformed|failed|error"
        )[:20],
    }
    health = report["health"] if isinstance(report["health"], dict) else {}
    totals_pre = ((pre.get("namespaces") or {}).get("totals") or {})
    totals_post = ((report.get("namespaces") or {}).get("totals") or {})
    recovered_keys = [
        key for key in ("long_term_memories", "memory_embeddings", "session_events",
                        "instructions", "session_summaries", "namespaces")
        if key in totals_pre or key in totals_post
    ]
    pre_temp = {item["path"] for item in pre.get("temp_files", [])}
    new_temp = [item for item in report.get("temp_files", []) if item["path"] not in pre_temp]
    gates = {
        "boot_id_changed": report["boot_id"] != pre["boot_id"],
        "uptime_reset": report["uptime_s"] < pre["uptime_s"],
        "service_active": report["unit"].get("ActiveState") == "active",
        "health_ok": health.get("status") == "ok" and health.get("database") == "ok",
        "schema_version_unchanged": health.get("schema_version") == (
            pre.get("health") or {}
        ).get("schema_version"),
        "key_package_usable": health.get("storage_encryption") == "sqlcipher",
        "markers_all_recovered": bool(recovered) and all(
            item.get("present") and item.get("text_matches") for item in recovered
        ),
        "row_counts_not_lost": bool(recovered_keys) and all(
            isinstance(totals_post.get(key), int) and isinstance(totals_pre.get(key), int)
            and totals_post[key] >= totals_pre[key] for key in recovered_keys
        ),
        "no_new_temp_files": not new_temp,
    }
    if arguments.forget_markers and all(gates.values()):
        try:
            from bianbu_agent_sdk.memory import MemoryClient

            secret_path = Path("/etc/bianbu-agent-memory/adapter-confirmation.secret")
            secret = secret_path.read_bytes() if os.access(secret_path, os.R_OK) else None
            forgotten = []
            if secret is not None:
                with memory_client() as client:
                    for marker in markers:
                        job = client.long_term.forget(
                            marker["memory_id"], confirm_token=confirmation_token(
                                secret, "long_term.forget"
                            )
                        )
                        client.long_term.wait_forget(job.job_id)
                        forgotten.append(marker["memory_id"])
            report["markers_forgotten"] = forgotten
            report["markers_forget_skipped"] = secret is None
        except Exception as error:  # noqa: BLE001
            report["markers_forget_error"] = f"{type(error).__name__}: {error}"
    report["gates"] = gates
    report["verdict"] = "PASS" if all(gates.values()) else "FAIL"
    report["comparison"] = {
        "totals_pre": {key: totals_pre.get(key) for key in recovered_keys},
        "totals_post": {key: totals_post.get(key) for key in recovered_keys},
        "new_temp_files": new_temp,
    }
    durable_report(arguments.json, report)
    print(json.dumps({key: report[key] for key in
                      ("run_id", "boot_id", "uptime_s", "gates", "verdict", "comparison")},
                     ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="断电注入采集与校验探针")
    subparsers = parser.add_subparsers(dest="command", required=True)

    pre = subparsers.add_parser("pre", help="掉电前采集并写入标记记忆")
    pre.add_argument("--json", required=True)
    pre.add_argument("--markers", type=int, default=20)
    pre.add_argument("--run-id")
    pre.set_defaults(function=command_pre)

    health = subparsers.add_parser("health", help="只报一次健康状态（等库自检出结论用，不落盘）")
    health.set_defaults(function=command_health)

    post = subparsers.add_parser("post", help="重启后校验")
    post.add_argument("--json", required=True)
    post.add_argument("--pre", required=True)
    post.add_argument("--forget-markers", action="store_true",
                      help="校验通过后把标记记忆 forget 掉（需要确认令牌）")
    post.set_defaults(function=command_post)

    arguments = parser.parse_args()
    return arguments.function(arguments)


if __name__ == "__main__":
    sys.exit(main())
