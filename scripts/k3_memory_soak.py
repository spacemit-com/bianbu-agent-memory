#!/usr/bin/env python3
"""K3 七天稳定性 soak（REL-09 / MEM-NFR-04-02）。

在 K3 现网守护进程上连续观测 7×24，并周期性施加写入/检索/重启扰动，最后用
`verdict` 把 `samples.jsonl` 与 journal 汇总成一份可归档的验收证据。

观测（默认每 60 s）：

- memoryd 的 systemd 状态、`NRestarts`、cgroup `MemoryCurrent`；
- `/proc` 的 RSS / 线程 / FD / CPU ticks；
- `memory.db`、`-wal`、`-shm` 体积与 `/var/lib` 余量；
- `system.health`（含 `database_check` 判定与生效 embedding 端点）与 `system.stats`。

扰动：

- 每 30 min：在**独立探针命名空间**写入一条带 `expires_at` 的记忆、按改写查询检索
  （命中只能来自向量分支）、再按确认令牌 `forget`；记录耗时与成功与否；
- 每 24 h：`systemctl restart bianbu-agent-memoryd`，并把它记进 planned restarts，
  与 journal 里的启动/停止计数对账，区分计划内与计划外重启。

脚本以 root 运行（读确认令牌需要 root，采样单元是 systemd service）。
它**不属于发布包**：镜像/发版前必须 `stop` 并删除该 unit。

用法（K3，仓库根目录）：

    sudo python3 scripts/k3_memory_soak.py start --interval 60 --disturb-interval 1800
    python3 scripts/k3_memory_soak.py status
    sudo python3 scripts/k3_memory_soak.py sample          # 立刻取一次样本，看格式
    sudo python3 scripts/k3_memory_soak.py verdict --json /tmp/soak-verdict.json
    sudo python3 scripts/k3_memory_soak.py stop

退出码：`verdict` 在 PASS 时为 0，FAIL/INCOMPLETE 时为 1。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from pathlib import Path

MEMORYD_UNIT = "bianbu-agent-memoryd"
SOAK_UNIT = "bianbu-memory-soak"
SOAK_UNIT_PATH = Path(f"/etc/systemd/system/{SOAK_UNIT}.service")
SOCKET = os.environ.get("MEMORY_SOCKET", "/run/bianbu/agent-memoryd.sock")
REGISTRY = Path("/run/bianbu/agent-runtime/memory-registry")
STATE_DIR = Path("/var/lib/bianbu-memory-soak")
DATA_DIR = Path("/var/lib/bianbu-agent-memory")
DATABASE = DATA_DIR / "memory.db"
SECRET_PATH = Path("/etc/bianbu-agent-memory/adapter-confirmation.secret")
MODEL_DIR = DATA_DIR / "models"

PROBE_AGENT = "com.bianbu.memory.soak.probe"
PROBE_CAPABILITIES = [
    "memory.long_term.read",
    "memory.long_term.write",
    "memory.forget",
    "memory.audit.status",
    "memory.privacy.read",
]
REGISTRATION_TTL_MS = 9 * 24 * 60 * 60 * 1000  # 7 天窗口 + 2 天余量
SEVEN_DAYS_SECONDS = 7 * 24 * 60 * 60
# 改写查询与写入文本在 trigram 上不重叠：FTS 必然检索不到，命中只能来自向量分支。
PROBE_PAIRS = [
    ("soak 探针：终端使用深色主题配色", "怎么让屏幕晚上不那么刺眼"),
    ("soak 探针：项目构建用 cargo build --release", "怎么把源码编译成可执行程序"),
    ("soak 探针：备份目录放在移动硬盘", "怎么把重要资料另存一份"),
]

UNIT_TEMPLATE = """[Unit]
Description=Bianbu Agent Memory 7-day soak sampler (test tooling, not shipped)
After=bianbu-agent-memoryd.service

[Service]
Type=simple
ExecStart=/usr/bin/python3 {script} --state-dir {state_dir} run-loop --interval {interval} --disturb-interval {disturb_interval} --restart-every-hours {restart_every_hours}
Restart=always
RestartSec=10
Nice=5

[Install]
WantedBy=multi-user.target
"""


def run(command: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:  # pragma: no cover - device only
        return 127, f"{type(error).__name__}: {error}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def start_time_ticks(pid: int) -> int:
    fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1]
    return int(fields.split()[19])


def local_device_id() -> str:
    override = os.environ.get("MEMORY_DEVICE_ID")
    if override:
        return override
    base = Path("/sys/class/net")
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if entry.name == "lo":
                continue
            address = (entry / "address").read_text(encoding="ascii").strip()
            if address and address != "00:00:00:00:00:00":
                return address
    return "soak"


def ensure_registration(space: str) -> Path:
    """稳跑期间持续使用同一份探针注册（memoryd 重启不会清除 /run）。"""
    pid = os.getpid()
    path = REGISTRY / f"{pid}.json"
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": start_time_ticks(pid),
        "account_id": "default",
        "device_id": local_device_id(),
        "agent_id": PROBE_AGENT,
        "agent_name": PROBE_AGENT,
        "agent_instance_id": f"soak.{pid}.{start_time_ticks(pid)}",
        "memory_space_id": space,
        "capabilities": PROBE_CAPABILITIES,
        "expires_at_ms": int(time.time() * 1000) + REGISTRATION_TTL_MS,
    }
    REGISTRY.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def confirmation_token(secret: bytes, method: str, ttl_ms: int = 120_000) -> str:
    import hashlib
    import secrets as secrets_module

    expiry = int(time.time() * 1000) + ttl_ms
    nonce = secrets_module.token_urlsafe(24)
    payload = f"v1:{expiry}:{nonce}:{method}".encode("utf-8")
    return f"v1:{expiry}:{nonce}:{hashlib.sha256(secret + payload).hexdigest()}"


def unit_properties(unit: str) -> dict[str, str]:
    code, output = run(
        [
            "systemctl", "show", unit,
            "-p", "ActiveState,SubState,NRestarts,MainPID,MemoryCurrent,MemoryMax,TasksCurrent",
        ]
    )
    if code != 0:
        return {"error": output}
    properties: dict[str, str] = {}
    for line in output.splitlines():
        key, _, value = line.partition("=")
        properties[key] = value
    return properties


def proc_metrics(pid: int) -> dict[str, object]:
    metrics: dict[str, object] = {}
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                metrics["rss_kb"] = int(line.split()[1])
            elif line.startswith("Threads:"):
                metrics["threads"] = int(line.split()[1])
    except OSError as error:
        metrics["error"] = f"{type(error).__name__}: {error}"
    try:
        metrics["fds"] = len(os.listdir(f"/proc/{pid}/fd"))
    except OSError:
        pass
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(")", 1)[1].split()
        metrics["cpu_ticks"] = int(fields[11]) + int(fields[12])
    except (OSError, IndexError, ValueError):
        pass
    return metrics


def storage_metrics() -> dict[str, object]:
    metrics: dict[str, object] = {}
    for name, path in (
        ("database_bytes", DATABASE),
        ("wal_bytes", Path(f"{DATABASE}-wal")),
        ("shm_bytes", Path(f"{DATABASE}-shm")),
    ):
        try:
            metrics[name] = path.stat().st_size
        except OSError:
            metrics[name] = None
    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        metrics["free_bytes"] = usage.free
    except OSError:
        metrics["free_bytes"] = None
    for name, directory in (("capsules_bytes", DATA_DIR / "capsules"), ("models_bytes", MODEL_DIR)):
        total = 0
        try:
            for entry in directory.rglob("*"):
                if entry.is_file():
                    total += entry.stat().st_size
            metrics[name] = total
        except OSError:
            metrics[name] = None
    return metrics


def memory_client():
    from bianbu_agent_sdk.memory import MemoryClient

    return MemoryClient.connect(
        SOCKET, timeout=30.0, account_id="default", agent_name=PROBE_AGENT
    )


def service_metrics() -> dict[str, object]:
    metrics: dict[str, object] = {}
    try:
        with memory_client() as client:
            health = client.health()
            metrics["status"] = health.get("status")
            metrics["database"] = health.get("database")
            metrics["schema_version"] = health.get("schema_version")
            check = health.get("database_check") or {}
            metrics["check_state"] = check.get("state")
            metrics["check_verdict"] = check.get("verdict")
            metrics["check_reason"] = check.get("reason")
            embedding = health.get("embedding") or {}
            metrics["embedding_enabled"] = embedding.get("enabled")
            metrics["embedding_model"] = embedding.get("model")
            metrics["embedding_base_url"] = embedding.get("base_url")
            storage = health.get("storage") or {}
            metrics["storage_reported"] = storage
            # stats 只统计本探针身份维度，用于确认写入链路可用。
            stats = client.stats()
            metrics["stats_ok"] = isinstance(stats, dict)
    except Exception as error:  # noqa: BLE001 - 采样失败要记进样本，不能中断稳跑
        metrics["error"] = f"{type(error).__name__}: {error}"
    return metrics


def disturb(cycle: int) -> dict[str, object]:
    """写入 + 检索 + forget 一轮；探针数据不留在库里。"""
    text, query = PROBE_PAIRS[cycle % len(PROBE_PAIRS)]
    outcome: dict[str, object] = {"cycle": cycle, "text": text}
    secret = SECRET_PATH.read_bytes() if os.access(SECRET_PATH, os.R_OK) else None
    try:
        from bianbu_agent_sdk.memory import MemoryKind, SyncPolicy

        with memory_client() as client:
            started = time.perf_counter()
            receipt = client.long_term.put(
                kind=MemoryKind.PREFERENCE,
                text=text,
                tags=("soak-probe",),
                sync_policy=SyncPolicy.LOCAL_ONLY,
                expires_at=int(time.time() * 1000) + 600_000,
                idempotency_key=f"soak:{cycle}:{int(time.time())}",
            )
            outcome["put_ms"] = round((time.perf_counter() - started) * 1000, 1)
            started = time.perf_counter()
            headers = []
            search_attempts = 0
            search_hit = False
            # LOCAL_ONLY writes are durable before returning, but semantic indexing
            # is asynchronous. Poll briefly so the soak measures eventual retrieval
            # instead of turning normal indexing latency into a false failure.
            while search_attempts < 6:
                search_attempts += 1
                headers = client.long_term.search_headers(query, top_k=5)
                search_hit = any(h.memory_id == receipt.memory_id for h in headers)
                if search_hit:
                    break
                time.sleep(0.5)
            outcome["search_ms"] = round((time.perf_counter() - started) * 1000, 1)
            outcome["search_attempts"] = search_attempts
            outcome["search_results"] = len(headers)
            outcome["search_hit"] = search_hit
            outcome["put_ok"] = True
            if secret is not None:
                started = time.perf_counter()
                job = client.long_term.forget(
                    receipt.memory_id,
                    confirm_token=confirmation_token(secret, "long_term.forget"),
                )
                client.long_term.wait_forget(job.job_id)
                outcome["forget_ms"] = round((time.perf_counter() - started) * 1000, 1)
                outcome["forget_ok"] = True
            else:
                outcome["forget_ok"] = None
                outcome["forget_skipped"] = "confirmation secret unreadable"
    except Exception as error:  # noqa: BLE001
        outcome["error"] = f"{type(error).__name__}: {error}"
        outcome.setdefault("put_ok", False)
        outcome.setdefault("search_hit", False)
    return outcome


def append_line(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def take_sample(window: dict[str, object], cycle: int) -> dict[str, object]:
    properties = unit_properties(MEMORYD_UNIT)
    pid = int(properties.get("MainPID") or 0)
    sample: dict[str, object] = {
        "ts": time.time(),
        "iso": now_iso(),
        "uptime_s": float(Path("/proc/uptime").read_text().split()[0]),
        "window_elapsed_s": round(time.time() - float(window["started_at"]), 1),
        "cycle": cycle,
        "unit": properties,
        "proc": proc_metrics(pid) if pid > 0 else {"error": "memoryd is not running"},
        "storage": storage_metrics(),
        "service": service_metrics(),
    }
    properties["MemoryCurrent"] = properties.get("MemoryCurrent", "")
    return sample


def run_loop(arguments: argparse.Namespace) -> int:
    state = Path(arguments.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    window_path = state / "window.json"
    if not window_path.exists():
        window_path.write_text(
            json.dumps(
                {
                    "started_at": time.time(),
                    "started_iso": now_iso(),
                    "interval": arguments.interval,
                    "disturb_interval": arguments.disturb_interval,
                    "restart_every_hours": arguments.restart_every_hours,
                    "unix_host": os.uname().nodename,
                    "kernel": os.uname().release,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    window = json.loads(window_path.read_text(encoding="utf-8"))
    samples_path = state / "samples.jsonl"
    disturbances_path = state / "disturbances.jsonl"
    restarts_path = state / "restarts.jsonl"
    ttl = float(window.get("restart_every_hours", 24.0)) * 3600.0
    next_disturbance = time.time()
    next_restart = time.time() + ttl
    cycle = 0
    while True:
        started = time.time()
        try:
            ensure_registration(f"soak.{PROBE_AGENT}.{local_device_id()}")
            sample = take_sample(window, cycle)
        except Exception as error:  # noqa: BLE001
            sample = {"ts": time.time(), "iso": now_iso(), "cycle": cycle,
                      "sample_error": f"{type(error).__name__}: {error}"}
        if time.time() >= next_disturbance:
            sample["disturbance"] = disturb(cycle)
            append_line(disturbances_path, {"ts": time.time(), "iso": now_iso(), **sample["disturbance"]})
            next_disturbance = time.time() + float(window["disturb_interval"])
        if time.time() >= next_restart:
            code, output = run(["systemctl", "restart", MEMORYD_UNIT], timeout=120)
            append_line(restarts_path, {"ts": time.time(), "iso": now_iso(), "planned": True,
                                        "exit_code": code, "output": output[:400]})
            sample["planned_restart"] = {"exit_code": code, "output": output[:200]}
            next_restart = time.time() + ttl
        append_line(samples_path, sample)
        cycle += 1
        elapsed = time.time() - started
        time.sleep(max(1.0, float(window["interval"]) - elapsed))


def window_report(state: Path) -> dict[str, object]:
    window_path = state / "window.json"
    if not window_path.exists():
        raise SystemExit(f"{window_path} 不存在：soak 还没启动过")
    window = json.loads(window_path.read_text(encoding="utf-8"))
    samples_path = state / "samples.jsonl"
    samples: list[dict[str, object]] = []
    if samples_path.exists():
        for line in samples_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                samples.append(json.loads(line))
    return {"window": window, "samples": samples}


def load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def journal_lines(pattern: str, since: str, until: str, unit: str = MEMORYD_UNIT) -> list[str]:
    code, output = run(
        ["journalctl", "-u", unit, "--since", since, "--until", until, "-g", pattern,
         "-o", "short-iso", "--no-pager"],
        timeout=180,
    )
    # journalctl exits 1 with a single localized meta line ("-- No entries --")
    # when the filter matches nothing.  For the negative queries here (fatal
    # patterns) that is the *clean* case, yet the old code turned it into a
    # "journalctl failed: -- No entries --" sentinel and made no_fatal_log fail
    # on any window with nothing to report.  Identify the meta line by shape, not
    # by text, and keep genuine errors.
    lines = [line for line in output.splitlines() if line.strip() and not line.startswith("-- ")]
    if code != 0:
        return [] if not lines else [f"journalctl failed: {' '.join(lines)[:200]}"]
    return lines


def verdict(state: Path, output_path: str | None) -> int:
    report = window_report(state)
    window, samples = report["window"], report["samples"]
    usable = [s for s in samples if "sample_error" not in s]
    started_at = float(window["started_at"])
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started_at))
    until = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() + 60))
    interval = float(window.get("interval", 60))

    def values(path: str, extractor):
        collected = []
        for sample in usable:
            node: object = sample
            for key in path.split("."):
                node = (node or {}).get(key) if isinstance(node, dict) else None
            if node is None:
                continue
            try:
                collected.append(extractor(node))
            except (TypeError, ValueError):
                continue
        return collected

    gaps = [
        round(b - a, 1)
        for a, b in zip([s["ts"] for s in usable], [s["ts"] for s in usable][1:])
    ]
    max_gap = max(gaps) if gaps else None
    expected_samples = int((time.time() - started_at) / interval)
    rss = values("proc.rss_kb", int)
    cgroup = [v for v in values("unit.MemoryCurrent", str) if v.isdigit()]
    cgroup_mb = [int(v) / 1024 / 1024 for v in cgroup]
    fds = values("proc.fds", int)
    health_status = [s.get("service", {}).get("status") for s in usable]
    health_errors = [s.get("service", {}).get("error") for s in usable if s.get("service", {}).get("error")]
    database_verdicts = [s.get("service", {}).get("database") for s in usable]
    disturbances = load_jsonl(state / "disturbances.jsonl")
    planned_restarts = load_jsonl(state / "restarts.jsonl")
    journal_errors = journal_lines("panic|OOM|out of memory|worker failed|corrupt|core dump", since, until)
    # systemd >= 254 logs "Started <unit> - <description>.", so the bare
    # description alone no longer matches and this gate silently counted zero
    # starts, which made an unplanned restart undetectable (systemd 259 here).
    starts = journal_lines(
        "Started Bianbu Agent Memory Service|Started bianbu-agent-memoryd\\.service",
        since, until,
    )
    stops = journal_lines("Stopped Bianbu Agent Memory Service|Deactivated successfully", since, until)  # noqa: E501

    window_seconds = time.time() - started_at
    rss_growth = None
    if len(rss) >= 20:
        head = statistics.median(rss[: max(1, len(rss) // 10)])
        tail = statistics.median(rss[-max(1, len(rss) // 10):])
        rss_growth = {"head_median_kb": head, "tail_median_kb": tail,
                      "delta_kb": tail - head,
                      "delta_ratio": round((tail - head) / head, 3) if head else None}
    max_rss_mb = max(rss) / 1024 if rss else None
    max_cgroup_mb = max(cgroup_mb) if cgroup_mb else None

    gates: dict[str, dict[str, object]] = {}

    def gate(identifier: str, ok: bool, detail: object, blocking: bool = True) -> None:
        gates[identifier] = {"ok": bool(ok), "detail": detail, "blocking": blocking}

    gate("sampler_continuity",
         bool(usable) and max_gap is not None and max_gap <= max(3 * interval, 300),
         {"samples": len(samples), "usable": len(usable), "expected": expected_samples,
          "max_gap_s": max_gap, "covered_ratio": round(len(usable) / expected_samples, 3)
          if expected_samples else None})
    gate("no_unplanned_restart",
         len(starts) <= 1 + len(planned_restarts) + 2,
         {"starts_in_journal": len(starts), "planned_restarts": len(planned_restarts),
          "stops_in_journal": len(stops)})
    gate("no_fatal_log",
         not any("failed" in line for line in journal_errors if "journalctl failed" in line)
         and len(journal_errors) == 0,
         {"matches": journal_errors[:20], "count": len(journal_errors)})
    gate("health_available",
         bool(usable) and len(health_errors) <= max(1, len(usable) // 100),
         {"samples": len(usable), "health_errors": len(health_errors),
          "first_error": health_errors[:3]})
    gate("database_integrity",
         bool(database_verdicts) and all(value == "ok" for value in database_verdicts),
         {"distinct_values": sorted({str(v) for v in database_verdicts}),
          "complaint_samples": sum(
              1 for s in usable if (s.get("service") or {}).get("check_state") == "complaint")})
    gate("memory_within_limit",
         max_cgroup_mb is not None and max_cgroup_mb <= 256.0,
         {"max_cgroup_mb": round(max_cgroup_mb, 1) if max_cgroup_mb else None,
          "max_rss_mb": round(max_rss_mb, 1) if max_rss_mb else None})
    gate("no_memory_growth",
         rss_growth is None or rss_growth["delta_kb"] <= 32768
         or (rss_growth["delta_ratio"] or 0) <= 0.25,
         rss_growth or "not enough samples")
    gate("no_fd_leak",
         bool(fds) and (max(fds) - min(fds)) <= 64,
         {"min": min(fds) if fds else None, "max": max(fds) if fds else None})
    gate("disturbance_success",
         bool(disturbances)
         and sum(1 for d in disturbances if d.get("put_ok") and d.get("search_hit"))
         >= 0.99 * len(disturbances),
         {"rounds": len(disturbances),
          "put_ok": sum(1 for d in disturbances if d.get("put_ok")),
          "search_hit": sum(1 for d in disturbances if d.get("search_hit")),
          "errors": [d.get("error") for d in disturbances if d.get("error")][:5]})
    gate("storage_headroom",
         (values("storage.free_bytes", int) or [0])[-1] > 1024 ** 3,
         {"free_bytes_last": (values("storage.free_bytes", int) or [None])[-1],
          "database_bytes_first": values("storage.database_bytes", int)[:1],
          "database_bytes_last": values("storage.database_bytes", int)[-1:]})
    gate("seven_day_window", window_seconds >= SEVEN_DAYS_SECONDS,
         {"window_seconds": round(window_seconds, 1),
          "window_days": round(window_seconds / 86400, 2),
          "required_days": 7.0})

    blocking_failures = [k for k, v in gates.items() if v["blocking"] and not v["ok"]]
    window_gate_failed = "seven_day_window" in blocking_failures
    filled = [k for k in blocking_failures if k != "seven_day_window"]
    if filled:
        result = "FAIL"
    elif window_gate_failed:
        result = "INCOMPLETE"
    else:
        result = "PASS"
    evidence = {
        "verdict": result,
        "checked_at": now_iso(),
        "window": window,
        "seven_day_window": gates["seven_day_window"]["detail"],
        "samples": len(samples),
        "usable_samples": len(usable),
        "gates": gates,
        "planned_restarts": len(planned_restarts),
        "disturbance_rounds": len(disturbances),
        "sampled_metrics": {
            "max_rss_mb": round(max_rss_mb, 1) if max_rss_mb else None,
            "max_cgroup_mb": round(max_cgroup_mb, 1) if max_cgroup_mb else None,
            "rss_growth": rss_growth,
            "fd_min": min(fds) if fds else None,
            "fd_max": max(fds) if fds else None,
            "database_bytes_first": values("storage.database_bytes", int)[:1],
            "database_bytes_last": values("storage.database_bytes", int)[-1:],
            "wal_bytes_last": values("storage.wal_bytes", int)[-1:],
            "free_bytes_last": values("storage.free_bytes", int)[-1:],
        },
    }
    print(f"soak 验收：{result}")
    for identifier, entry in gates.items():
        print(f"  [{'PASS' if entry['ok'] else 'FAIL'}] {identifier}: "
              f"{json.dumps(entry['detail'], ensure_ascii=False)[:300]}")
    if output_path:
        Path(output_path).write_text(json.dumps(evidence, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        print(f"证据已写入 {output_path}")
    return 0 if result == "PASS" else 1


def command_start(arguments: argparse.Namespace) -> int:
    state = Path(arguments.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    window_path = state / "window.json"
    if window_path.exists() and not arguments.restart_window:
        print(f"{window_path} 已存在：本窗口仍在计时，用 --restart-window 才重新开始")
        print(json.dumps(json.loads(window_path.read_text(encoding="utf-8")),
                         ensure_ascii=False, indent=2))
        return 1
    window_path.unlink(missing_ok=True)
    script = Path(__file__).resolve()
    SOAK_UNIT_PATH.write_text(
        UNIT_TEMPLATE.format(script=script, state_dir=state,
                             interval=arguments.interval,
                             disturb_interval=arguments.disturb_interval,
                             restart_every_hours=arguments.restart_every_hours),
        encoding="utf-8",
    )
    run(["systemctl", "daemon-reload"])
    code, output = run(["systemctl", "enable", "--now", SOAK_UNIT])
    print(f"systemctl enable --now {SOAK_UNIT}: exit={code} {output[:200]}")
    time.sleep(3)
    properties = unit_properties(SOAK_UNIT)
    print(json.dumps(properties, ensure_ascii=False, indent=2))
    version = run(["dpkg-query", "-W", "-f=${Version}", "bianbu-agent-memoryd"])[1]
    print(f"memoryd 版本（开发期编号）：{version}")
    return 0 if properties.get("ActiveState") == "active" else 1


def command_stop(arguments: argparse.Namespace) -> int:
    code, output = run(["systemctl", "disable", "--now", SOAK_UNIT])
    print(f"systemctl disable --now {SOAK_UNIT}: exit={code} {output[:200]}")
    SOAK_UNIT_PATH.unlink(missing_ok=True)
    run(["systemctl", "daemon-reload"])
    print(f"采样 unit 已删除；样本保留在 {arguments.state_dir}")
    return 0


def command_status(arguments: argparse.Namespace) -> int:
    state = Path(arguments.state_dir)
    print(json.dumps(unit_properties(SOAK_UNIT), ensure_ascii=False, indent=2))
    if not (state / "window.json").exists():
        print("窗口未开始")
        return 1
    report = window_report(state)
    window, samples = report["window"], report["samples"]
    last = samples[-1] if samples else None
    print(json.dumps({
        "started_iso": window["started_iso"],
        "elapsed_days": round((time.time() - window["started_at"]) / 86400, 3),
        "samples": len(samples),
        "disturbances": len(load_jsonl(state / "disturbances.jsonl")),
        "planned_restarts": len(load_jsonl(state / "restarts.jsonl")),
        "last_sample": last,
    }, ensure_ascii=False, indent=2)[:4000])
    return 0


def command_sample(arguments: argparse.Namespace) -> int:
    state = Path(arguments.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    ensure_registration(f"soak.{PROBE_AGENT}.{local_device_id()}")
    window = json.loads((state / "window.json").read_text(encoding="utf-8")) \
        if (state / "window.json").exists() else {"started_at": time.time(), "interval": 60}
    print(json.dumps(take_sample(window, -1), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="K3 七天稳定性 soak")
    parser.add_argument("--state-dir", default=str(STATE_DIR))
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="安装并启动采样 unit")
    start.add_argument("--interval", type=float, default=60.0)
    start.add_argument("--disturb-interval", type=float, default=1800.0)
    start.add_argument("--restart-every-hours", type=float, default=24.0)
    start.add_argument("--restart-window", action="store_true", help="丢弃旧窗口重新计时")
    start.set_defaults(function=command_start)

    stop = subparsers.add_parser("stop", help="停止并删除采样 unit（保留样本）")
    stop.set_defaults(function=command_stop)

    status = subparsers.add_parser("status", help="查看窗口与最近一次样本")
    status.set_defaults(function=command_status)

    sample = subparsers.add_parser("sample", help="立刻取一次样本")
    sample.set_defaults(function=command_sample)

    verdict_parser = subparsers.add_parser("verdict", help="汇总样本与 journal 出验收证据")
    verdict_parser.add_argument("--json", dest="json_path")
    verdict_parser.set_defaults(function=lambda arguments: verdict(
        Path(arguments.state_dir), arguments.json_path))

    loop = subparsers.add_parser("run-loop", help="采样循环（由 systemd 调用）")
    loop.add_argument("--interval", type=float, default=60.0)
    loop.add_argument("--disturb-interval", type=float, default=1800.0)
    loop.add_argument("--restart-every-hours", type=float, default=24.0)
    loop.set_defaults(function=run_loop)

    arguments = parser.parse_args()
    return arguments.function(arguments)


if __name__ == "__main__":
    sys.exit(main())
