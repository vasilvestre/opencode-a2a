#!/usr/bin/env bash
# Probe the v2-shared fork against the OpenCode installed on this machine.
# Requires: an OpenCode 2.x server on OPENCODE_BASE_URL whose log names its
# password, curl, python3, and this repository's uv environment.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
D="${PROBE_DIR:-/tmp/opencode/probe}"
LOG="$D/gateway.log"
REPORT="$D/report.txt"
PORT="${A2A_PORT:-8199}"
UPSTREAM="${OPENCODE_BASE_URL:-http://127.0.0.1:4097}"
DB="${A2A_PROBE_DB:-$D/a2a-probe.db}"

PASS=$(grep -oP 'server password \K\S+' "$D/serve.log" 2>/dev/null | head -1)
[ -n "$PASS" ] || { echo "no upstream password in $D/serve.log" >&2; exit 1; }
TOKEN=$(openssl rand -hex 16)
mkdir -p "$D"
: > "$REPORT"

say() { echo "$*" | tee -a "$REPORT"; }

start_gateway() {
  for pid in $(ss -ltnp 2>/dev/null | grep -oP ":$PORT .*pid=\K[0-9]+"); do kill "$pid"; done
  sleep 1
  ( cd "$REPO" && \
    A2A_STATIC_AUTH_CREDENTIALS="[{\"scheme\":\"bearer\",\"token\":\"$TOKEN\",\"principal\":\"probe\"}]" \
    OPENCODE_BASE_URL="$UPSTREAM" \
    OPENCODE_API_VERSION=v2 \
    OPENCODE_V2_EXECUTION=exclusive \
    OPENCODE_AUTH_USERNAME=opencode \
    OPENCODE_AUTH_PASSWORD="$PASS" \
    OPENCODE_WORKSPACE_ROOT="$D" \
    A2A_SHARED_WORKSPACE=1 \
    A2A_TOOL_POLICY=full \
    A2A_TASK_STORE_DATABASE_URL="sqlite+aiosqlite:///$DB" \
    A2A_HOST=127.0.0.1 A2A_PORT="$PORT" A2A_PUBLIC_URL="http://127.0.0.1:$PORT" \
    uv run opencode-a2a serve >>"$LOG" 2>&1 & )
  for _ in $(seq 1 30); do
    ss -ltn 2>/dev/null | grep -q ":$PORT" && return 0
    sleep 1
  done
  say "gateway did not bind $PORT"; tail -20 "$LOG" | tee -a "$REPORT"; return 1
}

send() { # send <context-id> <text>
  local body
  body=$(python3 - "$1" "$2" <<'PY'
import json, sys, uuid
ctx, text = sys.argv[1], sys.argv[2]
print(json.dumps({"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "SendMessage",
                  "params": {"message": {"messageId": "msg-" + uuid.uuid4().hex, "contextId": ctx,
                                         "role": "ROLE_USER", "parts": [{"text": text}]}}}))
PY
)
  curl -s -m 300 -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
    -X POST "http://127.0.0.1:$PORT/" -d "$body"
}

state() {
  python3 -c '
import json, sys
d = json.load(sys.stdin)
task = d.get("result", d).get("task", {})
print(task.get("status", {}).get("state", "NO_TASK"))'
}

alltext() {
  python3 -c '
import json, sys
out = []
def walk(x):
    if isinstance(x, dict):
        for k, v in x.items():
            if k == "text" and isinstance(v, str):
                out.append(v)
            else:
                walk(v)
    elif isinstance(x, list):
        for i in x:
            walk(i)
walk(json.load(sys.stdin))
print("\n".join(out))'
}

sessions() {
  curl -s -u "opencode:$PASS" \
    "$UPSTREAM/api/session?location%5Bdirectory%5D=$D" |
    python3 -c 'import json,sys; print("\n".join(sorted(s["id"] for s in json.load(sys.stdin).get("data", []))))'
}

say "=== upstream: $UPSTREAM ==="
say "=== host: $(hostname) ==="
start_gateway || exit 1
say "gateway up; card: $(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/.well-known/agent-card.json")"

# Case 1: plain text task.
say ""
say "=== 1. plain text task ==="
R1=$(send "probe-1" "Reply with exactly: PROBE-OK-1234")
say "$R1"
say "state: $(printf '%s' "$R1" | state)"

# Case 2: the agent must run real commands, including one it cannot guess.
say ""
say "=== 2. shell tool: run hostname and read a nonce file ==="
NONCE="nonce-$(openssl rand -hex 8)"
echo "$NONCE" > "$D/nonce.txt"
R2=$(send "probe-2" "Run the shell command \`hostname\` then \`cat $D/nonce.txt\`, and reply with both outputs.")
say "$R2"
say "state: $(printf '%s' "$R2" | state)"

# Case 3: two sequential contexts must not share a reply.
say ""
say "=== 3. two sequential contexts ==="
R3A=$(send "probe-3a" "Reply with exactly: ALPHA-777")
say "context probe-3a: $R3A"
R3B=$(send "probe-3b" "Reply with exactly: BETA-888")
say "context probe-3b: $R3B"

# Case 4: a restarted gateway serves new tasks and never adopts old sessions.
say ""
say "=== 4. gateway restart ==="
say "upstream sessions before restart:"
sessions | tee -a "$REPORT"
start_gateway || exit 1
R4A=$(send "probe-4a" "Reply with exactly: GAMMA-999")
say "new context after restart: $R4A"
say "state: $(printf '%s' "$R4A" | state)"
R4B=$(send "probe-3a" "Reply with exactly: DELTA-555")
say "old context probe-3a after restart: $R4B"
say "state: $(printf '%s' "$R4B" | state)"
say "upstream sessions after restart:"
sessions | tee -a "$REPORT"

for pid in $(ss -ltnp 2>/dev/null | grep -oP ":$PORT .*pid=\K[0-9]+"); do kill "$pid"; done

FAIL=0
check() { # check <label> <condition-exit-code>
  if [ "$2" -eq 0 ]; then say "PASS $1"; else say "FAIL $1"; FAIL=1; fi
}
printf '%s' "$R1" | alltext | grep -q PROBE-OK-1234; check "1 completed with PROBE-OK-1234" $?
printf '%s' "$R1" | state | grep -q TASK_STATE_COMPLETED; check "1 state completed" $?
T2=$(printf '%s' "$R2" | alltext)
printf '%s' "$T2" | grep -q "$(hostname)"; check "2 hostname in reply" $?
printf '%s' "$T2" | grep -q "$NONCE"; check "2 nonce in reply" $?
T3A=$(printf '%s' "$R3A" | alltext); T3B=$(printf '%s' "$R3B" | alltext)
printf '%s' "$T3A" | grep -q ALPHA-777; check "3 context a answered" $?
printf '%s' "$T3A" | grep -q BETA-888 && check "3 context a leaked b" 1 || check "3 context a leaked b" 0
printf '%s' "$T3B" | grep -q BETA-888; check "3 context b answered" $?
printf '%s' "$T3B" | grep -q ALPHA-777 && check "3 context b leaked a" 1 || check "3 context b leaked a" 0
printf '%s' "$R4A" | alltext | grep -q GAMMA-999; check "4 new task after restart" $?
printf '%s' "$R4B" | state | grep -q TASK_STATE_FAILED; check "4 old context not adopted" $?
say ""
[ "$FAIL" -eq 0 ] && say "=== PROBE PASSED ===" || say "=== PROBE FAILED ==="
say "report: $REPORT"
exit "$FAIL"

