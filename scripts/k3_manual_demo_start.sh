#!/usr/bin/env bash
set -euo pipefail

# 本脚本是 D+ 引擎的手工演示栈：`BIANBU_RUNTIME_ROOT` 必须指向一份带 `.venv` 的
# Runtime 源码 checkout（默认路径只是当时联调机上的目录名）。只跑 VEC-09 语义检索
# 端到端请用 `scripts/k3_semantic_e2e_stack_start.sh` —— 0.1.120 起 Runtime 发行包
# 自带 bianbu.memory 插件，不需要源码 checkout。

memory_root="${BIANBU_MEMORY_ROOT:-/home/example/bianbu-agent-memory}"
runtime_root="${BIANBU_RUNTIME_ROOT:-/home/example/personal-agent-runtime-memory-fix}"
runtime_config="${BIANBU_RUNTIME_CONFIG:-}"
state_dir="${BIANBU_MEMORY_MANUAL_STATE:-/home/example/.local/state/bianbu-agent-memory-manual}"
socket_path="$state_dir/agent-memoryd.sock"
database_path="$state_dir/memory.db"
memory_log="$state_dir/memoryd.log"
runtime_log="$state_dir/runtime.log"
dplus_log="$state_dir/dplus.log"
dplus_pid_file="$state_dir/dplus.pid"
embedding_log="$state_dir/embedding.log"
embedding_pid_file="$state_dir/embedding.pid"
memory_pid_file="$state_dir/memoryd.pid"
runtime_pid_file="$state_dir/runtime.pid"
confirm_token_file="$state_dir/confirm-token"
account_secret_file="$state_dir/account-secret"
key_package_path="$state_dir/memory.keypkg"
capsule_key_file="$state_dir/capsule-key"
backup_directory="$state_dir/capsules"

if [[ -z "$runtime_config" ]]; then
  if [[ -f "$runtime_root/k3-memory-runtime.yaml" ]]; then
    runtime_config="$runtime_root/k3-memory-runtime.yaml"
  else
    runtime_config="$runtime_root/.personal-agent/config.yaml"
  fi
fi

mkdir -p "$state_dir"
chmod 700 "$state_dir"

if [[ ! -x "$memory_root/target/release/bianbu-agent-memoryd" ]]; then
  (cd "$memory_root" && cargo build --workspace --release)
fi

embedding_model="${BIANBU_MEMORY_EMBEDDING_MODEL:-/home/example/.cache/models/embedding/Qwen3-Embedding-0.6B-Q8_0.gguf}"
# Outside the spacemit-ai-gateway Embed pool (18800-18900): this dev stack
# starts its own llama-server, and a port inside that range can be handed to a
# Gateway-spawned model at any time.
embedding_port="${BIANBU_MEMORY_EMBEDDING_PORT:-18901}"
dplus_port="${BIANBU_MEMORY_DPLUS_PORT:-18920}"
dplus_python="${BIANBU_MEMORY_DPLUS_PYTHON:-/home/example/mem0-venv/bin/python}"
# dplus by default; set BIANBU_MEMORY_ENGINE=native (or any non-dplus value)
# to exercise memoryd's native search_headers path — the semantic-retrieval
# e2e (k3_semantic_e2e_runtime.py) requires the native engine and no D+.
memory_engine="${BIANBU_MEMORY_ENGINE:-dplus}"
embedding_owner="external"

if ! curl -fsS --max-time 2 "http://127.0.0.1:$embedding_port/health" >/dev/null 2>&1; then
  [[ -f "$embedding_model" ]] || { echo "Embedding model not found: $embedding_model"; exit 1; }
  nohup llama-server -m "$embedding_model" \
    --port "$embedding_port" --host 127.0.0.1 --embedding --pooling last \
    --ctx-size 8192 --threads 8 --no-webui \
    >>"$embedding_log" 2>&1 </dev/null &
  printf '%s' "$!" >"$embedding_pid_file"
  embedding_owner="managed pid=$!"
elif [[ -s "$embedding_pid_file" ]] && kill -0 "$(cat "$embedding_pid_file")" 2>/dev/null; then
  embedding_owner="managed pid=$(cat "$embedding_pid_file")"
fi

for _ in $(seq 1 300); do
  curl -fsS --max-time 2 "http://127.0.0.1:$embedding_port/health" >/dev/null 2>&1 && break
  sleep 0.1
done
curl -fsS --max-time 5 "http://127.0.0.1:$embedding_port/health" >/dev/null

if [[ ! -s "$confirm_token_file" ]]; then
  python3.13 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$confirm_token_file"
  chmod 600 "$confirm_token_file"
fi

if [[ ! -s "$account_secret_file" ]]; then
  python3.13 -c 'import secrets; print(secrets.token_urlsafe(48))' >"$account_secret_file"
  chmod 600 "$account_secret_file"
fi

if [[ ! -s "$capsule_key_file" ]]; then
  python3.13 -c 'import secrets; print(secrets.token_hex(32))' >"$capsule_key_file"
  chmod 600 "$capsule_key_file"
fi
mkdir -p "$backup_directory"
chmod 700 "$backup_directory"

if [[ -s "$memory_pid_file" ]]; then
  old_memory_pid="$(cat "$memory_pid_file")"
  if kill -0 "$old_memory_pid" 2>/dev/null; then
    kill -TERM "$old_memory_pid"
    for _ in $(seq 1 100); do
      kill -0 "$old_memory_pid" 2>/dev/null || break
      sleep 0.05
    done
  fi
fi

confirm_token="$(cat "$confirm_token_file")"
migration_arguments=()
if [[ -f "$database_path" && ! -f "$key_package_path" ]]; then
  migration_arguments+=(--migrate-plaintext)
fi
if [[ ! -f "$key_package_path" ]]; then
  migration_arguments+=(--initialize-key-package)
fi
nohup env BIANBU_MEMORY_DEV_CONFIRM_TOKEN="$confirm_token" \
  "$memory_root/target/release/bianbu-agent-memoryd" \
  --development-identity \
  --socket "$socket_path" \
  --database "$database_path" \
  --key-package "$key_package_path" \
  --account-secret-file "$account_secret_file" \
  --backup-directory "$backup_directory" \
  --capsule-key-file "$capsule_key_file" \
  --background-device-id "k3-manual" \
  "${migration_arguments[@]}" \
  >>"$memory_log" 2>&1 </dev/null &
memory_pid=$!
printf '%s' "$memory_pid" >"$memory_pid_file"

python3.13 - "$socket_path" <<'PY'
import socket
import sys
import time

path = sys.argv[1]
for _ in range(200):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(path)
        client.close()
        break
    except OSError:
        client.close()
        time.sleep(0.05)
else:
    raise SystemExit("memoryd did not become ready")
PY

"$runtime_root/.venv/bin/python" -m pip install -q -e "$memory_root/sdk/python"

# The Runtime materializes bundled plugins into its data directory. Refresh the
# deployed memory plugin so source fixes are exercised by the running service.
if [[ -d "$runtime_root/.personal-agent/plugins/bianbu.memory" ]]; then
  mkdir -p "$runtime_root/.k3-memory-runtime/plugins/bianbu.memory"
  cp -a "$runtime_root/.personal-agent/plugins/bianbu.memory/." \
    "$runtime_root/.k3-memory-runtime/plugins/bianbu.memory/"
  find "$runtime_root/.k3-memory-runtime/plugins/bianbu.memory" \
    -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
  find "$runtime_root/.k3-memory-runtime/plugins/bianbu.memory" \
    -depth -type d -name '__pycache__' -empty -delete
fi

if [[ "$memory_engine" = "dplus" ]]; then
  if [[ -s "$dplus_pid_file" ]]; then
    old_dplus_pid="$(cat "$dplus_pid_file")"
    if kill -0 "$old_dplus_pid" 2>/dev/null; then
      kill -TERM "$old_dplus_pid"
      for _ in $(seq 1 100); do
        kill -0 "$old_dplus_pid" 2>/dev/null || break
        sleep 0.05
      done
    fi
  fi

  nohup env \
    PYTHONPATH="$memory_root/sdk/python/src" \
    MEM0_TELEMETRY=false \
    BIANBU_AGENT_MEMORY_SOCKET="$socket_path" \
    BIANBU_MEMORY_DPLUS_STATE_DIR="$state_dir/dplus" \
    BIANBU_MEMORY_DPLUS_INFER_MODE="${BIANBU_MEMORY_DPLUS_INFER_MODE:-rules}" \
    BIANBU_MEMORY_DPLUS_LLM_BASE_URL="${BIANBU_MEMORY_DPLUS_LLM_BASE_URL:-http://127.0.0.1:18790/v1}" \
    BIANBU_MEMORY_DPLUS_LLM_MODEL="${BIANBU_MEMORY_DPLUS_LLM_MODEL:-qwen3-k3-local}" \
    BIANBU_MEMORY_DPLUS_EMBEDDER_BASE_URL="http://127.0.0.1:$embedding_port/v1" \
    "$dplus_python" -m bianbu_agent_sdk.memory.dplus_service \
      --socket "$socket_path" --port "$dplus_port" \
    >>"$dplus_log" 2>&1 </dev/null &
  dplus_pid=$!
  printf '%s' "$dplus_pid" >"$dplus_pid_file"

  for _ in $(seq 1 600); do
    if curl -fsS --max-time 2 "http://127.0.0.1:$dplus_port/health" >/dev/null 2>&1; then
      break
    fi
    if ! kill -0 "$dplus_pid" 2>/dev/null; then
      tail -n 100 "$dplus_log"
      exit 1
    fi
    sleep 0.1
  done
  curl -fsS --max-time 5 "http://127.0.0.1:$dplus_port/health" >/dev/null
  curl -fsS --max-time 120 -X POST "http://127.0.0.1:$dplus_port/v1/clear" \
    -H 'Content-Type: application/json' -d '{}' >/dev/null
  curl -fsS --max-time 120 -X POST "http://127.0.0.1:$dplus_port/v1/rebuild" \
    -H 'Content-Type: application/json' -d '{"limit":100}' >>"$dplus_log" 2>&1 || true
else
  # Native engine: memoryd handles retrieval itself; no D+ process needed.
  if [[ -s "$dplus_pid_file" ]]; then
    old_dplus_pid="$(cat "$dplus_pid_file")"
    if kill -0 "$old_dplus_pid" 2>/dev/null; then
      kill -TERM "$old_dplus_pid" || true
    fi
  fi
fi

if [[ -s "$runtime_pid_file" ]]; then
  old_runtime_pid="$(cat "$runtime_pid_file")"
  if kill -0 "$old_runtime_pid" 2>/dev/null; then
    kill -TERM "$old_runtime_pid"
    for _ in $(seq 1 200); do
      kill -0 "$old_runtime_pid" 2>/dev/null || break
      sleep 0.05
    done
    if kill -0 "$old_runtime_pid" 2>/dev/null; then
      echo "Runtime did not stop: pid=$old_runtime_pid" >&2
      exit 1
    fi
  fi
fi

"$runtime_root/.venv/bin/bianbu-agent" \
  --config "$runtime_config" \
  process stop --timeout 5 >/dev/null 2>&1 || true

nohup env BIANBU_AGENT_MEMORY_SOCKET="$socket_path" \
  BIANBU_MEMORY_ENGINE="$memory_engine" \
  BIANBU_MEMORY_DPLUS_URL="http://127.0.0.1:$dplus_port" \
  BIANBU_MEMORY_DPLUS_SEARCH_TIMEOUT=1.2 \
  "$runtime_root/.venv/bin/bianbu-agent" \
  --config "$runtime_config" serve \
  >>"$runtime_log" 2>&1 </dev/null &
runtime_pid=$!
printf '%s' "$runtime_pid" >"$runtime_pid_file"

for _ in $(seq 1 300); do
  if kill -0 "$runtime_pid" 2>/dev/null \
    && curl -fsS --max-time 2 http://127.0.0.1:9899/chat >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$runtime_pid" 2>/dev/null; then
    tail -n 100 "$runtime_log"
    exit 1
  fi
  sleep 0.1
done

if ! kill -0 "$runtime_pid" 2>/dev/null; then
  tail -n 100 "$runtime_log"
  echo "Runtime exited before readiness: pid=$runtime_pid" >&2
  exit 1
fi
curl -fsS --max-time 5 http://127.0.0.1:9899/chat >/dev/null

cat <<EOF
K3_MANUAL_MEMORY_READY
Web:      http://192.0.2.10:9899/chat
Socket:   $socket_path
Database: $database_path
Memoryd:  $memory_pid
D+:       ${dplus_pid:-none (engine=$memory_engine)}
Embedding: http://127.0.0.1:$embedding_port ($embedding_owner)
Runtime:  $runtime_pid
Config:   $runtime_config
State:    $state_dir
EOF
