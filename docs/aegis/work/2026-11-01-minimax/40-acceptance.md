# C01–C32 验收对账

行为规格见技术方案第 11 节。此表只是执行证据索引；“部分”表示仍有子条件待后继片段验证，不是验收通过。C 视频/语音及 MCP 启动修复的独立审查已通过，root 全量结果在 90-evidence.md；所有线上账户能力/设备精度均未验证。

| ID | 当前离线证据 | 尚需验证/负责片段 |
| --- | --- | --- |
| C01 | A1 旧 registry、新 kind/vendor、禁用引用预检 | 最终发行入口再查 E |
| C02 | A1 Key 保留/clear、锁/原子写、Windows 权限 | HTTP/CLI 错误与公开报告 D2/D3 |
| C03 | A1 pinned runtime endpoint；B1 detector utility/OCR default；C 实际视频 utility SDK/ASR | 辅助/控制台 D1，入口 E |
| C04 | A2 实际 SDK thinking/signature、ID、流式/续传、raw gate | E 整链截断不派发 |
| C05 | B1 xy/LTRB、逆变换/来源；B2 capture/version/rotation 失效及真实派发点 | E 汇总 |
| C06 | B1 typed结果；B2 uncertain/ambiguous/invalid provenance不升级点击 | E 汇总 |
| C07 | B1 OCR typed/分区/cache；B2页面变化后旧目标失效 | E 汇总 |
| C08 | A2 独立搜索续接/预算/服务端块不本地执行 | E 汇总 |
| C09 | A2 SDK retry=0/同 wire 去重/共享预算；B2 unknown不重发；C 全视频/ASR作业预算及永久错误 | 后台 D1，整链 E |
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
| C22 | A2 schema 首次指令/真验证/合法 null/raw gate；B2 pixel；C 视频真实 schema/区间及逐模态覆盖 | 整链 E |
| C23 | B2 Pro单/burst/Flash/MCP strictgate；真实identity/整node消失/旧turn来源及三个opaque shell入口已覆盖；新发现ImageProcessor生成Python旁路未修 | E 必须补此旁路并验证全部消费者，不以三个shell已测替代 |
| C24 | B2 实际Actuator副作用后timeout只发送一次，unknown记录/观察，明确未发可有界重试 | E 汇总 |
| C25 | B1公共点/bbox；B2所有原始端点先验证、sharedround、实际wire点/bbox可表达/可见父级 | E 汇总与真实精度 |
| C26 | B1 版本 cache、旧历史保留/并发与失败不缓存 | E 汇总 |
| C27 | tests/unit/agents/test_minimax_video.py：原入口完成/partial/error、请求时段覆盖、缓存及消费者；C 独立真实入口正反路径通过 | 整链 E；真实精度待验证 |
| C28 | tests/unit/test_minimax_media.py 与 test_minimax_video.py：真实 SDK MockTransport/ffmpeg、ASR multipart/offset/限额、来源及重复取消清理 | 真实账号 ASR 权限及效果待验证 |
| C29 | 待实施 | D3 内部 canonical credential 验证，公开仅结果 |
| C30 | A2 Messages 业务错误/429/SSE；C ASR aliases/HTTP200业务失败/媒体分类 | D1 后台永久错误，整链 E |
| C31 | Messages 沿用锁定 SDK，厂商子类仅在完整 payload 上校验媒体限额；native ASR 用量独立，不伪造 text tokens | E 汇总计量与适用范围 |
| C32 | 待实施 | D2 全主备/aux 迁移候选与保护备份/失败回滚 |

所有测试证据的命令、数量及失败修复见 90-evidence.md。最终需每行指向具体已执行测试/发行验证；不以此索引的建立代替实际验收。


附带回归修复不改变 C01–C32 的业务判定：MCP metadata/首次执行/awake 关闭契约、Windows FIFO/票据清理、Python3.12同 tick cache与旧视频fixture均已独立规格及质量复核，43个源路径冻结。full8两失败的后继修复经独立规格/质量闭环，root full9为3161passed8skipped388.95s，45源suite后hash保持；失败历史与待验证冷启动波动保留于90；未将局部通过计为最终发行验收。
