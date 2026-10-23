#!/usr/bin/env python3
"""断电注入验收驱动（REL-06 / MEM-NFR-04-02）。

在开发机上通过 SSH 驱动 K3 完成一次"等效硬掉电 → 重启 → 校验"闭环：

1. 把 `scripts/k3_power_loss_probe.py` 送到设备，跑 `pre`：记录 boot_id、版本、unit
   状态、库/WAL 体积、`system.health`、命名空间行数，并写入一批不带过期时间的
   标记记忆（含 memory_id 与文本 sha256）；
2. 触发 `echo b > /proc/sysrq-trigger`：内核立即重启、**不做 sync**，页缓存里的脏页
   全部丢失 —— 这与拔电的表现一致（`sync` + 软重启不构成断电样本）；
3. 等设备回来，**先等守护进程自己的库自检出结论**（只看 `systemctl is-active` 会把
   「还没验」读成「验出问题」，2026-10-09 真跑时假 FAIL 过一次），再跑 `post`：比对
   boot_id、schema、key package 可用性、标记记忆逐条取回与文本 sha256、命名空间行数
   不清零、临时文件无新增、journal 恢复行与报错行。

**这会硬重启整台 K3**：bibit-intent、gateway 以及设备上其它任务的进程都会被中断。
真正拔市电的场地断电仍需现场受控操作；本脚本是"软件等效掉电"。

用法（开发机，PowerShell/任意 shell）：

    python3 scripts/k3_power_loss_test.py --dry-run                 # 只做 pre，不重启
    python3 scripts/k3_power_loss_test.py --trigger                 # 真的重启一次
    python3 scripts/k3_power_loss_test.py --trigger --json artifacts/k3-power-loss.json

需要 SSH 密码时用 `--password`，或用环境变量 `K3_PASSWORD`。
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_HOST = "192.0.2.10"
DEFAULT_USER = "bianbu"
DEFAULT_REMOTE_DIR = "/home/example/mem-gateway-build"
PRE_PATH = "/var/lib/bianbu-agent-memory/power-test/pre.json"
POST_PATH = "/var/lib/bianbu-agent-memory/power-test/post.json"


def ssh(arguments: argparse.Namespace, command: str, timeout: int = 120) -> tuple[int, str]:
    completed = subprocess.run(
        [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
            f"{arguments.user}@{arguments.host}", command,
        ],
        capture_output=True, text=True, timeout=timeout, errors="replace",
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def ssh_sudo(arguments: argparse.Namespace, command: str, timeout: int = 300) -> tuple[int, str]:
    inner = (
        f"echo {shlex.quote(arguments.password)} | sudo -S -p '' sh -c {shlex.quote(command)}"
    )
    return ssh(arguments, inner, timeout=timeout)


def scp(arguments: argparse.Namespace, local: Path, remote: str) -> None:
    for attempt in range(3):
        completed = subprocess.run(
            ["scp", "-o", "StrictHostKeyChecking=no", str(local),
             f"{arguments.user}@{arguments.host}:{remote}"],
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        if completed.returncode == 0:
            return
        time.sleep(3)
    raise SystemExit(f"scp {local} 失败：{completed.stderr}")


def stage_path(arguments: argparse.Namespace, name: str) -> Path:
    """中间产物落在 `--json` 旁边（没给 --json 就落到临时目录），不污染仓库。"""
    if arguments.json:
        return Path(arguments.json).resolve().parent / name
    import tempfile

    return Path(tempfile.gettempdir()) / name


def fetch(arguments: argparse.Namespace, remote: str, local: Path) -> dict:
    """取回设备上的探针报告。

    报告写在 root 独占的 `/var/lib/bianbu-agent-memory/power-test/`：pre 那一份必须
    活过重启，放 `/tmp` 会丢。代价是普通用户的 scp 读不到（目录 0700、文件 0600），
    所以这里用 sudo 取回，不去放宽设备数据目录的权限。
    """
    local.parent.mkdir(parents=True, exist_ok=True)
    code, output = ssh_sudo(arguments, f"cat {shlex.quote(remote)}")
    start, end = output.find("{"), output.rfind("}")
    if code != 0 or start < 0 or end <= start:
        raise SystemExit(f"读取设备上的 {remote} 失败：{output[:300]}")
    payload = output[start : end + 1]
    local.write_text(payload, encoding="utf-8")
    return json.loads(payload)


def wait_for_device(arguments: argparse.Namespace, timeout: int) -> float:
    started = time.time()
    while time.time() - started < timeout:
        try:
            code, output = ssh(arguments, "cat /proc/sys/kernel/random/boot_id", timeout=15)
        except subprocess.TimeoutExpired:
            code, output = 1, ""
        if code == 0 and output:
            return time.time() - started
        time.sleep(5)
    raise SystemExit(f"设备 {timeout}s 内没有回来，需要手动检查")


READY_STATES = frozenset({"ok", "degraded"})
SERVICE_STATES = frozenset(
    {"active", "activating", "inactive", "deactivating", "failed", "reloading"}
)


def service_state(output: str) -> str:
    """从 `systemctl is-active` 的输出里挑出状态词。

    不能取最后一行：Windows 的 OpenSSH 会把 `close - IO is still pending on closed
    socket.` 这类噪声落到 stderr，而 `ssh()` 把 stderr 也并进来了，最后一行往往是
    噪声。真跑时因此把 `active` 读成噪声，等待循环一次都没探测过健康检查。
    """
    for line in reversed(output.splitlines()):
        word = line.strip()
        if word in SERVICE_STATES:
            return word
    return "unknown"


def probe_health(arguments: argparse.Namespace) -> dict:
    """让设备上的探针只读地报一次 `system.health`，返回它的 JSON。"""
    code, output = ssh_sudo(
        arguments,
        f"cd {shlex.quote(arguments.remote_dir)} && "
        f"python3 scripts/k3_power_loss_probe.py health",
        timeout=60,
    )
    start, end = output.find("{"), output.rfind("}")
    if start < 0 or end <= start:
        return {"error": f"exit {code}: {output.strip()[-200:]}"}
    try:
        return json.loads(output[start : end + 1])
    except json.JSONDecodeError as error:
        return {"error": f"{type(error).__name__}: {error}"}


def wait_for_ready(arguments: argparse.Namespace, timeout: int = 300) -> dict:
    """等守护进程「起来了」并且「自己的库自检已出结论」，两条都要。

    只看 `systemctl is-active` 是不够的：重启后守护进程先起监听，`system.health` 这
    时才在静默窗口（`AGENT_MEMORY_HEALTH_SETTLE_MS`）之外取样，这段时间它报
    `unknown` / `database=busy` / `database_check.verdict=null` —— 库**根本没被验过**。
    2026-10-09 的真断电验收就因此在第一次 post 上假 FAIL 过一次（数据全程无损，
    三分钟后重跑全绿）。等结论再采，才不会把「还没验」读成「验出问题」。

    已出结论 = `status` 是 `ok` 或 `degraded`：后者是真缺陷，同样停止等待，交给
    post 的门禁去判 FAIL。等不到就返回 `conclusive=false` 并把原因带回去，由
    post 按实测结果判定，不在这一层掩盖。
    """
    started = time.time()
    service = "unknown"
    health: dict = {}
    polls = 0
    while time.time() - started < timeout:
        code, output = ssh(arguments, "systemctl is-active bianbu-agent-memoryd", timeout=20)
        service = service_state(output)
        if service == "active":
            polls += 1
            sample = probe_health(arguments)
            health = sample.get("health") if isinstance(sample.get("health"), dict) else {}
            if health.get("status") in READY_STATES:
                return {
                    "service": service,
                    "status": health.get("status"),
                    "database": health.get("database"),
                    "database_check": health.get("database_check"),
                    "conclusive": True,
                    "polls": polls,
                    "waited_s": round(time.time() - started, 1),
                }
        time.sleep(5)
    return {
        "service": service,
        "status": health.get("status"),
        "database": health.get("database"),
        "database_check": health.get("database_check"),
        "conclusive": False,
        "polls": polls,
        "waited_s": round(time.time() - started, 1),
        "note": "超时：守护进程的库自检未出结论（不是验出问题，是还没验）。",
    }


def ready_summary(ready: dict) -> str:
    check = ready.get("database_check") or {}
    return (f"bianbu-agent-memoryd {ready.get('service')}，库自检 "
            f"{ready.get('status')}/{ready.get('database')}"
            f"（state={check.get('state')}，探测 {ready.get('polls')} 次，"
            f"{ready.get('waited_s')}s）")


def main() -> int:
    parser = argparse.ArgumentParser(description="K3 断电注入验收")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=os.environ.get("K3_PASSWORD", "bianbu"))
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR)
    parser.add_argument("--markers", type=int, default=20)
    parser.add_argument("--boot-timeout", type=int, default=420)
    parser.add_argument("--ready-timeout", type=int, default=300,
                        help="重启后等「服务 active 且库自检出结论」的上限秒数")
    parser.add_argument("--json", help="把两次采集写成一份本地证据 JSON")
    parser.add_argument("--trigger", action="store_true", help="真的执行 echo b > sysrq-trigger")
    parser.add_argument("--dry-run", action="store_true",
                        help="只做 pre 采集，不触发断电（不加 --trigger 时的默认行为）")
    parser.add_argument("--forget-markers", action="store_true",
                        help="校验后删掉标记记忆，避免探针数据长期留在库里")
    arguments = parser.parse_args()
    if arguments.dry_run and arguments.trigger:
        raise SystemExit("--dry-run 与 --trigger 互斥")

    probe_local = Path(__file__).resolve().parent / "k3_power_loss_probe.py"
    probe_remote = f"{arguments.remote_dir}/scripts/k3_power_loss_probe.py"
    print(f"[1/6] 上传探针 {probe_local.name} → {probe_remote}")
    scp(arguments, probe_local, probe_remote)

    print("[2/6] 掉电前采集（pre）")
    code, output = ssh_sudo(
        arguments,
        f"install -d -m 0700 /var/lib/bianbu-agent-memory/power-test && "
        f"cd {shlex.quote(arguments.remote_dir)} && python3 scripts/k3_power_loss_probe.py pre "
        f"--json {PRE_PATH} --markers {arguments.markers}",
    )
    print(output[-2000:])
    if code != 0:
        raise SystemExit("pre 阶段失败，未触发断电")
    pre = fetch(arguments, PRE_PATH, stage_path(arguments, "_k3-power-pre.json"))
    # 只读句柄上 fsync 在 Windows 会直接报 EBADF；用可写句柄保住「本地基线已落盘」
    # 的意图，POSIX 与 Windows 都能跑。
    with stage_path(arguments, "_k3-power-pre.json").open("ab") as baseline:
        os.fsync(baseline.fileno())
    if (pre.get("health", {}).get("status") != "ok"
            or not pre.get("namespaces", {}).get("totals")
            or len(pre.get("markers", [])) != arguments.markers
            or arguments.markers <= 0):
        raise SystemExit("Invalid pre baseline; reboot blocked")
    print(f"      标记记忆 {len(pre['markers'])} 条，boot_id={pre['boot_id'][:8]}，"
          f"uptime={pre['uptime_s']:.0f}s，WAL={pre['storage_after_markers']['wal_bytes']} bytes")

    if not arguments.trigger:
        # 同一段等待在这里先空跑一次：设备本来就在跑，自检应当立刻有结论。这样
        # `--dry-run` 就能覆盖「等自检」这条新路径，不必靠真断电才发现它写错。
        ready = wait_for_ready(arguments, arguments.ready_timeout)
        print("[3/6] --dry-run：跳过断电触发。要真正执行请加 --trigger")
        print(f"      当前就绪状态：{ready_summary(ready)}")
        if not ready["conclusive"]:
            print(f"      警告：{ready['note']}")
        if arguments.json:
            Path(arguments.json).write_text(
                json.dumps({"pre": pre, "ready": ready, "triggered": False},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        return 0

    print("[3/6] 触发等效硬掉电：echo b > /proc/sysrq-trigger（不做 sync）")
    try:
        ssh_sudo(arguments, "echo b > /proc/sysrq-trigger", timeout=30)
    except subprocess.TimeoutExpired:
        print("      SSH 连接按预期断开")
    print(f"      触发时间 {time.strftime('%Y-%m-%d %H:%M:%S')}")

    print("[4/6] 等设备重启回来")
    elapsed = wait_for_device(arguments, arguments.boot_timeout)
    print(f"      设备 {elapsed:.0f}s 后恢复 SSH")
    ready = wait_for_ready(arguments, arguments.ready_timeout)
    print(f"      {ready_summary(ready)}")
    if not ready["conclusive"]:
        print(f"      警告：{ready['note']}")

    print("[5/6] 重启后校验（post）")
    code, output = ssh_sudo(
        arguments,
        f"cd {arguments.remote_dir} && python3 scripts/k3_power_loss_probe.py post "
        f"--json {POST_PATH} --pre {PRE_PATH}"
        + (" --forget-markers" if arguments.forget_markers else ""),
    )
    print(output[-4000:])
    post = fetch(arguments, POST_PATH, stage_path(arguments, "_k3-power-post.json"))

    print("[6/6] 结论")
    verdict = "PASS" if code == 0 and post.get("verdict") == "PASS" else "FAIL"
    print(f"      boot_id: {pre['boot_id'][:8]} → {post['boot_id'][:8]}")
    print(f"      标记记忆 {post['markers_expected']} 条期望 / "
          f"{sum(1 for item in post['markers_recovered'] if item.get('text_matches'))} 条文本一致")
    print(f"      namespace 行数对比：{json.dumps(post.get('comparison', {}), ensure_ascii=False)}")
    print(f"      REL-06 判定：{verdict}"
          + ("" if ready["conclusive"] else "（注意：库自检未出结论，health 门禁按未取样判）"))
    if arguments.json:
        Path(arguments.json).write_text(
            json.dumps(
                {
                    "pre": pre,
                    "post": post,
                    "verdict": verdict,
                    "reboot_elapsed_s": round(elapsed, 1),
                    "ready": ready,
                    "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                },
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
        print(f"      证据已写入 {arguments.json}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
