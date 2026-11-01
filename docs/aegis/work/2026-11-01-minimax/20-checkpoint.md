# 实施检查点

- 状态：active；已批准方案 v1.2，代码尚未实施。
- 当前片段：A1 注册表、配置与模型工厂。
- 完成：方案、两轮复核、Changelog；实现基线 `8e41b19`。
- 进行中：`uv sync --frozen --extra dev` 建立隔离 .venv；接着运行现有模型相关测试基线。
- 待办：A1 → A2 → B1 → B2 → C → D → E，完整验收映射见 10-intent.md。
- 已知外部验证边界：没有使用真实 MiniMax Key；没有设备任务；SDK 请求夹具与模型实际能力分别报告。
- DriftCheck：continue，用户已授权实施；保留现有兼容逻辑，不做 Google 依赖瘦身、不部署/合并。
- Resume：先读 10-intent.md、当前检查点与技术方案，再检查 git diff/status 和依赖会话，沿 active slice 继续；不得将尚未执行的 C 项当完成。
