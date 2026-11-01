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

## A2 协议与搜索

- 复用锁定 ChatAnthropic/Anthropic SDK；不另写 Messages 序列化器。真实 SDK MockTransport 覆盖 thinking/signature、工具参数分片、stop_reason、server search continuation 和 usage，未访问模型服务。
- 首轮 spec 发现并修复：server_tool_use 分片截断在续接时被 SDK 丢弃；搜索来源误采输入 URL；未支持的 dynamic/recursive 引用延后失败；普通搜索工具丢失失败状态。独立复核协议89 passed，structured/gateway/reliability/grounding30 passed，四个原复现均关闭。
- 质量审查补充并关闭：半开熔断遇到 typed terminal/cancel 的收尾及并发等待者超时误释放他人 trial；坏格式 server result 误报完成；schema 通用递归误判合法 `$ref` 字段/常量及嵌套 `$id` 范围；Outputter 合法 null 被误判为无结果及空 schema 被跳过。
- root 对照 [MiniMax 官方 server tools](https://platform.minimax.cn/docs/guides/server-tools) 发现其结果正文为 content，而非 Anthropic encrypted_content；已修正兼容两种形状，并用真实 SDK 两轮 MockTransport 证明 content/page_age/额外字段进入续接 wire，不因 SDK 类型声明而丢弃。
- 质量 reviewer 最终新鲜定向162 passed，边界复查11 passed；实际外层 Outputter 验证合法 null/空 schema 各一次格式请求、无 schema 不格式化。无未关闭 A2 审查阻断。root 最终全量 `uv run --frozen pytest tests/unit -q`：2713 passed、8 skipped、89 上游警告、165.79 秒、exit0；日志 `scratch/a2-final-unit.log`。root 18文件 Ruff 与 diff 检查通过。
- 保留边界：标准 SDK 为消息序列化/计量所有者，应用只增加 MiniMax 协议 gate 与已有可靠性分支；legacy 分支因兼容要求保留，未来只有明确放弃旧厂商才可移除。没有新 Agent、缓存客户端或重试框架。include_raw=True 未使用的解析失败组合不作为已覆盖消费者声称，现业务路径均采用验证后的返回值。
- 显式声明既有传递依赖：jsonschema 4.26.0（MIT，[PyPI](https://pypi.org/project/jsonschema/4.26.0/)）；referencing 0.37.0（MIT，Python >=3.10，[PyPI](https://pypi.org/project/referencing/0.37.0/)、[官方 API](https://referencing.readthedocs.io/en/stable/api/)）。使用库的 schema subresources/resolver，避免自写 JSON Schema 遍历及隐式远程取回；无新增包或版本升级。

## B1 定位、OCR 与来源

- 新边界：MiniMax检测utility scope、typed xy/LTRB/status、一次crop；OCR default-role/分区/重叠合并、complete/partial/empty、同图共用/取消、SQLite来源版本缓存；SnapshotRef与变换贯穿driver/controller、感知、Explorer各模式、indexed/Operator及Flash观察；SDK类型/builder/tracing保留OCR三态配置。
- 作者初轮区域438 passed；独立spec发现并关闭4项：旧SnapshotRef同像素洗新、旧融合OCR藏在ui_tree绕过cache来源、partial文本丢状态、SDK builder吞automatic开关。spec独立180 passed；root原始freshness复现重跑证明旧v0仍v0/invalid、当前origin重开valid、None未知图unbound。
- 质量审查再发现并关闭3项：零面积出屏节点使整帧/Operator崩溃、取消一个OCRwaiter误取消另一个、未裁剪却加状态栏offset。保留原始XML及显示/index顺序，不可见条目标为不可操作；共享work使用局部waiter计数/最后退出cancel+join；只按实际PIL尺寸与实际crop应用offset。
- 最终冻结作者42文件identity `502dbadbeb1a5ba385f0b81480746e78fc7ae53f700c5c7b1c65a7f0c8253284`。独立quality新鲜165 passed/1 warning/5.11s，原始三复现均关闭，hash前后匹配。真实SDK MockTransport证明图像请求、同图cancel-one保留survivor且wire/budget各1、cancel-all完成清理；partial中certain区域仍可注册candidate。
- root42文件Ruff、format --check及diff通过；首轮全量unit 2 failed、2794 passed、8 skipped、89 warnings、177.11秒、exit1，日志scratch/b1-final-unit.log，session70162已结束。失败为Planner旧Mock的device_id进入新增ScreenData str字段，以及observe_screen新增输出未同步既有schema fixture。没有新依赖、真实Key/模型/设备；B2实际动作门禁及D所选文件来源统一仍待各自所有者，不声称整个C14/六模块完工。
- 验证驱动修复分流：PatchShape=新字段producer/serialized contract；CanonicalOwner=ADB capture metadata生产者及canonical action manifest/固定schema fixture，不能给Planner加try/改其断言；UpwardDrillSignal=Mock值类型在新增metadata producer入模型即失败，非网络/设备。Change Necessity=code-change（合法未提供可选metadata不应破坏legacy捕屏）+既有fixture的contract-only同步。TDDoff/skipped，窄复现/邻近capture与schema回归后重新独立复核、fresh全套；已将唯一fixture路径加入lane，未放宽schema比较。
- 两项全量回归已由原作者修复并冻结：未知设备 ID 为 None/invalid（包括 canonical snapshot owner），Planner 源码及原测试无 delta；fixture 仅 observe_screen 输出增加 snapshot/xml_hierarchy/ocr_results/ocr_outcome 和 SnapshotRef，另外14工具及既有输入/描述/输出字段保持。修前2 failed、修后原两例与六例未知 ID 回归8 passed；最终 capture/schema143 passed。质量 reviewer 独立143 passed/1 warning/exit0，43项文件哈希前后相同。最终冻结 identity `e1a928a10d3087d7702bd91fc99620b806f753050738b490cca3830a1359e2d1`，旧42项 identity 仅为历史证据；root42 Python Ruff/format --check 与 diff 再次通过。
- root 第二次新鲜全量 `uv run --frozen pytest tests/unit -q`：2802 passed、8 skipped、89 上游警告、158.23 秒、exit0，日志 scratch/b1-final-unit-2.log。仅进程 LOCALAPPDATA 隔离，未设置 ARTEMIS_APP_DIR。独立审查均无未闭实际阻断；准备显式逐路径提交。尚未实现 B2/C/D/E，不将此结果当作整个迁移验收。
