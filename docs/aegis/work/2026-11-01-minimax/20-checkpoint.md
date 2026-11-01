# 实施检查点

## 当前有效状态（后文早期记事以此为准）

- A1 已提交推送 `3d5da4e`；A2 spec/quality 全部具体发现已关闭，root 新鲜全量2713 passed/8 skipped/89 warnings/165.79s/exit0，日志 scratch/a2-final-unit.log；Ruff18文件和diff通过，正准备 A2 提交。
- 当前下一片段 B1（尚未开始源码），之后 B2 → C → D → E，用户整项开发请求仍 active，不得提前最终完成。后续精确接线地图见 30-next-slices.md，行为契约见设计 v1.2。
- A2 公共契约：llm.minimax 的 normalize_minimax_error/raise_for_minimax_error/minimax_wire_target/validate_minimax_response；llm.reliability.logical_call_scope(max_attempts=8,timeout_seconds=180)；services.web_search.web_search(ctx,query,*,role='default',max_continuations=2)。role 决定搜索授权，default pinned endpoint 决定 wire 模型/Key。所有内容原样由 SDK 序列化，MiniMax server result.content 与 Anthropic encrypted_content 均支持。
- 复用 JSONSchema/referencing public Resource/Registry API 做本地 schema 预检，真远程或未支持 dynamic/recursive 引用请求前失败；合法 $ref 字段/const/nested$id 保持；Outputter null/{} 不当失败或退 raw。共享预算跨结构修复/主备/搜索，same-wire 忽略effort/timeout差异去重。
- A2 原实施者与两名 reviewer 均已停止源码写入；root sole Git owner，待提交后再交 B1 fresh writer。root 管 docs/依赖，source writer 始终串行。filelock/pywin32/jsonschema/referencing 已明确直接依赖，uv.lock 同步且无包版本升级。
- 环境：uv/.venv Python3.14.6，完整unit仅进程 LOCALAPPDATA=scratch/test-localapp，不设 ARTEMIS_APP_DIR。UI基线已build，D修改后需重建。无Key/模型付费/设备测试，真权限与视觉精度仍needs-verification。

## 前期记事（审查过程，不代表当前阻断）

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
- A1 已提交并推送 `3d5da4e`（此前登记提交 `4c94fec`）；最终 full unit 是 2602 passed/8 skipped/89 warnings/170.78s，源码审查已关闭发现。
- A2 source writer `/root/implement_protocol_search`，从 `3d5da4e` 开始，packet 要求 raw gate/JSON Schema/预算去重/搜索真实注册与业务错误，源码写入串行；root 只读准备 B 并管理依赖/docs。jsonschema 直接依赖由 root 添加，已有锁版本4.26.0；待 uv lock。
- 待办：A2 → B1 → B2 → C → D → E，完整验收映射见 10-intent.md；A1 不等于六模块已完成。
- A2 已 source freeze，源码写权交回 root：新增 minimax 协议 gate、JSON Schema 验证、共享 logical_call_scope、独立服务端搜索和真实工具注册。实施者报告相关251 passed/4 skipped，最终协议69 passed；root 新鲜协议69 passed。独立 spec review `/root/review_a2_spec` 进行中，之后质量审查和全量验证再提交。
- A2 jsonschema 直接依赖与 uv.lock 已同步，无包版本升级；文档/锁由 root 编辑。后续 B 扩大 lane 到实际截图 driver/controller 及 MCP device_state 的元数据接线，未开始源码修改。
- 已知外部验证边界：没有使用真实 MiniMax Key；没有设备任务；SDK 请求夹具与模型实际能力分别报告。
- DriftCheck：continue，用户已授权实施；保留现有兼容逻辑，不做 Google 依赖瘦身、不部署/合并。
- Resume：先读 10-intent.md、当前检查点与技术方案，再检查 git diff/status 和依赖会话，沿 active slice 继续；不得将尚未执行的 C 项当完成。
