#!/usr/bin/env python3
"""Correlate a delegate run: which SESSION (parent vs child) made which bridge
call, and did the child REUSE the parent's tool-provider context_id?

Two independent sources are joined:

  1. hermes side  -- <HERMES_HOME>/state.db, tables `sessions` + `messages`.
     Every tool_search / tool_call result the bridge returns is a JSON string
     carrying a top-level "context_id" (see tools/tool_search.py
     dispatch_tool_search / dispatch_provider_tool_call). Grouping those by
     session_id tells us whether the child minted a new trs_... or inherited.

  2. gateway side -- the dev log of the :3009 listener. Proves the request
     actually reached the provider (server-side truth, not transcript prose).
     NOTE: the gateway does NOT log context_id, so source 1 is the only
     ground truth for inheritance; the gateway log supplies arrival + latency.

Usage:
    python correlate.py --home /tmp/hh-deleg/deleg/hermes-home \
                        --gwlog /path/to/tool-gateway-dev.log \
                        --since 2026-08-04T19:40:00
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import OrderedDict

CTX_RE = re.compile(r"trs_[A-Za-z0-9_-]+")


def hermes_side(home: str) -> dict:
    con = sqlite3.connect(f"file:{home}/state.db?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sess_cols = {r[1] for r in con.execute("PRAGMA table_info(sessions)")}
    msg_cols = {r[1] for r in con.execute("PRAGMA table_info(messages)")}
    parent_col = "parent_session_id" if "parent_session_id" in sess_cols else None

    sessions = OrderedDict()
    q = "SELECT * FROM sessions ORDER BY rowid"
    for row in con.execute(q):
        d = dict(row)
        sessions[d["id"] if "id" in d else d.get("session_id")] = d

    # every message body that mentions a context id or a bridge tool name
    tool_col = "content" if "content" in msg_cols else None
    out = []
    for row in con.execute("SELECT * FROM messages ORDER BY rowid"):
        d = dict(row)
        blob = json.dumps(d, default=str)
        if "tool_search" not in blob and "tool_call" not in blob and "trs_" not in blob:
            continue
        out.append(
            {
                "session_id": d.get("session_id"),
                "role": d.get("role"),
                "ctx_ids": sorted(set(CTX_RE.findall(blob))),
                "mentions_tool_search": "tool_search" in blob,
                "mentions_tool_call": "tool_call" in blob,
                "excerpt": blob[:400],
            }
        )
    con.close()
    return {"sessions": sessions, "parent_col": parent_col, "rows": out}


def gateway_side(gwlog: str, since: str) -> list:
    hits = []
    with open(gwlog, "r", errors="replace") as fh:
        for line in fh:
            if since and line[:19] < since:
                continue
            if "/v1/" not in line:
                continue
            if "Composio" not in line and " POST /v1/" not in line:
                continue
            hits.append(line.rstrip()[:400])
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--gwlog", required=True)
    ap.add_argument("--since", default="")
    args = ap.parse_args()

    h = hermes_side(args.home)
    print(f"== sessions in {args.home}/state.db ==")
    for sid, row in h["sessions"].items():
        parent = row.get(h["parent_col"]) if h["parent_col"] else None
        print(f"  {sid}  parent={parent}  title={str(row.get('title'))[:60]!r}")

    print("\n== bridge-bearing messages, by session ==")
    per_sess: dict = {}
    for r in h["rows"]:
        per_sess.setdefault(r["session_id"], set()).update(r["ctx_ids"])
        flags = []
        if r["mentions_tool_search"]:
            flags.append("tool_search")
        if r["mentions_tool_call"]:
            flags.append("tool_call")
        print(f"  sess={r['session_id']} role={r['role']} ctx={r['ctx_ids']} {flags}")

    print("\n== context_id per session ==")
    for sid, ctxs in per_sess.items():
        print(f"  {sid} -> {sorted(ctxs)}")
    all_ctx = set().union(*per_sess.values()) if per_sess else set()
    print(f"\nVERDICT: distinct context_ids across all sessions = {sorted(all_ctx)}")
    if len(per_sess) > 1 and len(all_ctx) == 1:
        print("  -> child REUSED the parent's context_id (inheritance held)")
    elif len(all_ctx) > 1:
        print("  -> more than one context_id observed; inspect per-session mapping above")

    print("\n== gateway-side arrivals ==")
    for line in gateway_side(args.gwlog, args.since):
        print("  " + line)


if __name__ == "__main__":
    main()
