# C01–C32 验收对账

行为规格见技术方案第 11 节。此表只是执行证据索引；“部分”表示仍有子条件待后继片段验证，不是验收通过。截止 B2 最终全量2943 passed/8 skipped，所有线上账户能力/设备精度均未验证。

| ID | 当前离线证据 | 尚需验证/负责片段 |
| --- | --- | --- |
| C01 | A1 旧 registry、新 kind/vendor、禁用引用预检 | 最终发行入口再查 E |
| C02 | A1 Key 保留/clear、锁/原子写、Windows 权限 | HTTP/CLI 错误与公开报告 D2/D3 |
| C03 | A1 pinned runtime endpoint；B1 detector utility/OCR default | 视频 C，辅助/控制台 D1，入口 E |
| C04 | A2 实际 SDK thinking/signature、ID、流式/续传、raw gate | E 整链截断不派发 |
| C05 | B1 xy/LTRB、逆变换/来源；B2 capture/version/rotation 失效及真实派发点 | E 汇总 |
| C06 | B1 typed结果；B2 uncertain/ambiguous/invalid provenance不升级点击 | E 汇总 |
| C07 | B1 OCR typed/分区/cache；B2页面变化后旧目标失效 | E 汇总 |
| C08 | A2 独立搜索续接/预算/服务端块不本地执行 | E 汇总 |
| C09 | A2 SDK retry=0/同 wire 去重/共享预算；B2 unknown不重发 | 视频/后台 C/D1 |
| C10 | A1/A2/B1 单模块离线分支 | 六模块与辅助 Google 拒绝 spy E |
| C11 | A1 canonical registry 与 models API helper | HTTP/CLI/UI full draft D2 |
| C12 | A1 LLM explicit config 路径 | Agent loader D1，init 环境导入/保留 D2 |
| C13 | 待实施 | diagnostics/UI/doctor/缓存秘密 D2/D3 |
| C14 | B1 各 OCR consumer、offset/partial、共享取消 | 主配置来源 D1，最终入口 E |
| C15 | A2 日志/诊断/委员会工具注册、搜索授权 | E 混合残留配置 |
| C16 | A1 endpoint identity/snapshot，A2 breaker 隔离 | aux task config D1，最终 E |
| C17 | 待实施 | 预热/aux、session public snapshot、历史/Replay D1 |
| C18 | 仅 UI 原基线 build | 容器 D3，更新 UI/wheel/非 repo cwd E |
| C19 | A1 配置规范化/alias/禁用/legacy | 完整可用初始化 D2，E |
| C20 | A1 主配置/profile/旧格式/override，B1 OCR SDK 字段 | memory/aux typed 透传 D1，E |
| C21 | A1 实际 SDK 角色预算/模型参数 | explicit auxiliary 模型默认 D1 |
| C22 | A2 schema 首次指令/真验证/合法 null/raw gate | pixel B2，视频 C，E |
| C23 | B2 Pro单/burst/Flash/MCP strictgate；真实identity/整node消失/旧turn来源/opaqueAgent shell旁路均闭环 | E 汇总 |
| C24 | B2 实际Actuator副作用后timeout只发送一次，unknown记录/观察，明确未发可有界重试 | E 汇总 |
| C25 | B1公共点/bbox；B2所有原始端点先验证、sharedround、实际wire点/bbox可表达/可见父级 | E 汇总与真实精度 |
| C26 | B1 版本 cache、旧历史保留/并发与失败不缓存 | E 汇总 |
| C27 | 待实施 | C 视频终止状态与 processing |
| C28 | 已核实 native SDK wire/ASR 官方协议，未实现 | C 实际离线 ASR/时间/资源/限额 |
| C29 | 待实施 | D3 内部 canonical credential 验证，公开仅结果 |
| C30 | A2 Messages 业务错误/429/SSE 分类 | C ASR aliases/媒体，D1 后台永久错误 |
| C31 | 未引入独立 Messages 薄适配，继续标准 SDK | C 的 native ASR 是不同计量服务；E 明确适用范围，不伪造 token |
| C32 | 待实施 | D2 全主备/aux 迁移候选与保护备份/失败回滚 |

所有测试证据的命令、数量及失败修复见 90-evidence.md。最终需每行指向具体已执行测试/发行验证；不以此索引的建立代替实际验收。
