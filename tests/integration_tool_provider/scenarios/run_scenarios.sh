#!/usr/bin/env bash
# Rerunnable live-CLI scenario pass for the tool-provider bridge.
#
# ONE command:
#   bash tests/integration_tool_provider/scenarios/run_scenarios.sh
#
# What it does, in order:
#   0. harness `up` + `doctor` against the local stack (isolated HERMES_HOME under /tmp/hh-scen)
#   1. noauth_loop.txt        — pure tool_search discovery for HackerNews  (expect: 0 results)
#   2. noauth_execute.txt     — tool_call HACKERNEWS_GET_LATEST_POSTS      (expect: 5 real posts)
#   3. unconnected_github.txt — auth-required, unconnected toolkit         (expect: blocked + connect URL)
# Each run's gateway-log window is sliced into artifacts/ by line offset.
#
# It never touches the real ~/.hermes, never restarts the gateway or NAS,
# and never mutates org toolkit policy.
set -uo pipefail

REPO=/home/daimon/github/hermes-agent/.worktrees/composio-bridge
PY="$REPO/.venv/bin/python"
H="$REPO/tests/integration_tool_provider/harness/hermes_harness.py"
S="$REPO/tests/integration_tool_provider/scenarios"
HOMES=/tmp/hh-scen
PROFILE=scen
OUT="$S/artifacts"
# The gateway dev log is owned by whoever launched the stack; override if it moved.
GWLOG="${GWLOG:-/tmp/claude-1001/-home-daimon-github/56ee7cf8-bdd9-4b4d-9beb-e32fad2f3d9d/scratchpad/logs/tool-gateway-dev.log}"

mkdir -p "$OUT"

echo "== up + doctor =="
"$PY" "$H" up     --target local --profile "$PROFILE" --homes-root "$HOMES" || exit 1
"$PY" "$H" doctor --profile "$PROFILE" --homes-root "$HOMES" || exit 1

run_one() {
  local name="$1" file="$2"
  local mark=0
  [ -f "$GWLOG" ] && mark=$(wc -l < "$GWLOG")
  echo
  echo "== scenario: $name =="
  date -u +%FT%T.%3NZ
  "$PY" "$H" run --profile "$PROFILE" --homes-root "$HOMES" --scenario "$S/$file"
  date -u +%FT%T.%3NZ
  if [ -f "$GWLOG" ]; then
    tail -n +$((mark + 1)) "$GWLOG" | sed -E 's/\x1b\[[0-9;]*m//g' > "$OUT/gateway-$name.log"
    echo "gateway slice -> $OUT/gateway-$name.log"
    grep -cE 'POST /v1/search'  "$OUT/gateway-$name.log" | sed 's/^/  v1\/search  calls: /'
    grep -cE 'POST /v1/execute' "$OUT/gateway-$name.log" | sed 's/^/  v1\/execute calls: /'
  fi
}

run_one 01-search-only         noauth_loop.txt
run_one 02-execute             noauth_execute.txt
run_one 03-unconnected-github  unconnected_github.txt

echo
echo "== data-truth check (independent of the bridge) =="
echo "Take an objectID out of transcript-02 and confirm it against the public HN API yourself:"
echo "  curl -s https://hn.algolia.com/api/v1/items/<objectID> | jq '{id,type,story_id,story_title}'"
echo
echo "== vendor-name scan over what a USER sees =="
grep -rniE 'composio' "$REPO/tests/integration_tool_provider/harness/transcripts/" || echo "  none in transcripts"
echo
echo "== secret scan over deliverables (must print CLEAN) =="
grep -rlE 'eyJ[A-Za-z0-9_-]{10,}\.|sk-or-v1-|sk-ant-|\b(oak|ak)_[A-Za-z0-9]' "$S" || echo "CLEAN"
