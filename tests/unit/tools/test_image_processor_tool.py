# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from artemis.context import ArtemisContext
from artemis.graph.state import State
from artemis.tools.image_processor_tool import get_ask_image_processor_tool
import pytest


@pytest.mark.asyncio
async def test_ask_vision_coder_success():
    # Mock Context and State
    mock_ctx = MagicMock(spec=ArtemisContext)
    mock_ctx.data_engine = None

    mock_state = MagicMock(spec=State)
    mock_state.latest_screenshot = "/tmp/test_screen.jpg"

    # Mock VisionCoder Agent Outcome
    vision_coder_outcome = {
        "new_image_path": "/tmp/coder_output.jpg",
        "summary": "Found red button at pixel coords (100, 200)",
    }

    # We need to patch the ImageProcessor class inside the tool
    mock_coder_instance = MagicMock()
    mock_coder_instance.run = AsyncMock(return_value=vision_coder_outcome)

    with patch(
        "artemis.agents.image_processor.image_processor.ImageProcessor",
        return_value=mock_coder_instance,
    ):
        # Retrieve the tool
        ask_vision_coder_tool = get_ask_image_processor_tool(mock_ctx)

        # Invoke the tool
        result = await ask_vision_coder_tool.ainvoke(
            {
                "instruction": "Find the red button",
                "target_image_id": "img_0",
                "state": mock_state,
            }
        )

        # Verify the agent was called with correct arguments
        mock_coder_instance.run.assert_called_once_with(
            "Find the red button", "/tmp/test_screen.jpg"
        )

        # Verify the output matches
        assert result == vision_coder_outcome


def test_ask_image_processor_tool_subclass():
    """Verify AskImageProcessorTool is an ArtemisTool subclass."""
    from artemis.tools.base import ArtemisTool
    from artemis.tools.image_processor_tool import (
        AskImageProcessor,
        AskImageProcessorTool,
        AskVisionCoderArgs,
        AskVisionCoderTool,
        ImageProcessorTool,
        ask_image_processor,
    )

    assert issubclass(AskImageProcessorTool, ArtemisTool)
    assert issubclass(AskImageProcessor, ArtemisTool)
    assert issubclass(AskVisionCoderTool, ArtemisTool)
    assert issubclass(ImageProcessorTool, ArtemisTool)
    assert isinstance(ask_image_processor, ArtemisTool)
    assert isinstance(ask_image_processor, AskImageProcessorTool)

    assert ask_image_processor.name == "ask_image_processor"
    assert ask_image_processor.category == "custom"
    assert ask_image_processor.args_schema == AskVisionCoderArgs

    # GenAI FunctionDeclaration export
    declaration = ask_image_processor.to_genai_declaration()
    assert declaration.name == "ask_image_processor"
    assert "instruction" in declaration.parameters.properties
    assert "target_image_id" in declaration.parameters.properties


def test_legacy_image_processor_tool_keeps_sync_invocation():
    ctx = MagicMock(spec=ArtemisContext)
    ctx.data_engine = None
    processor = MagicMock()
    processor.run = AsyncMock(return_value={"outputs": [], "summary": "legacy fixture"})
    with patch(
        "artemis.agents.image_processor.image_processor.ImageProcessor", return_value=processor
    ):
        result = get_ask_image_processor_tool(ctx).invoke(
            {"instruction": "legacy image edit", "target_image_id": "img_0"}
        )
    assert result == {"outputs": [], "summary": "legacy fixture"}
    processor.run.assert_awaited_once_with("legacy image edit", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "pool_shape",
    [
        "root_list",
        "entry_list",
        "existing_entry_list",
        "transform_list",
        "annotations_list",
        "path_object",
        "missing_transform",
        "empty",
        "valid",
        "partial",
    ],
)
async def test_legacy_image_pool_shapes_remain_recoverable(
    monkeypatch, tmp_path, caplog, pool_shape
):
    from artemis.agents.image_processor import image_processor as module

    context = SimpleNamespace(llm_config=SimpleNamespace(), data_engine=None)
    llm = MagicMock()
    llm.bind_tools.return_value = llm
    llm.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "execute_python",
                        "args": {"code": "synthetic fixture output"},
                        "id": "step1",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_result",
                        "args": {"summary": "recovered"},
                        "id": "step2",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )
    monkeypatch.setattr(module, "get_llm", lambda *args, **kwargs: llm)
    monkeypatch.setattr(module.settings, "TRACES_PATH", tmp_path / "traces")
    image = tmp_path / "fixture.jpg"
    image.write_bytes(b"synthetic image; byte transport only")
    valid = {
        "path": str(image),
        "transform": {"offset_x": 0.0, "offset_y": 0.0, "scale_x": 1.0, "scale_y": 1.0},
        "annotations": {"D1": [0, 0]},
        "is_output": True,
    }
    pools = {
        "root_list": [],
        "entry_list": {"img_1": []},
        "existing_entry_list": {"img_0": []},
        "transform_list": {"img_1": {**valid, "transform": []}},
        "annotations_list": {"img_1": {**valid, "annotations": []}},
        "path_object": {"img_1": {**valid, "path": {}}},
        "missing_transform": {"img_1": {"path": str(image)}},
        "empty": {},
        "valid": {"img_1": valid},
        "partial": {"img_1": valid, "img_2": []},
    }
    closed = []

    class SyntheticExecutor:
        def __init__(self, session_dir):
            self.directory = session_dir

        def execute(self, code):
            pool = next(self.directory.rglob("intermediate_transforms.json"))
            pool.write_text(json.dumps(pools[pool_shape]), encoding="utf-8")
            return "synthetic executor result"

        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "PythonExecutor", SyntheticExecutor)
    processor = module.ImageProcessor(context)
    result = await processor.run("recover from malformed pool", str(image))
    assert result["summary"] == "recovered" and closed == [True]
    assert llm.ainvoke.await_count == 2
    if pool_shape in {"valid", "partial"}:
        assert list(processor.intermediate_image_pool) == ["img_0", "img_1"]
        assert result["outputs"][0]["image_id"] == "img_1"
        assert result["outputs"][0]["annotations"] == {"D1": [0, 0]}
    else:
        assert list(processor.intermediate_image_pool) == ["img_0"]
        assert result["outputs"][0]["image_id"] == "img_0"
    if pool_shape not in {"valid", "empty"}:
        assert "Failed to read pool_json_path" in caplog.text
