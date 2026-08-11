"""Generation-bound Relay Stop routed through GatewayRunner's hard-stop seam."""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import gateway.relay.ws_transport as ws_transport_module
import gateway.run as gateway_run_module
from gateway.interrupt_budget import (
    HARD_STOP_REAP_TIMEOUT_SECONDS,
    INTERRUPT_ACTIVITY_TIMEOUT_SECONDS,
    INTERRUPT_HANDLER_SAFETY_MARGIN_SECONDS,
    SESSION_PROCESSING_CANCEL_TIMEOUT_SECONDS,
)
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import (
    CONTRACT_VERSION,
    OWNER_BOUND_INTERRUPT_ACK_CAPABILITY,
    CapabilityDescriptor,
)
from gateway.relay.ws_transport import WebSocketRelayTransport, _event_from_wire
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from tools.process_registry import process_registry

from tests.gateway.relay.stub_connector import StubConnector


def _desc() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        contract_version=CONTRACT_VERSION,
        platform="discord",
        label="Discord",
        max_message_length=2000,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=True,
        markdown_dialect="discord",
        len_unit="chars",
        capabilities=(OWNER_BOUND_INTERRUPT_ACK_CAPABILITY,),
    )


@pytest.fixture
def adapter():
    return RelayAdapter(PlatformConfig(typing_indicator=False), _desc(), transport=StubConnector(_desc()))


def _event(owner_id: str, text: str = "run a long tool") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        owner_id=owner_id,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chanA",
            chat_type="dm",
            user_id="userX",
            delivered_via_upstream_relay=True,
        ),
    )


def _runner(adapter: RelayAdapter) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.RELAY: adapter.config})
    runner.adapters = {Platform.RELAY: adapter}
    runner._profile_adapters = {}
    runner._sessions = {}
    runner._persist_active_agents = MagicMock()
    runner._evict_cached_agent = MagicMock()
    adapter.gateway_runner = runner
    adapter.set_session_interrupt_handler(runner._handle_adapter_session_interrupt)
    return runner


class _ControlledAgent:
    def __init__(self) -> None:
        self.hard_interrupts: list[str | None] = []
        self._gateway_turn_process_task_id = ""
        self._gateway_turn_process_baseline = frozenset()

    def hard_interrupt(self, message: str | None = None) -> None:
        self.hard_interrupts.append(message)


def test_inbound_wire_owner_id_is_parsed_fail_closed():
    raw = {
        "text": "hello",
        "source": {"platform": "discord", "chat_id": "chanA", "chat_type": "dm"},
    }

    assert _event_from_wire({**raw, "owner_id": "relay-turn-wire"}).owner_id == "relay-turn-wire"
    assert _event_from_wire(raw).owner_id is None
    assert _event_from_wire({**raw, "owner_id": {"bad": "shape"}}).owner_id is None


@pytest.mark.asyncio
async def test_interrupt_wire_transports_owner_id_fail_closed():
    transport = object.__new__(WebSocketRelayTransport)
    calls: list[tuple[str, str, str | None]] = []
    sent: list[dict] = []

    async def handler(session_key: str, chat_id: str, owner_id: str | None) -> bool:
        calls.append((session_key, chat_id, owner_id))
        return True

    async def capture_send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = capture_send
    await transport._handle_frame(
        json.dumps(
            {
                "type": "interrupt_inbound",
                "session_key": "agent:main:discord:dm:chanA:userX",
                "chat_id": "chanA",
                "owner_id": "relay-turn-wire",
                "action_id": "stop-action-wire-valid",
            }
        )
    )
    await transport._handle_frame(
        json.dumps(
            {
                "type": "interrupt_inbound",
                "session_key": "agent:main:discord:dm:chanA:userX",
                "chat_id": "chanA",
                "owner_id": {"bad": "shape"},
                "action_id": "stop-action-wire-invalid",
            }
        )
    )

    for _ in range(50):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)
    assert calls == [
        ("agent:main:discord:dm:chanA:userX", "chanA", "relay-turn-wire"),
    ]
    assert {frame["action_id"]: frame["reason"] for frame in sent} == {
        "stop-action-wire-valid": "accepted",
        "stop-action-wire-invalid": "invalid_owner",
    }
    await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_missing_malformed_and_stale_owner_fail_closed(adapter):
    current = _event("relay-turn-current")
    started = asyncio.Event()
    block = asyncio.Event()

    async def handler(_event):
        started.set()
        await block.wait()

    calls: list[tuple] = []

    async def hard_stop(*args):
        calls.append(args)
        return True

    adapter.set_message_handler(handler)
    adapter.set_session_interrupt_handler(hard_stop)
    await adapter.connect()
    await adapter.handle_message(current)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    session_key = build_session_key(current.source)
    owner_task = adapter._session_tasks[session_key]

    try:
        for owner_id in (None, "", "stale-owner", {"bad": "shape"}):
            stopped = await adapter.on_interrupt(session_key, "chanA", owner_id)
            assert stopped is False
        assert calls == []
        assert owner_task.done() is False
    finally:
        owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stale_owner_does_not_stop_replacement_generation(adapter):
    replacement = _event("relay-turn-replacement")
    started = asyncio.Event()
    block = asyncio.Event()

    async def handler(_event):
        started.set()
        await block.wait()

    hard_stop = MagicMock()
    adapter.set_message_handler(handler)
    adapter.set_session_interrupt_handler(hard_stop)
    await adapter.connect()
    await adapter.handle_message(replacement)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    session_key = build_session_key(replacement.source)
    owner_task = adapter._session_tasks[session_key]

    try:
        assert await adapter.on_interrupt(session_key, "chanA", "relay-turn-prior") is False
        hard_stop.assert_not_called()
        assert owner_task.done() is False
        assert adapter._active_sessions[session_key]._hermes_owner_id == "relay-turn-replacement"
    finally:
        owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interrupt_sets_only_target_session_event_and_preserves_sibling(adapter):
    target = _event("relay-turn-target")
    sibling = MessageEvent(
        text="sibling run",
        message_type=MessageType.TEXT,
        owner_id="relay-turn-sibling",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chanB",
            chat_type="dm",
            user_id="userY",
            delivered_via_upstream_relay=True,
        ),
    )
    started = {"chanA": asyncio.Event(), "chanB": asyncio.Event()}

    async def handler(event):
        started[event.source.chat_id].set()
        await asyncio.Event().wait()

    async def hard_stop(session_key, source, _owner_id, _generation):
        await adapter.interrupt_session_activity(session_key, source.chat_id)
        return True

    adapter.set_message_handler(handler)
    adapter.set_session_interrupt_handler(hard_stop)
    await adapter.connect()
    await adapter.handle_message(target)
    await adapter.handle_message(sibling)
    await asyncio.wait_for(started["chanA"].wait(), timeout=1.0)
    await asyncio.wait_for(started["chanB"].wait(), timeout=1.0)
    target_key = adapter.session_key_for_source(target.source)
    sibling_key = adapter.session_key_for_source(sibling.source)
    target_guard = adapter._active_sessions[target_key]
    sibling_guard = adapter._active_sessions[sibling_key]
    target_guard._hermes_run_generation = 1

    assert await adapter.on_interrupt(target_key, "chanA", target.owner_id) is True

    assert target_guard.is_set() is True
    assert sibling_guard.is_set() is False
    assert sibling_key in adapter._active_sessions
    assert adapter._session_tasks[sibling_key].done() is False
    sibling_task = adapter._session_tasks[sibling_key]
    sibling_task.cancel()
    await asyncio.gather(sibling_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_matching_owner_uses_canonical_runner_stop_reaps_real_child_and_hands_off_fresh_turn(
    adapter, tmp_path: Path, monkeypatch
):
    runner = _runner(adapter)
    first = _event("relay-turn-first")
    doomed = _event("relay-turn-doomed", "queued before stop")
    fresh = _event("relay-turn-fresh", "fresh next turn")
    session_key = build_session_key(first.source)
    sentinel = tmp_path / "late-child-sentinel"
    first_started = asyncio.Event()
    fresh_started = asyncio.Event()
    child_holder = []
    agent = _ControlledAgent()
    late_callbacks: list[str] = []
    owner_generations: list[tuple[str, int, str | None]] = []
    scheduled_reapers: list[tuple] = []
    real_thread = threading.Thread

    class DelayedReaperThread:
        def __init__(self, *, target, args=(), kwargs=None, **_options):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            scheduled_reapers.append((self.target, self.args, self.kwargs))

    async def handler(event):
        generation = runner._begin_session_run_generation(session_key)
        runner._running_agents[session_key] = agent
        runner._bind_adapter_run_generation(adapter, session_key, generation)
        guard = adapter._active_sessions[session_key]
        owner_generations.append(
            (
                getattr(guard, "_hermes_owner_id", ""),
                getattr(guard, "_hermes_run_generation", 0),
                getattr(guard, "_hermes_runner_session_key", None),
            )
        )
        if event.owner_id == first.owner_id:
            adapter.register_post_delivery_callback(
                session_key,
                lambda: late_callbacks.append("stale"),
                generation=generation,
            )
            baseline = process_registry.snapshot_running_ids(session_key)
            command = (
                f"{shlex.quote(sys.executable)} -c "
                f"{shlex.quote(f'import time; from pathlib import Path; time.sleep(30); Path({str(sentinel)!r}).write_text(chr(120))')}"
            )
            child = process_registry.spawn_local(command, task_id=session_key)
            child_holder.append(child)
            agent._gateway_turn_process_task_id = session_key
            agent._gateway_turn_process_baseline = baseline
            first_started.set()
            # Model the real agent call naturally returning after hard_interrupt;
            # no test-only allow_unwind gate keeps the adapter task open.
            while not agent.hard_interrupts:
                await asyncio.sleep(0)
            return "stale response"
        fresh_started.set()
        return "fresh response"

    adapter.set_message_handler(handler)
    await adapter.connect()
    await adapter.handle_message(first)
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    # Patch only after spawn_local created its real reader threads: run.py and
    # process_registry share Python's threading module object.
    monkeypatch.setattr("gateway.run.threading.Thread", DelayedReaperThread)
    first_task = adapter._session_tasks[session_key]
    child = child_holder[0]
    canonical_stop = runner._interrupt_and_clear_session
    adapter._pending_messages[session_key] = doomed

    async def stop_then_receive_pending(*args, **kwargs):
        await canonical_stop(*args, **kwargs)
        # Model a message landing after canonical Stop discarded anything
        # pending-before-Stop, but before adapter cancellation/drain. The old
        # task is not held open by a test-only unwind gate.
        adapter._pending_messages[session_key] = fresh

    runner._interrupt_and_clear_session = stop_then_receive_pending
    stop_task = asyncio.create_task(
        adapter._transport.push_interrupt(session_key, "chanA", first.owner_id)
    )
    for _ in range(100):
        if scheduled_reapers:
            break
        await asyncio.sleep(0)
    assert scheduled_reapers, "hard-stop must schedule the canonical reaper"
    assert fresh_started.is_set() is False

    # Adversarial scheduler: release the reaper only after Stop reached its
    # barrier. A replacement generation must not start before this finishes.
    monkeypatch.setattr("gateway.run.threading.Thread", real_thread)
    target, args, kwargs = scheduled_reapers.pop(0)
    target(*args, **kwargs)
    await asyncio.wait_for(stop_task, timeout=3.0)
    await asyncio.wait_for(fresh_started.wait(), timeout=2.0)
    assert owner_generations[0][0] == first.owner_id
    assert owner_generations[1][0] == fresh.owner_id
    assert [owner for owner, _generation, _runner_key in owner_generations] == [
        first.owner_id,
        fresh.owner_id,
    ]
    assert owner_generations[1][1] > owner_generations[0][1]
    deadline = time.monotonic() + 5.0
    while child.process.poll() is None and time.monotonic() < deadline:
        await asyncio.sleep(0.05)

    try:
        assert child.process.poll() is not None, (
            "a fresh handoff generation must not exempt the abandoned turn's child"
        )
        assert child.id not in process_registry.snapshot_running_ids(session_key)
        assert agent.hard_interrupts
        runner._evict_cached_agent.assert_called_once_with(session_key)
    finally:
        if child.process.poll() is None:
            process_registry.kill_process(child.id, source="relay_interrupt_test_cleanup")
    deadline = time.monotonic() + 2.0
    while not any(
        action.get("op") == "send" and action.get("content") == "fresh response"
        for action in adapter._transport.sent
    ) and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    fresh_task = adapter._session_tasks.get(session_key)
    if fresh_task is not None:
        await asyncio.wait_for(asyncio.shield(fresh_task), timeout=2.0)

    assert first_task.cancelled() is True
    assert not sentinel.exists()
    assert late_callbacks == []
    assert not any(
        action.get("op") == "send" and action.get("content") not in {"fresh response"}
        for action in adapter._transport.sent
    )
    assert any(
        action.get("op") == "send" and action.get("content") == "fresh response"
        for action in adapter._transport.sent
    )
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_cancelling_real_canonical_stop_mid_reap_releases_barrier_and_allows_fresh_turn(
    adapter, tmp_path: Path, monkeypatch
):
    """Cancellation after barrier installation must not strand the session."""
    runner = _runner(adapter)
    first = _event("relay-turn-cancelled")
    fresh = _event("relay-turn-after-cancel", "fresh after cancelled stop")
    session_key = adapter.session_key_for_source(first.source)
    first_started = asyncio.Event()
    fresh_started = asyncio.Event()
    agent = _ControlledAgent()
    child_holder = []
    reap_entered = threading.Event()
    release_reap = threading.Event()
    real_reap = gateway_run_module._reap_gateway_turn_processes

    def held_real_reap(*args, **kwargs):
        reap_entered.set()
        release_reap.wait(timeout=2.0)
        return real_reap(*args, **kwargs)

    async def handler(event):
        generation = runner._begin_session_run_generation(session_key)
        runner._running_agents[session_key] = agent
        runner._bind_adapter_run_generation(
            adapter,
            session_key,
            generation,
            runner_session_key=session_key,
        )
        if event.owner_id == first.owner_id:
            baseline = process_registry.snapshot_running_ids(session_key)
            child = process_registry.spawn_local(
                f"{shlex.quote(sys.executable)} -c "
                f"{shlex.quote('import time; time.sleep(30)')}",
                task_id=session_key,
            )
            child_holder.append(child)
            agent._gateway_turn_process_task_id = session_key
            agent._gateway_turn_process_baseline = baseline
            first_started.set()
            while not agent.hard_interrupts:
                await asyncio.sleep(0)
            return "stale response"
        fresh_started.set()
        return "fresh response"

    monkeypatch.setattr(gateway_run_module, "_reap_gateway_turn_processes", held_real_reap)
    adapter.set_message_handler(handler)
    await adapter.connect()
    await adapter.handle_message(first)
    await asyncio.wait_for(first_started.wait(), timeout=2.0)
    owner_task = adapter._session_tasks[session_key]
    stop_task = asyncio.create_task(
        adapter.on_interrupt(session_key, "chanA", first.owner_id)
    )
    await asyncio.wait_for(asyncio.to_thread(reap_entered.wait, 1.0), timeout=1.5)
    guard = adapter._active_sessions[session_key]
    barrier = guard._hermes_hard_stop_reap_barrier

    try:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        assert barrier.is_set(), "canonical Stop cancellation must release the handoff"

        release_reap.set()
        await asyncio.wait_for(
            asyncio.gather(owner_task, return_exceptions=True), timeout=2.0
        )
        assert owner_task.cancelled()
        child = child_holder[0]
        deadline = time.monotonic() + 3.0
        while child.process.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert child.process.poll() is not None
        assert session_key not in adapter._active_sessions
        assert session_key not in adapter._session_tasks

        await adapter.handle_message(fresh)
        await asyncio.wait_for(fresh_started.wait(), timeout=1.0)
        fresh_task = adapter._session_tasks.get(session_key)
        if fresh_task is not None:
            await asyncio.wait_for(asyncio.shield(fresh_task), timeout=1.0)
    finally:
        release_reap.set()
        barrier.set()
        await asyncio.gather(stop_task, owner_task, return_exceptions=True)
        for child in child_holder:
            if child.process.poll() is None:
                process_registry.kill_process(
                    child.id, source="relay_cancel_barrier_test_cleanup"
                )


@pytest.mark.asyncio
async def test_socket_close_cancellation_releases_real_canonical_reap_barrier(
    adapter, monkeypatch
):
    """Transport teardown cancels the real adapter -> runner Stop call chain."""
    runner = _runner(adapter)
    event = _event("relay-turn-socket-close")
    session_key = adapter.session_key_for_source(event.source)
    started = asyncio.Event()
    agent = _ControlledAgent()
    agent._gateway_turn_process_task_id = session_key
    agent._gateway_turn_process_baseline = frozenset()
    reap_entered = threading.Event()
    release_reap = threading.Event()
    real_reap = gateway_run_module._reap_gateway_turn_processes

    def held_real_reap(*args, **kwargs):
        reap_entered.set()
        release_reap.wait(timeout=2.0)
        return real_reap(*args, **kwargs)

    async def handler(_event):
        generation = runner._begin_session_run_generation(session_key)
        runner._running_agents[session_key] = agent
        runner._bind_adapter_run_generation(
            adapter, session_key, generation, runner_session_key=session_key
        )
        started.set()
        while not agent.hard_interrupts:
            await asyncio.sleep(0)
        return "stopped"

    monkeypatch.setattr(gateway_run_module, "_reap_gateway_turn_processes", held_real_reap)
    adapter.set_message_handler(handler)
    await adapter.connect()
    await adapter.handle_message(event)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    interrupt_frame = {
        "type": "interrupt_inbound",
        "session_key": session_key,
        "chat_id": "chanA",
        "owner_id": event.owner_id,
        "action_id": "stop-socket-close-real-path",
    }

    class ClosingSocket:
        """Yield one real frame, then model the peer closing the socket."""

        def __init__(self) -> None:
            self._delivered = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._delivered:
                self._delivered = True
                return json.dumps(interrupt_frame) + "\n"
            await asyncio.to_thread(reap_entered.wait, 1.0)
            raise StopAsyncIteration

        async def send(self, _frame: str) -> None:
            raise ConnectionError("closed")

    transport = object.__new__(WebSocketRelayTransport)
    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = adapter.on_interrupt
    transport._ws = ClosingSocket()
    transport._reconnect = False
    transport._closing = False
    transport._auth_revoked = False
    transport._supervisor = None
    transport._handshake_succeeded = True
    read_task = asyncio.create_task(transport._read_loop())
    await asyncio.wait_for(asyncio.to_thread(reap_entered.wait, 1.0), timeout=1.5)
    guard = adapter._active_sessions[session_key]
    barrier = guard._hermes_hard_stop_reap_barrier

    try:
        await asyncio.wait_for(read_task, timeout=2.0)
        assert barrier.is_set()
        release_reap.set()
        owner_task = adapter._session_tasks.get(session_key)
        if owner_task is not None:
            await asyncio.wait_for(asyncio.shield(owner_task), timeout=2.0)
        assert transport._interrupt_tasks == set()
        assert transport._interrupt_workers == {}
        assert transport._interrupt_queues == {}
        assert session_key not in adapter._active_sessions
        assert session_key not in adapter._session_tasks
        hard_interrupt_count = len(agent.hard_interrupts)
        assert await adapter.on_interrupt(
            session_key, "chanA", event.owner_id
        ) is True
        assert len(agent.hard_interrupts) == hard_interrupt_count
    finally:
        release_reap.set()
        barrier.set()
        await asyncio.gather(read_task, return_exceptions=True)
        await transport._cancel_interrupt_tasks()
        owner_task = adapter._session_tasks.get(session_key)
        if owner_task is not None:
            owner_task.cancel()
            await asyncio.gather(owner_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_handoff_barrier_fail_safe_is_bounded_discarded_and_does_not_spin(
    monkeypatch, caplog
):
    import gateway.platforms.base as base_module

    class NeverReadyBarrier:
        def __init__(self) -> None:
            self.wait_calls: list[float] = []

        def is_set(self) -> bool:
            return False

        def wait(self, timeout: float) -> bool:
            self.wait_calls.append(timeout)
            return False

    guard = asyncio.Event()
    barrier = NeverReadyBarrier()
    guard._hermes_hard_stop_reap_barrier = barrier
    monkeypatch.setattr(
        base_module, "SESSION_HANDOFF_BARRIER_TIMEOUT_SECONDS", 0.01
    )

    await asyncio.wait_for(
        base_module.BasePlatformAdapter._wait_session_handoff_barrier(guard),
        timeout=0.2,
    )

    assert barrier.wait_calls == [0.01]
    assert not hasattr(guard, "_hermes_hard_stop_reap_barrier")
    assert "discarding the stale barrier" in caplog.text


@pytest.mark.asyncio
async def test_interrupt_reader_stays_live_until_correlated_result_is_ready():
    transport = object.__new__(WebSocketRelayTransport)
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[dict] = []
    inbound: list[str] = []

    async def handler(_session_key: str, _chat_id: str, _owner_id: str | None) -> bool:
        entered.set()
        await release.wait()
        return True

    async def send(frame: dict) -> None:
        sent.append(frame)

    async def handle_inbound(event: MessageEvent) -> None:
        inbound.append(event.text)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = send
    transport._inbound = handle_inbound
    loop = asyncio.get_running_loop()
    outbound = loop.create_future()
    transport._pending = {"outbound-1": outbound}

    await transport._handle_frame(json.dumps({
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-reader",
        "action_id": "stop-action-reader",
    }))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await transport._handle_frame(json.dumps({
        "type": "outbound_result",
        "requestId": "outbound-1",
        "result": {"success": True},
    }))
    assert await asyncio.wait_for(outbound, timeout=0.1) == {"success": True}
    await transport._handle_frame(json.dumps({
        "type": "inbound",
        "bufferId": "buffer-during-stop",
        "event": {
            "text": "delivered while stop waits",
            "message_type": "text",
            "source": {
                "platform": "relay", "chat_id": "chanA", "chat_type": "dm",
                "user_id": "owner",
            },
        },
    }))
    assert inbound == ["delivered while stop waits"]
    assert sent == [{"type": "inbound_ack", "bufferId": "buffer-during-stop"}]

    release.set()
    for _ in range(50):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)
    assert sent[-1] == {
        "type": "interrupt_result",
        "action_id": "stop-action-reader",
        "accepted": True,
        "reason": "accepted",
    }
    await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_duplicate_interrupt_action_id_has_one_hard_stop_and_one_result():
    transport = object.__new__(WebSocketRelayTransport)
    release = asyncio.Event()
    calls = 0
    sent: list[dict] = []

    async def handler(*_args) -> bool:
        nonlocal calls
        calls += 1
        await release.wait()
        return True

    async def send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = send
    frame = json.dumps({
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-duplicate",
        "action_id": "stop-action-duplicate",
    })
    await transport._handle_frame(frame)
    await transport._handle_frame(frame)
    for _ in range(50):
        if calls:
            break
        await asyncio.sleep(0)
    assert calls == 1
    release.set()
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0)
    assert len(sent) == 1
    assert sent[0]["action_id"] == "stop-action-duplicate"
    await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_distinct_interrupt_actions_are_serialized_per_session():
    transport = object.__new__(WebSocketRelayTransport)
    first_release = asyncio.Event()
    second_release = asyncio.Event()
    entered: list[str] = []
    active_handlers = 0
    max_active_handlers = 0
    sent: list[dict] = []

    async def handler(_session_key, _chat_id, owner_id) -> bool:
        nonlocal active_handlers, max_active_handlers
        active_handlers += 1
        max_active_handlers = max(max_active_handlers, active_handlers)
        entered.append(owner_id)
        try:
            await (first_release if len(entered) == 1 else second_release).wait()
            return True
        finally:
            active_handlers -= 1

    async def send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = send
    base = {
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
    }
    await transport._handle_frame(json.dumps({
        **base, "owner_id": "relay-turn-serial-1", "action_id": "stop-serial-1",
    }))
    await transport._handle_frame(json.dumps({
        **base, "owner_id": "relay-turn-serial-2", "action_id": "stop-serial-2",
    }))
    for _ in range(50):
        if entered:
            break
        await asyncio.sleep(0)
    assert entered == ["relay-turn-serial-1"]
    assert max_active_handlers == 1
    first_release.set()
    for _ in range(50):
        if len(entered) == 2:
            break
        await asyncio.sleep(0)
    assert entered == ["relay-turn-serial-1", "relay-turn-serial-2"]
    assert max_active_handlers == 1
    second_release.set()
    for _ in range(50):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)
    assert [frame["action_id"] for frame in sent] == ["stop-serial-1", "stop-serial-2"]
    await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_interrupt_worker_timeout_is_bounded_and_close_awaits_cancellation(monkeypatch):
    """A wedged canonical stop/Slack typing cleanup cannot deadlock the reader."""
    monkeypatch.setattr("gateway.relay.ws_transport._INTERRUPT_HANDLER_TIMEOUT_S", 0.01)
    transport = object.__new__(WebSocketRelayTransport)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    sent: list[dict] = []

    async def handler(*_args) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = send
    await transport._handle_frame(json.dumps({
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-timeout",
        "action_id": "stop-timeout",
    }))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0.002)
    assert cancelled.is_set()
    assert sent == [{
        "type": "interrupt_result", "action_id": "stop-timeout",
        "accepted": False, "reason": "handler_timeout",
    }]
    await transport._cancel_interrupt_tasks()
    assert transport._interrupt_tasks == set()
    assert transport._interrupt_workers == {}
    assert transport._interrupt_queues == {}


def test_interrupt_handler_budget_exceeds_all_inner_bounds_with_margin():
    """The outer ack cap must never cancel a normally bounded canonical Stop."""
    inner_budget = (
        HARD_STOP_REAP_TIMEOUT_SECONDS
        + INTERRUPT_ACTIVITY_TIMEOUT_SECONDS
        + SESSION_PROCESSING_CANCEL_TIMEOUT_SECONDS
        + INTERRUPT_HANDLER_SAFETY_MARGIN_SECONDS
    )
    assert ws_transport_module._INTERRUPT_HANDLER_TIMEOUT_S > inner_budget
    assert (
        gateway_run_module._INTERRUPT_REAP_TIMEOUT_SECONDS
        == HARD_STOP_REAP_TIMEOUT_SECONDS
    )
    assert (
        gateway_run_module._INTERRUPT_ACTIVITY_TIMEOUT_SECONDS
        == INTERRUPT_ACTIVITY_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_outer_timeout_during_slow_reap_and_unwind_acks_admitted_terminal_stop(
    adapter, monkeypatch
):
    """A half-applied exact-owner Stop must not become a retryable timeout NACK."""
    import gateway.platforms.base as base_module

    runner = _runner(adapter)
    event = _event("relay-turn-budget-boundary")
    session_key = adapter.session_key_for_source(event.source)
    started = asyncio.Event()
    unwind_entered = asyncio.Event()
    release_unwind = asyncio.Event()
    release_reap = threading.Event()
    agent = _ControlledAgent()
    agent._gateway_turn_process_task_id = session_key
    agent._gateway_turn_process_baseline = frozenset()
    real_reap = gateway_run_module._reap_gateway_turn_processes

    def slow_reap(*args, **kwargs):
        release_reap.wait(timeout=0.2)
        return real_reap(*args, **kwargs)

    async def handler(_event):
        generation = runner._begin_session_run_generation(session_key)
        runner._running_agents[session_key] = agent
        runner._bind_adapter_run_generation(
            adapter, session_key, generation, runner_session_key=session_key
        )
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwind_entered.set()
            await release_unwind.wait()
            raise

    monkeypatch.setattr(gateway_run_module, "_reap_gateway_turn_processes", slow_reap)
    monkeypatch.setattr(gateway_run_module, "_INTERRUPT_REAP_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(
        base_module, "SESSION_PROCESSING_CANCEL_TIMEOUT_SECONDS", 0.01
    )
    # Force the transport cap to cancel the real adapter -> runner chain while
    # the reaper is still held. Adapter cancellation then performs its bounded
    # slow-unwind cleanup and returns terminal acceptance.
    monkeypatch.setattr(ws_transport_module, "_INTERRUPT_HANDLER_TIMEOUT_S", 0.005)
    adapter.set_message_handler(handler)
    await adapter.connect()
    await adapter.handle_message(event)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    transport = object.__new__(WebSocketRelayTransport)
    sent: list[dict] = []
    result_sent = asyncio.Event()
    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = adapter.on_interrupt

    async def capture(frame):
        sent.append(frame)
        result_sent.set()

    transport._send = capture
    transport._queue_interrupt({
        "session_key": session_key,
        "chat_id": "chanA",
        "owner_id": event.owner_id,
        "action_id": "stop-budget-boundary",
    })
    try:
        await asyncio.wait_for(unwind_entered.wait(), timeout=0.2)
        await asyncio.wait_for(result_sent.wait(), timeout=0.2)
        assert sent == [{
            "type": "interrupt_result",
            "action_id": "stop-budget-boundary",
            "accepted": True,
            "reason": "accepted",
        }]
        assert session_key not in adapter._active_sessions
        assert session_key not in adapter._session_tasks
    finally:
        release_reap.set()
        release_unwind.set()
        await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_canonical_stop_bounds_wedged_slack_typing_cleanup(adapter, monkeypatch):
    """A platform typing call cannot consume the WS interrupt timeout budget."""
    runner = _runner(adapter)
    source = _event("relay-turn-typing").source
    session_key = adapter.session_key_for_source(source)
    cancelled = asyncio.Event()
    adapter._pending_messages[session_key] = _event("relay-turn-pending")
    runner._peek_session_state = MagicMock(return_value=None)
    runner._invalidate_session_run_generation = MagicMock()
    runner._thread_metadata_for_source = MagicMock(return_value={})

    async def wedged_typing(*_args, **_kwargs) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    monkeypatch.setattr(adapter, "interrupt_session_activity", wedged_typing)
    monkeypatch.setattr("gateway.run._INTERRUPT_ACTIVITY_TIMEOUT_SECONDS", 0.01)

    await asyncio.wait_for(
        runner._interrupt_and_clear_session(
            session_key,
            source,
            interrupt_reason="test stop",
            invalidation_reason="test",
            release_running_state=False,
        ),
        timeout=0.5,
    )

    assert cancelled.is_set()
    assert adapter.get_pending_message(session_key) is None


@pytest.mark.asyncio
async def test_interrupt_close_cancels_and_awaits_live_worker():
    transport = object.__new__(WebSocketRelayTransport)
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    sent: list[dict] = []

    async def handler(*_args) -> bool:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = send
    await transport._handle_frame(json.dumps({
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-close",
        "action_id": "stop-close",
    }))
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    await transport._cancel_interrupt_tasks()
    assert cancelled.is_set()
    assert sent == []
    assert transport._interrupt_tasks == set()
    assert transport._interrupt_workers == {}
    assert transport._interrupt_queues == {}


@pytest.mark.asyncio
async def test_interrupt_frame_identifiers_fail_closed_without_secret_logs(caplog):
    transport = object.__new__(WebSocketRelayTransport)
    calls = 0
    sent: list[dict] = []

    async def handler(*_args) -> bool:
        nonlocal calls
        calls += 1
        return True

    async def send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = send
    base = {
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-boundary",
    }
    await transport._handle_frame(json.dumps({
        **base, "session_key": {"secret": "raw-session-secret"},
        "action_id": "stop-bad-session",
    }))
    await transport._handle_frame(json.dumps({
        **base, "chat_id": "raw-chat-secret\n", "action_id": "stop-bad-chat",
    }))
    await transport._handle_frame(json.dumps({
        **base, "owner_id": ["raw-owner-secret"], "action_id": "stop-bad-owner",
    }))
    await transport._handle_frame(json.dumps({
        **base, "action_id": {"secret": "raw-action-secret"},
    }))
    for _ in range(50):
        if len(sent) == 3:
            break
        await asyncio.sleep(0)
    assert calls == 0
    assert {frame["action_id"]: frame["reason"] for frame in sent} == {
        "stop-bad-session": "invalid_binding",
        "stop-bad-chat": "invalid_binding",
        "stop-bad-owner": "invalid_owner",
    }
    logs = caplog.text
    for secret in (
        "raw-session-secret", "raw-chat-secret", "raw-owner-secret", "raw-action-secret"
    ):
        assert secret not in logs
    await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_interrupt_requires_negotiated_ack_capability_and_never_leaks_exception():
    transport = object.__new__(WebSocketRelayTransport)
    sent: list[dict] = []
    calls = 0

    async def handler(*_args) -> bool:
        nonlocal calls
        calls += 1
        raise RuntimeError("secret raw backend detail")

    async def send(frame: dict) -> None:
        sent.append(frame)

    transport._descriptor = CapabilityDescriptor(
        contract_version=1,
        platform="discord",
        label="legacy",
        max_message_length=2000,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=False,
        markdown_dialect="discord",
        len_unit="chars",
    )
    transport._interrupt_inbound_handler = handler
    transport._send = send
    base = {
        "type": "interrupt_inbound",
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-capability",
    }
    await transport._handle_frame(json.dumps({**base, "action_id": "stop-action-legacy"}))
    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0)
    assert calls == 0
    assert sent[-1]["reason"] == "capability_not_negotiated"

    transport._descriptor = _desc()
    await transport._handle_frame(json.dumps({**base, "action_id": "stop-action-error"}))
    for _ in range(50):
        if len(sent) == 2:
            break
        await asyncio.sleep(0)
    assert calls == 1
    assert sent[-1]["accepted"] is False
    assert sent[-1]["reason"] == "internal_error"
    assert "secret raw backend detail" not in json.dumps(sent[-1])
    await transport._cancel_interrupt_tasks()


@pytest.mark.asyncio
async def test_transport_handshake_rejects_legacy_descriptor_fail_closed():
    transport = object.__new__(WebSocketRelayTransport)
    loop = asyncio.get_running_loop()
    transport._descriptor = None
    transport._descriptors_by_platform = {}
    transport._descriptor_ready = loop.create_future()
    transport._handshake_succeeded = False
    closed: list[tuple[int, str]] = []

    class Socket:
        async def close(self, *, code: int, reason: str) -> None:
            closed.append((code, reason))

    transport._ws = Socket()
    legacy = CapabilityDescriptor(
        contract_version=1,
        platform="relay",
        label="legacy",
        max_message_length=4096,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=False,
        markdown_dialect="plain",
        len_unit="chars",
    )

    await transport._handle_frame(json.dumps({
        "type": "descriptor",
        "descriptor": legacy.__dict__,
    }))

    with pytest.raises(RuntimeError, match="contract v2 required"):
        await transport._descriptor_ready
    assert transport._descriptor is None
    assert transport._handshake_succeeded is False
    assert closed == [(4406, "relay contract v2 required")]


@pytest.mark.asyncio
async def test_runner_binds_generation_before_slow_preparation_can_be_stopped(adapter):
    runner = _runner(adapter)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._voice_mode = {}
    runner._background_tasks = set()
    runner._draining = False
    runner._restart_requested = False
    runner._restart_task_started = False
    runner._restart_detached = False
    runner._restart_via_service = False
    runner._restart_drain_timeout = 0.0
    runner._stop_task = None
    runner._exit_code = None
    runner._external_drain_active = False
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
    runner.hooks = MagicMock()
    runner.session_store = None

    event = _event("relay-turn-early")
    entered_slow_preparation = asyncio.Event()
    release_preparation = asyncio.Event()
    canonical_stop: list[tuple] = []

    async def slow_inner(_event, _source, _key, _generation):
        entered_slow_preparation.set()
        await release_preparation.wait()
        return "late response"

    async def capture_stop(*args, **kwargs):
        canonical_stop.append((args, kwargs))

    runner._handle_message_with_agent = slow_inner
    runner._interrupt_and_clear_session = capture_stop
    adapter.set_message_handler(runner._handle_message)
    await adapter.connect()
    await adapter.handle_message(event)
    await asyncio.wait_for(entered_slow_preparation.wait(), timeout=1.0)
    adapter_key = adapter.session_key_for_source(event.source)
    guard = adapter._active_sessions[adapter_key]

    assert isinstance(guard._hermes_run_generation, int)
    assert guard._hermes_runner_session_key == runner._session_key_for_source(event.source)
    assert await adapter.on_interrupt(adapter_key, "chanA", event.owner_id) is True
    assert canonical_stop
    assert canonical_stop[0][0][0] == runner._session_key_for_source(event.source)
    release_preparation.set()


@pytest.mark.asyncio
async def test_multiplexed_profile_stop_maps_adapter_key_to_exact_runner_session(adapter):
    runner = _runner(adapter)
    runner.config.multiplex_profiles = True
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chanA",
        chat_type="dm",
        user_id="userX",
        profile="research",
        delivered_via_upstream_relay=True,
    )
    event = MessageEvent(
        text="profile turn",
        message_type=MessageType.TEXT,
        owner_id="relay-turn-research",
        source=source,
    )
    sibling = MessageEvent(
        text="sibling turn",
        message_type=MessageType.TEXT,
        owner_id="relay-turn-sibling",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chanB",
            chat_type="dm",
            user_id="userY",
            delivered_via_upstream_relay=True,
        ),
    )
    adapter_key = adapter.session_key_for_source(source)
    sibling_key = adapter.session_key_for_source(sibling.source)
    research_guard = asyncio.Event()
    sibling_guard = asyncio.Event()
    adapter._bind_session_guard_event(research_guard, event)
    adapter._bind_session_guard_event(sibling_guard, sibling)
    adapter._active_sessions = {adapter_key: research_guard, sibling_key: sibling_guard}
    loop = asyncio.get_running_loop()
    research_task = loop.create_future()
    sibling_task = loop.create_future()
    adapter._session_tasks = {adapter_key: research_task, sibling_key: sibling_task}
    runner_key = runner._session_key_for_source(source)
    generation = runner._begin_session_run_generation(runner_key)
    runner._bind_adapter_run_generation_for_source(source, runner_key, generation)
    stopped: list[tuple] = []

    async def capture_stop(*args, **kwargs):
        stopped.append((args, kwargs))

    runner._interrupt_and_clear_session = capture_stop
    assert adapter_key.startswith("agent:main:")
    assert runner_key.startswith("agent:research:")
    assert await adapter.on_interrupt(adapter_key, "chanA", event.owner_id) is True
    assert stopped[0][0][0] == runner_key
    assert stopped[0][1]["adapter_session_key"] == adapter_key
    assert sibling_guard.is_set() is False
    assert sibling_task.done() is False


@pytest.mark.asyncio
async def test_multiplexed_post_delivery_callback_uses_authoritative_adapter_key_and_generation(
    adapter,
):
    runner = _runner(adapter)
    runner.config.multiplex_profiles = True
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chanA",
        chat_type="dm",
        user_id="userX",
        profile="research",
        delivered_via_upstream_relay=True,
    )
    event = MessageEvent(
        text="profile callback",
        message_type=MessageType.TEXT,
        owner_id="relay-turn-profile-callback",
        source=source,
    )
    adapter_key = adapter.session_key_for_source(source)
    runner_key = runner._session_key_for_source(source)
    guard = asyncio.Event()
    adapter._bind_session_guard_event(guard, event)
    adapter._active_sessions[adapter_key] = guard
    generation = runner._begin_session_run_generation(runner_key)
    runner._bind_adapter_run_generation_for_source(source, runner_key, generation)
    delivered: list[str] = []

    async def capture(_source, message):
        delivered.append(message)

    runner._send_goal_status_notice = capture
    await runner._defer_goal_status_notice_after_delivery(source, "after")

    assert runner_key != adapter_key
    assert runner_key not in adapter._post_delivery_callbacks
    callback = adapter.pop_post_delivery_callback(
        adapter_key, generation=generation
    )
    assert callback is not None
    await callback()
    assert delivered == ["after"]


@pytest.mark.asyncio
async def test_interrupt_send_result_failure_is_consumed_and_next_action_cleans_up(caplog):
    transport = object.__new__(WebSocketRelayTransport)
    calls: list[str] = []

    async def handler(_session_key, _chat_id, owner_id):
        calls.append(owner_id)
        return True

    async def failed_send(_frame):
        raise ConnectionError("raw socket detail")

    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = failed_send
    base = {
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
    }
    transport._queue_interrupt({
        **base,
        "owner_id": "relay-turn-send-fail-1",
        "action_id": "stop-send-fail-1",
    })
    transport._queue_interrupt({
        **base,
        "owner_id": "relay-turn-send-fail-2",
        "action_id": "stop-send-fail-2",
    })

    for _ in range(100):
        if not getattr(transport, "_interrupt_tasks", set()):
            break
        await asyncio.sleep(0)

    assert calls == ["relay-turn-send-fail-1", "relay-turn-send-fail-2"]
    assert transport._interrupt_tasks == set()
    assert transport._interrupt_workers == {}
    assert transport._interrupt_queues == {}
    assert "raw socket detail" not in caplog.text


@pytest.mark.asyncio
async def test_interrupt_session_worker_count_has_global_capacity(monkeypatch):
    transport = object.__new__(WebSocketRelayTransport)
    entered = asyncio.Event()
    release = asyncio.Event()
    sent: list[dict] = []

    async def handler(*_args):
        entered.set()
        await release.wait()
        return True

    async def capture(frame):
        sent.append(frame)

    monkeypatch.setattr(ws_transport_module, "_INTERRUPT_MAX_SESSION_WORKERS", 1)
    transport._descriptor = _desc()
    transport._interrupt_inbound_handler = handler
    transport._send = capture
    transport._queue_interrupt({
        "session_key": "agent:main:discord:dm:chanA:userX",
        "chat_id": "chanA",
        "owner_id": "relay-turn-capacity-a",
        "action_id": "stop-capacity-a",
    })
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    transport._queue_interrupt({
        "session_key": "agent:main:discord:dm:chanB:userY",
        "chat_id": "chanB",
        "owner_id": "relay-turn-capacity-b",
        "action_id": "stop-capacity-b",
    })
    for _ in range(100):
        if sent:
            break
        await asyncio.sleep(0)
    assert sent == [{
        "type": "interrupt_result",
        "action_id": "stop-capacity-b",
        "accepted": False,
        "reason": "interrupt_busy",
    }]
    release.set()
    await transport._cancel_interrupt_tasks()
