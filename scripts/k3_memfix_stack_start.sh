#!/usr/bin/env bash
# Isolated three-session e2e stack starter (K3, 2026-09-11).
# Starts: refreshed bianbu.memory plugin + deb-venv runtime on :9898
# (e2e-runtime config) + isolated memoryd (this tree's release build).
# Production services (:9899, /run/bianbu/agent-memoryd.sock) untouched.
set -u

STATE=/home/example/e2e-memfix
PY=/home/example/.venv/bin/python3

# 1. clean state
rm -rf "$STATE"
mkdir -p "$STATE"
chmod 700 "$STATE"
"$PY" -c 'import secrets; print(secrets.token_urlsafe(48))' > "$STATE/account-secret"
chmod 600 "$STATE/account-secret"
printf 'tok-%s' "$(date +%s%N)" > "$STATE/confirm-token"

# 2. refresh the deployed memory plugin with the redesigned source
rm -rf /home/example/e2e-runtime/data/plugins/bianbu.memory
cp -a /home/example/runtime-test/.bianbu-agent/plugins/bianbu.memory \
      /home/example/e2e-runtime/data/plugins/bianbu.memory
find /home/example/e2e-runtime/data/plugins/bianbu.memory -name '*.py[co]' -delete

# 3. start isolated runtime (control spawns the deb-venv worker; PYTHONPATH
#    points both at this tree's new SDK so record_run_started exists)
cd /home/example/e2e-runtime
nohup env \
  BIANBU_RUNTIME_ALLOW_UNMANAGED_WORKER=1 \
  BIANBU_RUNTIME_SECURITY=optional \
  BIANBU_RUNTIME_MODE=development \
  BIANBU_SECURITY=0 \
  BIANBU_AGENT_MEMORY_SOCKET="$STATE/memoryd.sock" \
  BIANBU_MEMORY_ENGINE=native \
  PYTHONPATH=/home/example/agent-memory-current/sdk/python/src \
  /usr/lib/bianbu-agent-runtime/venv/bin/bianbu-agent \
    --config /home/example/e2e-runtime/config.yaml serve \
  > "$STATE/runtime.log" 2>&1 &
echo $! > "$STATE/runtime.pid"

for _ in $(seq 1 240); do
  curl -fsS --max-time 2 http://127.0.0.1:9898/chat >/dev/null 2>&1 && break
  sleep 0.5
done
if curl -fsS --max-time 5 http://127.0.0.1:9898/chat >/dev/null 2>&1; then
  echo RUNTIME_READY
else
  echo RUNTIME_FAILED
  tail -60 "$STATE/runtime.log"
  exit 1
fi

# 4. discover the agent the stack serves
curl -fsS --max-time 10 http://127.0.0.1:9898/api/v1/agents > "$STATE/agents.json"
head -c 1500 "$STATE/agents.json"; echo
AGENT=$("$PY" - <<'PYEOF'
import json, re
data = json.load(open("/home/example/e2e-memfix/agents.json"))
entries = data.get("data") or []
if isinstance(entries, dict):
    entries = entries.get("agents") or []
pick = ""
for entry in entries:
    aid = str(entry.get("id") or entry.get("agent_id") or "")
    name = str(entry.get("name") or entry.get("zh_name") or "")
    if aid and re.search(r"test|测试", f"{aid} {name}", re.I):
        pick = aid
        break
if not pick:
    for entry in entries:
        aid = str(entry.get("id") or entry.get("agent_id") or "")
        if aid:
            pick = aid
            break
print(pick)
PYEOF
)
echo "AGENT=$AGENT"
if [ -z "$AGENT" ]; then
  echo NO_AGENT
  exit 1
fi
printf '%s' "$AGENT" > "$STATE/agent-id"

# 5. start the isolated memoryd (new release binary, dev identity bound to
#    the discovered agent so the worker bridge shares the namespace)
nohup env BIANBU_MEMORY_DEV_CONFIRM_TOKEN="$(cat "$STATE/confirm-token")" \
  /home/example/agent-memory-current/target/release/bianbu-agent-memoryd \
  --development-identity --development-agent-name "$AGENT" \
  --socket "$STATE/memoryd.sock" \
  --database "$STATE/memory.db" \
  --key-package "$STATE/memory.keypkg" \
  --account-secret-file "$STATE/account-secret" \
  --initialize-key-package \
  > "$STATE/memoryd.log" 2>&1 &
echo $! > "$STATE/memoryd.pid"

"$PY" - <<'PYEOF'
import socket, time
path = "/home/example/e2e-memfix/memoryd.sock"
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
print("MEMORYD_READY")
PYEOF
echo "STACK_UP agent=$AGENT socket=$STATE/memoryd.sock"
