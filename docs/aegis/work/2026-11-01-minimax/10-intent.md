# MiniMax 六模块实施

用户授权：2026-11-01「ok，根据方案开发吧」。唯一行为规格为 [技术方案 v1.2](../../../designs/2026-11-01-minimax-anthropic.md)，验收 C01–C32；本记录只追踪实施，不另造设计。

TaskStartSnapshot：`D:/Codex Project/artemis`，`feat/model-config`，HEAD `8e41b19e710abb19ba0304f15ddb542f7a0f6dd7`；与 origin/feat/model-config 差异 0/0；无 staged/unstaged/untracked 文件，无进行中 Git 操作，仅一个工作区。工作目录/依赖目录中的后续临时文件不提交。保留 legacy 厂商行为、公开工具契约、现有 Host/Origin 边界。

Change Necessity：现代码在类型、请求、媒体与动作路径绑定 Google；仅改配置不能达成全 MiniMax。最小改动为现有注册表/工厂/工具入口的适配，不重建 Agent、重试或遥测框架；决定 code-change。

Complexity Budget：跨模块高复杂度、服务和执行器已有大文件压力，at-risk；按下面片段划分并在现有所有者接线，独立协议/结构化职责可抽小文件，避免将全部逻辑塞进 services/llm.py。编辑职责以现有规范化/动作/媒体所有者为准，不新增通用框架。

TDD Route：Mode off，Decision skipped（未要求严格 TDD）；不强制测试先写顺序，但协议、持久化、错误与动作变更需要有区分力的离线回归。每片段先查看既有测试/消费者，运行对应检查，最终运行完整 unit 与 UI build。无 Key/无设备 mock transport 测试不能替代真实精度验收。

片段及依赖：

1. A1 注册表/配置类型/规范化、统一 resolver、端点/工厂与任务快照（C01–C03/C11/C12/C16/C19–C21/C32）。
2. A2 原始响应、结构化输出、业务错误、搜索与消息计量（C04/C08/C09/C22/C30/C31），依赖 A1。
3. B1 MiniMax 坐标定位、OCR/持久缓存、Explorer 候选及元数据（C05–C07/C14/C25/C26），依赖 A1/A2。
4. B2 Pro/Flash/MCP 动作门禁、快照失效与未知派发结果（C06/C23/C24），依赖 B1。
5. C 视频/ASR、完成状态与资源清理（C27/C28），依赖 A1/A2。
6. D CLI/API/UI、初始化/迁移、诊断鉴权、SDK/MCP/摘要/回放与容器入口（C10–C13/C15/C17/C18/C29/C32），依赖前述接口。
7. E 整体验证、使用文档和设计 Changelog、分支提交/推送；不部署、不合并 main。

所有代码片段串行 writer，协调者唯一 Git owner；技能要求的 spec 与质量审查可以只读交接。当前缺少 tower-do，沿用已记录 Codex 环境例外，不假造 Pi UUID/任务板。外部 API Key 与真实手机均不使用，视觉精度/账号权限维持 needs-verification。

Baseline read set：AGENTS、CONTRIBUTING、README、lane 注册、设计 v1.2、providers/router/services、配置模型、相关测试。既有库：锁定 Anthropic/LangChain、Pydantic、jsonschema；优先复用。源代码语义改变时更新本检查点与证据，不改变已批准的六模块方向。

B 片段编辑边界（2026-11-01 准备，尚未实施）：复用 object_detector/ocr_api/ScreenIndex、现有图片记录 extra_metadata 与动作执行器。新的纯数据 SnapshotRef/定位协议只承载一次截图、坐标和来源，不另建设备或执行框架；缓存更新由既有 Storage 原子读改写，不建设第二缓存库。B1 负责类型化识别、坐标与 provenance 接线；B2 负责同一执行边界的严格 gate/失效/派发结果。现有大文件仅接线或局部修复，独立数据/验证职责可抽专用小 helper。无代码方案无法消除 yx 交换、无上下文 OCR 和 fail-open 派发，故决定 code-change；预算 at-risk，以两个串行片段和 legacy 回归控制。所有新严格行为以实际 pinned vendor=minimax 为界；旧厂商保留原测试/契约，不能用放宽测试吸收回归。
