"""Pure, network-free helpers for the wire-sweep vendor scanner.

Kept separate from ``sweep.py`` (the live driver) so the recursive-scan logic
has a fast, mock-free pytest counterpart (see ``test_vendor_scanner.py``).

The vendor literal is used here ONLY as a scan pattern, per the house rule
that the vendor name must never appear in model-visible or user-visible
output. Every hit this module returns has the matched substring masked
before it leaves the function -- callers should never need to touch the raw
pattern themselves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List

# Scan pattern only. Never reproduced unmasked in return values, logs, or
# any file this harness writes to disk.
_VENDOR_PATTERN = re.compile("composio", re.IGNORECASE)
VENDOR_MASK = "[VENDOR]"

# A connect_url host that legitimately names the vendor's OAuth domain is an
# accepted boundary, not a leak -- callers point a real browser at it, and
# hiding the vendor there would break the redirect. Recognize the shape so
# classification can special-case it.
_ACCEPTED_CONNECT_URL_HOST = re.compile(
    r"^https://connect\.composio\.dev/", re.IGNORECASE
)


def redact(text: str) -> str:
    """Mask every vendor-name occurrence in ``text``. Safe to print/persist."""
    return _VENDOR_PATTERN.sub(VENDOR_MASK, text)


@dataclass(frozen=True)
class VendorHit:
    json_path: str
    redacted_context: str
    classification: str  # "accepted_boundary" | "model_visible_defect"


def classify(json_path: str, raw_text: str) -> str:
    if "connect_url" in json_path and _ACCEPTED_CONNECT_URL_HOST.match(raw_text.strip()):
        return "accepted_boundary"
    return "model_visible_defect"


def find_vendor_hits(obj: Any, path: str = "$") -> List[VendorHit]:
    """Recursively walk a JSON-decoded object (dict/list/str/num/bool/None)
    and return one VendorHit per string (dict key OR value) that contains
    the vendor name, anywhere in the tree -- this is what catches the
    easy-to-miss nested spots (plan/pitfalls entries, guidance lines,
    per-tool error strings, connections entries, schema descriptions and
    property names) without needing a hardcoded field list.
    """
    hits: List[VendorHit] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            child_path = f"{path}.{k}"
            if isinstance(k, str) and _VENDOR_PATTERN.search(k):
                hits.append(
                    VendorHit(
                        json_path=f"{path}.<key>",
                        redacted_context=redact(k),
                        classification=classify(f"{path}.<key>", k),
                    )
                )
            hits.extend(find_vendor_hits(v, child_path))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits.extend(find_vendor_hits(v, f"{path}[{i}]"))
    elif isinstance(obj, str):
        if _VENDOR_PATTERN.search(obj):
            hits.append(
                VendorHit(
                    json_path=path,
                    redacted_context=redact(obj),
                    classification=classify(path, obj),
                )
            )
    # numbers/bools/None: nothing to scan
    return hits


def scan_raw_text(text: str, path: str = "$.<non-json-body>") -> List[VendorHit]:
    """Fallback scanner for a response body that didn't parse as JSON
    (e.g. a plaintext 5xx from a proxy in front of the gateway)."""
    if _VENDOR_PATTERN.search(text):
        return [
            VendorHit(
                json_path=path,
                redacted_context=redact(text),
                classification=classify(path, text),
            )
        ]
    return []


def redact_json_value(obj: Any) -> Any:
    """Deep-copy ``obj`` with every vendor-name occurrence masked, so the
    value is safe to persist as a committed artifact regardless of what the
    live sweep found."""
    if isinstance(obj, dict):
        return {
            (redact(k) if isinstance(k, str) else k): redact_json_value(v)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact_json_value(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj
