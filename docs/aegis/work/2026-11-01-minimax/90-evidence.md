# 实施证据

## A1 基础配置链

- 验证对象：`feat: add task-pinned MiniMax Anthropic model configuration` 提交的基础链路；源码范围见提交文件清单。C01–C03/C11–C12/C16/C19–C21 的基础契约子集，不代表 UI/所有模块验收。
- 作者基线 providers/gateway：26 passed；作者最终 foundation/planner/providers/gateway/grounding/config：132 passed；Windows 实际 DACL、独立进程/线程写入和真实 ChatAnthropic+mock transport 均有回归。
- 协调者最终：`uv run --frozen pytest tests/unit -q` → 2602 passed、8 skipped、89 warnings、170.78s、exit 0；运行进程 LOCALAPPDATA 指向本机 scratch，不覆盖 ARTEMIS_APP_DIR。日志 `scratch/a1-final-unit.log` 为本机忽略文件，其他主机不可依赖该路径。
- 协调者：`uv run --frozen ruff check`（11 个实现/测试文件）→ All checks passed；`git diff --check` → exit 0。
- UI 基线：`npm ci --no-audit --no-fund` 与 `npm run build` → exit 0，当前尚未修改 UI，后续必须重新构建。
- 失败及修复：初次全套 4 failed，其中两个受 root 的 ARTEMIS_APP_DIR 环境影响、一个 IPC 顺序偶发（3 个隔离重跑均通过）；余下轻量 judge 默认回归已修。审查发现并修复 Windows ACL、显式路径优先、搜索开关丢失、legacy 无关 Key 预检及 flat+partial default 组合。最后一轮全套没有失败。
- 依赖复用：filelock 3.32.3（MIT，[PyPI](https://pypi.org/project/filelock/3.32.3/)）、pywin32 312（项目元数据 PSF、包内含不同许可，[PyPI](https://pypi.org/project/pywin32/312/)），均为已锁定版本，新增直接依赖；不复制实现。pywin32 仅 Windows，已有 WinAPI 绑定用于受控文件 ACL。
- 边界：未使用真实 Key、模型请求或设备；网络请求仅依赖/官方资料与离线 MockTransport。真实精度、权限及剩余阶段均未验证。保留现有框架与旧厂商，没有 Google 依赖瘦身或部署。
