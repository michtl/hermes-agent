#!/usr/bin/env python3
"""Live wire-sweep driver: vendor-name scan + latency envelope across the
four tool-provider-gateway /v1 routes (search, schemas, execute, connections).

Pure HTTP -- no CLI, no browser, no hermes-agent process. Mints its own
short-lived JWT at start (900s TTL) so a single run never needs a
hand-copied token.

Rerun with (ONE command):

    cd tests/integration_tool_provider/wire-sweep
    ../../../.venv/bin/python sweep.py

Writes:
    artifacts/responses/*.json   -- one file per request, vendor-redacted
    artifacts/findings.json       -- classified vendor hits + known-edge status
    artifacts/latency.json        -- raw per-route timing samples
    LATENCY.md                    -- committed p50/max table vs. observed envelope
    (stdout)                      -- human-readable summary, exit code signals
                                      whether any NEW model-visible vendor hit
                                      was found (nonzero = new hit)

Environment overrides (all optional, defaults match the live dev stack):
    NAS_BASE, GATEWAY_BASE
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from vendor_scan import (  # noqa: E402
    VendorHit,
    find_vendor_hits,
    redact,
    redact_json_value,
    scan_raw_text,
)

NAS_BASE = os.environ.get("NAS_BASE", "http://127.0.0.1:3111")
GATEWAY_BASE = os.environ.get("GATEWAY_BASE", "http://tools-gateway.localhost:3009")
USER_ID = "nas_user:f7141b46-b044-41b0-aa13-a36a66f64f26"
ORG_ID = "nas_organisation:cfafba9e-77f3-4f72-97e3-dc491fc90c19"
CLIENT_ID = "hermes-agent"
DEV_MINT_SECRET = "dummy-auth-secret"

HERE = Path(__file__).resolve().parent
ARTIFACTS_DIR = HERE / "artifacts"
RESPONSES_DIR = ARTIFACTS_DIR / "responses"

# Known edges from the task brief (#6): recorded, not re-reported as new
# findings. Each entry names the probe label used below that is designed to
# reproduce it, plus a human description (vendor-name-free).
KNOWN_EDGE_6 = {
    "upstream_502_envelope": "502 UPSTREAM_ERROR envelope names the vendor and echoes raw upstream prose",
    "plan_pitfalls_leak": "plan/pitfalls arrays leak vendor strings",
    "execute_context_id_echo": "/v1/execute echoes a caller-supplied context_id verbatim instead of the real session id",
    "nas_tool_overrides": "NAS toolOverrides are neither persisted by NAS nor enforced by the gateway",
}


def mint_token(client: httpx.Client) -> str:
    resp = client.post(
        f"{NAS_BASE}/api/internal/dev-mint-oauth-token",
        headers={
            "Authorization": f"Bearer {DEV_MINT_SECRET}",
            "Content-Type": "application/json",
        },
        json={"userId": USER_ID, "orgId": ORG_ID, "clientId": CLIENT_ID},
        timeout=15,
    )
    resp.raise_for_status()
    token = resp.json()["accessToken"]
    assert isinstance(token, str) and len(token) > 20
    return token


class Call:
    """One recorded /v1/* HTTP call: request label, wire timing, parsed body,
    vendor hits found in it."""

    def __init__(
        self,
        route: str,
        label: str,
        status: int,
        elapsed_s: float,
        body: Any,
        is_json: bool,
        hits: List[VendorHit],
    ):
        self.route = route
        self.label = label
        self.status = status
        self.elapsed_s = elapsed_s
        self.body = body
        self.is_json = is_json
        self.hits = hits

    def artifact_name(self) -> str:
        return f"{self.route}__{self.label}.json"


def do_call(
    client: httpx.Client,
    token: str,
    route: str,
    label: str,
    body: Dict[str, Any],
    *,
    override_auth: Optional[str] = None,
) -> Call:
    headers = {
        "Authorization": f"Bearer {override_auth if override_auth is not None else token}",
        "Content-Type": "application/json",
    }
    t0 = time.perf_counter()
    resp = client.post(f"{GATEWAY_BASE}/v1/{route}", headers=headers, json=body, timeout=30)
    elapsed = time.perf_counter() - t0
    try:
        parsed = resp.json()
        is_json = True
        hits = find_vendor_hits(parsed)
    except ValueError:
        parsed = resp.text
        is_json = False
        hits = scan_raw_text(parsed)
    return Call(route, label, resp.status_code, elapsed, parsed, is_json, hits)


def persist(call: Call) -> None:
    RESPONSES_DIR.mkdir(parents=True, exist_ok=True)
    safe_body = redact_json_value(call.body) if call.is_json else redact(call.body)
    record = {
        "route": call.route,
        "label": call.label,
        "status": call.status,
        "elapsed_s": round(call.elapsed_s, 4),
        "body": safe_body,
        "vendor_hit_count": len(call.hits),
    }
    (RESPONSES_DIR / call.artifact_name()).write_text(json.dumps(record, indent=2))


# ---------------------------------------------------------------------------
# Sample plan: several toolkits, broad + narrow queries, success + error paths
# ---------------------------------------------------------------------------

# Real HackerNews tool slugs + required args, hand-discovered via /v1/schemas
# probing (HackerNews needs no auth so these are safe to execute for real).
HN_SLUGS = [
    ("HACKERNEWS_GET_TOP_STORIES", {}),
    ("HACKERNEWS_GET_BEST_STORIES", {}),
    ("HACKERNEWS_GET_NEW_STORIES", {}),
    ("HACKERNEWS_GET_JOB_STORIES", {}),
    ("HACKERNEWS_GET_ASK_STORIES", {}),
    ("HACKERNEWS_GET_SHOW_STORIES", {}),
    ("HACKERNEWS_GET_MAX_ITEM_ID", {}),
    ("HACKERNEWS_GET_ITEM", {"id": 8863}),
    ("HACKERNEWS_GET_ITEM", {"id": 1}),
    ("HACKERNEWS_GET_USER", {"username": "pg"}),
]

SEARCH_SAMPLES = [
    ("broad-hn", ["get top stories from hacker news", "get a hacker news item by id"]),
    ("broad-slack", ["send a slack message to a channel"]),
    ("broad-github", ["list my github repository issues"]),
    ("broad-googlecalendar", ["create a google calendar event tomorrow at 3pm"]),
    ("broad-gmail", ["search my gmail inbox for unread messages"]),
    ("broad-notion", ["create a new page in notion"]),
    ("narrow-slug-shaped", ["HACKERNEWS_GET_TOP_STORIES"]),
    ("error-empty-queries", []),
    ("error-too-many-queries", ["a", "b", "c", "d", "e", "f"]),
]

SCHEMAS_SAMPLES = [
    ("valid-hn-batch", [s for s, _ in HN_SLUGS]),
    ("error-toolkit-not-enabled", ["ASANA_GET_WORKSPACES"]),
    ("error-tool-not-found", ["HACKERNEWS_TOTALLY_MADE_UP_TOOL"]),
]

EXECUTE_SAMPLES = [
    ("single-hn", [{"slug": HN_SLUGS[0][0], "arguments": HN_SLUGS[0][1]}], None),
    (
        "batch-10-hn",
        [{"slug": s, "arguments": a} for s, a in HN_SLUGS],
        None,
    ),
    (
        "error-toolkit-not-enabled",
        [{"slug": "ASANA_GET_WORKSPACES", "arguments": {}}],
        None,
    ),
    (
        "error-tool-level-bad-arg",
        [{"slug": "HACKERNEWS_GET_ITEM", "arguments": {"id": "not-a-number-xyz"}}],
        None,
    ),
    (
        "context-id-echo-probe",
        [{"slug": HN_SLUGS[0][0], "arguments": HN_SLUGS[0][1]}],
        "bogus-ctx-injected-marker-12345",
    ),
]

CONNECTIONS_SAMPLES = [
    ("status-all-enabled", [], "status"),
    ("connect-github", ["github"], "connect"),
    ("connect-slack", ["slack"], "connect"),
    ("error-toolkit-not-enabled", ["asana"], "status"),
]


def run_vendor_sweep(client: httpx.Client, token: str) -> List[Call]:
    calls: List[Call] = []

    for label, queries in SEARCH_SAMPLES:
        calls.append(do_call(client, token, "search", label, {"queries": queries}))

    for label, slugs in SCHEMAS_SAMPLES:
        calls.append(do_call(client, token, "schemas", label, {"tool_slugs": slugs}))

    for label, tools, ctx in EXECUTE_SAMPLES:
        body: Dict[str, Any] = {"tools": tools}
        if ctx is not None:
            body["context_id"] = ctx
        calls.append(do_call(client, token, "execute", label, body))

    for label, toolkits, action in CONNECTIONS_SAMPLES:
        calls.append(
            do_call(client, token, "connections", label, {"toolkits": toolkits, "action": action})
        )

    # Auth-error path: deliberately bad bearer token.
    calls.append(
        do_call(
            client,
            token,
            "search",
            "error-bad-jwt",
            {"queries": ["x"]},
            override_auth="garbage.jwt.value",
        )
    )

    for c in calls:
        persist(c)
    return calls


# ---------------------------------------------------------------------------
# Latency envelope
# ---------------------------------------------------------------------------

N_SAMPLES = 5

# Observed baseline from the task brief, used only for the regression
# comparison printed in LATENCY.md.
OBSERVED_BASELINE_S = {
    "search": (2.5, 4.6),
    "execute": (1.0, 1.3),
}


def timed_series(client: httpx.Client, token: str, route: str, label: str, body: Dict[str, Any]) -> List[float]:
    samples = []
    for i in range(N_SAMPLES):
        call = do_call(client, token, route, f"latency-{label}-{i}", body)
        samples.append(call.elapsed_s)
    return samples


def run_latency_envelope(client: httpx.Client, token: str) -> Dict[str, List[float]]:
    results: Dict[str, List[float]] = {}
    results["search"] = timed_series(
        client, token, "search", "search", {"queries": ["send a slack message to a channel"]}
    )
    results["schemas"] = timed_series(
        client, token, "schemas", "schemas", {"tool_slugs": [s for s, _ in HN_SLUGS]}
    )
    results["execute_single"] = timed_series(
        client, token, "execute", "execsingle", {"tools": [{"slug": HN_SLUGS[0][0], "arguments": HN_SLUGS[0][1]}]}
    )
    results["execute_batch10"] = timed_series(
        client,
        token,
        "execute",
        "execbatch",
        {"tools": [{"slug": s, "arguments": a} for s, a in HN_SLUGS]},
    )
    results["connections_status"] = timed_series(
        client, token, "connections", "connstatus", {"toolkits": [], "action": "status"}
    )
    return results


def p50(values: List[float]) -> float:
    return statistics.median(values)


def write_latency_report(latency: Dict[str, List[float]]) -> str:
    lines = []
    lines.append("# Wire-sweep latency envelope\n")
    lines.append(
        f"Live samples against `{GATEWAY_BASE}`, {N_SAMPLES} requests per route, "
        "captured in one `sweep.py` run. Regenerate with the command in README.md.\n"
    )
    lines.append("| Route | p50 (s) | max (s) | samples (s) | vs. observed envelope |")
    lines.append("|---|---|---|---|---|")
    row_defs = [
        ("search", "search"),
        ("schemas", "schemas (9-slug batch)"),
        ("execute_single", "execute (single tool)"),
        ("execute_batch10", "execute (10-tool batch)"),
        ("connections_status", "connections (status)"),
    ]
    for key, display in row_defs:
        vals = latency[key]
        p = p50(vals)
        m = max(vals)
        samples_str = ", ".join(f"{v:.2f}" for v in vals)
        baseline_key = "search" if key == "search" else ("execute" if key.startswith("execute") else None)
        if baseline_key and baseline_key in OBSERVED_BASELINE_S:
            lo, hi = OBSERVED_BASELINE_S[baseline_key]
            if p > hi:
                verdict = f"REGRESSION: p50 {p:.2f}s > observed high {hi:.2f}s (delta +{p - hi:.2f}s)"
            elif m > hi * 1.5:
                verdict = f"FLAG: max {m:.2f}s far above observed high {hi:.2f}s (delta +{m - hi:.2f}s)"
            else:
                verdict = f"within observed {lo:.1f}-{hi:.1f}s"
        else:
            verdict = "no observed baseline given for this route"
        lines.append(f"| {display} | {p:.2f} | {m:.2f} | {samples_str} | {verdict} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Classification against known edge #6
# ---------------------------------------------------------------------------


def classify_known_edges(calls: List[Call]) -> Dict[str, Dict[str, Any]]:
    status: Dict[str, Dict[str, Any]] = {
        k: {"description": v, "reproduced": False, "evidence": None} for k, v in KNOWN_EDGE_6.items()
    }

    for c in calls:
        if c.route == "execute" and c.label == "context-id-echo-probe":
            returned_ctx = c.body.get("context_id") if isinstance(c.body, dict) else None
            sent_ctx = "bogus-ctx-injected-marker-12345"
            if returned_ctx == sent_ctx:
                status["execute_context_id_echo"]["reproduced"] = True
                status["execute_context_id_echo"]["evidence"] = (
                    f"sent context_id={sent_ctx!r}, echoed back unchanged in response.context_id "
                    "(session-resolved-by-userId semantics mean it should have been superseded)"
                )
        if c.route == "search" and c.hits:
            for h in c.hits:
                if ".plan" in h.json_path or ".pitfalls" in h.json_path:
                    status["plan_pitfalls_leak"]["reproduced"] = True
                    status["plan_pitfalls_leak"]["evidence"] = (
                        f"label={c.label} path={h.json_path} classification={h.classification}"
                    )
        if c.status == 502:
            status["upstream_502_envelope"]["reproduced"] = True
            status["upstream_502_envelope"]["evidence"] = f"label={c.label} status=502 body-hits={len(c.hits)}"

    # toolOverrides is a NAS-config-mutation test; explicitly out of scope
    # for this read-only wire sweep (HARD RULES: don't mutate org policy
    # unless the task says so -- it doesn't here).
    status["nas_tool_overrides"]["evidence"] = (
        "not probed: would require mutating NAS org toolkit policy, which this "
        "read-only wire-sweep lens is not authorized to do"
    )

    return status


def main() -> int:
    with httpx.Client() as client:
        token = mint_token(client)
        print(f"[sweep] minted token (len={len(token)}, redacted)")

        calls = run_vendor_sweep(client, token)
        latency = run_latency_envelope(client, token)

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)

    # --- vendor findings ---
    new_defect_hits = []
    accepted_hits = []
    for c in calls:
        for h in c.hits:
            entry = {
                "route": c.route,
                "label": c.label,
                "status": c.status,
                "json_path": h.json_path,
                "redacted_context": h.redacted_context,
                "classification": h.classification,
            }
            if h.classification == "accepted_boundary":
                accepted_hits.append(entry)
            else:
                new_defect_hits.append(entry)

    known_edge_status = classify_known_edges(calls)
    # Any model_visible_defect hit whose call is already accounted for by a
    # known-edge match (plan/pitfalls, 502 envelope) is NOT "new" -- it's the
    # acceptance-test signal for that known edge. Everything else genuinely
    # surfacing a vendor string in a 200-shaped response is new.
    known_edge_labels = {"error-toolkit-not-enabled", "context-id-echo-probe"}
    genuinely_new = [
        h
        for h in new_defect_hits
        if not (h["route"] == "search" and (".plan" in h["json_path"] or ".pitfalls" in h["json_path"]))
        and h["status"] != 502
    ]

    findings = {
        "total_requests": len(calls),
        "total_vendor_hits": len(new_defect_hits) + len(accepted_hits),
        "accepted_boundary_hits": accepted_hits,
        "model_visible_defect_hits": new_defect_hits,
        "genuinely_new_hits_not_covered_by_known_edge_6": genuinely_new,
        "known_edge_6_status": known_edge_status,
        "per_call_summary": [
            {"route": c.route, "label": c.label, "status": c.status, "vendor_hit_count": len(c.hits)}
            for c in calls
        ],
    }
    (ARTIFACTS_DIR / "findings.json").write_text(json.dumps(findings, indent=2))

    # --- latency ---
    (ARTIFACTS_DIR / "latency.json").write_text(json.dumps(latency, indent=2))
    report_md = write_latency_report(latency)
    (HERE / "LATENCY.md").write_text(report_md)

    # --- stdout summary ---
    print(f"[sweep] {len(calls)} vendor-sweep requests, {N_SAMPLES * 5} latency requests")
    print(f"[sweep] vendor hits: {len(new_defect_hits)} model-visible, {len(accepted_hits)} accepted-boundary")
    print(f"[sweep] genuinely new (not known-edge-6): {len(genuinely_new)}")
    for k, v in known_edge_status.items():
        print(f"[sweep] known-edge#6 {k}: reproduced={v['reproduced']}")
    print()
    print(report_md)

    return 1 if genuinely_new else 0


if __name__ == "__main__":
    raise SystemExit(main())
