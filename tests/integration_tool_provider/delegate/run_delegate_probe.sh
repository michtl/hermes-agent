#!/usr/bin/env bash
# Rerunnable delegate->gateway probe.
#
# Proves (or disproves) that a delegate_task CHILD agent calls gateway tools
# through the bridge (tool_search / tool_call) rather than falling back to
# terminal+curl, and correlates the child's calls against the gateway dev log.
#
# ONE command:
#   bash tests/integration_tool_provider/delegate/run_delegate_probe.sh
#
# Env knobs:
#   PROFILE   profile name            (default: deleg2)
#   HOMES     homes-root              (default: /tmp/hh-deleg)
#   SCENARIO  scenario file basename  (default: delegate_bridge_child_slug.txt)
#   GWLOG     gateway dev log path    (default: auto-resolved from the :3009 pid)
#
# The homes-root deliberately lives OUTSIDE the repo: the worktree directory
# name contains the upstream vendor name and the agent's CWD is model-visible.

set -uo pipefail

REPO=/home/daimon/github/hermes-agent/.worktrees/composio-bridge
PY="$REPO/.venv/bin/python"
HARNESS="$REPO/tests/integration_tool_provider/harness/hermes_harness.py"
HERE="$REPO/tests/integration_tool_provider/delegate"

PROFILE="${PROFILE:-deleg2}"
HOMES="${HOMES:-/tmp/hh-deleg}"
SCENARIO="${SCENARIO:-delegate_bridge_child_slug.txt}"

if [ -z "${GWLOG:-}" ]; then
  GWPID=$(ss -ltnp 2>/dev/null | grep 3009 | grep -oP 'pid=\K[0-9]+' | head -1)
  GWLOG=$(readlink -f "/proc/${GWPID}/fd/1" 2>/dev/null)
fi
echo "gateway log: ${GWLOG:-<unresolved>}"

# 1. isolated profile, fresh 900s token, and THE LEVER:
#    delegation.subagent_auto_approve=true. Default false makes a subagent's
#    dangerous-command approvals resolve as auto-DENY (config_defaults.py:1718-1726).
"$PY" "$HARNESS" up --target local --profile "$PROFILE" --homes-root "$HOMES" \
      --subagent-auto-approve || exit 1
"$PY" "$HARNESS" doctor --profile "$PROFILE" --homes-root "$HOMES" || exit 1

MARK=$(date -u +%FT%TZ)
echo "run mark: $MARK"

# 2. drive the parent; it must delegate, and the child must use the bridge.
"$PY" "$HARNESS" run --profile "$PROFILE" --homes-root "$HOMES" \
      --scenario "$SCENARIO" --label delegate_child --timeout 420 2>&1 | tail -40

# 3. server-side + session-store correlation: which SESSION called the bridge,
#    with which context_id, and did the gateway actually see it?
"$PY" "$HERE/correlate.py" \
      --home "$HOMES/$PROFILE/hermes-home" \
      --gwlog "$GWLOG" \
      --since "${MARK%Z}" | tail -80
