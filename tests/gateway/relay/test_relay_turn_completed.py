"""Owner-bound Relay turn completion lifecycle contract."""

from __future__ import annotations

import asyncio

import pytest

import gateway.platforms.base as platform_base
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.session import SessionSource, build_session_key
from tests.gateway.relay.stub_connector import StubConnector


OWNER_CAPABILITY = "owner-bound-interrupt-ack"
COMPLETION_CAPABILITY = "owner-bound-turn-completion"
RECONCILIATION_CAPABILITY = "owner-bound-turn-reconciliation"
OWNER_1 = "relay-turn-00000000-0000-4000-8000-000000000001"
OWNER_2 = "relay-turn-00000000-0000-4000-8000-000000000002"


def _descriptor() -> CapabilityDescriptor:
    return CapabilityDescriptor(
        contract_version=CONTRACT_VERSION,
        platform="relay",
        label="Mission Control",
        max_message_length=4096,
        supports_draft_streaming=False,
        supports_edit=True,
        supports_threads=False,
        markdown_dialect="plain",
        len_unit="chars",
        supported_ops=("send", "edit", "typing", "prompt"),
        capabilities=(
            OWNER_CAPABILITY,
            COMPLETION_CAPABILITY,
            RECONCILIATION_CAPABILITY,
        ),
    )


def _event(owner_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        message_id=f"message-{owner_id[-1]}",
        owner_id=owner_id,
        source=SessionSource(
            platform=Platform.RELAY,
            chat_id="mission-control",
            chat_type="dm",
            user_id="mission-control-owner",
        ),
    )


@pytest.mark.asyncio
async def test_completion_is_after_final_projection_and_full_handler_unwind() -> None:
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    progress_projected = asyncio.Event()
    release_turn = asyncio.Event()
    order: list[str] = []

    original_send_outbound = stub.send_outbound
    original_completion = stub.send_turn_completed

    async def record_outbound(action, *, platform=None):
        order.append(f"outbound:{action.get('op')}:{action.get('content')}")
        return await original_send_outbound(action, platform=platform)

    stub.send_outbound = record_outbound  # type: ignore[method-assign]

    async def record_completion(session_key, chat_id, owner_id, outcome, *handoff):
        order.append(f"completed:{owner_id}")
        await original_completion(session_key, chat_id, owner_id, outcome, *handoff)

    stub.send_turn_completed = record_completion  # type: ignore[method-assign]

    async def handler(event):
        order.append("handler:start")
        await adapter.send(event.source.chat_id, "tool progress")
        await adapter._send_prompt(
            event.source.chat_id,
            prompt_kind="clarify",
            text="approval still pending",
            prompt_id="prompt-1",
            options=[{"id": "yes", "label": "Yes"}],
        )
        progress_projected.set()
        await release_turn.wait()
        order.append("handler:unwound")
        return "final answer"

    adapter.set_message_handler(handler)
    event = _event(OWNER_1, "run tools")
    session_key = build_session_key(event.source)
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task

    await progress_projected.wait()
    assert stub.turn_completions == []
    release_turn.set()
    await task

    assert stub.turn_completions == [
        {
            "session_key": session_key,
            "chat_id": "mission-control",
            "owner_id": OWNER_1,
            "outcome": "completed",
        }
    ]
    assert order.index("handler:unwound") < order.index("outbound:send:final answer")
    assert order.index("outbound:send:final answer") < order.index(
        f"completed:{OWNER_1}"
    )
    assert stub.sent[-1]["content"] == "final answer"


@pytest.mark.asyncio
async def test_completion_follows_generation_owned_post_delivery_callback() -> None:
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    order: list[str] = []
    original_send_outbound = stub.send_outbound
    original_completion = stub.send_turn_completed

    async def record_outbound(action, *, platform=None):
        if action.get("op") == "send":
            order.append(f"send:{action.get('content')}")
        return await original_send_outbound(action, platform=platform)

    async def record_completion(*args, **kwargs):
        order.append("completion")
        return await original_completion(*args, **kwargs)

    stub.send_outbound = record_outbound  # type: ignore[method-assign]
    stub.send_turn_completed = record_completion  # type: ignore[method-assign]
    event = _event(OWNER_1, "callback ordering")
    session_key = build_session_key(event.source)

    async def handler(_event):
        return "final answer"

    async def post_delivery():
        await adapter.send("mission-control", "post-delivery notice")

    adapter.set_message_handler(handler)
    adapter.register_post_delivery_callback(session_key, post_delivery)
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task
    await task

    assert order.index("send:final answer") < order.index("send:post-delivery notice")
    assert order.index("send:post-delivery notice") < order.index("completion")


@pytest.mark.asyncio
async def test_cancel_during_post_delivery_callback_still_completes_and_drains_handoff() -> (
    None
):
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    callback_started = asyncio.Event()
    callback_cancelled = asyncio.Event()
    release_callback_cleanup = asyncio.Event()
    callback_cleaned = asyncio.Event()
    second_started = asyncio.Event()
    callback_task: asyncio.Task | None = None

    async def handler(event):
        if event.owner_id == OWNER_2:
            second_started.set()
        return None

    async def slow_post_delivery():
        nonlocal callback_task
        callback_task = asyncio.current_task()
        callback_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            callback_cancelled.set()
            await release_callback_cleanup.wait()
            raise
        finally:
            callback_cleaned.set()

    adapter.set_message_handler(handler)
    first = _event(OWNER_1, "cancel during callback")
    second = _event(OWNER_2, "queued handoff survives")
    session_key = build_session_key(first.source)
    second.metadata.update(
        {
            "relay_delivery_id": "delivery-owner-2",
            "relay_session_key": session_key,
            "relay_chat_id": "mission-control",
        }
    )
    adapter._post_delivery_callbacks[session_key] = slow_post_delivery
    task = asyncio.create_task(adapter._process_message_background(first, session_key))
    adapter._session_tasks[session_key] = task

    await asyncio.wait_for(callback_started.wait(), timeout=0.5)
    adapter._pending_messages[session_key] = second
    started_at = asyncio.get_running_loop().time()
    task.cancel()
    await asyncio.wait_for(callback_cancelled.wait(), timeout=0.5)
    task.cancel()  # A repeated outer cancel must still stop at the safe boundary.
    release_callback_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=1.0)
    await asyncio.wait_for(second_started.wait(), timeout=0.5)
    while session_key in adapter._session_tasks:
        await asyncio.sleep(0)

    assert asyncio.get_running_loop().time() - started_at < 1.0
    assert callback_cleaned.is_set()
    assert callback_task is not None and callback_task.done()
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks
    assert [item["owner_id"] for item in stub.turn_completions] == [OWNER_1, OWNER_2]
    assert stub.turn_completions[0] == {
        "session_key": session_key,
        "chat_id": "mission-control",
        "owner_id": OWNER_1,
        "outcome": "failed",
        "next_owner_id": OWNER_2,
        "next_delivery_id": "delivery-owner-2",
    }


@pytest.mark.asyncio
async def test_post_delivery_callback_timeout_reaps_task_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_base, "_POST_DELIVERY_CALLBACK_TIMEOUT_SECONDS", 0.01)
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    callback_cleaned = asyncio.Event()
    callback_task: asyncio.Task | None = None

    async def handler(_event):
        return None

    async def timed_out_post_delivery():
        nonlocal callback_task
        callback_task = asyncio.current_task()
        try:
            await asyncio.Future()
        finally:
            callback_cleaned.set()

    adapter.set_message_handler(handler)
    event = _event(OWNER_1, "callback timeout")
    session_key = build_session_key(event.source)
    adapter._post_delivery_callbacks[session_key] = timed_out_post_delivery
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task

    await asyncio.wait_for(task, timeout=0.5)

    assert callback_cleaned.is_set()
    assert callback_task is not None and callback_task.done()
    assert stub.turn_completions[0]["outcome"] == "completed"
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_expected_cancel_during_post_delivery_callback_projects_cancelled() -> (
    None
):
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    callback_started = asyncio.Event()
    callback_cleaned = asyncio.Event()

    async def handler(_event):
        return None

    async def slow_post_delivery():
        callback_started.set()
        try:
            await asyncio.Future()
        finally:
            callback_cleaned.set()

    adapter.set_message_handler(handler)
    event = _event(OWNER_1, "expected callback cancel")
    session_key = build_session_key(event.source)
    adapter._post_delivery_callbacks[session_key] = slow_post_delivery
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task
    adapter._expected_cancelled_tasks.add(task)

    await asyncio.wait_for(callback_started.wait(), timeout=0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=0.5)

    assert callback_cleaned.is_set()
    assert stub.turn_completions[0]["outcome"] == "cancelled"
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("processing_outcome", "wire_outcome"),
    [
        (ProcessingOutcome.FAILURE, "failed"),
        (ProcessingOutcome.CANCELLED, "cancelled"),
    ],
)
async def test_terminal_outcome_is_projected_as_safe_enum(
    processing_outcome: ProcessingOutcome,
    wire_outcome: str,
) -> None:
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()

    await adapter.on_processing_complete(
        _event(OWNER_1, "terminal"), processing_outcome
    )

    assert stub.turn_completions == [
        {
            "session_key": "agent:main:relay:dm:mission-control",
            "chat_id": "mission-control",
            "owner_id": OWNER_1,
            "outcome": wire_outcome,
        }
    ]


@pytest.mark.asyncio
async def test_failed_turn_completion_follows_final_error_projection() -> None:
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    order: list[str] = []
    original_send_outbound = stub.send_outbound
    original_completion = stub.send_turn_completed

    async def record_outbound(action, *, platform=None):
        order.append(f"outbound:{action.get('content')}")
        return await original_send_outbound(action, platform=platform)

    async def record_completion(session_key, chat_id, owner_id, outcome, *handoff):
        order.append(f"completed:{outcome}")
        await original_completion(session_key, chat_id, owner_id, outcome, *handoff)

    stub.send_outbound = record_outbound  # type: ignore[method-assign]
    stub.send_turn_completed = record_completion  # type: ignore[method-assign]

    async def failing_handler(_event):
        raise RuntimeError("expected terminal failure")

    adapter.set_message_handler(failing_handler)
    event = _event(OWNER_1, "fail")
    session_key = build_session_key(event.source)
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task
    await task

    assert "expected terminal failure" in order[0]
    assert order[-1] == "completed:failed"
    assert stub.turn_completions[0]["outcome"] == "failed"


@pytest.mark.asyncio
async def test_cancel_during_failure_projection_cannot_skip_terminal_completion() -> (
    None
):
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    failure_projection_started = asyncio.Event()

    async def failing_handler(_event):
        raise RuntimeError("expected terminal failure")

    async def blocked_failure_projection(*_args, **_kwargs):
        failure_projection_started.set()
        await asyncio.Future()

    adapter.set_message_handler(failing_handler)
    adapter.send = blocked_failure_projection  # type: ignore[method-assign]
    event = _event("opaque-owner/cancel-1", "fail then cancel")
    session_key = build_session_key(event.source)
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task

    await failure_projection_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stub.turn_completions == [
        {
            "session_key": session_key,
            "chat_id": "mission-control",
            "owner_id": "opaque-owner/cancel-1",
            "outcome": "failed",
        }
    ]


@pytest.mark.asyncio
async def test_second_cancel_during_terminal_hook_still_drains_handoff_before_propagating() -> (
    None
):
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    failure_projection_started = asyncio.Event()
    terminal_hook_started = asyncio.Event()
    release_terminal_hook = asyncio.Event()
    second_started = asyncio.Event()

    async def handler(event):
        if event.owner_id == OWNER_1:
            raise RuntimeError("fail before cancellation")
        second_started.set()
        return None

    async def blocked_failure_projection(*_args, **_kwargs):
        failure_projection_started.set()
        await asyncio.Future()

    original_complete = adapter.on_processing_complete

    async def blocked_complete(event, outcome):
        if event.owner_id == OWNER_1:
            terminal_hook_started.set()
            await release_terminal_hook.wait()
        await original_complete(event, outcome)

    adapter.set_message_handler(handler)
    adapter.send = blocked_failure_projection  # type: ignore[method-assign]
    adapter.on_processing_complete = blocked_complete  # type: ignore[method-assign]
    first = _event(OWNER_1, "fail then cancel twice")
    second = _event(OWNER_2, "must still drain")
    session_key = build_session_key(first.source)
    second.metadata.update({
        "relay_delivery_id": "delivery-owner-2",
        "relay_session_key": session_key,
        "relay_chat_id": "mission-control",
    })
    task = asyncio.create_task(adapter._process_message_background(first, session_key))
    adapter._session_tasks[session_key] = task

    await failure_projection_started.wait()
    task.cancel()
    await terminal_hook_started.wait()
    adapter._pending_messages[session_key] = second
    task.cancel()
    release_terminal_hook.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(second_started.wait(), timeout=0.5)
    assert adapter._session_tasks[session_key] is not task


@pytest.mark.asyncio
async def test_internally_cancelled_terminal_hook_cannot_skip_guard_cleanup() -> None:
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()

    async def handler(_event):
        return "done"

    async def internally_cancelled(_event, _outcome):
        raise asyncio.CancelledError

    adapter.set_message_handler(handler)
    adapter.on_processing_complete = internally_cancelled  # type: ignore[method-assign]
    event = _event(OWNER_1, "internal hook cancel")
    session_key = build_session_key(event.source)
    task = asyncio.create_task(adapter._process_message_background(event, session_key))
    adapter._session_tasks[session_key] = task

    await task
    assert session_key not in adapter._active_sessions
    assert session_key not in adapter._session_tasks


@pytest.mark.asyncio
async def test_queued_handoff_completes_owner_n_before_owner_n_plus_one_binds() -> None:
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    order: list[str] = []

    original_completion = stub.send_turn_completed

    async def record_completion(session_key, chat_id, owner_id, outcome, *handoff):
        order.append(f"completed:{owner_id}")
        await original_completion(session_key, chat_id, owner_id, outcome, *handoff)

    stub.send_turn_completed = record_completion  # type: ignore[method-assign]

    async def handler(event):
        order.append(f"started:{event.owner_id}")
        if event.owner_id == OWNER_1:
            first_started.set()
            await release_first.wait()
        else:
            second_started.set()
        return f"final:{event.owner_id}"

    adapter.set_message_handler(handler)
    first = _event(OWNER_1, "first")
    second = _event(OWNER_2, "second")
    second.metadata.update({
        "relay_delivery_id": "delivery-owner-2",
        "relay_session_key": build_session_key(second.source),
        "relay_chat_id": "mission-control",
    })
    session_key = build_session_key(first.source)
    task = asyncio.create_task(adapter._process_message_background(first, session_key))
    adapter._session_tasks[session_key] = task
    await first_started.wait()
    adapter._pending_messages[session_key] = second
    release_first.set()
    await task
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    while len(stub.turn_completions) < 2:
        await asyncio.sleep(0)

    assert order.index(f"completed:{OWNER_1}") < order.index(f"started:{OWNER_2}")
    assert [item["owner_id"] for item in stub.turn_completions] == [OWNER_1, OWNER_2]
    assert stub.turn_completions[0]["next_owner_id"] == OWNER_2
    assert stub.turn_completions[0]["next_delivery_id"] == "delivery-owner-2"


@pytest.mark.asyncio
async def test_active_turn_bypass_command_is_absorbed_by_the_real_owner_without_phantom_owner() -> None:
    """A relay /reset is control traffic for A, never a short-lived owner B."""
    stub = StubConnector(_descriptor())
    adapter = RelayAdapter(
        PlatformConfig(typing_indicator=False), _descriptor(), transport=stub
    )
    await adapter.connect()
    first_started = asyncio.Event()

    async def handler(event):
        if event.owner_id == OWNER_1:
            first_started.set()
            await asyncio.Event().wait()
        return "Reset confirmed"

    adapter.set_message_handler(handler)
    first = _event(OWNER_1, "long-running turn")
    session_key = build_session_key(first.source)
    await adapter.handle_message(first)
    await asyncio.wait_for(first_started.wait(), timeout=0.5)

    command = _event(OWNER_2, "/reset")
    result = await asyncio.wait_for(adapter._on_inbound(command), timeout=0.5)

    assert result == {
        "disposition": "absorbed",
        "canonical_turn_owner_id": OWNER_1,
        "session_key": session_key,
        "chat_id": "mission-control",
        "reason": None,
    }
    assert stub.turn_starts == [OWNER_1]
    for _ in range(50):
        if stub.turn_completions:
            break
        await asyncio.sleep(0)
    assert [completion["owner_id"] for completion in stub.turn_completions] == [OWNER_1]
