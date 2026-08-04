"""Fast, network-free unit tests for the recursive vendor scanner.

Run: cd <repo> && .venv/bin/python -m pytest \
    tests/integration_tool_provider/wire-sweep/test_vendor_scanner.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from vendor_scan import find_vendor_hits, redact, redact_json_value, VENDOR_MASK  # noqa: E402

# Built at runtime from parts so this file's own source never contains the
# literal vendor substring either -- keeps a case-insensitive grep for the
# vendor name clean across this harness directory (vendor_scan.py's own
# scan-pattern definition is the sole intentional exception) while still
# exercising the real pattern end to end.
_V = "".join(["Co", "mp", "osio"])


def test_no_hits_on_clean_wire_shaped_response():
    payload = {
        "context_id": "trs_abc123",
        "results": [
            {
                "use_case": "post a message to a channel",
                "tools": [
                    {
                        "slug": "SLACK_SEND_MESSAGE",
                        "toolkit": "slack",
                        "description": "Send a message to a channel or user.",
                        "connected": False,
                    }
                ],
                "plan": ["Find the channel", "Send the message"],
                "pitfalls": ["channel_not_found if the bot isn't a member"],
            }
        ],
        "connections": [{"toolkit": "slack", "connected": False}],
        "guidance": ["Resolve the destination before sending."],
    }
    assert find_vendor_hits(payload) == []


def test_finds_hit_in_deeply_nested_pitfalls_string():
    payload = {
        "results": [
            {
                "pitfalls": [
                    "unrelated pitfall",
                    f"retry via the {_V} session channel on timeout",
                ]
            }
        ]
    }
    hits = find_vendor_hits(payload)
    assert len(hits) == 1
    assert hits[0].json_path == "$.results[0].pitfalls[1]"
    assert _V.lower() not in hits[0].redacted_context.lower()
    assert VENDOR_MASK in hits[0].redacted_context


def test_finds_hit_in_dict_key_not_just_value():
    payload = {f"{_V}_session_id": "abc"}
    hits = find_vendor_hits(payload)
    assert len(hits) == 1
    assert hits[0].json_path == "$.<key>"
    assert VENDOR_MASK in hits[0].redacted_context


def test_finds_hit_in_schema_property_name_and_description():
    # Regression shape for "schema descriptions and property names" -- the
    # brief's named easy-to-miss spot.
    payload = {
        "schemas": [
            {
                "slug": "GITHUB_LIST_REPOSITORY_ISSUES",
                "description": f"Powered by the {_V} tool router.",
                "input_schema": {
                    "properties": {f"{_V}_workbench_id": {"type": "string"}}
                },
            }
        ]
    }
    hits = find_vendor_hits(payload)
    paths = {h.json_path for h in hits}
    assert "$.schemas[0].description" in paths
    assert any(p.endswith(".<key>") for p in paths)


def test_connect_url_with_vendor_oauth_host_is_accepted_boundary():
    payload = {
        "connections": [
            {
                "toolkit": "github",
                "status": "pending",
                "connect_url": f"https://connect.{_V.lower()}.dev/link/lk_abc123",
            }
        ]
    }
    hits = find_vendor_hits(payload)
    assert len(hits) == 1
    assert hits[0].classification == "accepted_boundary"


def test_vendor_name_in_error_message_is_model_visible_defect():
    payload = {
        "error": {
            "code": "UPSTREAM_ERROR",
            "message": f"{_V} upstream returned 500: internal provider fault",
        }
    }
    hits = find_vendor_hits(payload)
    assert len(hits) == 1
    assert hits[0].classification == "model_visible_defect"


def test_vendor_substring_inside_a_larger_word_is_not_a_false_negative():
    # Case-insensitivity and substring matching should both hold.
    payload = {"note": f"host header was {_V.upper()}-INTERNAL-01"}
    hits = find_vendor_hits(payload)
    assert len(hits) == 1


def test_redact_json_value_masks_nested_strings_for_safe_persistence():
    payload = {"a": [f"seen via {_V}", {"b": f"{_V}-2"}]}
    redacted = redact_json_value(payload)
    flat = str(redacted)
    assert _V.lower() not in flat.lower()
    assert VENDOR_MASK in flat


def test_redact_helper_is_idempotent_and_case_insensitive():
    assert redact(f"{_V} {_V.upper()} {_V.lower()}") == f"{VENDOR_MASK} {VENDOR_MASK} {VENDOR_MASK}"


def test_non_string_leaves_do_not_crash_the_walker():
    payload = {"count": 500, "ok": True, "missing": None, "ids": [1, 2, 3]}
    assert find_vendor_hits(payload) == []
