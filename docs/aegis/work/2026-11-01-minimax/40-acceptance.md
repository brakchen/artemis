# C01–C32 验收对账

行为规格见技术方案第 11 节。此表只是执行证据索引；“部分”表示仍有子条件待后继片段验证，不是验收通过。D3 v3独立规格17项及质量18项通过，root完整单测3343 passed、8 skipped、386.09秒，23路径suite前后哈希/范围/静态保持；旧视频fixture线程警告归E修复，最终发行验证仍待。所有线上账户能力/设备精度均未验证。

| ID | 当前离线证据 | 尚需验证/负责片段 |
| --- | --- | --- |
| C01 | A1 旧 registry、新 kind/vendor、禁用引用预检 | 最终发行入口再查 E |
| C02 | A1 Key保留/clear、锁/原子写/Windows权限；D2 test_minimax_configuration.py 实际ASGI/CLI secret异常、422、并发空Key保留与确认clear；D3 test_minimax_d3_ingress.py 源头/嵌套/cache公开报告及错误配置input隐藏 | 最终完整回归 E |
| C03 | A1 pinned runtime endpoint；B1 detector utility/OCR default；C 实际视频 utility SDK/ASR；D1 test_minimax_task_runtime.py 辅助/控制台/Replay，root3220pass | 入口整链 E |
| C04 | A2 实际 SDK thinking/signature、ID、流式/续传、raw gate | E 整链截断不派发 |
| C05 | B1 xy/LTRB、逆变换/来源；B2 capture/version/rotation 失效及真实派发点 | E 汇总 |
| C06 | B1 typed结果；B2 uncertain/ambiguous/invalid provenance不升级点击 | E 汇总 |
| C07 | B1 OCR typed/分区/cache；B2页面变化后旧目标失效 | E 汇总 |
| C08 | A2 独立搜索续接/预算/服务端块不本地执行 | E 汇总 |
| C09 | A2 SDK retry=0/同 wire 去重/共享预算；B2 unknown不重发；C 全视频/ASR作业预算；D1 pending/terminal后台job真实SDK预算与永久错误回归 | 整链 E |
| C10 | A1/A2/B1 单模块离线分支 | 六模块与辅助 Google 拒绝 spy E |
| C11 | A1 canonical registry/models helper；D2 HTTP/CLI完整draft、协议鉴权/分页/手填未验证保存；Angular15例与root离线UI preset/edit检查 | 最终 E |
| C12 | A1 explicit配置路径；D1共同主文件/独立legacy；D2 process导入优先/显式替换/占位拒绝、保留.env、中文空格路径actualdotenv next-load与wheelcopy测试 | 最终发行 E |
| C13 | D2 UI移除rawKey回填，server422/目录异常脱敏与当前模型失败清空/恢复；D3生产tests与独立原反例覆盖fresh/cached公开报告、readiness/device选择、Queue/实际FastMCP/container错误input隐藏 | 最终完整回归 E |
| C14 | B1 各 OCR consumer、offset/partial、共享取消 | 主配置来源 D1，最终入口 E |
| C15 | A2 日志/诊断/委员会工具注册、搜索授权 | E 混合残留配置 |
| C16 | A1 endpoint identity/snapshot，A2 breaker 隔离；D1同registry修订派生aux主备、任务typed副本/对象pin/no reload回归 | 最终 E |
| C17 | D1 test_minimax_task_runtime.py与ModelService/SDK/Flash；预热零调用、辅助模型、public快照/历史与Replay；D2 CLI、D3 API/Queue/MCP父入口及worker的真实profile/legacyoverride纯预检与幂等测试 | 最终整链 E |
| C18 | D2生产UI build11.797秒、TSnoEmit、Angular15及隔离Chrome实际构建检查；D3共享runtime静态契约、最小文件RO挂载/可写APP_DIR/403、host映射及按container ID抗取消清理，fakeDocker与same-loader | wheel/sdist/非repo cwd E；真实Docker仍未验证 |
| C19 | A1配置规范化/alias/禁用/legacy；D2完整MiniMax init与4条legacy保持、真实SDK Flash/M3候选及预发布错误无写入回归 | 最终 E |
| C20 | A1 主配置/profile/旧格式/override，B1 OCR SDK 字段；D1 typed memory/aux副本、fields_set、带锁callback身份及消费契约测试 | 最终 E |
| C21 | A1 实际 SDK 角色预算/模型参数；D1 implicit aux继承/explicit M3与Flash参数、主备预算真实SDK回归 | 最终 E |
| C22 | A2 schema 首次指令/真验证/合法 null/raw gate；B2 pixel；C 视频真实 schema/区间及逐模态覆盖 | 整链 E |
| C23 | B2 Pro单/burst/Flash/MCP strictgate；真实identity/整node消失/旧turn来源及三个opaque shell入口已覆盖；新发现ImageProcessor生成Python旁路未修 | E 必须补此旁路并验证全部消费者，不以三个shell已测替代 |
| C24 | B2 实际Actuator副作用后timeout只发送一次，unknown记录/观察，明确未发可有界重试 | E 汇总 |
| C25 | B1公共点/bbox；B2所有原始端点先验证、sharedround、实际wire点/bbox可表达/可见父级 | E 汇总与真实精度 |
| C26 | B1 版本 cache、旧历史保留/并发与失败不缓存 | E 汇总 |
| C27 | tests/unit/agents/test_minimax_video.py：原入口完成/partial/error、请求时段覆盖、缓存及消费者；C 独立真实入口正反路径通过 | 整链 E；真实精度待验证 |
| C28 | tests/unit/test_minimax_media.py 与 test_minimax_video.py：真实 SDK MockTransport/ffmpeg、ASR multipart/offset/限额、来源及重复取消清理 | 真实账号 ASR 权限及效果待验证 |
| C29 | D3 test_minimax_d3_ingress.py 内部canonical端点实际catalog验证、不同alias/base/key的fallback、不相关host key不参与、同auth目标私有去重、default顺序、empty/invalid不误报verified；公开仅结论，未作推理调用 | root完整回归及E；实际账号能力未验证 |
| C30 | A2 Messages 业务错误/429/SSE；C ASR aliases/HTTP200业务失败/媒体分类 | D1 后台永久错误，整链 E |
| C31 | Messages 沿用锁定 SDK，厂商子类仅在完整 payload 上校验媒体限额；native ASR 用量独立，不伪造 text tokens | E 汇总计量与适用范围 |
| C32 | D2全主备/default/nodes/utils/aux迁移、canonical未发布候选、保护备份/条件回滚、后续编辑和source/target读入竞态拒绝；root3282pass/8skip | 最终 E；不宣称多文件原子事务 |

所有测试证据的命令、数量及失败修复见 90-evidence.md。最终需每行指向具体已执行测试/发行验证；不以此索引的建立代替实际验收。


附带回归修复不改变 C01–C32 的业务判定：MCP metadata/首次执行/awake 关闭契约、Windows FIFO/票据清理、Python3.12同 tick cache与旧视频fixture均已独立规格及质量复核，43个源路径冻结。full8两失败的后继修复经独立规格/质量闭环，root full9为3161passed8skipped388.95s，45源suite后hash保持；失败历史与待验证冷启动波动保留于90；未将局部通过计为最终发行验收。
