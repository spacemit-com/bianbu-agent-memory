#!/usr/bin/env bash
set -euo pipefail

memory_root="${BIANBU_MEMORY_ROOT:-/home/example/bianbu-agent-memory}"
state_dir="${BIANBU_MEMORY_MANUAL_STATE:-/home/example/.local/state/bianbu-agent-memory-manual}"
socket_path="$state_dir/agent-memoryd.sock"
dplus_port="${BIANBU_MEMORY_DPLUS_PORT:-18920}"
# Kept in step with k3_manual_demo_start.sh: outside the spacemit-ai-gateway
# Embed pool (18800-18900).
embedding_port="${BIANBU_MEMORY_EMBEDDING_PORT:-18901}"

echo "Runtime health:"
curl -fsS --max-time 5 -o /dev/null http://127.0.0.1:9899/chat
echo "OK http://127.0.0.1:9899/chat"
echo

echo "Processes:"
for file in "$state_dir/memoryd.pid" "$state_dir/dplus.pid" "$state_dir/embedding.pid" "$state_dir/runtime.pid"; do
  [[ -s "$file" ]] || continue
  pid="$(cat "$file")"
  ps -o pid,rss,pcpu,etime,args -p "$pid" || true
done

if [[ ! -s "$state_dir/embedding.pid" ]]; then
  echo "Embedding is reachable but is not managed by this demo state directory:"
  ss -ltnp 2>/dev/null | grep -E "127\\.0\\.0\\.1:$embedding_port\\b" || true
fi

echo "D+ health:"
curl -fsS --max-time 5 "http://127.0.0.1:$dplus_port/health"
echo

echo "Memory health/stats:"
PYTHONPATH="$memory_root/sdk/python/src" python3.13 - "$socket_path" <<'PY'
import json
import sys

from bianbu_agent_sdk.memory import MemoryClient

with MemoryClient.connect(sys.argv[1], timeout=5.0) as client:
    print(json.dumps({"health": client.health(), "stats": client.stats()}, ensure_ascii=False, indent=2))
PY
