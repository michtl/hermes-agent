"""Regression: invalid persisted image parts must never poison later gateway turns."""

from __future__ import annotations

import base64

from gateway.run import _build_gateway_agent_history, _select_cached_agent_history


def _data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def test_replay_drops_persisted_html_mislabeled_as_jpeg_but_keeps_text():
    bad = _data_url("image/jpeg", b"<!doctype html><title>Expired relay media</title>")
    history = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "original caption"},
                {"type": "image_url", "image_url": {"url": bad}},
            ],
        },
        {"role": "assistant", "content": "I could not inspect it."},
        {"role": "user", "content": "new text-only turn"},
    ]

    replay, observed = _build_gateway_agent_history(history)

    assert observed is None
    assert replay[0]["content"] == [{"type": "text", "text": "original caption"}]
    assert all("data:image" not in str(message) for message in replay)
    assert replay[-1]["content"] == "new text-only turn"


def test_replay_repairs_declared_mime_and_drops_expired_relay_url():
    png = b"\x89PNG\r\n\x1a\n" + b"payload"
    mislabeled = _data_url("image/jpeg", png)
    history = [{
        "role": "user",
        "content": [
            {"type": "input_text", "text": "two old references"},
            {"type": "input_image", "image_url": mislabeled},
            {"type": "image_url", "image_url": {"url": "https://connector.invalid/relay/media/deadbeef"}},
        ],
    }]

    replay, _ = _build_gateway_agent_history(history)
    content = replay[0]["content"]

    assert content[1]["image_url"].startswith("data:image/png;base64,")
    assert all("/relay/media/" not in str(part) for part in content)


def test_longer_live_history_cannot_reintroduce_a_bad_persisted_image_part():
    bad = _data_url("image/jpeg", b"<html>expired</html>")
    persisted = [{"role": "user", "content": "durable row", "_db_persisted": True}]
    live = [
        persisted[0],
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "cached caption"},
                {"type": "image_url", "image_url": {"url": bad}},
            ],
        },
    ]

    selected = _select_cached_agent_history(persisted, live)

    assert selected[1]["content"] == [{"type": "text", "text": "cached caption"}]
    assert "data:image" not in str(selected)
