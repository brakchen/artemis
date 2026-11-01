# 后续片段接线地图

这是 10-intent 的实施导航，行为规格仍以技术方案 v1.2 为准。下面是基线阅读结果，不能当作已经修改或验证。

## B1 识别与来源

- object_detector/object_detector.py：MiniMax 用 utility-scoped get_llm；旧 get_llm 异常退 Operator、宽泛错误变 []、无条件 yx 交换均不可用于 MiniMax。新 schema 的 found 恰好 1、ambiguous 至少 2、not_found 为 0；bbox 有限/非 bool/LTRB/0–1000；point 与 snapshot/source 程序赋值。允许一次有上下文裁剪复核，映射回原图。真正 not_found 才进 failed，服务错误不能伪装未找到。
- tools/object_detection_tool.py、Explorer run_setup._run_flash / perception_tools.exec_detect_objects / tools/explorer_tool.py 候选解析与 register_candidates 会丢 bbox、status 和 provenance；所有模式与裁剪/缩放逆变换都要保留。不要让 ambiguous 候选成为可直接派发的 detected。
- Explorer._resolve_model_name/_detect_native_engine 目前按 raw Gemini 名、残留 Key/shared Google Client 选引擎；MiniMax 必须按 pinned actual vendor 走 universal。
- utils/ocr_api.py 增加 typed MiniMax 入口，默认角色为 default。PIL 取得输入实际尺寸；转换为旧 pixel position 多边形，不加状态栏偏移。图像分区需要去重/映射，unknown 字符不推断补字。
- OCR 全部调用点：graph/perception.py、Checker._capture_final_screen、Explorer.run_setup._build_fused_hierarchy / exec_get_ocr_list、tools/mobile/ocr.py、artemis/mcp_server/tools/device_state.py。传真实任务上下文；周期自动 OCR 在 MiniMax 默认 off，显式工具/Explorer lazy request 仍能用；legacy 默认不变。
- DataEngine.get_or_create_image 只按 hash 且只填 None，Storage.create_image 会把 [] 写 NULL，update_image_data 仅 COALESCE。复用 extra_metadata['ocr_cache'] 存按 endpoint/vendor/model/prompt/schema/coords/transform 限定的结果，原子合并；旧无版本/Google 结果不可命中，失败 [] 不缓存；同图并发请求合并。Explorer 不能绕过新版本缓存读取旧 ocr_result。
- 复用截图/ObserveResult/ScreenIndex 与 operator_raw_data 传小型 immutable SnapshotRef，包含 capture id/hash、device/display、尺寸/rotation、state version、capture validity。hash 不是唯一有效性依据；验证截图采集本身不应使决策版本失效，派发屏幕变化才失效。
- drivers/base.py ScreenData、controllers/device_controller.py ScreenDataResponse、vendored unified_controller、Android fallback1×1 必须携带真实/无效采集信息。geometry 当前 norm1000 可得到 width 越界；统一 public coordinates 的 round + width-1 点边界，bbox edge 单列，有限数字与真实尺寸严格。原几何测试断言的越界是待修契约，不能保留错误；其他 legacy/副作用测试不随意放宽。

## B2 执行门禁

- Validator._run_precondition_gate / precondition_pixel.py 当前 missing is_present=True、低置信 False、模型/裁剪异常可 fail-open；MiniMax typed allow/block/unknown，未知阻断，不根据缺字段放行。
- execution_loop.py Pro 单动作已有 gate，burst 跳过；Flash 走 McpActionExecutor，需在这些现有边界共享严格检查，不能只改检测 JSON。XML/OCR/模型所有 Agent index target 都保留来源；独立用户明确坐标工具沿用现有直接调用契约。
- 同快照 UI 树验证可操作节点/最近可点击父级/可见交集；不能机械用跨多个子项父容器的中心。XML 证据充分时无需付费验证；缺乏证据才局部视觉验证；仍不能确认则 unknown。旋转/设备/显示/尺寸/版本不符要求重新观察。
- Operator target translation、McpActionExecutor._resolve_index 保留所有 provenance。首个改变屏幕动作后，Pro burst 与 Flash 剩余旧 index 动作停止；非派发 web_search/getstate 不作为改变屏幕动作。
- 设备结果 not_dispatched/dispatched/unknown；旧 Pro loop 捕获全部异常重发可重复副作用，只有确定未执行可有界重发；命令可能生效再 timeout 只派发一次、记录 unknown 并观察。保留 legacy 多动作测试，新增 MiniMax strict 模式测试。

## C 视频与原生语音

- VideoAnalyzer._init_engine/_resolve_video_processing 用 actual pinned utility endpoint；MiniMax auto/static 支持，显式 Gemini-only agentic 返回 unsupported，不静默切 static。
- _invoke_universal_model 及外层 _invoke_with_retry 当前多层重试；MiniMax 复用 A2 canonical recovery/logical_call_scope，永久错误不重试成字符串。
- universal_engine 自动 raw audio + 拒绝后删音频重试改为 MiniMax ASR；音频 required/optional/no-track/unsupported 分开。run_universal 的循环耗尽、子工具失败、空最终答案不能 success；内部 complete/partial/incomplete/failed，旧 tuple 仅 complete 对应 success。
- 官方 ASR：POST https://api.minimax.cn/v1/speech_to_text，Bearer，model asr-1.0，multipart，verbose_json + stream=false 返回 segments；单片 <=500秒/50MB，偏移加原录屏时间，必要重叠去重。自定义网关无已核实 native base 不把 Key 发往官方主机。
- 首版关键帧，必要时本地片段内联 video（source.type=url/base64，媒体限制50MB/总body64MB含base64膨胀，图像10MB）；不启用 Files 上传。官方 video_understanding 文件最长7天，删除 purpose 尚未核实，不能承诺主动清理；Files 保持可选后续升级。
- 资源清理仅本任务创建临时文件/客户端，保留原始录屏；取消/失败也清理，不掩盖原错误。ASR 业务错误复用 A2 normalizer，不能凭 HTTP200 报成功；不要伪造 text token 用量。

## D 入口与配置体验（可按 D1/D2/D3 串行拆分）

- apps/admin_console/routers/system.py Provider Request/Test/Models + CLI commands/providers.py + credentials_validator：共用完整待保存 candidate；kind/vendor/base 从草稿优先，空 Key 编辑复用/首次拒绝/clear 冲突一致。A1 registry 除 Key/enabled 外 metadata 为全量 replacement，D 必须发送完整候选。目录查询不是图片/工具能力认证，未知 vendor 明确 unsupported。
- Angular 首页/Service/types/HTML/zh.ts：协议/厂商与 MiniMax preset、manual model entry、编辑保留 Key、保存后清空输入；去掉 currentApiKey/apiKeysMap/savedGemini/OCR 从诊断回填秘密。所有新字符串 t pipe，完成 npm run build。
- CLI init：显式手动/环境导入 MINIMAX_API_KEY/MINIMAX_BASE_URL（bootstrap-only），已有 Key 非显式 replacement 不覆盖；不整体覆盖 .env、不写 Gemini ARTEMIS_DEFAULT_MODEL。规范化选中主配置全部主备与 utility、Flash summarizer/memory chunking 显式模型/旧参数，保留无关配置；先验证 candidate 再受保护备份/发布，失败恢复本次写入，不声称多文件原子。
- credentials_probe metadata 目前包含 raw_key/key/current_key/api_keys；保留旧 probe IDs、公开仅布尔/掩码/来源；readiness/doctor/设备选择/嵌套缓存全检查。MiniMax OCR 不要求独立 Google Key。MCP diagnose._verify_credentials 改内部 resolver 取真实注册凭据，脱敏后不能返回 [] 假装验证；更新 Gemini .env 专用修复建议。
- SDK._prewarm_llm_connections 在任务 ctx 前被调用，当前残留 Google Key 会初始化两个 Google client+付费 ping；先 resolve selected config，非 Google 不预热。FlashRunner._init_llm catch-all Google fallback 不适用 MiniMax。
- VisualStepSummarizer 显式 model_name 强制 Google，且 get_llm summarizer 错用 is_utils=True；StepCapsuleLens._get_llm/_get_fallback_llm 与 HistoryChunkManager 同样 raw Google。全部从同任务 pinned summarizer endpoint 获取；显式 auxiliary 模型覆盖须迁移/校验，不能偷偷忽略或产生 Google fallback；后台 retry 也尊重 canonical 永久错误/预算。
- ModelService 去 raw Operator/10s 旧缓存，SDK._prepare_tracing 将 public 实际模型快照放 Session.device_info；tasks/sessions 读取历史快照，不用当前全局模型污染历史。
- ReplayManager.create_ctx 不能吞无效 config→None；replay_step_tool 仅真实 Google 路径 patch AsyncModels，MiniMax 走标准 trace，不创建 Google client。
- docker_service/Compose：最小选定 provider secret JSON 与角色配置文件 readonly mount，APP_DIR runtime其他子目录仍可写；统一显式配置路径。禁止整个host用户目录/全部hostKey环境转发。缺注入明确错误，容器存在 ≠已配置。源码/wheel/非repo cwd验证，wheel携带新UI build产物。

## E 整体证据

- 完整 C01–C32 映射；Google client/HTTP 全拒绝 spy 下 MiniMax 六模块与辅助入口的 offline 调用，残留 Google Key/raw Gemini/shared Client 仍不转厂商。新 schema/动作门禁测试不以语法正确证明目标语义正确。
- 每片段 fresh unit suite；环境仅进程 LOCALAPPDATA 指 scratch/test-localapp，不设置 ARTEMIS_APP_DIR（会破坏 platform 单测）；无 Key/设备/付费服务。最终 UI build + wheel 资源/非repo cwd +容器 mount 契约验证；真模型权限、定位精度和实机另记 needs-verification。
- root sole Git owner，显式逐路径stage、提交并push feat/model-config，不合并/部署。同步技术方案 Changelog、20-checkpoint 与90-evidence；完整实现前不发最终完成声明。
