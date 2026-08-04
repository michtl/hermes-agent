#!/usr/bin/env bash
# DELEG-4 wire probe. Prints no token material.
set -u
GW=http://tools-gateway.localhost:3009
T=$(curl -sX POST http://127.0.0.1:3111/api/internal/dev-mint-oauth-token \
  -H "Authorization: Bearer dummy-auth-secret" -H "Content-Type: application/json" \
  -d '{"userId":"nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26","orgId":"nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19","clientId":"hermes-agent"}' | jq -r .accessToken)
ex() { curl -s -X POST "$GW/v1/execute" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d "$1" | jq -c '.results // .error'; }
echo "schemas(unknown): $(curl -s -X POST "$GW/v1/schemas" -H "Authorization: Bearer $T" -H 'Content-Type: application/json' -d '{"tool_slugs":["HACKERNEWS_GET_FRONTPAGE"]}' | jq -c '.error.code')"
echo "execute(unknown):    $(ex '{"tools":[{"slug":"HACKERNEWS_GET_FRONTPAGE","arguments":{}}]}')"
echo "execute(ok):         $(ex '{"tools":[{"slug":"HACKERNEWS_GET_LATEST_POSTS","arguments":{}}]}' | cut -c1-120)"
echo "execute(missingarg): $(ex '{"tools":[{"slug":"HACKERNEWS_SEARCH_POSTS","arguments":{}}]}' | cut -c1-200)"
echo "execute(badvalue):   $(ex '{"tools":[{"slug":"HACKERNEWS_GET_USER","arguments":{"username":"zzz_no_such_user_zzz_9931"}}]}' | cut -c1-200)"
echo "execute(offpolicy):  $(ex '{"tools":[{"slug":"STRIPE_MAKE_ME_RICH","arguments":{}}]}')"
