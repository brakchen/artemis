# 实施检查点

- 状态：active；已批准方案 v1.2，A1 完成，继续 A2。
- 当前片段：A2 原始响应、结构化输出、搜索与业务错误。
- 完成：方案、两轮复核、Changelog；实现基线 `8e41b19`。
- 已建立：`uv sync --frozen --extra dev` 成功，Python 3.14.6；`npm ci` 与现有 UI `npm run build` 成功。协议 mock 实验证明锁定 ChatAnthropic 保留 thinking/server_tool_use/search result/text/stop_reason/usage，未联网模型。
- A1 实施者 `/root/implement_foundation`：源修改在未提交 diff；新接口位于 `artemis/llm/resolution.py`，services 重导出 `get_effective_endpoint(ctx, role, *, is_utils=False, use_fallback=False)`、`context_uses_minimax(ctx)`、`get_effective_default_endpoint(config=None)`。支持 default 全局角色；private `_llm_endpoint_snapshot` 固定全部真实角色主备。注册表 key 省略/空保留、显式 clear；其他 metadata 编辑当前是全量提交，UI/CLI 后续须传完整候选。
- A1 窄测：115 passed（foundation/provides/gateway/grounding/config），ruff passed。第一次 full unit：4 failed、2583 passed、8 skipped（190.26s）。platform 两例因 root 设置全局 ARTEMIS_APP_DIR，IPC 一例是顺序/背景线程干扰；改测试进程 LOCALAPPDATA 到 `scratch/test-localapp` 而不设 ARTEMIS_APP_DIR，三例隔离重跑均通过。不要修改这些测试预期。
- A1 确认回归：test_planner_validation_node_defaults_to_lightweight_judge；实施者正在保留 legacy 可选 judge 默认同时使 MiniMax 配置继承全局。
- Spec reviewer `/root/review_a1_spec` 要修 Windows temp/最终注册表 DACL（复用 pywin32，秘密写入前保护）、显式 JSON/旧配置环境路径优先于常规 JSONC；另保留 MiniMax enable_grounding 应用级开关供 A2 映射搜索。root 还要求 M3 显式 max_tokens 初值。等待实施者冻结后定向复审，再独立 quality review。
- 依赖：root 已加 filelock 直接依赖并 `uv lock` 成功（所有版本保持，锁格式 revision 由当前 uv 重新生成）；刚加 pywin32 Windows 条件直接依赖，待下一次 uv lock。A2 再加 jsonschema 直接依赖；其 4.26.0 已锁定/安装。
- A2 准备：既有 structured/reliability 基线 13 passed。需要扩展 acomplete_structured 首次 schema/JSON 提示、支持 JSON Schema dict，并通过 MiniMax wrapper.raw gate 检查终止状态；with_structured_output 的 MiniMax 分支复用该路径，保留 legacy。不要将合法 JSON 无 tool_call 判失败。Outputter schema失败不得退原文冒充结构成功，planner advisory异常当前也会返回success，需按MiniMax契约处理。搜索新增普通工具，内部独立 Messages 会话+server tool/pause_turn，role=default，不发送手机历史。供应商业务错误分类与同端点主备去重尚未实施。
- 后续只读准备已完成：`/root/prepare_vision_slice` 和 `/root/prepare_entry_slice` 的协作 final 消息提供 B/D 精确函数地图，可读取协作消息或重新按原路径查源；没有任何其他代理修改文件。B 需要 driver/controller 元数据少量改动，届时更新 lane scope。
- A1 最终结果：spec/quality 发现均修复并定向复核关闭；定向132 passed。root 新鲜全量 `uv run --frozen pytest tests/unit -q`（仅进程 LOCALAPPDATA 隔离、无全局 ARTEMIS_APP_DIR）2602 passed、8 skipped、89 上游警告，170.78 秒；日志本机 `scratch/a1-final-unit.log`。root ruff11文件及diff检查通过。依赖锁已更新且无包版本升级。
- 审查补充：legacy snapshot 固定未使用角色的缺 Key 状态，但只在请求该角色时失败；MiniMax 仍整任务严格预检。LLM/Provider/Endpoint 的 ValidationError 与加载日志隐藏输入值，防误粘 Key。quality 最后发现的 flat+partial default 合并由 Operator 物化基准，独立角色模型保留。
- A1 helper 契约已冻结，A2 必须复用：get_effective_endpoint/default 返回 ModelEndpoint，api_key exclude/reprFalse；endpoint.identity_key 隔离可靠性状态，enable_grounding 是应用开关，不作为 MiniMax SDK 参数。
- 待办：A2 → B1 → B2 → C → D → E，完整验收映射见 10-intent.md；A1 不等于六模块已完成。
- 已知外部验证边界：没有使用真实 MiniMax Key；没有设备任务；SDK 请求夹具与模型实际能力分别报告。
- DriftCheck：continue，用户已授权实施；保留现有兼容逻辑，不做 Google 依赖瘦身、不部署/合并。
- Resume：先读 10-intent.md、当前检查点与技术方案，再检查 git diff/status 和依赖会话，沿 active slice 继续；不得将尚未执行的 C 项当完成。
