#!/usr/bin/env bash
set -euo pipefail

state_dir="${BIANBU_MEMORY_MANUAL_STATE:-/home/example/.local/state/bianbu-agent-memory-manual}"

stop_pid_file() {
  local name="$1"
  local file="$2"
  [[ -s "$file" ]] || return 0
  local pid
  pid="$(cat "$file")"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid"
    for _ in $(seq 1 100); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.05
    done
  fi
  printf '%s stopped (pid=%s)\n' "$name" "$pid"
}

stop_pid_file Runtime "$state_dir/runtime.pid"
stop_pid_file D+ "$state_dir/dplus.pid"
stop_pid_file memoryd "$state_dir/memoryd.pid"
stop_pid_file Embedding "$state_dir/embedding.pid"

echo "Database retained: $state_dir/memory.db"
