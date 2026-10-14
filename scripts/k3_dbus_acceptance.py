#!/usr/bin/env python3
"""K3 现网 D-Bus 控制面验收（MEM-INT-06-02）。

验收对象是设备上**现网**的 `bianbu-agent-memoryd`，不启动任何测试实例、不改动
生产命名空间：只验证 D-Bus 接口本身 —— 总线名 `com.bianbu.AgentMemory1`、对象路径
`/com/bianbu/AgentMemory1`、方法 `Call(s) -> s`、属性 `ProtocolVersion`。

三个阶段：

1. **发现**：总线名归属、`Introspect` 暴露的接口、`ProtocolVersion` 取值。受信系统
   组件（BiBit、系统设置）就是靠这三步找到控制面。
2. **未注册调用者**：用 `busctl` 直接调用（该进程不在 Runtime 注册表里）→ 必须被拒
   （`Unauthenticated: runtime registration was not found`）。证明 D-Bus 不是绕开
   SDK/UDS 身份校验的运维后门。
3. **已注册调用者**：把**本进程**注册进
   `/run/bianbu/agent-runtime/memory-registry/<pid>.json`（Runtime 自己就是按这个
   格式注册的），再由同一进程经 Gio 发 D-Bus 请求 → 拿到与 UDS 等价的成功响应；
   随后删掉注册文件，同一进程再调用 → 又被拒。这证明授权来自注册身份，而不是
   "能连上总线"。

另附信封负例（都在已注册状态下发，确保拒绝理由不是身份）：协议版本不符、塞入
`system.hello`、超出帧上限、非 JSON。

用法（K3，仓库根目录，`bianbu` 普通用户即可，不需要 root）：

    python3 scripts/k3_dbus_acceptance.py
    python3 scripts/k3_dbus_acceptance.py --json /tmp/k3-dbus-acceptance.json

退出码 0 表示全部门禁通过；探针注册会在脚本退出前删除，且带 15 分钟兜底过期。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

BUS_NAME = "com.bianbu.AgentMemory1"
OBJECT_PATH = "/com/bianbu/AgentMemory1"
INTERFACE = "com.bianbu.AgentMemory1"
PROTOCOL_VERSION = "agent-memory.v1"
MAX_FRAME_BYTES = 4 * 1024 * 1024
REGISTRY = Path("/run/bianbu/agent-runtime/memory-registry")
# 探针身份只能看到自己的 account/agent 维度，且 15 分钟后自动过期。
PROBE_AGENT = "com.bianbu.memory.dbus.probe"
PROBE_CAPABILITIES = ["memory.audit.status", "memory.audit.read"]
REGISTRATION_TTL_MS = 15 * 60 * 1000


def run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
    except (OSError, subprocess.TimeoutExpired) as error:  # pragma: no cover - device only
        return 127, f"{type(error).__name__}: {error}"
    return completed.returncode, (completed.stdout + completed.stderr).strip()


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
    return "dbus-acceptance"


def register_probe(space: str) -> Path:
    """按 Runtime 的格式把本进程登记为已认证调用者。"""
    pid = os.getpid()
    record = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "pid_start_time_ticks": start_time_ticks(pid),
        "account_id": "default",
        "device_id": local_device_id(),
        "agent_id": PROBE_AGENT,
        "agent_name": PROBE_AGENT,
        "agent_instance_id": f"dbus-acceptance.{pid}",
        "memory_space_id": space,
        "capabilities": PROBE_CAPABILITIES,
        "expires_at_ms": int(time.time() * 1000) + REGISTRATION_TTL_MS,
    }
    path = REGISTRY / f"{pid}.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o640)
    return path


def envelope(method: str, params: dict | None = None, protocol: str = PROTOCOL_VERSION,
             request_id: str = "dbus-acceptance") -> str:
    return json.dumps(
        {
            "protocol": protocol,
            "request_id": request_id,
            "trace_id": request_id,
            "method": method,
            "params": params or {},
        },
        ensure_ascii=False,
    )


def dbus_call(payload: str, timeout_ms: int = 15000) -> dict:
    """在**本进程**里发 D-Bus 请求（调用者身份 = 本进程 PID）。"""
    import gi  # noqa: PLC0415 - 只在需要时加载，避免无 Gio 的环境导入失败

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib  # noqa: PLC0415

    connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    try:
        reply = connection.call_sync(
            BUS_NAME,
            OBJECT_PATH,
            INTERFACE,
            "Call",
            GLib.Variant("(s)", (payload,)),
            GLib.VariantType.new("(s)"),
            Gio.DBusCallFlags.NONE,
            timeout_ms,
            None,
        )
    except GLib.Error as error:  # pragma: no cover - device only
        return {"transport_error": error.message}
    return json.loads(reply.unpack()[0])


def busctl_call(payload: str) -> tuple[int, dict | str]:
    """用 `busctl` 子进程调用：调用者身份 = busctl 自己，不在注册表里。"""
    code, output = run(
        [
            "busctl", "--system", "call", BUS_NAME, OBJECT_PATH, INTERFACE,
            "Call", "s", payload,
        ]
    )
    body = output
    prefix = 's "'
    if body.startswith(prefix) and body.endswith('"'):
        body = body[len(prefix):-1].replace('\\"', '"').replace("\\\\", "\\")
    try:
        return code, json.loads(body)
    except json.JSONDecodeError:
        return code, output


def gate(gates: list[dict], identifier: str, description: str, ok: bool, detail: object) -> None:
    gates.append({"id": identifier, "description": description, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {identifier} {description}")
    if not ok:
        print(f"       detail: {json.dumps(detail, ensure_ascii=False)[:600]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="K3 现网 D-Bus 控制面验收")
    parser.add_argument("--json", help="把验收证据写成 JSON 文件")
    arguments = parser.parse_args()

    gates: list[dict] = []
    evidence: dict = {"host": os.uname().nodename, "uid": os.getuid(), "gates": gates}

    # --- 阶段 1：发现 -----------------------------------------------------
    code, listing = run(["busctl", "--system", "list"])
    owner_line = next((line for line in listing.splitlines() if BUS_NAME in line), "")
    evidence["busctl_list_line"] = owner_line
    gate(
        gates,
        "G1-bus-owner",
        "系统总线上总线名归属 memoryd",
        code == 0 and "bianbu-agent-memoryd" in owner_line and "bianbu-memory" in owner_line,
        owner_line or listing[:400],
    )

    code, introspection = run(
        ["busctl", "--system", "introspect", BUS_NAME, OBJECT_PATH]
    )
    evidence["introspect"] = introspection
    gate(
        gates,
        "G2-introspect",
        "Introspect 暴露 Call(s)->s 与 ProtocolVersion",
        ".Call" in introspection and ".ProtocolVersion" in introspection,
        [line for line in introspection.splitlines() if "Call" in line or "ProtocolVersion" in line],
    )

    code, version_line = run(
        [
            "busctl", "--system", "get-property", BUS_NAME, OBJECT_PATH, INTERFACE,
            "ProtocolVersion",
        ]
    )
    evidence["protocol_version_line"] = version_line
    gate(
        gates,
        "G3-protocol-version",
        f"ProtocolVersion == {PROTOCOL_VERSION}",
        code == 0 and f'"{PROTOCOL_VERSION}"' in version_line,
        version_line,
    )

    # --- 阶段 2：未注册调用者必须被拒 -------------------------------------
    held_ok = busctl_call(envelope("system.health", request_id="unregistered"))
    unregistered = held_ok[1] if isinstance(held_ok[1], dict) else {"raw": held_ok[1]}
    evidence["unregistered_call"] = unregistered
    gate(
        gates,
        "G4-unregistered-rejected",
        "未注册调用者被拒（Unauthenticated）",
        unregistered.get("error", {}).get("code") == "Unauthenticated"
        and "runtime registration was not found" in unregistered.get("error", {}).get("message", ""),
        unregistered,
    )

    # --- 阶段 3：注册后同一进程可调用，注销后又被拒 -----------------------
    registration = register_probe(f"dbus-acceptance.{os.getpid()}")
    evidence["registration_file"] = str(registration)
    try:
        health = dbus_call(envelope("system.health", request_id="registered-health"))
        evidence["registered_health"] = health
        result = health.get("result") or {}
        gate(
            gates,
            "G5-registered-health",
            "已注册调用者拿到 system.health 成功响应",
            health.get("status") == "ok"
            and result.get("schema_version") is not None
            and result.get("database_check") is not None,
            {k: health.get(k) for k in ("status", "request_id", "server_protocol_version", "error")}
            | {"result_keys": sorted(result.keys())},
        )

        stats = dbus_call(envelope("system.stats", request_id="registered-stats"))
        evidence["registered_stats"] = stats
        gate(
            gates,
            "G6-registered-stats",
            "已注册调用者拿到 system.stats 成功响应",
            stats.get("status") == "ok" and isinstance(stats.get("result"), dict),
            stats.get("error") or {"status": stats.get("status")},
        )

        mismatch = dbus_call(
            envelope("system.health", protocol="agent-memory.v9", request_id="bad-protocol")
        )
        evidence["protocol_mismatch"] = mismatch
        gate(
            gates,
            "G7-protocol-mismatch",
            "协议版本不符返回 ProtocolMismatch",
            mismatch.get("error", {}).get("code") == "ProtocolMismatch",
            mismatch,
        )

        hello = dbus_call(envelope("system.hello", request_id="hello"))
        evidence["hello_rejected"] = hello
        gate(
            gates,
            "G8-hello-rejected",
            "system.hello 在 D-Bus 上被拒（身份由总线提供）",
            hello.get("error", {}).get("code") == "InvalidArgument"
            and "system.hello" in hello.get("error", {}).get("message", ""),
            hello,
        )

        oversized = envelope(
            "system.health", params={"padding": "x" * (MAX_FRAME_BYTES + 16)},
            request_id="oversized",
        )
        oversized_response = dbus_call(oversized)
        evidence["oversized_frame"] = oversized_response
        gate(
            gates,
            "G9-oversized-frame",
            "超出帧上限的请求被拒（request exceeds protocol limit）",
            oversized_response.get("error", {}).get("code") == "InvalidArgument"
            and "exceeds protocol limit" in oversized_response.get("error", {}).get("message", ""),
            oversized_response,
        )

        not_json = dbus_call("this is not json")
        evidence["invalid_json"] = not_json
        gate(
            gates,
            "G10-invalid-json",
            "非 JSON 请求被拒（request is not valid JSON）",
            not_json.get("error", {}).get("code") == "InvalidArgument"
            and "not valid JSON" in not_json.get("error", {}).get("message", ""),
            not_json,
        )
    finally:
        registration.unlink(missing_ok=True)
        evidence["registration_removed"] = not registration.exists()

    revoked = dbus_call(envelope("system.health", request_id="revoked"))
    evidence["after_unregister"] = revoked
    gate(
        gates,
        "G11-registration-revoked",
        "删除注册文件后同一进程重新被拒（授权来自注册身份）",
        revoked.get("error", {}).get("code") == "Unauthenticated",
        revoked,
    )

    passed = [entry for entry in gates if entry["ok"]]
    evidence["summary"] = {
        "passed": len(passed),
        "total": len(gates),
        "verdict": "PASS" if len(passed) == len(gates) else "FAIL",
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    print(
        f"\nD-Bus 控制面验收：{evidence['summary']['verdict']} "
        f"({len(passed)}/{len(gates)})"
    )
    if arguments.json:
        output = Path(arguments.json)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"证据已写入 {output}")
    return 0 if evidence["summary"]["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
