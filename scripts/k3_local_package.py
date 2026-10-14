#!/usr/bin/env python3
"""K3 本地开发包：构建 / 安装 / 校验（MEM-NFR-04-02 的测试锚点）。

稳定性验收必须钉在**一个可复现的产物**上。本脚本在 K3 的构建树里用最新源码
打本地开发包，记录两个 deb 与包内守护进程二进制的 sha256，需要时直接装包并
用现网接口复核，最后把证据写成 JSON。

约定（与 `docs/TEST_CASES.md` §0、README「打包命令与注意事项」一致）：

- 包版本号是 build 树 `debian/changelog` 的开发期编号（本机为 `0.1.20`），
  **不**修改 main 分支的 `debian/changelog`，也不打 tag；发布版本只由 tag 决定；
- 构建命令不带 `-nc`：`debian/rules clean` 会清掉 debhelper 的构建标记，否则
  dh 会跳过编译、把 `target/release` 里既有的二进制重新装包；
- riscv64 上 Ubuntu 的 dpkg vendor hook 会自动追加 `nocheck`，所以 `cargo test`
  必须单独跑（见 `docs/TEST_CASES.md`）；
- 构建以 root + `CARGO_HOME=/home/example/.cargo` 执行，debs 落在构建树上一级目录。

用法（开发机）：

    python scripts/k3_local_package.py --build                 # 只打包 + 记录哈希
    python scripts/k3_local_package.py --build --install       # 打包后直接安装并复核
    python scripts/k3_local_package.py --install               # 装现有的最新 debs 并复核
    python scripts/k3_local_package.py --json artifacts/k3-local-package.json

`--install` 之后会依次核对：`dpkg -l` 版本、`bianbu-agent-memoryd` active、
`--report-namespaces` 行数（与安装前一致）以及 `scripts/k3_dbus_acceptance.py`
的 11 条门禁（其中 `system.health` 覆盖 schema、库完整性、生效 embedding 端点）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_HOST = "192.0.2.10"
DEFAULT_USER = "bianbu"
DEFAULT_REMOTE_DIR = "/home/example/mem-gateway-build"
DAEMON_DEB_GLOB = "bianbu-agent-memoryd_*_riscv64.deb"
SDK_DEB_GLOB = "bianbu-agent-memory_*_all.deb"
BUILD_LOG = "/tmp/k3-local-build.log"


def ssh(arguments: argparse.Namespace, command: str, timeout: int = 600) -> tuple[int, str]:
    completed = subprocess.run(
        [
            "ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=10",
            f"{arguments.user}@{arguments.host}", command,
        ],
        capture_output=True, text=True, timeout=timeout, errors="replace",
    )
    return completed.returncode, (completed.stdout + completed.stderr).strip()


def ssh_sudo(arguments: argparse.Namespace, command: str, timeout: int = 1800) -> tuple[int, str]:
    inner = f"echo {shlex.quote(arguments.password)} | sudo -S -p '' sh -c {shlex.quote(command)}"
    return ssh(arguments, inner, timeout=timeout)


def remote_hashes(arguments: argparse.Namespace) -> dict[str, object]:
    """包与包内二进制的 sha256，以及和上一版包的对比。"""
    script = (
        f"cd {DEFAULT_REMOTE_DIR}/.. && "
        f"ls -1 {DAEMON_DEB_GLOB} {SDK_DEB_GLOB} | tail -4 && echo '--- hashes ---' && "
        f"sha256sum $(ls -1t {DAEMON_DEB_GLOB} | head -1) $(ls -1t {SDK_DEB_GLOB} | head -1) && "
        "echo '--- embedded binary ---' && "
        f"newest=$(ls -1t {DAEMON_DEB_GLOB} | head -1) && "
        "dpkg-deb --fsys-tarfile \"$newest\" | tar -xO ./usr/libexec/bianbu-agent-memoryd | sha256sum && "
        "echo '--- previous package embedded binary ---' && "
        f"previous=$(ls -1t {DAEMON_DEB_GLOB} | sed -n 2p) && "
        "dpkg-deb --fsys-tarfile \"$previous\" | tar -xO ./usr/libexec/bianbu-agent-memoryd | sha256sum && "
        "echo '--- installed binary ---' && sha256sum /usr/libexec/bianbu-agent-memoryd"
    )
    code, output = ssh(arguments, script, timeout=300)
    return {"exit_code": code, "raw": output}


def parse_deb_hashes(raw: str) -> dict[str, object]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    sections: dict[str, list[str]] = {"list": [], "deb": [], "embedded": [], "previous": [], "installed": []}
    current = "list"
    for line in lines:
        if line.startswith("--- hashes"):
            current = "deb"
            continue
        if line.startswith("--- embedded"):
            current = "embedded"
            continue
        if line.startswith("--- previous"):
            current = "previous"
            continue
        if line.startswith("--- installed"):
            current = "installed"
            continue
        sections[current].append(line)
    def first_hash(entries: list[str]) -> str | None:
        for entry in entries:
            match = re.match(r"^([0-9a-f]{64})\s+(.*)$", entry)
            if match:
                return match.group(1)
        return None
    return {
        "debs": sections["deb"],
        "embedded_binary_sha256": first_hash(sections["embedded"]),
        "previous_package_binary_sha256": first_hash(sections["previous"]),
        "installed_binary_sha256": first_hash(sections["installed"]),
    }


def namespace_count(arguments: argparse.Namespace) -> dict[str, object]:
    """生产库的行数清册（只读），用于证明安装/升级没有丢数据。"""
    code, output = ssh_sudo(
        arguments,
        "/usr/libexec/bianbu-agent-memoryd --database /var/lib/bianbu-agent-memory/memory.db "
        "--key-package /var/lib/bianbu-agent-memory/memory.keypkg "
        "--account-secret-file /etc/bianbu-agent-memory/account-secret --report-namespaces",
        timeout=300,
    )
    start, end = output.find("{"), output.rfind("}")
    if code != 0 or start < 0:
        return {"error": output[:300]}
    try:
        report = json.loads(output[start : end + 1])
    except json.JSONDecodeError:
        return {"error": output[:300]}
    return {
        "long_term_memories": report.get("long_term_memories"),
        "namespace_count": report.get("namespace_count"),
    }


def build(arguments: argparse.Namespace) -> dict[str, object]:
    command = (
        f"cd {DEFAULT_REMOTE_DIR} && "
        f"nice -n {arguments.nice} env CARGO_HOME=/home/example/.cargo "
        f"DEB_BUILD_OPTIONS=nocheck dpkg-buildpackage -us -uc -b > {BUILD_LOG} 2>&1; "
        f"RC=$?; echo BUILD_RC=$RC; "
        f"echo COMPILING=$(grep -cE 'Compiling' {BUILD_LOG} || true); "
        f"echo METADATA=$(grep -c 'package metadata is valid' {BUILD_LOG} || true); "
        f"tail -5 {BUILD_LOG}"
    )
    started = time.time()
    code, output = ssh_sudo(arguments, command, timeout=3600)
    elapsed = round(time.time() - started, 1)

    def marker(name: str) -> int | None:
        match = re.search(rf"^{name}=(\d+)$", output, re.M)
        return int(match.group(1)) if match else None

    return {
        "exit_code": marker("BUILD_RC"),
        "elapsed_s": elapsed,
        "compile_actions": marker("COMPILING"),
        "metadata_assertions": marker("METADATA"),
        "tail": output[-1200:],
    }


def install(arguments: argparse.Namespace, before: dict[str, object]) -> dict[str, object]:
    remote = f"cd {DEFAULT_REMOTE_DIR}/.. && "
    command = (
        remote
        + f"dpkg -i $(ls -1t {DAEMON_DEB_GLOB} | head -1) $(ls -1t {SDK_DEB_GLOB} | head -1) > /tmp/k3-local-install.log 2>&1; "
        "echo INSTALL_RC=$?; tail -6 /tmp/k3-local-install.log; "
        "sleep 3; systemctl is-active bianbu-agent-memoryd; "
        "dpkg-query -W -f='${Package} ${Version}\\n' bianbu-agent-memory bianbu-agent-memoryd"
    )
    code, output = ssh_sudo(arguments, command, timeout=600)
    after_namespaces = namespace_count(arguments)
    acceptance_code, acceptance = ssh(
        arguments,
        f"cd {DEFAULT_REMOTE_DIR} && python3 scripts/k3_dbus_acceptance.py",
        timeout=300,
    )
    return {
        "exit_code": code,
        "output": output[-1500:],
        "service_active": "active" in output,
        "namespaces_before": before,
        "namespaces_after": after_namespaces,
        "row_counts_kept": (
            isinstance(before, dict)
            and isinstance(after_namespaces, dict)
            and before.get("long_term_memories") == after_namespaces.get("long_term_memories")
            and before.get("namespace_count") == after_namespaces.get("namespace_count")
        ),
        "acceptance_exit_code": acceptance_code,
        "acceptance_tail": acceptance[-600:],
        "acceptance_pass": "PASS" in acceptance,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="K3 本地开发包：构建 / 安装 / 校验")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--password", default=os.environ.get("K3_PASSWORD", ""))
    parser.add_argument("--nice", type=int, default=19, help="构建优先级（默认最低，避免扰动设备）")
    parser.add_argument("--build", action="store_true", help="执行 dpkg-buildpackage")
    parser.add_argument("--install", action="store_true", help="安装最新 debs 并复核")
    parser.add_argument("--json", help="证据 JSON 输出路径")
    arguments = parser.parse_args()
    if not arguments.build and not arguments.install:
        parser.error("至少指定 --build 或 --install")

    evidence: dict[str, object] = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": arguments.host,
        "remote_dir": DEFAULT_REMOTE_DIR,
    }
    if arguments.install:
        evidence["namespaces_before_install"] = namespace_count(arguments)
    if arguments.build:
        print("[build] dpkg-buildpackage -us -uc -b（不带 -nc）")
        evidence["build"] = build(arguments)
        print(json.dumps(evidence["build"], ensure_ascii=False, indent=2)[:1500])
    hashes = parse_deb_hashes(remote_hashes(arguments)["raw"])
    evidence["hashes"] = hashes
    print("[hashes]", json.dumps(hashes, ensure_ascii=False))
    if hashes.get("embedded_binary_sha256") == hashes.get("previous_package_binary_sha256"):
        evidence["binary_unchanged_since_previous_package"] = True
    if arguments.install:
        print("[install] dpkg -i 最新 debs")
        evidence["install"] = install(arguments, evidence.get("namespaces_before_install", {}))
        print(json.dumps({key: evidence["install"][key] for key in
                          ("exit_code", "service_active", "row_counts_kept",
                           "acceptance_pass")}, ensure_ascii=False, indent=2))
    if arguments.json:
        output = Path(arguments.json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"证据已写入 {output}")
    failed = []
    if arguments.build and evidence["build"].get("exit_code") != 0:
        failed.append("build")
    if arguments.install:
        install_evidence = evidence["install"]
        if install_evidence.get("exit_code") != 0 or not install_evidence.get("service_active"):
            failed.append("install")
        if not install_evidence.get("acceptance_pass"):
            failed.append("acceptance")
    print("VERDICT:", "PASS" if not failed else f"FAIL ({', '.join(failed)})")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
