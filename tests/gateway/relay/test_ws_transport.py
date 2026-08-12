"""WebSocketRelayTransport against a real in-process WebSocket server.

Exercises the production transport over an actual ``websockets`` server (no
mock socket): handshake (hello -> descriptor), inbound frame -> handler,
outbound request/response correlation, and follow_up routing. Proves the wire
framing (newline-delimited JSON) and the request/response future plumbing work
end to end on a live socket.

Skipped cleanly if the optional ``websockets`` dependency is absent.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import pytest_asyncio

from gateway.relay.descriptor import (
    CONTRACT_VERSION,
    OWNER_BOUND_INTERRUPT_ACK_CAPABILITY,
    OWNER_BOUND_TURN_COMPLETION_CAPABILITY,
    OWNER_BOUND_TURN_RECONCILIATION_CAPABILITY,
    CapabilityDescriptor,
)
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.relay.ws_transport import WebSocketRelayTransport, WEBSOCKETS_AVAILABLE

pytestmark = pytest.mark.skipif(not WEBSOCKETS_AVAILABLE, reason="websockets not installed")

if WEBSOCKETS_AVAILABLE:
    import websockets


DESCRIPTOR = {
    "contract_version": CONTRACT_VERSION,
    "platform": "discord",
    "label": "Discord",
    "max_message_length": 2000,
    "supports_draft_streaming": False,
    "supports_edit": True,
    "supports_threads": True,
    "markdown_dialect": "discord",
    "len_unit": "chars",
    "capabilities": [
        OWNER_BOUND_INTERRUPT_ACK_CAPABILITY,
        OWNER_BOUND_TURN_COMPLETION_CAPABILITY,
        OWNER_BOUND_TURN_RECONCILIATION_CAPABILITY,
    ],
}


class _StubConnectorServer:
    """Minimal connector: answers hello with a descriptor, echoes outbound."""

    def __init__(self):
        self.received: list[dict] = []
        self.descriptor = dict(DESCRIPTOR)
        self._server = None
        self.url = ""
        # Push channel: tests set this to a frame dict to deliver inbound.
        self._to_push: list[dict] = []

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        sock = next(iter(self._server.sockets))
        port = sock.getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                self.received.append(frame)
                await self._on_frame(ws, frame)

    async def _on_frame(self, ws, frame):
        ftype = frame.get("type")
        if ftype == "hello":
            await ws.send(
                json.dumps({"type": "descriptor", "descriptor": self.descriptor})
                + "\n"
            )
            # Deliver any queued inbound frames right after handshake.
            for f in self._to_push:
                await ws.send(json.dumps(f) + "\n")
        elif ftype == "outbound":
            action = frame.get("action", {})
            # Echo a successful result correlated by requestId.
            result = {"success": True, "message_id": f"srv-{action.get('op')}"}
            await ws.send(
                json.dumps({"type": "outbound_result", "requestId": frame["requestId"], "result": result})
                + "\n"
            )


@pytest_asyncio.fixture
async def server():
    srv = _StubConnectorServer()
    await srv.start()
    yield srv
    await srv.stop()


@pytest.mark.asyncio
async def test_handshake_negotiates_descriptor(server):
    t = WebSocketRelayTransport(server.url, "discord", "appShared")
    await t.connect()
    try:
        desc = await t.handshake()
        assert desc.platform == "discord"
        assert desc.max_message_length == 2000
        # The hello carried the platform + botId.
        hello = next(f for f in server.received if f["type"] == "hello")
        assert hello["platform"] == "discord"
        assert hello["botId"] == "appShared"
        assert isinstance(hello["runtime_epoch"], str)
        assert len(hello["runtime_epoch"]) == 32
        assert hello["runtime_epoch"] == t._runtime_epoch
        assert hello["capabilities"] == [
            OWNER_BOUND_INTERRUPT_ACK_CAPABILITY,
            OWNER_BOUND_TURN_COMPLETION_CAPABILITY,
            OWNER_BOUND_TURN_RECONCILIATION_CAPABILITY,
        ]
    finally:
        await t.disconnect()


@pytest.mark.asyncio
async def test_handshake_rejects_mixed_v3_without_turn_reconciliation(server):
    server.descriptor = {
        **DESCRIPTOR,
        "capabilities": [OWNER_BOUND_INTERRUPT_ACK_CAPABILITY],
    }
    t = WebSocketRelayTransport(server.url, "discord", "appShared")
    await t.connect()
    try:
        with pytest.raises(RuntimeError, match="contract v3 required"):
            await t.handshake()
    finally:
        await t.disconnect()


@pytest.mark.asyncio
async def test_runtime_epoch_is_unique_after_transport_restart(server):
    first = WebSocketRelayTransport(server.url, "discord", "appShared")
    replacement = WebSocketRelayTransport(server.url, "discord", "appShared")
    assert first._runtime_epoch != replacement._runtime_epoch


@pytest.mark.asyncio
async def test_turn_completed_frame_carries_exact_owner_session_chat_and_runtime_epoch(server):
    t = WebSocketRelayTransport(server.url, "discord", "appShared")
    await t.connect()
    try:
        await t.handshake()
        owner_id = "opaque-owner:00000001"
        assert await t.send_turn_completed(
            "agent:main:relay:dm:mission-control",
            "mission-control",
            owner_id,
            "completed",
        )
        assert not await t.send_turn_completed(
            "agent:main:relay:dm:mission-control",
            "mission-control",
            owner_id,
            "completed",
        )
        assert not await t.send_turn_completed(
            "agent:main:relay:dm:mission-control",
            "mission-control",
            "opaque-owner:00000002",
            "completed",
        )
        assert not await t.send_turn_completed(
            "agent:main:relay:dm:mission-control",
            "mission-control",
            "opaque-owner:00000002",
            "unknown",
        )
        await asyncio.sleep(0.05)
        assert [f for f in server.received if f.get("type") == "turn_completed"] == [
            {
                "type": "turn_completed",
                "session_key": "agent:main:relay:dm:mission-control",
                "chat_id": "mission-control",
                "owner_id": owner_id,
                "runtime_epoch": t._runtime_epoch,
                "outcome": "completed",
                "owner_state_seq": 1,
                "status": "idle",
                "next_owner_id": None,
                "next_delivery_id": None,
            }
        ]
    finally:
        await t.disconnect()


@pytest.mark.asyncio
async def test_completion_write_failure_is_retained_for_same_epoch_hello_reconciliation():
    t = WebSocketRelayTransport("ws://127.0.0.1:1", "discord", "appShared")
    t._descriptor = CapabilityDescriptor.from_json(json.dumps(DESCRIPTOR))

    async def disconnected_send(_frame):
        raise RuntimeError("socket closed exactly at completion")

    t._send = disconnected_send  # type: ignore[method-assign]
    owner_id = "opaque-owner:completion-loss"
    assert not await t.send_turn_completed(
        "agent:main:relay:dm:mission-control",
        "mission-control",
        owner_id,
        "completed",
    )
    assert t._turn_state_snapshots() == [
        {
            "session_key": "agent:main:relay:dm:mission-control",
            "chat_id": "mission-control",
            "owner_state_seq": 1,
            "status": "idle",
            "active_owner_id": None,
            "terminal_owner_id": owner_id,
            "terminal_outcome": "completed",
            "next_owner_id": None,
            "next_delivery_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_cancelled_terminal_send_propagates_after_child_send_task_is_reaped():
    t = WebSocketRelayTransport("ws://127.0.0.1:1", "relay", "")
    t._descriptor = CapabilityDescriptor.from_json(json.dumps(DESCRIPTOR))
    send_started = asyncio.Event()
    send_reaped = asyncio.Event()

    async def blocked_send(_frame):
        send_started.set()
        try:
            await asyncio.Future()
        finally:
            send_reaped.set()

    t._send = blocked_send  # type: ignore[method-assign]
    completion = asyncio.create_task(t.send_turn_completed(
        "agent:main:relay:dm:mission-control",
        "mission-control",
        "opaque-owner:cancelled-send",
        "completed",
    ))
    await send_started.wait()
    completion.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(completion, timeout=0.5)
    assert send_reaped.is_set()


def test_hello_turn_states_hide_foreign_session_and_chat_keys_behind_authenticated_scope():
    t = WebSocketRelayTransport(
        "ws://127.0.0.1:1", "relay", "",
        gateway_id="gw-scope", upgrade_secret="scope-secret",
    )
    t._state_for_scope("agent:main:relay:dm:room-a", "room-a")
    t._state_for_scope("agent:main:relay:dm:room-b", "room-b")

    snapshots = t._hello_turn_state_snapshots()

    assert len(snapshots) == 2
    assert all("session_key" not in item and "chat_id" not in item for item in snapshots)
    assert all(len(item["scope_fingerprint"]) == 64 for item in snapshots)
    assert snapshots[0]["scope_fingerprint"] != snapshots[1]["scope_fingerprint"]


@pytest.mark.asyncio
async def test_duplicate_delivery_replays_bounded_ack_without_dispatching_twice(server):
    frame = {
        "type": "inbound",
        "delivery_id": "delivery-replay-1",
        "bufferId": "buffer-replay-1",
        "event": {
            "text": "deliver once",
            "message_type": "text",
            "owner_id": "opaque-owner:replay-1",
            "source": {"platform": "discord", "chat_id": "chan1", "chat_type": "dm"},
        },
    }
    server._to_push = [frame, frame]
    received = []
    t = WebSocketRelayTransport(server.url, "discord", "appShared")

    async def accept(ev):
        received.append(ev.text)
        return {
            "disposition": "started",
            "canonical_turn_owner_id": ev.owner_id,
            "session_key": "agent:main:relay:dm:chan1",
            "chat_id": "chan1",
        }

    t.set_inbound_handler(accept)
    await t.connect()
    try:
        await t.handshake()
        await asyncio.sleep(0.1)
        assert received == ["deliver once"]
        acks = [f for f in server.received if f.get("type") == "inbound_ack"]
        assert len(acks) == 2
        assert acks[0] == acks[1]
        assert len(t._inbound_ack_frames) <= 1024
    finally:
        await t.disconnect()


@pytest.mark.asyncio
async def test_transient_inbound_handler_failure_is_not_acked_or_cached_and_can_retry():
    t = WebSocketRelayTransport("ws://127.0.0.1:1", "relay", "")
    sent: list[dict] = []
    attempts = 0

    async def record(frame):
        sent.append(dict(frame))

    async def flaky(event):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("transient handler failure")
        return {
            "disposition": "started",
            "canonical_turn_owner_id": event.owner_id,
            "session_key": "agent:main:relay:dm:mission-control",
            "chat_id": "mission-control",
        }

    t._send = record  # type: ignore[method-assign]
    t.set_inbound_handler(flaky)
    frame = json.dumps({
        "type": "inbound",
        "delivery_id": "delivery-handler-retry",
        "bufferId": "delivery-handler-retry",
        "event": {
            "text": "must survive",
            "message_type": "text",
            "owner_id": "opaque-owner:handler-retry",
            "source": {
                "platform": "relay", "chat_id": "mission-control", "chat_type": "dm",
            },
        },
    })

    with pytest.raises(RuntimeError, match="transient handler failure"):
        await t._handle_frame(frame)
    assert sent == []
    assert "delivery-handler-retry" not in t._inbound_ack_frames

    await t._handle_frame(frame)
    assert attempts == 2
    assert sent[-1]["type"] == "inbound_ack"
    assert sent[-1]["disposition"] == "started"


@pytest.mark.asyncio
async def test_identical_event_local_started_ack_is_emitted_once_but_failed_send_retries():
    t = WebSocketRelayTransport("ws://127.0.0.1:1", "relay", "")
    attempts: list[dict] = []
    fail_first = True

    async def record(frame):
        nonlocal fail_first
        attempts.append(dict(frame))
        if fail_first:
            fail_first = False
            raise OSError("fixture send failure")

    event = MessageEvent(
        text="start once",
        message_type=MessageType.TEXT,
        owner_id="opaque-owner:start-once",
        source=SessionSource(
            platform=Platform.RELAY,
            chat_id="mission-control",
            chat_type="dm",
        ),
    )
    result = {
        "disposition": "started",
        "canonical_turn_owner_id": event.owner_id,
        "session_key": "agent:main:relay:dm:mission-control",
        "chat_id": "mission-control",
    }
    t._send = record  # type: ignore[method-assign]

    assert not await t._publish_inbound_ack(
        event, result, delivery_id="delivery-start-once"
    )
    assert await t._publish_inbound_ack(
        event, result, delivery_id="delivery-start-once"
    )
    assert await t._publish_inbound_ack(
        event, result, delivery_id="delivery-start-once"
    )
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]


@pytest.mark.asyncio
async def test_inbound_during_handoff_exposes_neither_terminal_a_nor_unbound_b():
    t = WebSocketRelayTransport("ws://127.0.0.1:1", "discord", "appShared")
    t._descriptor = CapabilityDescriptor.from_json(json.dumps(DESCRIPTOR))
    frames: list[dict] = []

    async def record(frame):
        frames.append(dict(frame))

    t._send = record  # type: ignore[method-assign]

    def event(owner: str) -> MessageEvent:
        return MessageEvent(
            text=owner,
            message_type=MessageType.TEXT,
            owner_id=owner,
            source=SessionSource(
                platform=Platform.RELAY,
                chat_id="mission-control",
                chat_type="dm",
            ),
        )

    scope = "agent:main:relay:dm:mission-control"
    owner_a = "opaque-owner:A"
    owner_b = "opaque-owner:B"
    owner_c = "opaque-owner:C"
    assert await t._publish_inbound_ack(
        event(owner_a),
        {
            "disposition": "started",
            "canonical_turn_owner_id": owner_a,
            "session_key": scope,
            "chat_id": "mission-control",
        },
        delivery_id="delivery-A",
    )
    assert await t.send_turn_completed(
        scope,
        "mission-control",
        owner_a,
        "completed",
        owner_b,
        "delivery-B",
    )
    assert await t._publish_inbound_ack(
        event(owner_c),
        {
            "disposition": "absorbed",
            # The adapter guard has not rebound yet and can still report A.
            "canonical_turn_owner_id": owner_a,
            "session_key": scope,
            "chat_id": "mission-control",
        },
        delivery_id="delivery-C",
    )

    ack = frames[-1]
    assert ack["type"] == "inbound_ack"
    assert ack["delivery_id"] == "delivery-C"
    assert ack["canonical_turn_owner_id"] is None
    assert ack["owner_state_seq"] == 2
    assert t._turn_state_snapshots()[0]["status"] == "handoff"

    successor = event(owner_b)
    successor.metadata.update(
        {
            "relay_delivery_id": "delivery-B",
            "relay_session_key": scope,
            "relay_chat_id": "mission-control",
        }
    )
    assert await t.send_turn_started(successor)
    assert t._turn_state_snapshots() == [
        {
            "session_key": scope,
            "chat_id": "mission-control",
            "owner_state_seq": 3,
            "status": "running",
            "active_owner_id": owner_b,
            "terminal_owner_id": None,
            "terminal_outcome": None,
            "next_owner_id": None,
            "next_delivery_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_inbound_frame_reaches_handler(server):
    opaque_owner = "owner:mc/7f6c2a8d"
    server._to_push = [
        {
            "type": "inbound",
            "delivery_id": "delivery-1",
            "event": {
                "text": "hello from connector",
                "message_type": "text",
                "owner_id": opaque_owner,
                "source": {"platform": "discord", "chat_id": "chan1", "chat_type": "group", "scope_id": "guildA"},
            },
            "bufferId": "buf-1",
        }
    ]
    received = []
    t = WebSocketRelayTransport(server.url, "discord", "appShared")

    async def accept(ev):
        received.append(ev)
        return {
            "disposition": "started",
            "canonical_turn_owner_id": ev.owner_id,
            "session_key": "agent:main:relay:dm:chan1",
            "chat_id": "chan1",
        }

    t.set_inbound_handler(accept)
    await t.connect()
    try:
        await t.handshake()
        # Give the reader a tick to deliver the pushed inbound frame.
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0].text == "hello from connector"
        assert received[0].source.scope_id == "guildA"
        await asyncio.sleep(0.05)
        assert [f for f in server.received if f.get("type") == "inbound_ack"] == [
            {
                "type": "inbound_ack",
                "bufferId": "buf-1",
                "delivery_id": "delivery-1",
                "session_key": "agent:main:relay:dm:chan1",
                "chat_id": "chan1",
                "owner_id": opaque_owner,
                "runtime_epoch": t._runtime_epoch,
                "disposition": "started",
                "canonical_turn_owner_id": opaque_owner,
                "owner_state_seq": 1,
            }
        ]
    finally:
        await t.disconnect()


# ── Phase 7 Unit 7d-B: terminal 4401 (opt-out revocation) ────────────────────


class _Revoking4401Server:
    """Connector stub that, on hello, optionally sends a descriptor and then
    closes the socket with application code 4401 (unauthorized) — the shape of a
    connector that has revoked this gateway's per-gateway secret (opt-out)."""

    def __init__(self, *, send_descriptor_first: bool):
        self._server = None
        self.url = ""
        self._send_descriptor_first = send_descriptor_first

    async def start(self):
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self):
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        async for raw in ws:
            for line in str(raw).split("\n"):
                if not line.strip():
                    continue
                frame = json.loads(line)
                if frame.get("type") == "hello":
                    if self._send_descriptor_first:
                        await ws.send(
                            json.dumps({"type": "descriptor", "descriptor": DESCRIPTOR}) + "\n"
                        )
                        # Let the descriptor flush + be processed before the close.
                        await asyncio.sleep(0.05)
                    # Close with 4401 (the connector's "unauthorized" close).
                    await ws.close(code=4401, reason="unauthorized")
                    return


@pytest.mark.asyncio
async def test_4401_after_handshake_is_terminal_no_reconnect():
    """A 4401 close AFTER a successful handshake = a revoked credential (opt-out):
    the transport latches auth_revoked and does NOT spin the reconnect supervisor."""
    srv = _Revoking4401Server(send_descriptor_first=True)
    await srv.start()
    try:
        t = WebSocketRelayTransport(
            srv.url, "discord", "appShared",
            gateway_id="gw-x", upgrade_secret="secret-x",
            reconnect=True, reconnect_backoff_s=0.05,
        )
        await t.connect()
        await t.handshake()  # records _handshake_succeeded
        # Wait for the server's 4401 close to propagate through the read loop.
        for _ in range(100):
            if t.auth_revoked:
                break
            await asyncio.sleep(0.02)
        assert t.auth_revoked is True
        # Terminal: no reconnect supervisor was spawned.
        assert t._supervisor is None
        # Give a reconnect (if it were going to happen) time to NOT happen.
        await asyncio.sleep(0.2)
        assert t._supervisor is None
    finally:
        await t.disconnect()
        await srv.stop()
