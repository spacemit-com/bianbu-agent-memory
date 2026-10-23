#!/usr/bin/env bash
# K3 隔离栈启动器：给 scripts/k3_semantic_e2e_runtime.py（VEC-09）准备一台可被脚本
# 直连的 memoryd 和一台装着 bianbu.memory 插件的 Runtime。
#
# 为什么不用现网那一对（9899 + /run/bianbu/agent-memoryd.sock）：现网 memoryd 按
# Runtime 注册表判权，裸 SDK 客户端会被拒（实测 `Unauthenticated: runtime
# registration was not found`），而 VEC-09 的 A/B 断言要的正是这条直连通道。
#
# 0.1.120 起 Runtime 包自带 bianbu.memory 插件（装在
# /usr/share/bianbu-agent-runtime/plugins/），因此这里不需要 Runtime 源码 checkout：
# 装好的 venv + 一份开发配置就够。守护进程用 --development-identity 启动，按
# SO_PEERCRED 信任同 uid 的客户端，不需要注册表参与判权。
#
# 用法：
#   bash scripts/k3_semantic_e2e_stack_start.sh
#   BIANBU_AGENT_MEMORY_AGENT_NAME=<agent-id> bash scripts/k3_semantic_e2e_stack_start.sh
#
# 起栈后按脚本末尾打印的那条命令跑 VEC-09；开发栈与现网服务互不影响
# （端口 9898 对 9899，独立 socket / 库 / key package）。
set -euo pipefail

state_dir="${BIANBU_VEC09_STATE:-/home/example/e2e-vec09}"
runtime_bin="${BIANBU_RUNTIME_BIN:-/usr/lib/bianbu-agent-runtime/venv/bin/bianbu-agent}"
memoryd_bin="${BIANBU_MEMORYD_BIN:-/home/example/mem-gateway-build/target/release/bianbu-agent-memoryd}"
runtime_port="${BIANBU_VEC09_PORT:-9898}"
account_id="${BIANBU_AGENT_MEMORY_ACCOUNT_ID:-default}"
profile_dir="${BIANBU_AGENT_PROFILE_DIR:-/home/example/.bianbu-agent}"

[[ -x "$runtime_bin" ]] || { echo "Runtime 未安装：$runtime_bin" >&2; exit 1; }
[[ -x "$memoryd_bin" ]] || { echo "memoryd 未编译：$memoryd_bin" >&2; exit 1; }

# 开发栈冒充哪个专家：VEC-09 通过 Runtime 与测试专家（测试/test）对话，两侧必须一致。
agent_id="${BIANBU_AGENT_MEMORY_AGENT_NAME:-}"
if [[ -z "$agent_id" ]]; then
  agent_id="$(python3 - <<'PY'
import json, re, urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:9899/api/v1/agents", timeout=10) as r:
        data = json.load(r)
except Exception:
    data = {}
entries = data.get("data") or []
for entry in entries:
    aid = str(entry.get("id") or entry.get("agent_id") or "")
    name = str(entry.get("name") or "") + str(entry.get("zh_name") or "")
    if aid and re.search(r"test|测试", f"{aid} {name}", re.I):
        print(aid)
        break
PY
)"
fi
[[ -n "$agent_id" ]] || {
  echo "无法确定 Runtime 专家 id（可用 BIANBU_AGENT_MEMORY_AGENT_NAME=<id> 指定）" >&2
  exit 1
}
[[ -f "$profile_dir/agents/$agent_id/agent.yaml" ]] || {
  echo "找不到专家定义 $profile_dir/agents/$agent_id/agent.yaml" >&2
  exit 1
}

mkdir -p "$state_dir/data/agents/$agent_id" "$state_dir/memory-registry"
chmod 700 "$state_dir"
for secret in account-secret confirm-token; do
  [[ -s "$state_dir/$secret" ]] || python3 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$state_dir/$secret"
  chmod 600 "$state_dir/$secret"
done

# 开发 data 目录：只复制 VEC-09 用得到的专家定义与模型 provider（都是本机既有的
# 开发资料），不复制会话历史与 workspace。
cp -a "$profile_dir/agents/$agent_id/agent.yaml" "$state_dir/data/agents/$agent_id/agent.yaml"
[[ -d "$profile_dir/models" ]] && cp -a "$profile_dir/models" "$state_dir/data/"

cat >"$state_dir/config.yaml" <<EOF
data_dir: $state_dir/data
service:
  title: Bianbu Agent (VEC-09 dev)
  host: 127.0.0.1
  port: $runtime_port
language: zh
plugin_stop_timeout_seconds: 5.0
allow_unmanaged_capabilities: true
security:
  required: false
  securityd_uds: /run/bianbu/securityd.sock
runtime:
  control:
    execution:
      requested_mode: host
    startup_timeout_seconds: 10
    handshake_timeout_seconds: 10
    heartbeat_interval_seconds: 5
    heartbeat_timeout_seconds: 15
    restart_policy:
      enabled: true
      max_restarts: 5
      backoff_seconds: [1, 2, 5, 10, 30]
  worker:
    execution:
      requested_mode: host
    entrypoint:
    - $runtime_bin
    - --role
    - agent-worker
    - --config
    - $state_dir/config.yaml
    - worker
    startup_timeout_seconds: 15
    max_concurrent_runs: 4
EOF

for pid_file in "$state_dir/runtime.pid" "$state_dir/memoryd.pid"; do
  [[ -s "$pid_file" ]] || continue
  pid="$(cat "$pid_file")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" || true
    for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.05; done
  fi
  rm -f "$pid_file"
done
rm -f "$state_dir/memoryd.sock"

migration_arguments=()
[[ -f "$state_dir/memory.db" && ! -f "$state_dir/memory.keypkg" ]] && migration_arguments+=(--migrate-plaintext)
[[ ! -f "$state_dir/memory.keypkg" ]] && migration_arguments+=(--initialize-key-package)
nohup env BIANBU_MEMORY_DEV_CONFIRM_TOKEN="$(cat "$state_dir/confirm-token")" \
  "$memoryd_bin" \
    --development-identity \
    --development-account-id "$account_id" \
    --development-agent-name "$agent_id" \
    --socket "$state_dir/memoryd.sock" \
    --database "$state_dir/memory.db" \
    --key-package "$state_dir/memory.keypkg" \
    --account-secret-file "$state_dir/account-secret" \
    "${migration_arguments[@]}" \
  >"$state_dir/memoryd.log" 2>&1 </dev/null &
echo $! >"$state_dir/memoryd.pid"

python3 - "$state_dir/memoryd.sock" "$(cat "$state_dir/memoryd.pid")" <<'PY'
import os, socket, sys, time
path, pid = sys.argv[1], int(sys.argv[2])
for _ in range(400):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise SystemExit("memoryd 在 socket 就绪前退出")
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
    except OSError:
        time.sleep(0.05)
    else:
        break
    finally:
        client.close()
else:
    raise SystemExit("memoryd socket 未就绪")
PY

nohup env \
  BIANBU_RUNTIME_MODE=development \
  BIANBU_RUNTIME_SECURITY=optional \
  BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER=1 \
  BIANBU_SECURITY=0 \
  BIANBU_SANDBOX=off \
  BIANBU_WORKER_MEMORY_MAX=off \
  BIANBU_WORKER_CPU_QUOTA=off \
  BIANBU_AGENT_MEMORY_ENABLED=true \
  BIANBU_AGENT_MEMORY_ACCOUNT_ID="$account_id" \
  BIANBU_AGENT_MEMORY_AGENT_NAME="$agent_id" \
  BIANBU_AGENT_MEMORY_SOCKET="$state_dir/memoryd.sock" \
  BIANBU_AGENT_MEMORY_REGISTRY_DIR="$state_dir/memory-registry" \
  BIANBU_MEMORY_CONFIRM_TOKEN_FILE="$state_dir/confirm-token" \
  BIANBU_MEMORY_ENGINE=native \
  "$runtime_bin" --role control --config "$state_dir/config.yaml" serve \
  >"$state_dir/runtime.log" 2>&1 </dev/null &
echo $! >"$state_dir/runtime.pid"

for _ in $(seq 1 400); do
  if curl -fsS --max-time 2 "http://127.0.0.1:$runtime_port/chat" >/dev/null 2>&1; then
    cat <<EOF
K3_VEC09_STACK_READY
Runtime:   http://127.0.0.1:$runtime_port (pid $(cat "$state_dir/runtime.pid"))
Memoryd:   $state_dir/memoryd.sock (pid $(cat "$state_dir/memoryd.pid"), native 引擎)
Agent:     $agent_id
State:     $state_dir
下一步：
  python3 scripts/k3_semantic_e2e_runtime.py \\
    --base-url http://127.0.0.1:$runtime_port \\
    --socket $state_dir/memoryd.sock \\
    --registry $state_dir/memory-registry \\
    --agent-id $agent_id
EOF
    exit 0
  fi
  kill -0 "$(cat "$state_dir/runtime.pid")" 2>/dev/null || { echo "Runtime 启动即退出：" >&2; tail -40 "$state_dir/runtime.log" >&2; exit 1; }
  sleep 0.2
done
echo "Runtime 未就绪：" >&2
tail -40 "$state_dir/runtime.log" >&2
exit 1