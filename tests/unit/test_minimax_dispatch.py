"""Offline dispatch regressions through the real MCP owner and model SDK transport."""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
from langchain_core.messages import ToolMessage
from PIL import Image
import pytest
import pytest_asyncio

from artemis.agents.flash.runner import FlashRunner, _TurnRecord
from artemis.agents.validator.action_execution import exec_action
from artemis.agents.validator.execution_loop import _attempt_local_execution, run_validation_loop
from artemis.agents.validator.precondition_pixel import validate_action_precondition_pixel
from artemis.agents.validator.validator import ValidatorNode
from artemis.graph.state import State
from artemis.graph.visibility import StateView
from artemis.llm.reliability import CircuitBreaker, logical_call_scope
from artemis.llm.router import ModelEndpoint, ModelFactory
from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.action_server import build_action_server
from artemis.mcp.action_session import ActionSession
from artemis.mcp.action_types import ActionResult, DispatchState
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.observation import apply_observation_to_state
from artemis.mcp.target_guard import (
    TargetDecision,
    guard_target,
    local_target_verdict,
    snapshot_error,
)
from artemis.services.llm import RobustChatModelWrapper
from artemis.utils.vision import (
    copy_provenance,
    create_snapshot,
    invalidate_snapshot,
    state_version,
)

pytestmark = pytest.mark.asyncio


def endpoint():
    return ModelEndpoint(
        provider="anthropic",
        vendor="minimax",
        model_name="MiniMax-M3.1-Flash",
        api_base="https://dispatch.invalid/anthropic",
        api_key="fixture-not-a-key",
    )


@pytest.fixture(autouse=True)
def isolate_gateway(monkeypatch):
    from artemis.services import llm

    monkeypatch.setattr(llm, "_get_current_data_engine", lambda: None)
    monkeypatch.setattr(llm, "_ENDPOINT_BREAKER", CircuitBreaker(threshold=1000))
    monkeypatch.setattr(llm, "get_google_llm", lambda *a, **kw: pytest.fail("Google fallback"))


class Rig:
    async def start(self):
        image = BytesIO()
        Image.new("RGB", (100, 100), "white").save(image, "PNG")
        self.image = image.getvalue()
        self.b64 = base64.b64encode(self.image).decode()
        self.tree = [
            {
                "text": "Open",
                "resource-id": "open",
                "class": "Button",
                "bounds": "[10,20][90,60]",
                "clickable": True,
                "enabled": True,
            }
        ]
        self.ctx = SimpleNamespace(
            device=SimpleNamespace(
                device_id="fixture", display_id="0", rotation=0, device_width=100, device_height=100
            ),
            _llm_endpoint_snapshot={("default", False, False): endpoint()},
            _screen_state_version=0,
            _current_snapshot=None,
            data_engine=None,
        )
        self.taps = []
        self.capture_metadata = {"rotation": 0, "display_id": "0", "device_id": "fixture"}

        async def capture():
            snap = create_snapshot(self.ctx, self.image, self.capture_metadata, bind=False)
            return SimpleNamespace(
                base64=self.b64, snapshot=snap, width=100, height=100, elements=deepcopy(self.tree)
            )

        async def tap(x, y, **kwargs):
            self.taps.append((x, y))
            return SimpleNamespace(error=None)

        self.controller = SimpleNamespace(
            get_screen_data=capture,
            get_ui_elements=AsyncMock(side_effect=lambda: deepcopy(self.tree)),
            tap_at=tap,
        )
        self.actuator = AdbActuator(self.ctx, self.controller)
        self.session = await ActionSession(build_action_server(self.actuator), self.ctx).start()
        self.ctx.action_session = self.session
        self.state = State(initial_goal="Open the item")
        obs = await self.session.observe(settle_ms=0)
        assert obs.ok and obs.snapshot.capture_valid
        apply_observation_to_state(self.state, obs, self.image)
        return self

    def action(self):
        element = self.state.indexed_elements[0]
        return {
            "action": "tap",
            "coordinates": element["center"],
            "normalized_coordinates": [500, 400],
            "target_text": element["text"],
            "target_resource_id": element.get("resource_id"),
            "target_bounds": element["bounds"],
            **{k: v for k, v in copy_provenance(element).items() if k != "coordinate_space"},
        }


@pytest_asyncio.fixture
async def rig():
    instance = await Rig().start()
    try:
        yield instance
    finally:
        await instance.session.aclose()


def no_model(*args, **kwargs):
    pytest.fail("Strong XML must not call a paid model")


async def test_strong_live_xml_needs_no_model_and_same_owner_dispatch(rig):
    action = rig.action()
    verdict = await guard_target(
        rig.ctx, rig.session, action, rig.state, rig.b64, get_llm_fn=no_model
    )
    assert verdict.allowed and rig.controller.get_ui_elements.await_count == 1
    outcome = await exec_action(rig.ctx, rig.session, action)
    assert outcome == (True, "") and outcome.dispatch_state is DispatchState.DISPATCHED
    assert rig.taps == [(50, 40)] and state_version(rig.ctx) == 1
    assert rig.ctx._current_snapshot is None


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "version",
        "capture",
        "hash",
        "device",
        "display",
        "rotation",
        "width",
        "incomplete",
    ],
)
async def test_all_capture_identity_fields_are_required(rig, change):
    action = rig.action()
    if change == "missing":
        action.pop("snapshot")
    elif change == "incomplete":
        action["snapshot"].pop("rotation")
    else:
        key = {
            "version": "state_version",
            "capture": "capture_id",
            "hash": "image_hash",
            "device": "device_id",
            "display": "display_id",
        }.get(change, change)
        old = action["snapshot"][key]
        action["snapshot"][key] = old + 1 if isinstance(old, int) else "wrong"
    assert local_target_verdict(rig.ctx, action, rig.state).decision is TargetDecision.BLOCK


@pytest.mark.parametrize(
    "field,value",
    [("visible", False), ("bbox", None), ("status", "ambiguous"), ("uncertain", True)],
)
async def test_invisible_and_uncertain_indexes_never_become_xml_allow(rig, field, value):
    action = rig.action()
    action[field] = value
    assert not local_target_verdict(rig.ctx, action, rig.state).allowed


async def test_live_disabled_node_is_direct_block_without_paid_upgrade(rig):
    rig.tree[0]["enabled"] = False
    verdict = await guard_target(
        rig.ctx, rig.session, rig.action(), rig.state, rig.b64, get_llm_fn=no_model
    )
    assert verdict.decision is TargetDecision.BLOCK and not rig.taps


async def test_parent_center_over_child_is_not_local_allow(rig):
    rig.state.operator_raw_data["xml_hierarchy"][0]["children"] = [
        {"text": "Other", "bounds": "[30,30][70,50]", "clickable": True}
    ]
    assert local_target_verdict(rig.ctx, rig.action(), rig.state).decision is TargetDecision.UNKNOWN


async def test_label_uses_own_intersection_inside_clickable_ancestor(rig):
    label = {
        "text": "Open",
        "resource-id": "open",
        "class": "Button",
        "bounds": "[10,20][90,60]",
        "clickable": False,
    }
    rig.state.operator_raw_data["xml_hierarchy"] = [
        {"bounds": "[0,0][100,100]", "clickable": True, "children": [label]}
    ]
    assert local_target_verdict(rig.ctx, rig.action(), rig.state).allowed


async def test_queued_second_target_rechecked_after_first_dispatch(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.controller.tap_at

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    rig.controller.tap_at = slow
    expected = rig.state.operator_raw_data["snapshot"]
    first = asyncio.create_task(
        rig.session.call("click", {"target": [500, 400]}, expected_snapshot=expected)
    )
    await entered.wait()
    second = asyncio.create_task(
        rig.session.call("click", {"target": [500, 400]}, expected_snapshot=expected)
    )
    release.set()
    one, two = await asyncio.gather(first, second)
    assert one.ok and not two.ok and two.dispatch_state is DispatchState.NOT_DISPATCHED
    assert len(rig.taps) == 1 and state_version(rig.ctx) == 1


async def test_caller_cancellation_does_not_restore_old_target(rig):
    entered, release = asyncio.Event(), asyncio.Event()
    original = rig.controller.tap_at

    async def slow(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    rig.controller.tap_at = slow
    expected = rig.state.operator_raw_data["snapshot"]
    caller = asyncio.create_task(
        rig.session.call("click", {"target": [500, 400]}, expected_snapshot=expected)
    )
    await entered.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert snapshot_error(rig.ctx, expected)
    release.set()
    await rig.session.ui_hierarchy()  # queues behind the actual in-flight command
    result = await rig.session.call("click", {"target": [500, 400]}, expected_snapshot=expected)
    assert not result.ok and len(rig.taps) == 1


async def test_timeout_after_effect_is_once_unknown_and_observed(rig):
    async def timeout(*args, **kwargs):
        rig.taps.append(args)
        raise TimeoutError("ack lost")

    rig.controller.tap_at = timeout
    node = ValidatorNode(rig.ctx)
    rig.state.structured_decisions = json.dumps([rig.action()])
    report = await run_validation_loop(node, rig.state)
    assert report["last_execution_result"]["status"] == "unknown"
    assert len(rig.taps) == 1
    assert rig.ctx._current_snapshot.state_version == 1


async def test_confirmed_not_dispatched_can_retry_bounded(rig):
    attempts = []
    original = rig.actuator.click

    async def reject_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            return ActionResult.failure(
                "click", "not sent", dispatch_state=DispatchState.NOT_DISPATCHED
            )
        return await original(*args, **kwargs)

    rig.actuator.click = reject_once
    node = ValidatorNode(rig.ctx)
    action = rig.action()
    result = await _attempt_local_execution(
        node, rig.session, action, "tap", [], max_local_retries=2
    )
    assert result == (True, "") and len(attempts) == 2 and len(rig.taps) == 1
    assert state_version(rig.ctx) == 1


async def test_pro_burst_runs_gate_and_stops_after_first_change_under_state_view(rig, monkeypatch):
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", no_model)
    rig.state.structured_decisions = json.dumps([rig.action(), rig.action()])
    result = await run_validation_loop(ValidatorNode(rig.ctx), StateView(rig.state, "validator"))
    execution = result["last_execution_result"]["execution"]
    assert len(rig.taps) == 1 and "Skipped" in execution[1]["attempts"][0]
    assert execution[0]["precondition_outcome"]["decision"] == "allow"
    assert rig.state.operator_raw_data["snapshot"]["state_version"] == 1


async def test_flash_index_keeps_provenance_and_stale_index_never_remaps(rig, monkeypatch):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    old = rig.state.indexed_elements
    first = await executor.execute("click", {"target": 1}, "one", rig.state, index_elements=old)
    second = await executor.execute("click", {"target": 1}, "two", rig.state, index_elements=old)
    assert first.status == "success" and second.status == "error" and len(rig.taps) == 1
    assert first.metadata["target_semantics"]["snapshot_id"] == old[0]["snapshot_id"]


async def test_flash_multi_tool_turn_cannot_upgrade_via_explorer_after_change(rig, monkeypatch):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    runner = object.__new__(FlashRunner)
    runner.ctx = rig.ctx
    runner.executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    runner.summarizer = None
    runner._llm = None
    runner._capture_post_screenshot = AsyncMock(return_value=None)
    runner._build_action_record = lambda *args: {"action": "click"}
    runner._finalize_task_report = AsyncMock()
    messages = []
    calls = [
        {"name": "click", "args": {"target": 1}, "id": "one"},
        {"name": "ask_explorer", "args": {}, "id": "helper"},
        {
            "name": "click",
            "args": {"target": [500, 400], "target_description": "Open"},
            "id": "two",
        },
    ]
    await runner._process_tool_calls(
        calls, rig.state, messages, "", {}, rig.image, "", 0, _TurnRecord()
    )
    assert len(rig.taps) == 1
    assert len([m for m in messages if isinstance(m, ToolMessage)]) == 3
    assert "stale" in messages[-1].content


async def test_agent_sequence_blocked_but_explicit_user_sequence_and_doubletap_work(rig):
    executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    result = await executor.execute(
        "click_sequence",
        {"sequence": [[500, 400], [600, 400]], "target_descriptions": ["Open", "Next"]},
        "agent",
        rig.state,
    )
    assert result.status == "error" and not rig.taps
    direct = await rig.session.call(
        "click_sequence", {"sequence": [[500, 400], [600, 400]], "delay_ms": 0}
    )
    assert direct.ok and len(rig.taps) == 2
    direct = await rig.session.call("click", {"target": [1000, 1000], "times": 2})
    assert direct.ok and rig.taps[-1] == (99, 99)


async def test_public_rounding_uses_shared_contract(rig):
    rig.ctx.device.device_width = 101
    rig.ctx.device.device_height = 201
    assert rig.actuator._to_px(555, 555) == [56, 112]
    assert rig.actuator._to_px(1000, 1000) == [100, 200]


async def sdk_wrapper(output, status=200):
    requests = []

    async def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "fixture rejected"},
                },
            )
        if payload.get("stream"):
            events = [
                (
                    "message_start",
                    {
                        "message": {
                            "id": "fixture",
                            "type": "message",
                            "role": "assistant",
                            "model": "MiniMax-M3.1-Flash",
                            "content": [],
                            "stop_reason": None,
                            "stop_sequence": None,
                            "usage": {"input_tokens": 8, "output_tokens": 0},
                        }
                    },
                ),
                (
                    "content_block_start",
                    {"index": 0, "content_block": {"type": "text", "text": ""}},
                ),
                (
                    "content_block_delta",
                    {"index": 0, "delta": {"type": "text_delta", "text": json.dumps(output)}},
                ),
                ("content_block_stop", {"index": 0}),
                (
                    "message_delta",
                    {
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 5},
                    },
                ),
                ("message_stop", {}),
            ]
            content = "".join(
                f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"
                for kind, data in events
            )
            return httpx.Response(
                200, content=content, headers={"content-type": "text/event-stream"}
            )
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "type": "message",
                "role": "assistant",
                "model": "MiniMax-M3.1-Flash",
                "content": [{"type": "text", "text": json.dumps(output)}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 8, "output_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sdk = anthropic.AsyncAnthropic(
        api_key="fixture-not-a-key",
        base_url="https://dispatch.invalid/anthropic",
        http_client=client,
        max_retries=0,
    )
    underlying = ModelFactory.create_model(endpoint())
    underlying._async_client = sdk
    return RobustChatModelWrapper(underlying, endpoint=endpoint()), requests, sdk


@pytest.mark.parametrize(
    "output,allowed",
    [
        ({}, False),
        (
            {"decision": "allow", "is_present": True, "confidence": 0.2, "reasoning": "uncertain"},
            False,
        ),
        (
            {"decision": "allow", "is_present": False, "confidence": 0.2, "reasoning": "uncertain"},
            False,
        ),
        (
            {"decision": "block", "is_present": False, "confidence": 0.99, "reasoning": "covered"},
            False,
        ),
        (
            {"decision": "unknown", "is_present": True, "confidence": 0.9, "reasoning": "unsure"},
            False,
        ),
        (
            {"decision": "allow", "is_present": "true", "confidence": 1.0, "reasoning": "present"},
            False,
        ),
        (
            {"decision": "allow", "is_present": True, "confidence": 1.0, "reasoning": "present"},
            True,
        ),
    ],
)
async def test_strict_pixel_real_sdk_schema_denies_missing_low_confidence_and_bad_types(
    rig, output, allowed
):
    wrapped, requests, sdk = await sdk_wrapper(output)
    action = rig.action()
    action["source"] = "minimax"
    try:
        with logical_call_scope(max_attempts=2):
            passed, _, reason = await validate_action_precondition_pixel(
                rig.ctx,
                rig.session,
                action,
                rig.b64,
                state=rig.state,
                get_llm_fn=lambda *a, **kw: wrapped,
            )
        assert passed is allowed and 1 <= len(requests) <= 2
        assert reason or passed
        assert requests[0]["messages"]  # actual SDK payload, no hand-written parser seam
    finally:
        await sdk.close()


async def test_pixel_auth_permanent_failure_is_one_request_no_fallback(rig):
    wrapped, requests, sdk = await sdk_wrapper({}, status=401)
    calls = []

    def getter(*args, **kwargs):
        calls.append(kwargs.get("name"))
        return wrapped

    action = rig.action()
    try:
        passed, _, reason = await validate_action_precondition_pixel(
            rig.ctx, rig.session, action, rig.b64, state=rig.state, get_llm_fn=getter
        )
        assert not passed and len(requests) == 1 and len(calls) == 1
        assert action["precondition_outcome"]["failure_category"] == "authentication"
        assert "LLMPermanentError" in reason
    finally:
        await sdk.close()


@pytest.mark.parametrize("source", ["ocr", "minimax"])
async def test_certain_partial_ocr_and_model_targets_require_pixel_before_dispatch(rig, source):
    action = rig.action()
    action.update(source=source, ocr_status="partial", uncertain=False)
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "visible control",
        }
    )
    try:
        verdict = await guard_target(
            rig.ctx, rig.session, action, rig.state, rig.b64, get_llm_fn=lambda *a, **kw: wrapped
        )
        assert verdict.allowed and len(requests) == 1
        await exec_action(rig.ctx, rig.session, action)
        assert len(rig.taps) == 1
    finally:
        await sdk.close()


@pytest.mark.parametrize("failure", ["crop", "capture", "rotation", "display", "device", "size"])
async def test_bad_verification_capture_or_crop_never_calls_model(rig, monkeypatch, failure):
    if failure == "crop":
        monkeypatch.setattr(
            "artemis.utils.visualization.crop_and_annotate_target",
            lambda *a, **kw: (_ for _ in ()).throw(ValueError("crop failed")),
        )
    elif failure == "capture":
        rig.controller.get_screen_data = AsyncMock(side_effect=TimeoutError("capture"))
    else:
        key = {"device": "device_id", "display": "display_id"}.get(failure, failure)
        if failure == "size":
            buf = BytesIO()
            Image.new("RGB", (200, 100)).save(buf, "PNG")
            rig.image = buf.getvalue()
            rig.b64 = base64.b64encode(rig.image).decode()
        else:
            rig.capture_metadata[key] = 1 if key == "rotation" else "other"
    action = rig.action()
    passed, _, _ = await validate_action_precondition_pixel(
        rig.ctx,
        rig.session,
        action,
        rig.state.operator_raw_data["screenshot_b64"],
        state=rig.state,
        get_llm_fn=no_model,
    )
    assert not passed and not rig.taps


async def test_nonchanging_wait_keeps_original_targets_valid(rig):
    snapshot = rig.state.operator_raw_data["snapshot"]
    result = await rig.session.call("wait_for_delay", {"time_in_ms": 0})
    assert result.ok and snapshot_error(rig.ctx, snapshot) is None
    assert state_version(rig.ctx) == 0


async def test_concurrent_lazy_get_session_has_one_owner(rig):
    from artemis.mcp.action_session import get_action_session

    await rig.session.aclose()
    rig.ctx.action_session = None
    first, second = await asyncio.gather(
        get_action_session(rig.ctx, rig.actuator), get_action_session(rig.ctx, rig.actuator)
    )
    try:
        assert first is second and first._owner is second._owner
        assert first.started
    finally:
        await first.aclose()
    assert not second.started


class RawProcess:
    def __init__(self):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.returncode = None
        self.pid = 123
        self.stdout = SimpleNamespace(readline=self.readline)
        self.stdin = SimpleNamespace(write=lambda data: None, drain=AsyncMock())

    async def readline(self):
        self.started.set()
        await self.release.wait()
        return b""

    async def wait(self):
        await self.release.wait()
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    async def communicate(self):
        self.started.set()
        await self.wait()
        return b"fixture", b""

    def terminate(self):
        self.returncode = -15
        self.release.set()

    def kill(self):
        self.returncode = -9
        self.release.set()


async def fresh(rig):
    obs = await rig.session.observe(settle_ms=0)
    apply_observation_to_state(rig.state, obs, rig.image)
    return rig.state.operator_raw_data["snapshot"]


@pytest.mark.parametrize("short", [False, True])
async def test_raw_foreground_and_diagnostic_alias_block_new_capture_until_real_exit(
    rig, monkeypatch, short
):
    from artemis.tools.command_tool import RunAdbCommandTool, RunShortAdbCommandTool

    process = RawProcess()
    monkeypatch.setattr(
        "artemis.tools.command_tool.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )
    tool = RunShortAdbCommandTool() if short else RunAdbCommandTool()
    operation = asyncio.create_task(tool.execute(ctx=rig.ctx, CommandLine="opaque fixture"))
    await process.started.wait()
    expected = await fresh(rig)
    assert "active" in snapshot_error(rig.ctx, expected)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert snapshot_error(rig.ctx, expected)
    process.release.set()
    # The shielded tool owns completion even after its caller leaves.
    for _ in range(100):
        if rig.ctx._raw_adb_active == 0:
            break
        await asyncio.sleep(0.001)
    assert rig.ctx._raw_adb_active == 0
    assert snapshot_error(rig.ctx, expected)  # end invalidates captures made during script
    assert snapshot_error(rig.ctx, await fresh(rig)) is None


async def test_raw_background_send_input_and_kill_barrier_clears_only_after_exit(rig, monkeypatch):
    from artemis.tools.command_tool import RunAdbCommandTool, ManageTaskTool, get_adb_task_registry

    process = RawProcess()
    monkeypatch.setattr(
        "artemis.tools.command_tool.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )
    result = await RunAdbCommandTool().execute(
        ctx=rig.ctx, CommandLine="opaque fixture", Interactive=True, WaitMsBeforeAsync=1
    )
    assert "background" in result
    registry = get_adb_task_registry(rig.ctx)
    task_id = next(iter(registry.background))
    expected = await fresh(rig)
    assert snapshot_error(rig.ctx, expected)
    before = state_version(rig.ctx)
    await ManageTaskTool().execute(
        ctx=rig.ctx, Action="send_input", TaskId=task_id, Input="opaque input"
    )
    assert state_version(rig.ctx) == before + 1
    await ManageTaskTool().execute(ctx=rig.ctx, Action="kill", TaskId=task_id)
    assert not registry.background and process.returncode is not None
    assert snapshot_error(rig.ctx, expected)
    assert snapshot_error(rig.ctx, await fresh(rig)) is None


async def test_cloud_thread_timeout_keeps_barrier_until_thread_really_finishes(rig, monkeypatch):
    import threading
    from artemis.tools.command_tool import RunAdbCommandTool

    entered, release = threading.Event(), threading.Event()

    def shell(script):
        entered.set()
        release.wait(2)
        return "done"

    monkeypatch.setenv("ARTEMIS_CLOUD_MODE", "1")
    monkeypatch.setattr(
        "artemis.tools.command_tool.get_adb_device", lambda ctx: SimpleNamespace(shell=shell)
    )
    result = await RunAdbCommandTool().execute(
        ctx=rig.ctx, CommandLine="opaque fixture", WaitMsBeforeAsync=1
    )
    assert "did not finish" in result and entered.is_set()
    expected = await fresh(rig)
    assert snapshot_error(rig.ctx, expected)
    release.set()
    for _ in range(100):
        if rig.ctx._raw_adb_active == 0:
            break
        await asyncio.sleep(0.001)
    assert rig.ctx._raw_adb_active == 0 and snapshot_error(rig.ctx, expected)
    assert snapshot_error(rig.ctx, await fresh(rig)) is None


async def test_missing_index_provenance_cannot_be_pixel_upgraded(rig):
    action = rig.action()
    action.pop("source")
    verdict = await guard_target(
        rig.ctx, rig.session, action, rig.state, rig.b64, get_llm_fn=no_model
    )
    assert verdict.decision is TargetDecision.BLOCK


async def test_model_cannot_self_grant_xml_evidence(rig):
    action = rig.action()
    action.update(source="minimax", evidence="xml_actionable", actionable=True)
    assert local_target_verdict(rig.ctx, action, rig.state).decision is TargetDecision.UNKNOWN


async def test_background_failed_kill_keeps_barrier_then_real_exit_clears_it(rig, monkeypatch):
    from artemis.tools.command_tool import RunAdbCommandTool, ManageTaskTool, get_adb_task_registry

    process = RawProcess()
    process.terminate = lambda: None
    process.kill = lambda: None
    monkeypatch.setattr("artemis.tools.command_tool._TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        "artemis.tools.command_tool.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )
    await RunAdbCommandTool().execute(
        ctx=rig.ctx, CommandLine="opaque fixture", WaitMsBeforeAsync=1
    )
    registry = get_adb_task_registry(rig.ctx)
    task_id = next(iter(registry.background))
    result = await ManageTaskTool().execute(ctx=rig.ctx, Action="kill", TaskId=task_id)
    assert "unknown" in result and task_id in registry.background
    assert snapshot_error(rig.ctx, await fresh(rig))
    process.release.set()
    await registry.background[task_id].listener_task
    assert not registry.background
    assert snapshot_error(rig.ctx, await fresh(rig)) is None


async def test_short_diagnostic_unconfirmed_termination_uses_existing_registry(rig, monkeypatch):
    from artemis.tools.command_tool import RunShortAdbCommandTool, get_adb_task_registry

    process = RawProcess()
    process.terminate = lambda: None
    process.kill = lambda: None
    monkeypatch.setattr("artemis.tools.command_tool._SHORT_COMMAND_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("artemis.tools.command_tool._TERMINATE_GRACE_SECONDS", 0.01)
    monkeypatch.setattr(
        "artemis.tools.command_tool.asyncio.create_subprocess_exec", AsyncMock(return_value=process)
    )
    result = await RunShortAdbCommandTool().execute(ctx=rig.ctx, CommandLine="opaque fixture")
    assert "timed out" in result
    registry = get_adb_task_registry(rig.ctx)
    task = next(iter(registry.background.values()))
    assert snapshot_error(rig.ctx, await fresh(rig))
    process.release.set()
    await task.listener_task
    assert not registry.background and snapshot_error(rig.ctx, await fresh(rig)) is None


async def test_flash_wait_does_not_replace_current_target_capture(rig):
    executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    before = rig.ctx._current_snapshot
    result = await executor.execute("wait_for_delay", {"time_in_ms": 0}, "wait", rig.state)
    assert result.status == "success" and rig.ctx._current_snapshot == before


async def test_queued_cancelled_target_is_not_sent_and_does_not_invalidate(rig):
    entered, release = asyncio.Event(), asyncio.Event()

    async def hierarchy():
        entered.set()
        await release.wait()
        return []

    rig.controller.get_ui_elements = hierarchy
    owner_busy = asyncio.create_task(rig.session.ui_hierarchy())
    await entered.wait()
    command = asyncio.create_task(
        rig.session.call(
            "click",
            {"target": [500, 400]},
            expected_snapshot=rig.state.operator_raw_data["snapshot"],
        )
    )
    await asyncio.sleep(0)
    command.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command
    release.set()
    await owner_busy
    await rig.session.ui_hierarchy()
    assert not rig.taps and state_version(rig.ctx) == 0


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize("source", ["ocr", "minimax"])
async def test_actual_agent_profiles_gate_non_xml_targets(rig, monkeypatch, profile, source):
    element = rig.state.indexed_elements[0]
    element.update(source=source, uncertain=False, ocr_status="partial")
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "block",
            "is_present": False,
            "confidence": 0.99,
            "reasoning": "target covered",
        }
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    try:
        if profile == "pro":
            rig.state.structured_decisions = json.dumps([rig.action(), rig.action()])
            report = await run_validation_loop(ValidatorNode(rig.ctx), rig.state)
            assert report["last_execution_result"]["status"] == "failed"
            assert (
                report["last_execution_result"]["execution"][0]["precondition_outcome"]["decision"]
                == "block"
            )
        else:
            executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
            result = await executor.execute("click", {"target": 1}, "fixture", rig.state)
            assert (
                result.status == "error"
                and result.metadata["target_semantics"]["precondition_outcome"]["decision"]
                == "block"
            )
        assert len(requests) == 1 and not rig.taps
    finally:
        await sdk.close()


async def test_flash_timeout_is_once_unknown_and_refreshes_provenance(rig, monkeypatch):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)

    async def timeout(*args, **kwargs):
        rig.taps.append(args)
        raise TimeoutError("ack lost")

    rig.controller.tap_at = timeout
    result = await McpActionExecutor(rig.ctx, actuator=rig.actuator).execute(
        "click", {"target": 1}, "fixture", rig.state
    )
    assert result.status == "error" and result.metadata["dispatch_state"] == "unknown"
    assert len(rig.taps) == 1 and rig.state.operator_raw_data["snapshot"]["state_version"] == 1


async def test_minimax_app_launch_uses_only_one_existing_helper_attempt(rig, monkeypatch):
    monkeypatch.setattr(
        "artemis.tools.mobile.launch_app.find_package", lambda *a, **kw: "fixture.app"
    )
    launch = AsyncMock(return_value=(False, "ack unknown"))
    monkeypatch.setattr(
        "third_party.mobile_use.utils.app_launch_utils.launch_app_with_retries", launch
    )
    result = await rig.session.call("manage_app", {"action": "launch", "app_name": "Fixture"})
    assert not result.ok and result.dispatch_state is DispatchState.UNKNOWN
    launch.assert_awaited_once_with(rig.ctx, "fixture.app", max_retries=1)


async def test_reference_same_dimensions_wrong_hash_cannot_be_pixel_upgraded(rig):
    stream = BytesIO()
    Image.new("RGB", (100, 100), "black").save(stream, "PNG")
    wrong_reference = base64.b64encode(stream.getvalue()).decode()
    passed, _, reason = await validate_action_precondition_pixel(
        rig.ctx, rig.session, rig.action(), wrong_reference, state=rig.state, get_llm_fn=no_model
    )
    assert not passed and "does not match" in reason


async def test_minimax_pixel_initialization_error_never_switches_role(rig):
    names = []

    def broken(ctx, name):
        names.append(name)
        raise ValueError("permanent invalid endpoint")

    action = rig.action()
    result = await validate_action_precondition_pixel(
        rig.ctx, rig.session, action, rig.b64, state=rig.state, get_llm_fn=broken
    )
    assert not result[0] and names == ["validator_pixel_safety_net"]
    assert action["precondition_outcome"]["error_type"] == "ValueError"


async def test_actual_pro_shell_wrapper_bindings_reject_scripts_and_input_without_driver(
    rig, monkeypatch
):
    from artemis.core.tool_failure import is_tool_failure
    from artemis.graph.graph import run_adb_command_wrapper, manage_task_wrapper
    from artemis.tools.index import get_tools_from_wrappers

    monkeypatch.setattr(
        "artemis.tools.base.get_driver",
        lambda *a: pytest.fail("Agent raw binding resolved a driver"),
    )
    monkeypatch.setattr(
        "artemis.tools.command_tool.asyncio.create_subprocess_exec",
        AsyncMock(side_effect=AssertionError("script dispatched")),
    )
    monkeypatch.setattr(
        "artemis.tools.command_tool.get_adb_device", lambda *a: pytest.fail("raw bridge created")
    )
    tools = get_tools_from_wrappers(rig.ctx, [run_adb_command_wrapper, manage_task_wrapper])
    by_name = {tool.name: tool for tool in tools}
    result = await by_name["run_adb_command"].ainvoke({"CommandLine": "input tap 50 40"})
    assert is_tool_failure(result) and "Unsupported" in result
    # invoke_tool_with_injection calls the actual coroutine directly; extra
    # model fields still cannot override the program's Agent binding closure.
    result = await by_name["run_adb_command"].coroutine(
        CommandLine="input tap 50 40", origin="user", _agent_origin=False
    )
    assert is_tool_failure(result)
    result = await by_name["manage_task"].ainvoke(
        {"Action": "send_input", "TaskId": "fixture", "Input": "input tap 50 40"}
    )
    assert is_tool_failure(result) and "Unsupported" in result
    assert (
        await by_name["manage_task"].ainvoke({"Action": "list"})
        == "No active background tasks running."
    )
    assert state_version(rig.ctx) == 0 and not rig.taps


async def test_actual_diagnoser_short_shell_alias_is_disabled_for_minimax(rig, monkeypatch):
    from artemis.agents.diagnoser.diagnoser import Diagnoser
    from artemis.core.tool_failure import is_tool_failure

    rig.ctx.execution_setup = SimpleNamespace(video_recording_tools_enabled=False)
    diagnoser = object.__new__(Diagnoser)
    diagnoser.ctx = rig.ctx
    diagnoser.is_device_online = True
    monkeypatch.setattr(
        "artemis.tools.base.get_driver", lambda *a: pytest.fail("Diagnoser raw driver")
    )
    tools = diagnoser._build_traced_tools()
    by_name = {tool.name: tool for tool in tools}
    assert "analyze_logs" in by_name and "get_ui_hierarchy" in by_name
    result = await by_name["run_adb_command"].ainvoke({"CommandLine": "input tap 50 40"})
    assert is_tool_failure(result) and "Unsupported" in result
    assert not rig.taps and state_version(rig.ctx) == 0


async def test_actual_flash_tools_have_no_opaque_script_surface(rig):
    runner = object.__new__(FlashRunner)
    runner.ctx = rig.ctx
    runner._video_tools_enabled = lambda: False
    assert not {"run_adb_command", "manage_task", "run_short_adb_command"} & {
        tool.name for tool in runner._get_tools()
    }
    executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    result = await executor.execute(
        "run_adb_command", {"CommandLine": "input tap 50 40", "origin": "user"}, "model", rig.state
    )
    assert result.status == "error" and not rig.taps


async def test_explicit_user_raw_alias_keeps_direct_contract(rig, monkeypatch):
    from artemis.tools.system.adb_shell import run_adb_command

    process = RawProcess()
    process.release.set()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr("artemis.tools.command_tool.asyncio.create_subprocess_exec", spawn)
    result = await run_adb_command.execute(ctx=rig.ctx, CommandLine="opaque explicit user fixture")
    assert "completed" in result and spawn.await_count == 1
    assert state_version(rig.ctx) == 2


async def execute_agent_profile(rig, profile, name, args):
    """Actual Agent translation followed by the shared dispatch owner."""
    if profile == "flash":
        result = await McpActionExecutor(rig.ctx, actuator=rig.actuator).execute(
            name, args, "regression", rig.state
        )
        return result.status, None
    from artemis.agents.operator.operator import OperatorNode

    operator = object.__new__(OperatorNode)
    operator.ctx = rig.ctx
    actions, error = operator._translate_and_validate_tool_inner(
        {"name": name, "args": args}, rig.state
    )
    if error:
        return "error", error
    rig.state.structured_decisions = json.dumps(actions)
    report = await run_validation_loop(ValidatorNode(rig.ctx), rig.state)
    return report["last_execution_result"]["status"], actions


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize("uncertain", [True, False])
async def test_typed_partial_ocr_uncertain_never_upgrades_but_certain_region_can_act(
    rig, monkeypatch, profile, uncertain
):
    from artemis.utils.visual_ocr import OCRResult, ocr_to_legacy
    from artemis.utils.vision_models import OCRRegion
    from artemis.utils.ocr_xml_fusion import fuse_ocr_with_xml
    from artemis.utils.visualization import format_minimal_list_with_elements
    from artemis.utils.vision import stamp_elements

    snapshot = rig.ctx._current_snapshot
    result = OCRResult(
        status="partial",
        regions=[
            OCRRegion(
                text="�pen" if uncertain else "Open", bbox=[100, 200, 900, 600], uncertain=uncertain
            )
        ],
        snapshot=snapshot,
        image_size=(100, 100),
        cache_key="regression",
    )
    _, elements, _ = format_minimal_list_with_elements(
        fuse_ocr_with_xml([], ocr_to_legacy(result)), 100, 100
    )
    stamp_elements(elements, snapshot)
    rig.state.indexed_elements = elements
    rig.state.operator_raw_data["xml_hierarchy"] = []
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "visible control",
        }
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    try:
        status, _ = await execute_agent_profile(rig, profile, "click", {"target": 1})
        if uncertain:
            assert status in {"error", "failed"} and not rig.taps and not requests
        else:
            assert (
                status in {"success", "dispatched"}
                and rig.taps == [(50, 40)]
                and len(requests) == 1
            )
    finally:
        await sdk.close()


@pytest.mark.parametrize(
    "metadata", [{"uncertain": True}, {"status": "ambiguous"}, {"status": "not_found"}]
)
async def test_direct_pixel_cannot_upgrade_terminal_recognition_unknown(rig, metadata):
    action = rig.action()
    action.update(source="minimax", **metadata)
    passed, _, _ = await validate_action_precondition_pixel(
        rig.ctx, rig.session, action, rig.b64, state=rig.state, get_llm_fn=no_model
    )
    assert not passed and action["precondition_outcome"]["decision"] == "unknown"


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize(
    "endpoint,point",
    [
        ("end", [999999, 400]),
        ("start", [-100, 400]),
        ("end", [True, 400]),
        ("end", [500.5, 400]),
        ("start", [float("nan"), 400]),
        ("end", [float("inf"), 400]),
        ("end", ["500", 400]),
        ("end", [500, 400, 500]),
    ],
)
async def test_all_raw_swipe_endpoints_rejected_before_model_or_device(
    rig, monkeypatch, profile, endpoint, point
):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", no_model)
    swipes = AsyncMock(return_value=None)
    rig.controller.swipe_coords = swipes
    args = {
        "start": [500, 400],
        "end": [600, 400],
        "target_description": "slider knob",
        endpoint: point,
    }
    status, _ = await execute_agent_profile(rig, profile, "swipe", args)
    assert status == "error" and not swipes.await_count and not rig.taps


@pytest.mark.parametrize("profile", ["pro", "flash"])
async def test_valid_targeted_swipe_validates_start_and_dispatches_complete_geometry(
    rig, monkeypatch, profile
):
    wrapped, requests, sdk = await sdk_wrapper(
        {"decision": "allow", "is_present": True, "confidence": 0.99, "reasoning": "visible knob"}
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    rig.controller.swipe_coords = AsyncMock(return_value=None)
    try:
        status, _ = await execute_agent_profile(
            rig,
            profile,
            "swipe",
            {"start": [500, 400], "end": [1000, 400], "target_description": "slider knob"},
        )
        assert status in {"success", "dispatched"} and len(requests) == 1
        assert rig.controller.swipe_coords.await_args.args[:4] == (50, 40, 99, 40)
    finally:
        await sdk.close()


@pytest.mark.parametrize("profile", ["pro", "flash"])
async def test_fractional_point_target_is_rejected_in_integer_wire_contract(
    rig, monkeypatch, profile
):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", no_model)
    status, _ = await execute_agent_profile(
        rig, profile, "click", {"target": [500.5, 400], "target_description": "Open"}
    )
    assert status == "error" and not rig.taps


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize(
    "left,right,expected", [(19, 21, (19, 101)), (19, 22, (21, 101)), (20, 21, None)]
)
async def test_gate_checks_exact_wire_point_with_right_exclusive_bounds(
    rig, monkeypatch, profile, left, right, expected
):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", no_model)
    image = BytesIO()
    Image.new("RGB", (1080, 2400), "white").save(image, "PNG")
    rig.image = image.getvalue()
    rig.b64 = base64.b64encode(rig.image).decode()
    rig.ctx.device.device_width, rig.ctx.device.device_height = 1080, 2400
    rig.tree[0]["bounds"] = f"[{left},80][{right},120]"
    await fresh(rig)
    status, _ = await execute_agent_profile(rig, profile, "click", {"target": 1})
    if expected is not None:
        assert status in {"success", "dispatched"} and rig.taps == [expected]
        assert left <= rig.taps[0][0] < right
    else:
        assert status in {"failed", "error"} and not rig.taps


async def test_legacy_swipe_parser_keeps_legacy_conversion():
    from artemis.utils.coordinates import parse_swipe_parameters

    assert parse_swipe_parameters({"start": [-100.9, 400], "end": [999999.5, 400]})[:2] == (
        "coords",
        [-100, 400, 999999, 400],
    )


async def test_explicit_unknown_with_invalid_args_code_never_restores_version_or_replays(
    rig, monkeypatch
):
    from artemis.mcp.action_types import ActionCode

    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", no_model)
    calls = []

    async def may_have_run(*args, **kwargs):
        calls.append(args)
        rig.taps.append((50, 40))
        return ActionResult.failure(
            "click",
            "backend outcome unknown",
            code=ActionCode.INVALID_ARGS,
            dispatch_state=DispatchState.UNKNOWN,
        )

    rig.actuator.click = may_have_run
    rig.state.structured_decisions = json.dumps([rig.action()])
    report = await run_validation_loop(ValidatorNode(rig.ctx), rig.state)
    assert report["last_execution_result"]["status"] == "unknown"
    assert len(calls) == 1 and rig.taps == [(50, 40)]
    assert state_version(rig.ctx) == 1 and rig.ctx._current_snapshot.state_version == 1


async def test_omitted_dispatch_state_still_infers_confirmed_invalid_args_rejection(rig):
    from artemis.mcp.action_types import ActionCode

    calls = []
    original = rig.actuator.click

    async def reject_once(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            return ActionResult.failure(
                "click", "pre-effect rejection", code=ActionCode.INVALID_ARGS
            )
        return await original(*args, **kwargs)

    rig.actuator.click = reject_once
    action = rig.action()
    result = await _attempt_local_execution(
        ValidatorNode(rig.ctx), rig.session, action, "tap", [], max_local_retries=2
    )
    assert result == (True, "") and len(calls) == 2 and len(rig.taps) == 1
    assert state_version(rig.ctx) == 1


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize(
    "field,replacement",
    [
        ("text", "Different item"),
        ("resource-id", "different-control"),
        ("text", None),
        ("resource-id", None),
        ("content-desc", "Changed description"),
        ("content-desc", None),
    ],
)
async def test_live_identity_conflict_cannot_be_masked_by_another_matching_field_or_pixel_allow(
    rig, monkeypatch, profile, field, replacement
):
    rig.tree[0]["content-desc"] = "Original description"
    await fresh(rig)
    if replacement is None:
        rig.tree[0].pop(field)
    else:
        rig.tree[0][field] = replacement
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "visible control",
        }
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    try:
        status, _ = await execute_agent_profile(rig, profile, "click", {"target": 1})
        assert status in {"failed", "error"} and not rig.taps and not requests
        assert (
            rig.state.operator_raw_data["xml_hierarchy"] == rig.tree
        )  # recover by a fresh observation
    finally:
        await sdk.close()


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize("identity_fields", ["all", "text_only", "content_desc_only"])
async def test_matching_existing_xml_identity_remains_local_without_paid_judge(
    rig, monkeypatch, profile, identity_fields
):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", no_model)
    if identity_fields == "all":
        rig.tree[0]["content-desc"] = "Original description"
    else:
        rig.tree[0].pop("resource-id")
        if identity_fields == "content_desc_only":
            rig.tree[0]["content-desc"] = rig.tree[0].pop("text")
    await fresh(rig)
    status, _ = await execute_agent_profile(rig, profile, "click", {"target": 1})
    assert status in {"success", "dispatched"} and rig.taps == [(50, 40)]


@pytest.mark.parametrize("profile", ["pro", "flash"])
async def test_missing_original_stable_identity_can_still_use_existing_pixel_review(
    rig, monkeypatch, profile
):
    rig.state.indexed_elements[0].update(text=None, resource_id=None)
    for node in (rig.tree[0], rig.state.operator_raw_data["xml_hierarchy"][0]):
        node.pop("text", None)
        node.pop("resource-id", None)
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "visible control",
        }
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    try:
        status, _ = await execute_agent_profile(rig, profile, "click", {"target": 1})
        assert status in {"success", "dispatched"} and rig.taps == [(50, 40)] and len(requests) == 1
    finally:
        await sdk.close()


async def test_flash_failed_index_refresh_cannot_launder_same_turn_raw_point(rig, monkeypatch):
    rig.tree[0]["resource-id"] = "different-control"
    initial_snapshot = rig.ctx._current_snapshot
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "visible control",
        }
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    runner = object.__new__(FlashRunner)
    runner.ctx = rig.ctx
    runner.controller = rig.controller
    runner.executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    runner.summarizer = None
    runner._llm = None
    messages = []
    calls = [
        {"name": "click", "args": {"target": 1}, "id": "index"},
        {
            "name": "click",
            "args": {"target": [500, 400], "target_description": "Open"},
            "id": "point",
        },
    ]
    try:
        await runner._process_tool_calls(
            calls, rig.state, messages, "", {}, rig.image, "", 0, _TurnRecord()
        )
        assert len(messages) == 2 and all(message.status == "error" for message in messages)
        assert "stale" in messages[-1].content
        assert not rig.taps and not requests and state_version(rig.ctx) == 0
        assert rig.ctx._current_snapshot != initial_snapshot
    finally:
        await sdk.close()


async def test_pro_failed_index_refresh_stops_remaining_same_batch_raw_point(rig, monkeypatch):
    from artemis.agents.operator.operator import OperatorNode

    rig.tree[0]["resource-id"] = "different-control"
    operator = object.__new__(OperatorNode)
    operator.ctx = rig.ctx
    actions = []
    for args in ({"target": 1}, {"target": [500, 400], "target_description": "Open"}):
        translated, error = operator._translate_and_validate_tool_inner(
            {"name": "click", "args": args}, rig.state
        )
        assert error is None
        actions.extend(translated)
    rig.state.structured_decisions = json.dumps(actions)
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "visible control",
        }
    )
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    try:
        report = await run_validation_loop(ValidatorNode(rig.ctx), rig.state)
        assert report["last_execution_result"]["status"] == "failed"
        assert "Skipped" in report["last_execution_result"]["execution"][1]["attempts"][0]
        assert not requests and not rig.taps and state_version(rig.ctx) == 0
    finally:
        await sdk.close()


@pytest.mark.parametrize("profile", ["pro", "flash"])
@pytest.mark.parametrize(
    "live_change", ["empty_tree", "removed_node", "moved_node", "duplicate_node"]
)
async def test_bound_xml_target_without_unique_live_match_never_uses_old_pixel_allow(
    rig, monkeypatch, profile, live_change
):
    if live_change == "empty_tree":
        rig.tree.clear()
    elif live_change == "removed_node":
        rig.tree[:] = [
            {
                "text": "Other",
                "resource-id": "other",
                "class": "Button",
                "bounds": "[0,0][10,10]",
                "clickable": True,
            }
        ]
    elif live_change == "moved_node":
        rig.tree[0]["bounds"] = "[10,65][90,95]"
    else:
        rig.tree.append(deepcopy(rig.tree[0]))
    wrapped, requests, sdk = await sdk_wrapper(
        {
            "decision": "allow",
            "is_present": True,
            "confidence": 0.99,
            "reasoning": "Old pixels must not authorize a disappeared or ambiguous target",
        }
    )
    monkeypatch.setattr("artemis.services.llm.get_llm", lambda *a, **kw: wrapped)
    monkeypatch.setattr("artemis.agents.validator.validator.get_llm", lambda *a, **kw: wrapped)
    try:
        status, _ = await execute_agent_profile(rig, profile, "click", {"target": 1})
        assert status in {"failed", "error"} and not rig.taps and not requests
        assert rig.state.operator_raw_data["xml_hierarchy"] == rig.tree
    finally:
        await sdk.close()


async def test_actual_flash_runner_wait_then_index_keeps_capture_until_target_dispatch(
    rig, monkeypatch
):
    monkeypatch.setattr("artemis.services.llm.get_llm", no_model)
    runner = object.__new__(FlashRunner)
    runner.ctx, runner.controller = rig.ctx, rig.controller
    runner.executor = McpActionExecutor(rig.ctx, actuator=rig.actuator)
    runner.summarizer = runner._llm = None
    original_capture = rig.ctx._current_snapshot
    captures_before_click = []
    real_click = rig.actuator.click

    async def clicked(*args, **kwargs):
        # The owner invalidates just before dispatch. State still carries the
        # decision capture; no fallback observation should have replaced it.
        captures_before_click.append(rig.state.operator_raw_data["snapshot"]["capture_id"])
        return await real_click(*args, **kwargs)

    rig.actuator.click = clicked
    calls = [
        {"name": "wait_for_delay", "args": {"time_in_ms": 0}, "id": "wait"},
        {"name": "click", "args": {"target": 1}, "id": "click"},
    ]
    messages = []
    await runner._process_tool_calls(
        calls, rig.state, messages, "", {}, rig.image, "", 0, _TurnRecord()
    )
    assert rig.taps == [(50, 40)] and all(message.status == "success" for message in messages)
    assert captures_before_click == [original_capture.capture_id] and state_version(rig.ctx) == 1


async def test_nonchanging_fallback_keeps_real_returned_image_behavior(rig):
    runner = object.__new__(FlashRunner)
    runner.ctx, runner.controller = rig.ctx, rig.controller
    before = rig.ctx._current_snapshot
    result = SimpleNamespace(screenshot_bytes=rig.image)
    actual = await runner._capture_post_screenshot(
        result, "wait_for_delay", {"wait_for_delay"}, rig.state
    )
    assert actual == rig.image and rig.ctx._current_snapshot == before


async def test_actual_context_with_opaque_injected_config_keeps_legacy_binding_without_resolver(
    rig, monkeypatch
):
    from artemis.context import ArtemisContext
    from artemis.tools.command_tool import get_run_short_adb_command_tool
    from artemis.utils.vision import uses_minimax

    opaque = SimpleNamespace(
        get_agent=lambda *a: pytest.fail("Opaque config was interpreted as providers")
    )
    ctx = ArtemisContext.model_construct(device=rig.ctx.device, llm_config=opaque, data_engine=None)
    monkeypatch.setattr(
        "artemis.services.llm.context_uses_minimax",
        lambda *a: pytest.fail("Opaque injection entered resolver"),
    )
    monkeypatch.setattr(
        "artemis.llm.router.ModelFactory.create_model",
        lambda *a, **kw: pytest.fail("Opaque binding constructed a provider model"),
    )
    assert not uses_minimax(ctx)
    tool = get_run_short_adb_command_tool(ctx)
    assert tool.name == "run_adb_command" and "Unsupported" not in tool.description
    assert not ctx._llm_endpoint_snapshot


async def test_pinned_real_endpoint_still_controls_opaque_injected_context(rig, monkeypatch):
    from artemis.context import ArtemisContext
    from artemis.utils.vision import uses_minimax

    ctx = ArtemisContext.model_construct(
        device=rig.ctx.device, llm_config=object(), data_engine=None
    )
    ctx._llm_endpoint_snapshot = {("default", False, False): endpoint()}
    monkeypatch.setattr(
        "artemis.services.llm.context_uses_minimax",
        lambda *a: pytest.fail("Pinned endpoint was re-resolved"),
    )
    assert uses_minimax(ctx)


async def test_real_none_config_still_resolves_selected_minimax_for_agent_binding(
    rig, monkeypatch, isolated_llm_provider_registry
):
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes
    from artemis.context import ArtemisContext
    from artemis.tools.command_tool import get_run_short_adb_command_tool
    from third_party.mobile_use.context import DeviceContext

    isolated_llm_provider_registry.add(
        name="binding-mini",
        kind="anthropic",
        vendor="minimax",
        model="MiniMax-M3.1-Flash",
        api_base="https://dispatch.invalid/anthropic",
        api_key="fixture-not-a-key",
    )
    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "binding-mini",
                    "model": "default",
                    "fallback": {"provider": "binding-mini", "model": "default"},
                }
            }
        )
    )
    monkeypatch.setattr("artemis.config.llm.get_default_llm_config", lambda: config)
    ctx = ArtemisContext(device=DeviceContext(device_id="fixture"), llm_config=None)
    tool = get_run_short_adb_command_tool(ctx)
    assert "Unsupported" in tool.description and ctx.llm_config is config
    assert ctx._llm_endpoint_snapshot[("default", False, False)].vendor == "minimax"


@pytest.mark.parametrize("config_mode", ["typed", "none_default"])
@pytest.mark.parametrize("alias_mode", ["missing", "disabled"])
async def test_real_invalid_or_disabled_provider_config_never_degrades_to_legacy_binding(
    rig, monkeypatch, isolated_llm_provider_registry, config_mode, alias_mode
):
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes
    from artemis.context import ArtemisContext
    from artemis.tools.command_tool import get_run_short_adb_command_tool
    from third_party.mobile_use.context import DeviceContext

    alias = "invalid-binding-mini"
    if alias_mode == "disabled":
        isolated_llm_provider_registry.add(
            name=alias,
            kind="anthropic",
            vendor="minimax",
            model="MiniMax-M3.1-Flash",
            api_base="https://dispatch.invalid/anthropic",
            api_key="fixture-not-a-key",
            enabled=False,
        )
    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": alias,
                    "model": "default",
                    "fallback": {"provider": alias, "model": "default"},
                }
            }
        )
    )
    monkeypatch.setattr("artemis.config.llm.get_default_llm_config", lambda: config)
    ctx = ArtemisContext(
        device=DeviceContext(device_id="fixture"),
        llm_config=config if config_mode == "typed" else None,
    )
    with pytest.raises(ValueError):
        get_run_short_adb_command_tool(ctx)
    assert not getattr(ctx, "_llm_endpoint_snapshot", None)
