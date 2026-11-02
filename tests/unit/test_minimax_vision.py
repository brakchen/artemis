"""Offline visual recognition contracts; no model key, network or device needed."""

from __future__ import annotations

import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import httpx
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import ToolException
from PIL import Image
from pydantic import ValidationError
import pytest

from artemis.agents.explorer.explorer import Explorer
from artemis.agents.explorer.screen_index import ScreenIndex
from artemis.agents.explorer.tiers import get_tier
from artemis.agents.object_detector.object_detector import _run_object_detection
from artemis.config.agent import AgentGlobalConfig
from artemis.data_engine.engine import DataEngine
from artemis.data_engine.models import ImageRecord
from artemis.data_engine.storage import StorageManager
from artemis.drivers.android.adb_driver import AndroidAdbDriver
from artemis.llm.reliability import (
    LLMInvalidOutputError,
    LLMPermanentError,
    Failure,
    FailureCategory,
)
from artemis.llm.router import ModelEndpoint, ModelFactory
from artemis.services.llm import RobustChatModelWrapper
from artemis.tools.explorer_tool import ExplorerOutcome, register_candidates
from artemis.utils.coordinates import denormalize_point
from artemis.utils.ocr_api import automatic_ocr_enabled, is_ocr_configured, perform_ocr
from artemis.utils.ocr_xml_fusion import _map_coordinates_back, fuse_ocr_with_xml
from artemis.utils.vision import (
    ImageTransform,
    create_snapshot,
    invalidate_snapshot,
    snapshot_for_image,
    stamp_elements,
)
from artemis.utils.vision_models import DetectionOutput, OCRModelOutput
from artemis.utils.visual_ocr import recognize_ocr, ocr_to_legacy, ocr_cache_key
from artemis.utils.visualization import format_minimal_list_with_elements


@pytest.mark.parametrize("pinned_role", ["all", "explorer"])
def test_minimax_image_coder_is_absent_from_both_declarations_and_prompt(pinned_role, monkeypatch):
    monkeypatch.setenv("OCR_API_KEY", "fixture-only-ocr-key")
    ctx = context()
    if pinned_role != "all":
        ctx._llm_endpoint_snapshot = {(pinned_role, False, False): endpoint()}
    explorer = Explorer(ctx)
    tier = get_tier("ultra")
    explorer._apply_tier(tier)
    assert "ask_image_processor" not in {tool.name for tool in explorer.get_exposed_tools()}
    names = {tool["function"]["name"] for tool in explorer._universal_exposed_tools()}
    assert "ask_image_processor" not in names
    assert {"inspect_region", "detect_objects", "get_ocr_list"} <= names
    prompt, error = explorer._build_prompt_template(tier)
    assert error is None
    assert "ask_image_processor" not in prompt
    assert "inspect_region" in prompt and "detect_objects" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry", ["central", "tool", "export", "history", "explorer", "native", "universal"]
)
async def test_minimax_cached_image_coder_rejects_before_any_effect(monkeypatch, tmp_path, entry):
    from google.genai import types
    from artemis.agents.image_processor import image_processor as module
    from artemis.tools.image_processor_tool import ask_image_processor, get_ask_image_processor_tool
    from artemis.tools.tool_wrapper import invoke_tool_with_injection
    from artemis.graph.state import State

    ctx = context()
    monkeypatch.setenv("GOOGLE_API_KEY", "residual-fixture-key")
    forbidden = MagicMock(side_effect=AssertionError("image coder created an external effect"))
    monkeypatch.setattr(module, "get_llm", forbidden)
    monkeypatch.setattr(module, "PythonExecutor", forbidden)
    monkeypatch.setattr("artemis.tools.base.get_driver", forbidden)
    monkeypatch.setattr("google.genai.Client", forbidden)
    monkeypatch.setattr(module.settings, "TRACES_PATH", tmp_path / "must-not-create")
    explorer = Explorer(ctx)
    explorer.image_pool = {"img_0": {"path": "missing-original.png", "transform": {}}}
    original_pool = dict(explorer.image_pool)
    state = State(initial_goal="offline")
    state.latest_screenshot = "missing-original.png"
    arguments = {"instruction": "opaque Python", "target_image_id": "img_0"}
    tool = get_ask_image_processor_tool(ctx)
    if entry == "universal":
        messages = []
        await explorer._universal_dispatch_tools(
            [{"name": "ask_image_processor", "id": "cached", "args": arguments}], messages, 1
        )
        assert len(messages) == 1 and isinstance(messages[0], ToolMessage)
        assert messages[0].status == "error" and "unsupported" in messages[0].content
    elif entry == "native":
        record, parts = {"tool_calls": []}, []
        await explorer._dispatch_tool_calls(
            [types.FunctionCall(name="ask_image_processor", args=arguments)],
            forbidden,
            record,
            parts,
        )
        assert "unsupported" in record["tool_calls"][0]["response"]["error"]
        assert len(parts) == 1 and parts[0].function_response.response.get("error")
    else:
        with pytest.raises(ToolException, match="unsupported for MiniMax"):
            if entry == "central":
                await module.ImageProcessor(ctx).run("opaque Python", "missing-original.png")
            elif entry == "tool":
                await ask_image_processor.execute(ctx=ctx, state=state, **arguments)
            elif entry == "export":
                await ask_image_processor.to_langchain_tool(ctx).ainvoke(arguments)
            elif entry == "history":
                await invoke_tool_with_injection(tool, arguments, "cached", state=state)
            else:
                await explorer.exec_ask_image_processor(**arguments)
    forbidden.assert_not_called()
    assert explorer.image_pool == original_pool
    assert explorer.next_img_id == 1 and explorer.detection_registry == {}
    assert not (tmp_path / "must-not-create").exists()


@pytest.mark.asyncio
async def test_legacy_image_coder_resolution_error_is_not_an_operator_retry(monkeypatch):
    from artemis.agents.image_processor import image_processor as module

    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(image_processor=SimpleNamespace(model="legacy")),
        data_engine=None,
    )
    broken = MagicMock(side_effect=ValueError("invalid image processor config"))
    monkeypatch.setattr(module, "get_llm", broken)
    with pytest.raises(ValueError, match="invalid image processor config"):
        await module.ImageProcessor(ctx).run("fixture", "missing.png")
    broken.assert_called_once_with(ctx, name="image_processor")


@pytest.fixture(autouse=True)
def isolate_gateway_telemetry(monkeypatch):
    from artemis.services import llm as gateway
    from artemis.llm.reliability import CircuitBreaker

    # Other DataEngine tests may have ended their temporary session already.
    # Vision fixtures have no active trace and must not write into that session.
    monkeypatch.setattr(gateway, "_get_current_data_engine", lambda: None)
    monkeypatch.setattr(gateway, "_ENDPOINT_BREAKER", CircuitBreaker(threshold=1000))


def png(size=(1080, 2400)):
    stream = BytesIO()
    Image.new("RGB", size, "white").save(stream, "PNG")
    return stream.getvalue()


def endpoint(**kwargs):
    return ModelEndpoint(
        provider="anthropic",
        vendor="minimax",
        model_name="MiniMax-M3.1-Flash",
        api_base="https://vision.invalid/anthropic",
        api_key="fixture-only-not-a-key",
        **kwargs,
    )


def context(tmp_path=None):
    selected = endpoint()
    pinned = {
        (role, utility, fallback): selected
        for role, utility in [("default", False), ("explorer", False), ("object_detector", True)]
        for fallback in (False, True)
    }
    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(explorer=SimpleNamespace(model="gemini-raw-residue")),
        _llm_endpoint_snapshot=pinned,
        _screen_state_version=0,
        _current_snapshot=None,
        _ocr_inflight={},
        _ocr_last_outcome={},
        _genai_client=object(),
        device=SimpleNamespace(device_id="fake-device", display_id="0", rotation=0),
        agent_config=AgentGlobalConfig(),
        data_engine=None,
    )
    if tmp_path is not None:
        engine = object.__new__(DataEngine)
        engine.global_base_dir = tmp_path
        engine.storage = StorageManager(tmp_path / "data.db", tmp_path)
        ctx.data_engine = engine
    return ctx


class RawFake:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    async def ainvoke(self, messages, **kwargs):
        self.calls.append(messages)
        response = next(self.responses)
        if isinstance(response, BaseException):
            raise response
        return AIMessage(
            content=json.dumps(response), response_metadata={"stop_reason": "end_turn"}
        )


def model(*responses):
    raw = RawFake(*responses)
    return RobustChatModelWrapper(raw, endpoint=endpoint()), raw


def found(bbox=(700, 100, 900, 300)):
    return {"status": "found", "candidates": [{"label": "Search", "bbox": list(bbox)}]}


def text_output(status="complete", bbox=(100, 200, 300, 400), text="搜索", uncertain=False):
    return {
        "status": status,
        "regions": [{"text": text, "bbox": list(bbox), "uncertain": uncertain}],
    }


@pytest.mark.parametrize(
    "bbox",
    [
        [True, 0, 900, 900],
        [0, 0, float("nan"), 900],
        [0, 0, float("inf"), 900],
        [900, 0, 100, 900],
        [0, 2, 10, 1],
        [-1, 0, 1, 1],
        [0, 0, 1001, 1],
        ["0", 0, 1, 1],
        [0, 0, 0, 1],
    ],
)
def test_model_boxes_reject_invalid_values(bbox):
    with pytest.raises(ValidationError):
        DetectionOutput.model_validate(found(bbox))
    with pytest.raises(ValidationError):
        OCRModelOutput.model_validate(text_output(bbox=bbox))


@pytest.mark.parametrize(
    "status,count",
    [("found", 0), ("found", 2), ("ambiguous", 0), ("ambiguous", 1), ("not_found", 1)],
)
def test_detection_cardinality(status, count):
    with pytest.raises(ValidationError):
        DetectionOutput.model_validate(
            {"status": status, "candidates": found()["candidates"] * count}
        )


def test_model_cannot_supply_program_fields_or_claim_uncertain_complete():
    with pytest.raises(ValidationError):
        DetectionOutput.model_validate({**found(), "snapshot_id": "forged"})
    with pytest.raises(ValidationError):
        OCRModelOutput.model_validate(text_output(uncertain=True))
    assert (
        OCRModelOutput.model_validate(text_output("partial", text="�索", uncertain=True))
        .regions[0]
        .text
        == "�索"
    )


@pytest.mark.asyncio
async def test_minimax_utility_role_xy_and_errors_are_not_failed_queries(monkeypatch):
    ctx = context()
    data = png()
    snapshot = create_snapshot(ctx, data)
    llm, raw = model(found(), {"status": "not_found", "candidates": []})
    getter = MagicMock(return_value=llm)
    monkeypatch.setattr("artemis.agents.object_detector.minimax.get_llm", getter)
    result = await _run_object_detection(
        ctx, data, ["Search", "missing"], templates=["legacy yx"] * 4, snapshot=snapshot
    )
    getter.assert_called_once_with(ctx, "object_detector", is_utils=True)
    assert len(raw.calls) == 2
    candidate = result["detected"][0]
    assert candidate["point"] == [800, 200]
    assert denormalize_point(*candidate["point"], 1080, 2400) == [864, 480]
    assert candidate["snapshot_id"] == snapshot.capture_id
    assert result["failed"] == ["missing"]
    invalid, _ = model({"status": "found", "candidates": []}, {"status": "found", "candidates": []})
    getter.return_value = invalid
    with pytest.raises(LLMInvalidOutputError):
        await _run_object_detection(ctx, data, ["Search"], snapshot=snapshot)
    permanent = LLMPermanentError(
        "fixture auth failure", failure=Failure(FailureCategory.AUTHENTICATION, False, False)
    )
    getter.return_value = model(permanent)[0]
    with pytest.raises(LLMPermanentError):
        await _run_object_detection(ctx, data, ["Search"], snapshot=snapshot)


@pytest.mark.asyncio
async def test_ambiguous_and_small_targets_get_at_most_one_context_crop(monkeypatch):
    ctx = context()
    data = png((1000, 1000))
    snapshot = create_snapshot(ctx, data)
    ambiguous = {
        "status": "ambiguous",
        "candidates": [
            {"label": "a", "bbox": [100, 100, 110, 110]},
            {"label": "b", "bbox": [120, 100, 130, 110]},
        ],
    }
    llm, raw = model(ambiguous, ambiguous)
    monkeypatch.setattr("artemis.agents.object_detector.minimax.get_llm", lambda *a, **kw: llm)
    result = await _run_object_detection(ctx, data, ["a"], snapshot=snapshot)
    assert len(raw.calls) == 2
    assert result["detected"] == result["failed"] == []
    assert len(result["ambiguous"][0]["candidates"]) == 2
    # Small target full image: crop clamps to (500,0), dimensions (500,500).
    llm, raw = model(found((800, 200, 810, 210)), found((200, 200, 400, 400)))
    result = await _run_object_detection(ctx, data, ["small"], snapshot=snapshot)
    assert len(raw.calls) == 2
    assert result["detected"][0]["bbox"] == [600, 100, 700, 200]


def test_composed_crop_resize_keeps_bounds_and_point_frame():
    first = ImageTransform(offset_x=100, offset_y=200, scale_x=2, scale_y=2)
    child = ImageTransform(offset_x=40, offset_y=60, scale_x=0.5, scale_y=0.5)
    composed = first.compose(child)
    assert composed.to_root(100, 100) == (220, 330)
    assert composed.map_bbox([0, 0, 1000, 1000], (100, 100), (1000, 1000)) == [120, 230, 220, 330]
    assert ImageTransform(offset_x=-10).map_bbox([0, 0, 1000, 1000], (100, 100), (100, 100)) == [
        0,
        0,
        900,
        1000,
    ]


@pytest.mark.asyncio
async def test_detection_timeout_cleans_current_query_and_stops_following_queries(monkeypatch):
    ctx = context()
    data = png((100, 100))
    snapshot = create_snapshot(ctx, data)
    finished = asyncio.Event()
    calls = 0

    async def blocked(*args):
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            finished.set()

    monkeypatch.setattr("artemis.agents.object_detector.minimax.get_llm", lambda *a, **kw: object())
    monkeypatch.setattr("artemis.agents.object_detector.minimax.complete_visual", blocked)
    with pytest.raises(TimeoutError):
        await _run_object_detection(ctx, data, ["a", "b"], snapshot=snapshot, global_timeout=0.01)
    assert calls == 1 and finished.is_set()


def test_snapshot_identity_version_rotation_device_and_invalid_capture():
    ctx = context()
    data = png((200, 100))
    first = create_snapshot(ctx, data)
    assert snapshot_for_image(ctx, data, first).capture_id == first.capture_id
    assert not snapshot_for_image(ctx, data).capture_valid
    verification = create_snapshot(ctx, data, bind=False)
    assert verification.capture_id != first.capture_id
    assert verification.state_version == first.state_version == 0
    assert ctx._current_snapshot.capture_id == first.capture_id
    with pytest.raises(ValidationError):
        first.width = 1
    ctx.device.rotation = 1
    assert not snapshot_for_image(ctx, data, first).capture_valid
    ctx.device.rotation = 0
    ctx.device.device_id = "another"
    assert not snapshot_for_image(ctx, data, first).capture_valid
    ctx.device.device_id = "fake-device"
    ctx.device.display_id = "1"
    assert not snapshot_for_image(ctx, data, first).capture_valid
    ctx.device.display_id = "0"
    assert invalidate_snapshot(ctx) == 1
    assert not snapshot_for_image(ctx, data).capture_valid
    assert not create_snapshot(ctx, png((1, 1))).capture_valid


@pytest.mark.parametrize("as_dict", [False, True])
def test_old_origin_cannot_be_rebound_to_identical_current_pixels(as_dict):
    ctx = context()
    data = png((100, 200))
    old = create_snapshot(ctx, data)
    old_metadata = old.model_dump() if as_dict else old
    verification = create_snapshot(ctx, data, bind=False)
    reopened_verification = snapshot_for_image(ctx, data, verification)
    assert not reopened_verification.capture_valid
    assert reopened_verification.capture_id == verification.capture_id
    assert snapshot_for_image(ctx, data, old_metadata) == old
    invalidate_snapshot(ctx)
    current = create_snapshot(ctx, data)
    reopened = snapshot_for_image(ctx, data, old_metadata)
    assert not reopened.capture_valid and reopened.invalid_reason == "stale_snapshot"
    assert (
        reopened.capture_id == old.capture_id and reopened.state_version == old.state_version == 0
    )
    assert current.capture_id != old.capture_id and current.state_version == 1
    assert snapshot_for_image(ctx, data, current.model_dump()) == current
    assert not snapshot_for_image(ctx, data).capture_valid
    assert not snapshot_for_image(ctx, data, {"state_version": 0}).capture_valid
    assert not create_snapshot(ctx, data, old_metadata).capture_valid
    assert ctx._current_snapshot == current
    fresh = create_snapshot(
        ctx,
        data,
        SimpleNamespace(device_id="fake-device", display_id="0", rotation=0, capture_valid=True),
    )
    assert fresh.capture_valid and fresh.capture_id != current.capture_id
    assert snapshot_for_image(ctx, data, fresh) == fresh


@pytest.mark.asyncio
async def test_saved_identical_image_path_does_not_inherit_latest_origin(monkeypatch, tmp_path):
    ctx = context()
    data = png((100, 200))
    current = create_snapshot(ctx, data)
    latest, historical = tmp_path / "latest.png", tmp_path / "historical.png"
    latest.write_bytes(data)
    historical.write_bytes(data)
    state = SimpleNamespace(
        latest_screenshot=str(latest),
        operator_raw_data={"snapshot": current.model_dump(), "xml_hierarchy": []},
        latest_ui_hierarchy=[],
    )
    explorer = Explorer(ctx)
    monkeypatch.setattr(explorer, "_resolve_image_record", lambda _: (None, None))
    await explorer._load_screen(str(historical), state, get_tier("flash"))
    assert not explorer.snapshot.capture_valid
    assert explorer.snapshot.capture_id != current.capture_id
    await explorer._load_screen(str(latest), state, get_tier("flash"))
    assert explorer.snapshot == current


@pytest.mark.asyncio
async def test_flash_capture_to_explorer_uses_current_raw_xml_and_preserves_history(
    monkeypatch, tmp_path
):
    from copy import deepcopy
    from artemis.agents.validator.tool_declarations import capture_screenshot_and_parse_ui

    ctx = context(tmp_path)
    data = png((200, 200))
    live_xml = [
        {
            "class": "android.widget.TextView",
            "text": "Profile",
            "bounds": "[0,0][100,100]",
            "clickable": True,
        }
    ]
    legacy_ocr = [
        {
            "text": "OLD-GOOGLE-TEXT",
            "position": [
                {"x": 10, "y": 10},
                {"x": 30, "y": 10},
                {"x": 30, "y": 30},
                {"x": 10, "y": 30},
            ],
        }
    ]
    historical_tree = fuse_ocr_with_xml(live_xml, legacy_ocr)
    assert "ocr_elements" not in live_xml[0]  # fusion no longer mutates raw capture data
    image_hash = ctx.data_engine.get_or_create_image(
        data, ui_tree=historical_tree, ocr_result=legacy_ocr
    )
    original_record = ctx.data_engine.storage.get_image(image_hash).model_dump()
    response = SimpleNamespace(
        base64=base64.b64encode(data).decode(), elements=live_xml, width=200, height=200, rotation=0
    )
    controller = SimpleNamespace(get_screen_data=AsyncMock(return_value=response))
    state = SimpleNamespace(
        operator_raw_data={}, indexed_points=[], indexed_elements=[], latest_screenshot=None
    )
    path, _, _ = await capture_screenshot_and_parse_ui(ctx, state, controller, skip_settling=True)
    assert path == state.latest_screenshot
    assert state.operator_raw_data["xml_hierarchy"] == live_xml
    assert state.operator_raw_data["snapshot"]["capture_id"] == ctx._current_snapshot.capture_id
    assert state.indexed_elements[0]["snapshot_id"] == ctx._current_snapshot.capture_id
    explorer = Explorer(ctx)
    monkeypatch.setattr(
        explorer,
        "_resolve_image_record",
        lambda _: (image_hash, ctx.data_engine.storage.get_image(image_hash)),
    )
    fused = await explorer._load_screen(path, state, get_tier("flash"))
    assert not explorer.screen_index.ocr_elements
    assert [element.text for element in explorer.screen_index.elements] == ["Profile"]
    assert "OLD-GOOGLE-TEXT" not in str(fused)
    assert explorer.snapshot == ctx._current_snapshot
    assert ctx.data_engine.storage.get_image(image_hash).model_dump() == original_record
    # A historical fused tree is sanitized even when a current raw tree is unavailable.
    without_raw = SimpleNamespace(operator_raw_data={}, latest_screenshot=path)
    clean = await explorer._build_fused_hierarchy(
        ctx.data_engine.storage.get_image(image_hash), without_raw, path
    )
    assert [element.text for element in ScreenIndex.from_hierarchy(clean, 200, 200).elements] == [
        "Profile"
    ]
    assert ctx.data_engine.storage.get_image(image_hash).model_dump() == original_record
    # Typed OCR added after sanitization remains visible with its actual status/source.
    llm, raw = model(text_output("partial", text="NEW-MINIMAX", uncertain=True))
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    explorer.screenshot_path = path
    explorer._draw_labeled_dots = lambda *a: None
    saved_raw = deepcopy(state.operator_raw_data["xml_hierarchy"])
    inventory = await explorer.exec_get_ocr_list()
    assert "NEW-MINIMAX" in inventory["text"] and "status: partial" in inventory["text"]
    assert explorer.screen_index.ocr_elements[0].metadata["source"] == "minimax"
    assert state.operator_raw_data["xml_hierarchy"] == saved_raw == live_xml
    assert len(raw.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["explorer", "checker", "mcp"])
@pytest.mark.parametrize("with_regions", [False, True])
async def test_ocr_partial_status_is_visible_to_each_consumer(
    monkeypatch, tmp_path, consumer, with_regions
):
    import importlib
    from artemis.agents.checker.checker import _capture_final_screen

    ctx = context()
    ctx.device.device_width = ctx.device.device_height = 200
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    path = tmp_path / "input.png"
    path.write_bytes(data)
    output = (
        text_output("partial", uncertain=True)
        if with_regions
        else {"status": "partial", "regions": []}
    )
    llm, raw = model(output)
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    response = SimpleNamespace(
        base64=base64.b64encode(data).decode(),
        elements=[],
        width=200,
        height=200,
        snapshot=snapshot,
    )
    controller = SimpleNamespace(ctx=ctx, get_screen_data=AsyncMock(return_value=response))
    if consumer == "explorer":
        explorer = Explorer(ctx)
        explorer.snapshot = snapshot
        explorer.screenshot_path = str(path)
        explorer.width = explorer.height = 200
        explorer._draw_labeled_dots = lambda *a: None
        text = (await explorer.exec_get_ocr_list())["text"]
        # An unrelated later request must not change this inventory's provenance/status.
        ctx._ocr_last_outcome = {
            "status": "complete",
            "source": "minimax",
            "snapshot_id": "another-request",
        }
        repeated = (await explorer.exec_get_ocr_list())["text"]
        assert repeated.splitlines()[0] == text.splitlines()[0]
        assert (
            "status: partial" in repeated
            and explorer._ocr_outcome["snapshot_id"] == snapshot.capture_id
        )
    elif consumer == "checker":
        ctx.agent_config.ocr.automatic = True
        monkeypatch.setattr(
            "artemis.controllers.unified_controller.UnifiedMobileController", lambda _: controller
        )
        _, text = await _capture_final_screen(ctx)
    else:
        module = importlib.import_module("mcp_server.tools.device_state")
        monkeypatch.setattr("artemis.mcp.adb_server._get_controller", lambda **kw: controller)
        monkeypatch.setattr(module.env_utils, "get_project_root", lambda: str(tmp_path))
        text = await module.mobile_get_device_state("hierarchy")
    assert "status: partial" in text and "source: minimax" in text
    assert "incomplete" in text and "No text elements detected" not in text
    assert len(raw.calls) == 1


@pytest.mark.asyncio
async def test_current_typed_ocr_survives_xml_baseline_cleanup_without_new_request(
    monkeypatch, tmp_path
):
    from copy import deepcopy

    ctx = context()
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    path = tmp_path / "current.png"
    path.write_bytes(data)
    llm, _ = model(text_output("partial", text="CURRENT-MINIMAX", uncertain=True))
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    pixels = await perform_ocr(base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot)
    outcome = dict(ctx._ocr_last_outcome)
    mixed_tree = fuse_ocr_with_xml([], pixels)
    original_tree = deepcopy(mixed_tree)
    state = SimpleNamespace(
        latest_screenshot=str(path),
        operator_raw_data={
            "snapshot": snapshot.model_dump(),
            "xml_hierarchy": mixed_tree,
            "ocr_results": pixels,
            "ocr_outcome": outcome,
        },
    )
    explorer = Explorer(ctx)
    explorer.snapshot = snapshot
    explorer.screenshot_path = str(path)
    explorer.width = explorer.height = 200
    monkeypatch.setattr(
        explorer, "_request_ocr", AsyncMock(side_effect=AssertionError("duplicate paid request"))
    )
    fused = await explorer._build_fused_hierarchy(None, state, str(path))
    explorer.screen_index = ScreenIndex.from_hierarchy(fused, 200, 200)
    explorer._draw_labeled_dots = lambda *a: None
    inventory = await explorer.exec_get_ocr_list()
    assert "CURRENT-MINIMAX" in inventory["text"] and "status: partial" in inventory["text"]
    assert state.operator_raw_data["xml_hierarchy"] == original_tree
    assert explorer._ocr_outcome == outcome


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["explorer", "checker", "mcp"])
async def test_ocr_error_is_visible_as_xml_degradation_not_empty(monkeypatch, tmp_path, consumer):
    import importlib
    from artemis.agents.checker.checker import _capture_final_screen

    ctx = context()
    ctx.device.device_width = ctx.device.device_height = 200
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    path = tmp_path / "input.png"
    path.write_bytes(data)
    error = LLMPermanentError(
        "fixture permission error", failure=Failure(FailureCategory.AUTHENTICATION, False, False)
    )
    llm, raw = model(error)
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    xml = [{"text": "Native XML", "bounds": "[0,0][100,100]"}]
    controller = SimpleNamespace(
        ctx=ctx,
        get_screen_data=AsyncMock(
            return_value=SimpleNamespace(
                base64=base64.b64encode(data).decode(),
                elements=xml,
                width=200,
                height=200,
                snapshot=snapshot,
            )
        ),
    )
    if consumer == "explorer":
        explorer = Explorer(ctx)
        explorer.snapshot = snapshot
        explorer.screenshot_path = str(path)
        explorer.width = explorer.height = 200
        text = (await explorer.exec_get_ocr_list())["text"]
    elif consumer == "checker":
        ctx.agent_config.ocr.automatic = True
        monkeypatch.setattr(
            "artemis.controllers.unified_controller.UnifiedMobileController", lambda _: controller
        )
        _, text = await _capture_final_screen(ctx)
        assert "Native XML" in text
    else:
        module = importlib.import_module("mcp_server.tools.device_state")
        monkeypatch.setattr("artemis.mcp.adb_server._get_controller", lambda **kw: controller)
        monkeypatch.setattr(module.env_utils, "get_project_root", lambda: str(tmp_path))
        text = await module.mobile_get_device_state("hierarchy")
        assert "Native XML" in text
    assert "status: error" in text and "source: minimax" in text
    assert "XML only" in text and "No text elements detected" not in text
    assert len(raw.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["executor", "flash_fallback"])
async def test_actual_observation_adapters_write_source_for_explorer(monkeypatch, adapter):
    from artemis.agents.flash.runner import FlashRunner
    from artemis.mcp.action_executor import McpActionExecutor
    from artemis.mcp.action_types import ActionResult
    from artemis.mcp.observation import observe

    ctx = context()
    ctx.device.device_width = ctx.device.device_height = 200
    data = png((200, 200))
    xml = [
        {
            "text": "Current native text",
            "class": "android.widget.TextView",
            "bounds": "[0,0][100,100]",
            "clickable": True,
        }
    ]
    controller = SimpleNamespace(
        get_screen_data=AsyncMock(
            return_value=SimpleNamespace(
                base64=base64.b64encode(data).decode(), elements=xml, width=200, height=200
            )
        )
    )
    state = SimpleNamespace(
        operator_raw_data={}, indexed_elements=[], indexed_points=[], latest_screenshot=None
    )
    if adapter == "flash_fallback":
        runner = SimpleNamespace(ctx=ctx, controller=controller)
        result = SimpleNamespace(screenshot_bytes=None, screenshot_path=None, ui_elements_text=None)
        captured = await FlashRunner._capture_post_screenshot(
            runner, result, "press_key", {"press_key"}, state
        )
        assert captured == data
    else:

        class Span:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        monkeypatch.setattr("artemis.mcp.action_executor.TraceSpan", Span)
        actuator = SimpleNamespace(controller=controller, extensions=lambda: [])
        executor = McpActionExecutor(ctx, actuator=actuator)

        async def observed(**kwargs):
            return (await observe(ctx, controller, settle_ms=0))[0]

        executor._session = SimpleNamespace(
            started=True,
            call=AsyncMock(return_value=ActionResult.success("press_key", "ok")),
            observe=observed,
        )
        result = await executor.execute("press_key", {"key": "BACK"}, "fixture", state)
        assert result.status == "success"
    assert state.operator_raw_data["xml_hierarchy"] == xml
    assert state.operator_raw_data["snapshot"]["capture_id"] == ctx._current_snapshot.capture_id
    explorer = Explorer(ctx)
    monkeypatch.setattr(explorer, "_resolve_image_record", lambda _: (None, None))
    await explorer._load_screen(state.latest_screenshot, state, get_tier("flash"))
    assert explorer.snapshot == ctx._current_snapshot and explorer.snapshot.capture_valid
    assert [element.text for element in explorer.screen_index.elements] == ["Current native text"]


@pytest.mark.asyncio
async def test_clipped_semantic_rows_do_not_destroy_observe_or_operator_index(monkeypatch):
    from copy import deepcopy
    from artemis.agents.operator.operator import OperatorNode
    from artemis.config.agent import MemoryTranscriptConfig
    from artemis.graph.state import State
    from artemis.mcp.observation import observe
    from artemis.utils.ui_filter import filter_ui_hierarchy

    ctx = context()
    ctx.device.device_width = ctx.device.device_height = 200
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    filtered = filter_ui_hierarchy(
        [
            {"text": "Visible", "bounds": "[10,10][90,50]", "clickable": True},
            {"text": "Offscreen", "bounds": "[250,30][300,70]", "clickable": True},
        ],
        screen_width=200,
        screen_height=200,
    )
    raw_before = deepcopy(filtered)
    assert filtered[1]["bounds"] == "[200,30][200,30]" and filtered[1]["is_clipped"]
    text, elements, labels = format_minimal_list_with_elements(filtered, 200, 200)
    stamp_elements(elements, snapshot)
    assert labels == ["1", "2"] and [item["index"] for item in elements] == [1, 2]
    assert "[1] Text: 'Visible'" in text and "[2] Text: 'Offscreen'" in text
    assert elements[0]["evidence"] == "xml_actionable" and elements[0]["bbox"] == [50, 50, 450, 250]
    assert elements[1]["visible"] is elements[1]["actionable"] is False
    assert elements[1]["bbox"] is elements[1]["actionable_bounds"] is None
    assert elements[1]["evidence"] == "unverified"
    assert filtered == raw_before
    controller = SimpleNamespace(
        get_screen_data=AsyncMock(
            return_value=SimpleNamespace(
                base64=base64.b64encode(data).decode(),
                elements=filtered,
                width=200,
                height=200,
                snapshot=snapshot,
            )
        )
    )
    observation, _ = await observe(ctx, controller, settle_ms=0)
    assert observation.ok and observation.hierarchy_ok
    assert [item["text"] for item in observation.elements] == ["Visible", "Offscreen"]
    assert observation.elements[1]["visible"] is False
    assert observation.xml_hierarchy == raw_before and filtered == raw_before
    node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
    node.tools = [SimpleNamespace(name="web_search"), SimpleNamespace(name="analyze_task_output")]
    node._get_history_and_plan = lambda: ([], "")
    node._available_device_actions = lambda: set()
    node._build_prompt = AsyncMock(return_value=[])
    node._invoke_llm_loop = AsyncMock(return_value=([], "observed", None, False))
    node._stage_transcript_turn = lambda messages: None
    monkeypatch.setattr(
        "artemis.agents.operator.operator.trace_langchain_tool", lambda tool, ctx: tool
    )
    monkeypatch.setattr("artemis.agents.operator.operator.get_llm", lambda **kwargs: object())
    monkeypatch.setattr(
        "artemis.agents.operator.operator.get_adb_task_registry",
        lambda ctx: SimpleNamespace(
            active_task_summaries=lambda: [], pop_unnotified_finished=lambda: []
        ),
    )
    state = State(
        initial_goal="index fixture",
        operator_raw_data={
            "screenshot_b64": base64.b64encode(data).decode(),
            "xml_hierarchy": filtered,
            "snapshot": snapshot.model_dump(),
            "width": 200,
            "height": 200,
        },
        latest_ui_hierarchy=filtered,
    )
    result = await node(state)
    assert [item["text"] for item in result["indexed_elements"]] == ["Visible", "Offscreen"]
    assert result["indexed_elements"][0]["actionable"] is True
    assert (
        result["indexed_elements"][1]["visible"]
        is result["indexed_elements"][1]["actionable"]
        is False
    )
    assert result["indexed_points"] == [[50, 30], [200, 30]]
    assert node._build_prompt.call_args.args[3] == text
    assert filtered == raw_before


@pytest.mark.asyncio
@pytest.mark.parametrize("consumer", ["perception", "checker", "mcp"])
@pytest.mark.parametrize("bar_height", [0, 20, 200, 300])
async def test_statusbar_offset_matches_actual_crop_in_each_consumer(
    monkeypatch, tmp_path, consumer, bar_height
):
    import importlib
    from artemis.agents.checker.checker import _capture_final_screen
    from artemis.graph.perception import perception_node
    from artemis.graph.state import State
    from artemis.utils.ocr_xml_fusion import _detect_status_bar_height

    ctx = context()
    ctx.agent_config.ocr.automatic = True
    ctx.device.device_width, ctx.device.device_height = 1080, 2400
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    xml = [
        {
            "package": "com.android.systemui",
            "bounds": f"[0,0][200,{bar_height}]",
            "text": "Native XML",
        }
    ]
    expected_offset = 20 if bar_height == 20 else 0
    assert _detect_status_bar_height(xml, 200) == expected_offset
    response = SimpleNamespace(
        base64=base64.b64encode(data).decode(),
        elements=xml,
        width=200,
        height=2400,
        snapshot=snapshot,
    )
    controller = SimpleNamespace(ctx=ctx, get_screen_data=AsyncMock(return_value=response))
    llm, raw = model(text_output(text="Notification"))
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    captured_rows = []
    original_fusion = fuse_ocr_with_xml

    def fusion(nodes, rows):
        captured_rows.extend(rows)
        return original_fusion(nodes, rows)

    if consumer == "perception":
        ctx.data_engine = SimpleNamespace(
            current_step_id="fixture",
            base_dir=tmp_path,
            get_image_path=lambda _: tmp_path / "screen.png",
            get_or_create_image=lambda *a, **kw: "fixture",
            _run_in_background=lambda fn, *args: fn(*args),
        )
        monkeypatch.setattr(
            "artemis.graph.perception.UnifiedMobileController", lambda _: controller
        )
        monkeypatch.setattr("artemis.graph.perception.fuse_ocr_with_xml", fusion)
        result = await perception_node(State(initial_goal="fixture"), ctx)
        assert result["operator_raw_data"]["ocr_outcome"]["status"] == "complete"
        await asyncio.sleep(0)
    elif consumer == "checker":
        monkeypatch.setattr(
            "artemis.controllers.unified_controller.UnifiedMobileController", lambda _: controller
        )
        monkeypatch.setattr("artemis.agents.checker.checker.fuse_ocr_with_xml", fusion)
        _, text = await _capture_final_screen(ctx)
        assert "Notification" in text and "status: complete" in text
    else:
        module = importlib.import_module("mcp_server.tools.device_state")
        monkeypatch.setattr("artemis.mcp.adb_server._get_controller", lambda **kw: controller)
        monkeypatch.setattr(module.env_utils, "get_project_root", lambda: str(tmp_path))
        monkeypatch.setattr("artemis.utils.ocr_xml_fusion.fuse_ocr_with_xml", fusion)
        text = await module.mobile_get_device_state("hierarchy")
        assert "Notification" in text and "status: complete" in text
    assert len(raw.calls) == len(captured_rows) == 1
    top = round(0.2 * (200 - expected_offset)) + expected_offset
    bottom = round(0.4 * (200 - expected_offset)) + expected_offset
    assert captured_rows[0]["position"] == [
        {"x": 20, "y": top},
        {"x": 60, "y": top},
        {"x": 60, "y": bottom},
        {"x": 20, "y": bottom},
    ]
    assert captured_rows[0]["bbox"] == pytest.approx([100, top * 5, 300, bottom * 5])
    blocks = [
        block
        for message in raw.calls[0]
        for block in (message.content if isinstance(message.content, list) else [])
        if isinstance(block, dict) and block.get("type") == "image_url"
    ]
    image_data = base64.b64decode(blocks[0]["image_url"]["url"].split(",", 1)[1])
    with Image.open(BytesIO(image_data)) as submitted:
        assert submitted.size == (200, 200 - expected_offset)


@pytest.mark.asyncio
async def test_ocr_default_role_actual_size_cache_old_google_miss_and_rebind(monkeypatch, tmp_path):
    ctx = context(tmp_path)
    data = png((600, 900))
    snapshot = create_snapshot(ctx, data)
    image_hash = snapshot.image_hash
    ctx.data_engine.get_or_create_image(data, ocr_result=[{"text": "old google"}])
    ctx.data_engine.merge_image_metadata(image_hash, {"unrelated": 7})
    llm, raw = model(text_output())
    getter = MagicMock(return_value=llm)
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", getter)
    monkeypatch.setattr(
        "artemis.utils.ocr_api.get_http_client", lambda: pytest.fail("Google HTTP client created")
    )
    result = await recognize_ocr(base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot)
    assert result.image_size == (600, 900)
    getter.assert_called_once_with(ctx, "default")
    assert len(raw.calls) == 1
    assert ocr_to_legacy(result)[0]["position"] == [
        {"x": 60, "y": 180},
        {"x": 180, "y": 180},
        {"x": 180, "y": 360},
        {"x": 60, "y": 360},
    ]
    current = create_snapshot(ctx, data)
    cached = await recognize_ocr(base64.b64encode(data).decode(), ctx=ctx, snapshot=current)
    assert (
        cached.cache_hit and cached.snapshot.capture_id == current.capture_id != snapshot.capture_id
    )
    assert len(raw.calls) == 1
    record = ctx.data_engine.storage.get_image(image_hash)
    assert record.ocr_result == [{"text": "old google"}]
    assert record.extra_metadata["unrelated"] == 7
    assert len(record.extra_metadata["ocr_cache"]) == 1
    ctx._llm_endpoint_snapshot[("default", False, False)] = endpoint(reasoning_effort="low")
    assert ocr_cache_key(ctx, data) != result.cache_key


@pytest.mark.asyncio
async def test_ocr_failure_is_not_cached_and_recovery_empty_is_persisted(monkeypatch, tmp_path):
    ctx = context(tmp_path)
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    invalid = {"status": "complete", "regions": []}
    llm, raw = model(invalid, invalid, {"status": "empty", "regions": []})
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    encoded = base64.b64encode(data).decode()
    with pytest.raises(LLMInvalidOutputError):
        await perform_ocr(encoded, ctx=ctx, snapshot=snapshot)
    assert ctx._ocr_last_outcome["status"] == "error"
    assert ctx.data_engine.storage.get_image(snapshot.image_hash) is None
    assert not ctx._ocr_inflight
    result = await recognize_ocr(encoded, ctx=ctx, snapshot=snapshot)
    assert result.status == "empty" and result.regions == []
    assert len(raw.calls) == 3
    assert (await recognize_ocr(encoded, ctx=ctx, snapshot=snapshot)).cache_hit
    # Empty legacy history remains distinguishable from NULL.
    ctx.data_engine.get_or_create_image(data, ocr_result=[])
    assert ctx.data_engine.storage.get_image(snapshot.image_hash).ocr_result == []


@pytest.mark.asyncio
async def test_concurrent_ocr_single_request_and_cancellation_cleanup(monkeypatch):
    ctx = context()
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def complete(*args):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return OCRModelOutput.model_validate(text_output())

    monkeypatch.setattr("artemis.utils.visual_ocr.complete_visual", complete)
    encoded = base64.b64encode(data).decode()
    a = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=object()))
    await entered.wait()
    b = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=object()))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(a, b)
    assert calls == 1 and not ctx._ocr_inflight
    release.clear()
    entered.clear()
    a = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=object()))
    await entered.wait()
    a.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a
    assert not ctx._ocr_inflight


@pytest.mark.asyncio
async def test_cancel_one_ocr_waiter_preserves_survivor_and_single_sdk_request():
    from artemis.llm.reliability import logical_call_scope

    ctx = context()
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def respond(request):
        requests.append(json.loads(request.content))
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "id": "fixture",
                "type": "message",
                "role": "assistant",
                "model": "MiniMax-M3.1-Flash",
                "content": [{"type": "text", "text": json.dumps(text_output())}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 9, "output_tokens": 8},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sdk = anthropic.AsyncAnthropic(
        api_key="fixture-only-not-a-key",
        base_url="https://vision.invalid/anthropic",
        http_client=client,
        max_retries=0,
    )
    underlying = ModelFactory.create_model(endpoint())
    underlying._async_client = sdk
    wrapped = RobustChatModelWrapper(underlying, endpoint=endpoint())
    encoded = base64.b64encode(data).decode()
    try:
        with logical_call_scope(max_attempts=1) as budget:
            a = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=wrapped))
            await entered.wait()
            b = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=wrapped))
            await asyncio.sleep(0)
            entry = next(iter(ctx._ocr_inflight.values()))
            assert entry.waiters == 2
            a.cancel()
            with pytest.raises(asyncio.CancelledError):
                await a
            assert not entry.task.done() and entry.waiters == 1
            release.set()
            result = await b
            assert result.status == "complete" and entry.task.done()
            assert len(requests) == budget.attempts == 1
    finally:
        await client.aclose()
    assert not ctx._ocr_inflight


@pytest.mark.asyncio
async def test_cancel_all_ocr_waiters_joins_cleanup_and_allows_later_request(monkeypatch):
    ctx = context()
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    cleaned = []

    async def complete(*args):
        task = asyncio.current_task()
        calls.append(task)
        entered.set()
        try:
            await release.wait()
            return OCRModelOutput.model_validate(text_output())
        finally:
            await asyncio.sleep(0)
            cleaned.append(task)

    monkeypatch.setattr("artemis.utils.visual_ocr.complete_visual", complete)
    encoded = base64.b64encode(data).decode()
    a = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=object()))
    await entered.wait()
    b = asyncio.create_task(recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=object()))
    await asyncio.sleep(0)
    assert next(iter(ctx._ocr_inflight.values())).waiters == 2
    a.cancel()
    b.cancel()
    cancelled = await asyncio.gather(a, b, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in cancelled)
    assert len(calls) == len(cleaned) == 1 and calls[0].done()
    assert not ctx._ocr_inflight
    release.set()
    result = await recognize_ocr(encoded, ctx=ctx, snapshot=snapshot, llm=object())
    assert result.status == "complete" and len(calls) == len(cleaned) == 2
    assert all(task.done() for task in calls) and not ctx._ocr_inflight


@pytest.mark.asyncio
async def test_tiles_merge_overlap_and_preserve_uncertainty(monkeypatch):
    ctx = context()
    data = png((1000, 2400))
    snapshot = create_snapshot(ctx, data)
    # Two tiles y=0..1600 and y=800..2400; both read the same word.
    llm, raw = model(text_output(bbox=(100, 600, 300, 700)), text_output(bbox=(100, 100, 300, 200)))
    result = await recognize_ocr(
        base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot, llm=llm
    )
    assert len(raw.calls) == 2
    assert len(result.regions) == 1
    assert result.regions[0].bbox == [100, 400, 300, 1400 / 3]
    llm, _ = model(
        text_output("partial", text="�", uncertain=True), {"status": "empty", "regions": []}
    )
    result = await recognize_ocr(
        base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot, llm=llm
    )
    assert result.status == "partial" and result.regions[0].uncertain


@pytest.mark.asyncio
async def test_statusbar_offset_once_through_fusion_formatted_index(monkeypatch):
    ctx = context()
    root = png((100, 220))
    snapshot = create_snapshot(ctx, root)
    cropped = png((100, 200))
    llm, _ = model(text_output())
    result = await recognize_ocr(
        base64.b64encode(cropped).decode(),
        ctx=ctx,
        snapshot=snapshot,
        transform=ImageTransform(offset_y=20),
        llm=llm,
    )
    adapter = ocr_to_legacy(result)
    assert adapter[0]["position"][0] == {"x": 10, "y": 40}
    assert adapter[0]["bbox"][1] == pytest.approx(60 * 1000 / 220)
    assert adapter[0]["transform"]["offset_y"] == 20
    mapped = _map_coordinates_back(adapter, 20)
    assert mapped[0]["position"][0] == {"x": 10, "y": 60}
    fused = fuse_ocr_with_xml([], mapped)
    _, elements, _ = format_minimal_list_with_elements(fused, 100, 220)
    stamp_elements(elements, snapshot)
    assert elements[0]["bounds"] == [10, 60, 30, 100]
    assert elements[0]["bbox"] == pytest.approx([100, 60 * 1000 / 220, 300, 100 * 1000 / 220])
    assert elements[0]["snapshot_id"] == snapshot.capture_id
    assert elements[0]["ocr_status"] == "complete"
    assert elements[0]["source"] == "minimax"


def test_atomic_metadata_merge_preserves_parallel_versions_and_history(tmp_path):
    ctx = context(tmp_path)
    data = png((100, 100))
    key = ctx.data_engine.get_or_create_image(data, ocr_result=[{"text": "historical"}])
    ctx.data_engine.merge_image_metadata(key, {"other": {"x": 1}})

    def write(i):
        storage = StorageManager(tmp_path / "data.db", tmp_path)
        storage.merge_image_metadata(key, {"ocr_cache": {str(i): {"status": "empty"}}})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(12)))
    record = ctx.data_engine.storage.get_image(key)
    assert len(record.extra_metadata["ocr_cache"]) == 12
    assert record.extra_metadata["other"] == {"x": 1}
    assert record.ocr_result == [{"text": "historical"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [None, "[]", "broken-json"])
async def test_legacy_nullable_or_unversioned_metadata_is_cache_miss(
    monkeypatch, tmp_path, metadata
):
    ctx = context(tmp_path)
    data = png((100, 100))
    snapshot = create_snapshot(ctx, data)
    storage = ctx.data_engine.storage
    with storage._get_connection() as connection:
        connection.execute(
            "INSERT INTO images (image_name,timestamp,ocr_result,extra_metadata) VALUES (?,?,?,?)",
            (snapshot.image_hash, 0, json.dumps([{"text": "old Google"}]), metadata),
        )
        connection.commit()
    before = storage.get_image(snapshot.image_hash)
    assert isinstance(before.extra_metadata, dict)
    assert "ocr_cache" not in before.extra_metadata
    llm, raw = model(text_output())
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    result = await recognize_ocr(base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot)
    assert result.status == "complete" and not result.cache_hit and len(raw.calls) == 1
    after = storage.get_image(snapshot.image_hash)
    assert after.ocr_result == [{"text": "old Google"}]
    assert len(after.extra_metadata["ocr_cache"]) == 1
    for key, value in before.extra_metadata.items():
        assert after.extra_metadata[key] == value


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["flash", "pro", "ultra"])
async def test_explorer_all_tiers_keep_capture_geometry_and_program_evidence(
    monkeypatch, tmp_path, tier
):
    ctx = context()
    data = png((1000, 1000))
    snapshot = create_snapshot(ctx, data)
    path = tmp_path / "screen.png"
    path.write_bytes(data)
    hierarchy = [
        {"text": "Search", "bounds": "[-100,100][100,300]", "clickable": True, "enabled": True}
    ]
    state = SimpleNamespace(
        latest_screenshot=str(path),
        latest_ui_hierarchy=hierarchy,
        operator_raw_data={"snapshot": snapshot.model_dump(), "xml_hierarchy": hierarchy},
        indexed_points=[],
        indexed_elements=[],
    )
    explorer = Explorer(ctx)
    assert not explorer.use_native_gemini
    assert explorer.model_name == "MiniMax-M3.1-Flash"
    monkeypatch.setattr(explorer, "_resolve_image_record", lambda _: (None, None))
    monkeypatch.setattr(
        "artemis.agents.explorer.run_setup.httpx.AsyncClient",
        lambda: pytest.fail("Google client created"),
    )
    outcome = ExplorerOutcome.from_raw(
        await explorer.run("Search", "", str(path), state, version=tier)
    )
    assert len(outcome.candidates) == 1
    candidate = outcome.candidates[0]
    assert candidate.coords == (50, 200)  # visible intersection, not off-screen center
    assert candidate.metadata["evidence"] == "xml_actionable"
    assert candidate.metadata["snapshot_id"] == snapshot.capture_id
    register_candidates(ctx, state, outcome)
    assert state.indexed_elements[0]["bbox"] == [0, 100, 100, 300]
    assert state.indexed_elements[0]["snapshot_id"] == snapshot.capture_id
    # Unregistered model values cannot promote themselves to XML allow evidence.
    forged = explorer._enrich_candidates(
        [
            {
                "label": "unknown",
                "coords": [800, 200],
                "snapshot_id": "forged",
                "source": "xml",
                "evidence": "xml_actionable",
                "bounds": [0, 0, 1000, 1000],
            }
        ]
    )[0]
    assert forged["source"] == "minimax" and forged["evidence"] == "unverified"
    assert forged["snapshot_id"] == snapshot.capture_id and forged["bbox"] is None


@pytest.mark.asyncio
async def test_explorer_automatic_off_explicit_ocr_and_old_record_not_trusted(
    monkeypatch, tmp_path
):
    ctx = context()
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    path = tmp_path / "screen.png"
    path.write_bytes(data)
    explorer = Explorer(ctx)
    explorer.screenshot_path = str(path)
    explorer.snapshot = snapshot
    explorer.width = explorer.height = 200
    state = SimpleNamespace(
        latest_screenshot=str(path),
        operator_raw_data={"snapshot": snapshot.model_dump(), "xml_hierarchy": []},
    )
    record = ImageRecord(
        image_name=snapshot.image_hash,
        ui_tree=[],
        ocr_result=[{"text": "old Google", "position": []}],
    )
    llm, raw = model(text_output())
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    monkeypatch.setattr(
        "artemis.utils.ocr_api.get_http_client", lambda: pytest.fail("Google HTTP called")
    )
    monkeypatch.setattr(
        "artemis.agents.explorer.run_setup.httpx.AsyncClient",
        lambda: pytest.fail("Google HTTP client created"),
    )
    assert is_ocr_configured(ctx) and not automatic_ocr_enabled(ctx)
    fused = await explorer._build_fused_hierarchy(record, state, str(path))
    assert fused == [] and raw.calls == []
    explorer.screen_index = ScreenIndex.from_hierarchy(fused, 200, 200)
    explorer._draw_labeled_dots = lambda *a: None
    inventory = await explorer.exec_get_ocr_list()
    assert "搜索" in inventory["text"] and len(raw.calls) == 1
    assert "old Google" not in inventory["text"]
    assert explorer.http_client is None


@pytest.mark.asyncio
async def test_driver_actual_rotation_size_before_xml_filter_and_synthetic_invalid():
    data = png((2400, 1080))
    ui = SimpleNamespace(
        base64=base64.b64encode(data).decode(),
        width=1080,
        height=2400,
        rotation=1,
        display_id="0",
        elements=[{"text": "Landscape right", "bounds": "[1800,100][2300,300]"}],
    )
    ui_client = SimpleNamespace(get_screen_data=lambda: ui)
    adb = MagicMock()
    driver = AndroidAdbDriver("fake-device", adb, ui_client, width=1080, height=2400)
    result = await driver.get_screen_data(skip_settling=True)
    assert (result.width, result.height) == (2400, 1080)
    assert result.rotation == 1 and result.capture_valid
    assert any(e["text"] == "Landscape right" for e in result.ui_elements)
    ui.base64 = base64.b64encode(png((1, 1))).decode()
    result = await driver.get_screen_data(skip_settling=True)
    assert not result.capture_valid and (result.width, result.height) == (1, 1)


@pytest.mark.asyncio
async def test_driver_marks_unexplained_display_scaling_invalid():
    data = png((200, 100))
    ui = SimpleNamespace(base64=base64.b64encode(data).decode(), width=200, height=100, elements=[])
    adb = MagicMock()
    adb.device.return_value.shell.return_value = "mRotation=1 cur=400x200"
    driver = AndroidAdbDriver("fake-device", adb, SimpleNamespace(get_screen_data=lambda: ui))
    result = await driver.get_screen_data(skip_settling=True)
    assert not result.capture_valid
    assert result.invalid_reason == "display_capture_size_mismatch"
    assert result.rotation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unknown_id", [None, 42, True, "", "   ", MagicMock(name="unknown-device")]
)
async def test_unknown_device_metadata_remains_invalid_without_breaking_capture(unknown_id):
    data = png((200, 200))
    ui = SimpleNamespace(
        base64=base64.b64encode(data).decode(), width=200, height=200, rotation=0, elements=[]
    )
    adb = MagicMock()
    adb.device.return_value.shell.return_value = ""
    driver = AndroidAdbDriver(unknown_id, adb, SimpleNamespace(get_screen_data=lambda: ui))
    result = await driver.get_screen_data(skip_settling=True)
    assert result.screenshot_bytes == data
    assert result.device_id is None and not result.capture_valid
    assert result.invalid_reason == "unknown_device_id"
    ctx = context()
    ctx.device.device_id = unknown_id
    snapshot = create_snapshot(ctx, data, bind=False)
    assert snapshot.device_id is None and not snapshot.capture_valid
    assert snapshot.invalid_reason == "unknown_device_id"


@pytest.mark.asyncio
async def test_perception_and_checker_automatic_off_and_opt_in_ctx_once(monkeypatch, tmp_path):
    from artemis.agents.checker.checker import _capture_final_screen
    from artemis.graph.perception import perception_node
    from artemis.graph.state import State

    ctx = context()
    engine = SimpleNamespace(
        current_step_id="fixture-step",
        base_dir=tmp_path,
        get_image_path=lambda _: tmp_path / "screen.png",
        get_or_create_image=lambda *a, **kw: "fixture",
        _run_in_background=lambda fn, *args: fn(*args),
    )
    ctx.data_engine = engine
    data = png((200, 300))
    response = SimpleNamespace(
        base64=base64.b64encode(data).decode(), elements=[], width=200, height=300
    )
    controller = SimpleNamespace(get_screen_data=AsyncMock(return_value=response))
    monkeypatch.setattr("artemis.graph.perception.UnifiedMobileController", lambda _: controller)
    monkeypatch.setattr(
        "artemis.controllers.unified_controller.UnifiedMobileController", lambda _: controller
    )
    perception_ocr = AsyncMock(return_value=[])
    checker_ocr = AsyncMock(return_value=[])
    monkeypatch.setattr("artemis.graph.perception.perform_ocr", perception_ocr)
    monkeypatch.setattr("artemis.agents.checker.checker.perform_ocr", checker_ocr)
    state = State(initial_goal="fixture")
    result = await perception_node(state, ctx)
    assert result["operator_raw_data"]["ocr_outcome"]["status"] == "disabled"
    assert result["operator_raw_data"]["snapshot"]["width"] == 200
    _, final_text = await _capture_final_screen(ctx)
    assert "status: disabled" in final_text and "XML only" in final_text
    assert not perception_ocr.called and not checker_ocr.called
    first_version = ctx._screen_state_version
    ctx.agent_config.ocr.automatic = True
    await perception_node(state, ctx)
    await _capture_final_screen(ctx)
    assert perception_ocr.await_count == checker_ocr.await_count == 1
    assert perception_ocr.call_args.kwargs["ctx"] is ctx
    assert checker_ocr.call_args.kwargs["ctx"] is ctx
    assert ctx._screen_state_version == first_version
    await asyncio.sleep(0)  # join the existing perception background-save scheduling


@pytest.mark.asyncio
async def test_explicit_ocr_tool_uses_image_size_and_preserves_partial(monkeypatch, tmp_path):
    from artemis.tools.mobile.ocr import run_ocr_core

    ctx = context()
    ctx.device.device_width, ctx.device.device_height = 1080, 2400
    data = png((400, 200))
    snapshot = create_snapshot(ctx, data)
    path = tmp_path / "input.png"
    path.write_bytes(data)
    state = SimpleNamespace(
        latest_screenshot=str(path), operator_raw_data={"snapshot": snapshot.model_dump()}
    )
    llm, _ = model(text_output("partial", bbox=(700, 100, 900, 300), text="�", uncertain=True))
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    result = json.loads(await run_ocr_core(ctx, state))
    assert result[0]["coordinates"] == [800, 200]
    assert result[0]["ocr_status"] == "partial" and result[0]["uncertain"]
    assert result[0]["snapshot_id"] == snapshot.capture_id


@pytest.mark.asyncio
async def test_query_and_tile_requests_share_canonical_total_budget(monkeypatch):
    from artemis.llm.reliability import logical_call_scope, LLMExhaustedError

    ctx = context()
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    llm, raw = model(found(), found(), found())
    monkeypatch.setattr("artemis.agents.object_detector.minimax.get_llm", lambda *a, **kw: llm)
    with logical_call_scope(max_attempts=2):
        with pytest.raises(LLMExhaustedError):
            await _run_object_detection(ctx, data, ["a", "b", "c"], snapshot=snapshot)
    assert len(raw.calls) == 2
    data = png((200, 4500))
    snapshot = create_snapshot(ctx, data)
    llm, raw = model(text_output(), text_output(), text_output())
    with logical_call_scope(max_attempts=2):
        with pytest.raises(LLMExhaustedError):
            await recognize_ocr(
                base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot, llm=llm
            )
    assert len(raw.calls) == 2 and not ctx._ocr_inflight


@pytest.mark.asyncio
async def test_real_mcp_context_without_config_uses_selected_minimax(
    monkeypatch, isolated_llm_provider_registry
):
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes
    from artemis.context import ArtemisContext
    from third_party.mobile_use.context import DeviceContext

    isolated_llm_provider_registry.add(
        name="mini-vision",
        kind="anthropic",
        vendor="minimax",
        model="MiniMax-M3.1-Flash",
        api_base="https://vision.invalid/anthropic",
        api_key="fixture-only-not-a-key",
    )
    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "mini-vision",
                    "model": "default",
                    "fallback": {"provider": "mini-vision", "model": "default"},
                }
            }
        )
    )
    monkeypatch.setattr("artemis.services.llm.get_default_llm_config", lambda: config)
    monkeypatch.setattr("artemis.config.llm.get_default_llm_config", lambda: config)
    monkeypatch.setenv("GOOGLE_API_KEY", "residual-fixture-key")
    monkeypatch.setenv("OCR_API_KEY", "residual-fixture-key")
    monkeypatch.setattr(
        "artemis.utils.ocr_api.get_http_client",
        lambda: pytest.fail("MiniMax MCP OCR used Google HTTP"),
    )
    ctx = ArtemisContext(device=DeviceContext(device_id="fake-device"), llm_config=None)
    ctx._genai_client = object()
    llm, raw = model(text_output())
    monkeypatch.setattr("artemis.utils.visual_ocr.get_llm", lambda *a, **kw: llm)
    data = png((200, 200))
    snapshot = create_snapshot(ctx, data)
    assert is_ocr_configured(ctx) and not automatic_ocr_enabled(ctx)
    result = await perform_ocr(base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot)
    assert result[0]["text"] == "搜索" and len(raw.calls) == 1
    assert ctx.llm_config is config
    assert not Explorer(ctx).use_native_gemini


@pytest.mark.asyncio
async def test_legacy_real_context_ocr_only_key_does_not_require_text_key(
    monkeypatch, no_default_llm_provider
):
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes
    from artemis.context import ArtemisContext
    from third_party.mobile_use.context import DeviceContext

    config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": "google",
                    "model": "gemini-2.5-flash",
                    "fallback": {"provider": "google", "model": "gemini-2.5-flash"},
                }
            }
        )
    )
    monkeypatch.setattr("artemis.config.llm.get_default_llm_config", lambda: config)
    from artemis.config.settings import Settings

    monkeypatch.setattr(Settings, "get_api_key", lambda self, provider: None)
    monkeypatch.setenv("OCR_API_KEY", "fixture-ocr-only")
    for name in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "GCP_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    ctx = ArtemisContext(device=DeviceContext(device_id="fake-device"), llm_config=None)
    client = AsyncMock()
    client.post.return_value = httpx.Response(
        200,
        json={
            "responses": [
                {
                    "textAnnotations": [
                        {"description": "all"},
                        {
                            "description": "legacy",
                            "boundingPoly": {"vertices": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]},
                        },
                    ]
                }
            ]
        },
    )
    assert is_ocr_configured(ctx) and automatic_ocr_enabled(ctx)
    result = await perform_ocr("legacy-injected-image", client=client, ctx=ctx)
    assert result[0]["text"] == "legacy"
    assert client.post.await_count == 1


@pytest.mark.parametrize("coords", [[123, 456], [0.5, 1.5], [1000, 1000]])
def test_operator_translation_uses_public_rounding_and_pixel_endpoints(coords):
    from artemis.agents.operator.operator import OperatorNode
    from artemis.config.agent import MemoryTranscriptConfig

    ctx = context()
    snapshot = create_snapshot(ctx, png())
    state = SimpleNamespace(
        operator_raw_data={"snapshot": snapshot.model_dump(), "width": 1080, "height": 2400}
    )
    node = OperatorNode(ctx, transcript_config=MemoryTranscriptConfig(enabled=False))
    actions, error = node._translate_and_validate_tool(
        {"name": "click", "args": {"target": coords, "target_description": "Search"}}, state
    )
    if coords == [0.5, 1.5]:
        # Agent and wire schemas declare integer arrays. Fractional input must
        # be rejected before adapters can truncate it into a different target.
        assert not actions and "integers" in error
        return
    assert error is None
    assert actions[0]["coordinates"] == denormalize_point(*coords, 1080, 2400)
    if coords == [123, 456]:
        assert actions[0]["coordinates"] == [133, 1094]
    if coords == [1000, 1000]:
        assert actions[0]["coordinates"] == [1079, 2399]


@pytest.mark.parametrize("automatic", [True, False, None])
@pytest.mark.parametrize("minimax", [True, False])
def test_sdk_builder_and_execution_setup_preserve_ocr_configuration(
    monkeypatch, tmp_path, isolated_llm_provider_registry, automatic, minimax
):
    from artemis.config.llm import LLMConfig, _expand_default_into_nodes
    from artemis.context import ArtemisContext
    from artemis.sdk.agent import Agent
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder
    from third_party.mobile_use.context import DeviceContext

    if minimax:
        isolated_llm_provider_registry.add(
            name="mini-builder",
            kind="anthropic",
            vendor="minimax",
            model="MiniMax-M3.1-Flash",
            api_base="https://vision.invalid/anthropic",
            api_key="fixture-only-not-a-key",
        )
    provider = "mini-builder" if minimax else "google"
    llm_config = LLMConfig.model_validate(
        _expand_default_into_nodes(
            {
                "default": {
                    "provider": provider,
                    "model": "default" if minimax else "gemini-2.5-flash",
                    "fallback": {
                        "provider": provider,
                        "model": "default" if minimax else "gemini-2.5-flash",
                    },
                }
            }
        )
    )
    loaded = AgentGlobalConfig.model_validate({"ocr": {"automatic": automatic}})
    monkeypatch.setattr(
        "artemis.sdk.builders.agent_config_builder.load_agent_config", lambda: loaded
    )
    monkeypatch.setattr(
        "third_party.mobile_use.sdk.builders.agent_config_builder.get_default_llm_config",
        lambda: llm_config,
    )
    built = AgentConfigBuilder().build(validate_profiles=False)
    assert built.ocr.automatic is automatic
    assert built.ocr is not loaded.ocr
    ctx = ArtemisContext(
        device=DeviceContext(device_id="fake-device"),
        llm_config=built.default_profile.llm_config,
        agent_config=built,
    )
    assert automatic_ocr_enabled(ctx) is (automatic if automatic is not None else not minimax)
    # Exercise the real per-task copy without initializing the SDK or a device.
    agent = object.__new__(Agent)
    agent._config = built
    agent._session_id = None
    agent._tmp_traces_dir = tmp_path
    agent._prepare_trace_paths = lambda task: "fixture"
    engine = object.__new__(DataEngine)
    engine.start_session = MagicMock()
    monkeypatch.setattr("artemis.sdk.agent.DataEngine", lambda **kw: engine)
    task = SimpleNamespace(
        id="fixture",
        request=SimpleNamespace(
            record_trace=False, enable_remote_tracing=False, profile="flash", goal="fixture"
        ),
    )
    Agent._prepare_tracing(agent, task, ctx)
    assert ctx.execution_setup.ocr.automatic is automatic


@pytest.mark.asyncio
async def test_real_sdk_images_for_detection_and_ocr(monkeypatch):
    requests = []

    async def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        output = found() if len(requests) == 1 else text_output()
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
                "usage": {"input_tokens": 9, "output_tokens": 8},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sdk = anthropic.AsyncAnthropic(
        api_key="fixture-only-not-a-key",
        base_url="https://vision.invalid/anthropic",
        http_client=client,
        max_retries=0,
    )
    underlying = ModelFactory.create_model(endpoint())
    underlying._async_client = sdk
    wrapped = RobustChatModelWrapper(underlying, endpoint=endpoint())
    monkeypatch.setattr("artemis.agents.object_detector.minimax.get_llm", lambda *a, **kw: wrapped)
    ctx = context()
    data = png((1000, 1000))
    snapshot = create_snapshot(ctx, data)
    try:
        result = await _run_object_detection(ctx, data, ["Search"], snapshot=snapshot)
        ocr = await recognize_ocr(
            base64.b64encode(data).decode(), ctx=ctx, snapshot=snapshot, llm=wrapped
        )
    finally:
        await client.aclose()
    assert result["detected"][0]["point"] == [800, 200]
    assert ocr.status == "complete" and ocr.regions[0].text == "搜索"
    assert len(requests) == 2
    for request in requests:
        content = request["messages"][0]["content"]
        image = next(block for block in content if block["type"] == "image")
        assert image["source"]["media_type"] == "image/png"
        assert base64.b64decode(image["source"]["data"]) == data
        assert request["model"] == "MiniMax-M3.1-Flash"
