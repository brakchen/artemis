# 实施检查点

## 当前状态

- 用户批准按技术方案 v1.2 完整实施六模块；整项 active。A1/A2/B1 已实现验证，B1 准备提交推送；之后 B2 → C → D1/D2/D3 → E，不以局部完成结束。
- 分支 feat/model-config，B1 基线 fb7389c。root 唯一 Git owner，源码始终单 writer；root 只管理文档/依赖。当前全部 source writer 停写，B1 提交后交 fresh B2 writer。
- 规格唯一来源：技术方案；实施导航：30-next-slices.md；验收与审查证据：90-evidence.md。tower/Pi/origin-master 环境例外见 handoff/ACTIVE.md。

## 已完成及稳定契约

- A1 3d5da4e：Provider kind/vendor、秘密保留/清除、锁/原子保存/Windows ACL、角色解析与任务端点快照；全量2602 passed/8 skipped。
- A2 fb7389c：raw gate、schema 验证、共享 logical_call_scope、同 wire 主备去重、独立搜索及业务错误；全量2713 passed/8 skipped/89 warnings/165.79s。
- B1 最终：MiniMax utility 定位、全局 OCR 分区/typed outcome/版本缓存/共享取消、截图来源与变换、Explorer/Operator/Flash 观察、SDK OCR 三态。独立 spec/quality 全部具体发现闭环；父全量2802 passed/8 skipped/89 warnings/158.23s/exit0，日志 scratch/b1-final-unit-2.log；42 Python Ruff/format 和 diff 通过。
- B1 最终43项 manifest identity e1a928a10d3087d7702bd91fc99620b806f753050738b490cca3830a1359e2d1，作者 implement_vision 已停写。首轮全量两失败已修，未知设备 ID 保持 None/invalid；Planner 源码/原断言未动，fixture 仅 observe_screen 四字段和 SnapshotRef additive，同步说明须进 commit body。
- services.llm.get_effective_endpoint(ctx,name,is_utils=False,use_fallback=False)：default 全局；object_detector/video_analyzer utility；summarizer agent。私有 _llm_endpoint_snapshot 固定 registry/主备/角色，不另建 resolver/cache。
- llm.minimax normalize_minimax_error/validate_minimax_response/minimax_wire_target；reliability.logical_call_scope(max_attempts=8,timeout_seconds=180)。web_search 的角色决定授权、default pinned endpoint 决定 wire。
- Provider metadata 编辑除 Key/enabled 外全量替换，D 必须传完整候选；Key 空/省略保留，首次拒绝，clear 独立。不要按模型名或 host 推断 vendor。
- create_snapshot 只用于真实 capture；snapshot_for_image 必须有明确 origin，None 始终 unbound。invalidate_snapshot 递增版本并清 current，B2 尚需实际派发接线。
- 出屏语义行保留编号，bbox=None/visible=False/actionable=False；B2 直接阻断，不允许视觉兜底升级。partial OCR 的确定区域仍可用。

## 后继交接

- B2：typed allow/block/unknown、同快照 XML 可操作证据；Pro 单动作/burst 与 Flash/MCP 共用；ActionSession 真正派发队列 recheck/invalidate，含取消与并发；unknown side effect 不重发；click_sequence/旧索引屏障/公共坐标/StateView writes。详细导航已记录。
- C 只读准备 prepare_media_contract 已完成：native source 视频、ASR multipart/错误别名、全时段采样、消费者状态、独占临时媒体清理。不可删借用录屏/共享目录，speech 不等于非语音声学分析。
- D1 只读准备 prepare_aux_runtime 已完成：取消 init 可选 Google ping；typed memory/builder/task pin、aux fields_set 和模型参数重建、统一所选配置、Session 公共实际模型快照、历史显示、Replay。不得 dump/revalidate 洗掉配置显式性。
- D2/D3：Provider API/CLI/UI 候选与初始化迁移、诊断去秘密、最小容器注入和发行物；E 完整 C01–C32、无 Google 请求混合残留测试、README/Changelog。

## 环境与边界

- uv/.venv Python3.14.6；filelock/pywin32/jsonschema/referencing 直接依赖均沿用锁定版本，无版本升级。
- 单测只给进程 LOCALAPPDATA=scratch/test-localapp，移除 ARTEMIS_APP_DIR；后者全局设置会破坏 platform 测试。
- UI 仅基线 npm ci/build 已过，D 修改后必须 rebuild；最终 wheel/非 repo cwd/资源/容器 mount 契约仍待验证。
- 无真实 Key 读取/输出、付费调用、SDK.initialize 或真实设备测试。actual SDK 用 httpx.MockTransport，控制器 fake；权限、定位精度与实机闭环 needs-verification。
- TDD off / Decision skipped，无 strict/test-first 要求，保留有意义的离线回归。不得弱化已有测试/Host-Origin 边界。
- DriftCheck continue：六模块全 MiniMax、legacy 兼容、复用现有 owner；不移除 Google SDK，不部署/合并。恢复时检查 Git/代理状态后沿 active slice 继续。
