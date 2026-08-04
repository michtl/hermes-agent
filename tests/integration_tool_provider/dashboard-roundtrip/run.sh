#!/usr/bin/env bash
# Dashboard toggle round-trip — ONE command for the whole area.
#
#   ./run.sh
#
# Mutates the org's OrgToolkitPolicy (disables + re-enables `notion` via the
# dashboard UI) and ALWAYS restores the exact baseline it captured, including
# array ORDER and toolOverrides, even if the probe fails or is interrupted.
#
# Prereqs (see README.md): NAS on :3111, gateway on :3009, the e2e postgres
# container, a built web dist, and a dashboard launched from an isolated
# HERMES_HOME on a free port.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
PY="$REPO/.venv/bin/python"
HARNESS="$REPO/tests/integration_tool_provider/harness/hermes_harness.py"

PROFILE="${PROFILE:-dashrt}"
PORT="${PORT:-8092}"
NAS_URL="${NAS_URL:-http://127.0.0.1:3111}"
GW_URL="${GW_URL:-http://tools-gateway.localhost:3009}"
ORG_ID="${ORG_ID:-nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19}"
SCRATCH="${SCRATCH:-/tmp/dashrt}"
export DB_QUERY_CMD="${DB_QUERY_CMD:-docker exec e2e-postgres-1 psql -U postgres -d nous-account-service -At -c}"

psql_c() { eval "$DB_QUERY_CMD" "$(printf '%q' "$1")"; }

mkdir -p "$SCRATCH" "$HERE/artifacts"

# ---------------------------------------------------------------- baseline --
BASELINE_SQL='select row_to_json(t) from (select "orgId","enabledToolkits","toolOverrides" from "OrgToolkitPolicy") t;'
BASELINE="$(psql_c "$BASELINE_SQL")"
echo "BASELINE OrgToolkitPolicy: $BASELINE"
printf '%s\n' "$BASELINE" > "$HERE/artifacts/policy_baseline.json"

# Restore is registered BEFORE anything mutates, so an interrupt still restores.
restore() {
  local rc=$?
  echo
  echo "== RESTORE =="
  "$PY" - "$HERE/artifacts/policy_baseline.json" <<'PYEOF' > "$SCRATCH/restore.sql"
import json, sys
base = json.load(open(sys.argv[1]))
toolkits = base["enabledToolkits"]
overrides = base["toolOverrides"]
arr = "ARRAY[" + ",".join("'" + t.replace("'", "''") + "'" for t in toolkits) + "]::text[]"
ov = "NULL" if overrides is None else "'" + json.dumps(overrides).replace("'", "''") + "'::jsonb"
print(
    f'''update "OrgToolkitPolicy" set "enabledToolkits" = {arr}, "toolOverrides" = {ov} '''
    f'''where "orgId" = '{base["orgId"]}';'''
)
PYEOF
  psql_c "$(cat "$SCRATCH/restore.sql")"
  local after
  after="$(psql_c "$BASELINE_SQL")"
  if [ "$after" = "$BASELINE" ]; then
    echo "RESTORE OK — OrgToolkitPolicy is byte-identical to the baseline:"
    echo "  $after"
  else
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    echo "!! RESTORE FAILED — the org policy is NOT back to its baseline. !!"
    echo "!!   CORRECT (baseline): $BASELINE"
    echo "!!   CURRENT:            $after"
    echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
    rc=3
  fi
  # throwaway cold principal
  if [ -f "$SCRATCH/cold_uid.txt" ]; then
    local cold; cold="$(cat "$SCRATCH/cold_uid.txt")"
    psql_c "delete from \"OrgMembership\" where \"userId\" = '$cold'; delete from \"User\" where id = '$cold';" >/dev/null
    echo "cleaned up throwaway cold principal $cold"
    rm -f "$SCRATCH/cold_uid.txt" "$SCRATCH/.cold_token"
  fi
  exit $rc
}
trap restore EXIT INT TERM

# ------------------------------------------------------- warm principal JWT --
"$PY" "$HARNESS" up --target local --profile "$PROFILE" >/dev/null
"$PY" - "$REPO/tests/integration_tool_provider/harness/.homes/$PROFILE/hermes-home/auth.json" \
  "$SCRATCH/.token" <<'PYEOF'
import json, os, sys
tok = json.load(open(sys.argv[1]))["providers"]["nous"]["access_token"]
open(sys.argv[2], "w").write(tok)
os.chmod(sys.argv[2], 0o600)
PYEOF

# --------------------------------------- throwaway COLD principal (org member) --
COLD_UID="nas_user:dashrt-cold-$(date +%s)"
echo "$COLD_UID" > "$SCRATCH/cold_uid.txt"
psql_c "insert into \"User\" (id, email, name, \"updatedAt\") values ('$COLD_UID', '${COLD_UID##*:}@example.invalid', 'dashrt cold probe', now());
insert into \"OrgMembership\" (id, \"userId\", \"orgId\", role) values ('mem-${COLD_UID##*:}', '$COLD_UID', '$ORG_ID', 'MEMBER');" >/dev/null
curl -sX POST "$NAS_URL/api/internal/dev-mint-oauth-token" \
  -H "Authorization: Bearer dummy-auth-secret" -H "Content-Type: application/json" \
  -d "{\"userId\":\"$COLD_UID\",\"orgId\":\"$ORG_ID\",\"clientId\":\"hermes-agent\"}" \
  | "$PY" -c "import json,sys,os; open('$SCRATCH/.cold_token','w').write(json.load(sys.stdin)['accessToken']); os.chmod('$SCRATCH/.cold_token',0o600)"
# Validate the cold principal's policy visibility against NAS DIRECTLY — this
# must NOT touch the gateway, or it warms the very cache we are about to measure.
echo -n "cold principal NAS visibility: "
curl -s "$NAS_URL/api/portal/tools/toolkits" -H "Authorization: Bearer $(cat "$SCRATCH/.cold_token")" \
  | "$PY" -c "import json,sys; d=json.load(sys.stdin); print(sorted(t['slug'] for t in d['toolkits'] if t.get('enabled')))"

# ------------------------------------------------------------------- probe --
cd "$HERE"
BASE_URL="http://127.0.0.1:$PORT" \
DASH_SESSION_TOKEN="${DASH_SESSION_TOKEN:-dashrt-local-probe-session}" \
NAS_URL="$NAS_URL" GW_URL="$GW_URL" \
WARM_TOKEN_FILE="$SCRATCH/.token" COLD_TOKEN_FILE="$SCRATCH/.cold_token" \
TOOLKIT_SLUG=notion TOOLKIT_NAME=Notion \
  node roundtrip.mjs 2>&1 | tee "$HERE/artifacts/roundtrip_run_output.log"

echo
echo "===== probe 2: does a toggle round-trip preserve the rest of the policy? ====="
BASE_URL="http://127.0.0.1:$PORT" \
DASH_SESSION_TOKEN="${DASH_SESSION_TOKEN:-dashrt-local-probe-session}" \
NAS_URL="$NAS_URL" GW_URL="$GW_URL" \
WARM_TOKEN_FILE="$SCRATCH/.token" COLD_TOKEN_FILE="$SCRATCH/.cold_token" \
TOOLKIT_SLUG=notion TOOLKIT_NAME=Notion \
  node scope_destruction.mjs 2>&1 | tee "$HERE/artifacts/scope_run_output.log"
