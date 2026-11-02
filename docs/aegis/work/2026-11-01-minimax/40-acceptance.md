# C01–C32 验收对账

本表只对账本次无真实Key/设备的程序条件。A–E独立复核及最终跨模块审查通过；root默认测试3427 passed、9 skipped、8 deselected、88既有普通warnings，389.76秒，线程异常设error且未出现；前端145 SUCCESS/build12.287秒，全Ruff/format674/核心pyright/ratchet/公开依赖源通过。新wheel/sdist及仓库外CLI/SDK验证通过，172源哈希保持。下表的“离线通过”不表示真实账号权限、语义精度或设备/Docker验收。

| ID | 本次离线证据 | 结论与未验证范围 |
| --- | --- | --- |
| C01 | A1 旧 registry、新 kind/vendor、禁用引用预检 | 离线通过 |
| C02 | A1 Key保留/clear、锁/原子写/Windows权限；D2 test_minimax_configuration.py 实际ASGI/CLI secret异常、422、并发空Key保留与确认clear；D3 test_minimax_d3_ingress.py 源头/嵌套/cache公开报告及错误配置input隐藏 | 离线通过 |
| C03 | A1 pinned endpoint；B1 detector utility/OCR default；C 视频utility SDK/ASR；D1 aux/控制台/Replay；E test_minimax_task_runtime.py 真实SDK具名模块矩阵含主factory/request、Planner/Operator/Explorer及视频chunk | 离线通过；具名方法/模拟SDK，未实测设备整任务 |
| C04 | A2 实际SDK thinking/signature、ID、流式/续传/raw gate；E修真实Operator非连续SystemMessage并调整transcript base，默认__call__ helper→limit→nextturn三wirecall及独立三turn保留检查 | 离线通过 |
| C05 | B1 xy/LTRB、逆变换/来源；B2 capture/version/rotation 失效及真实派发点 | 离线通过；坐标协议与来源，不含真实识别精度 |
| C06 | B1 typed结果；B2 uncertain/ambiguous/invalid provenance不升级点击 | 离线通过 |
| C07 | B1 OCR typed/分区/cache；B2页面变化后旧目标失效 | 离线通过 |
| C08 | A2 独立搜索续接/预算/服务端块不本地执行 | 离线通过；真实搜索权限未验证 |
| C09 | A2 SDK retry=0/同 wire 去重/共享预算；B2 unknown不重发；C 全视频/ASR作业预算；D1 pending/terminal后台job真实SDK预算与永久错误回归 | 离线通过 |
| C10 | A1/A2/B1离线分支；E test_actual_module_sdk_matrix_with_residual_google_and_live_http_denied真实SDK具名模块、rawGemini/残留Key/sharedclient、Google构造/实际HTTP拒绝，aux及Replay/父入口沿D1/D3已有实际回归 | 离线通过；实际SDK+模拟HTTP，非线上/设备E2E |
| C11 | A1 canonical registry/models helper；D2 HTTP/CLI完整draft、协议鉴权/分页/手填未验证保存；Angular15例与root离线UI preset/edit检查 | 离线通过 |
| C12 | A1 explicit配置路径；D1共同主文件/独立legacy；D2 process导入优先/显式替换/占位拒绝、保留.env、中文空格路径actualdotenv next-load与wheelcopy测试 ；实际安装CLI初始化/中文dotenv指针/随包模板不变 | 离线通过；含仓库外安装CLI及中文路径 |
| C13 | D2 UI移除rawKey回填，server422/目录异常脱敏与当前模型失败清空/恢复；D3生产tests与独立原反例覆盖fresh/cached公开报告、readiness/device选择、Queue/实际FastMCP/container错误input隐藏 | 离线通过 |
| C14 | B1 各 OCR consumer、offset/partial、共享取消 | 离线通过；MiniMax周期自动OCR默认关闭 |
| C15 | A2 日志/诊断/委员会工具注册、搜索授权 | 离线通过 |
| C16 | A1 endpoint identity/snapshot，A2 breaker 隔离；D1同registry修订派生aux主备、任务typed副本/对象pin/no reload回归 | 离线通过 |
| C17 | D1 test_minimax_task_runtime.py与ModelService/SDK/Flash；预热零调用、辅助模型、public快照/历史与Replay；D2 CLI、D3 API/Queue/MCP父入口及worker的真实profile/legacyoverride纯预检与幂等测试 | 离线通过 |
| C18 | D2生产UI build11.797秒、TSnoEmit、Angular15及隔离Chrome实际构建检查；D3共享runtime静态契约、最小文件RO挂载/可写APP_DIR/403、host映射及按container ID抗取消清理，fakeDocker与same-loader ；root前端145/build12.287秒、新client/core wheel与sdist、sdist独立重建、仓库外安装CLI/SDK及175包依赖一致性 | 离线通过；真实Docker构建/运行未验证 |
| C19 | A1配置规范化/alias/禁用/legacy；D2完整MiniMax init与4条legacy保持、真实SDK Flash/M3候选及预发布错误无写入回归 | 离线通过 |
| C20 | A1 主配置/profile/旧格式/override，B1 OCR SDK 字段；D1 typed memory/aux副本、fields_set、带锁callback身份及消费契约测试 | 离线通过 |
| C21 | A1 实际 SDK 角色预算/模型参数；D1 implicit aux继承/explicit M3与Flash参数、主备预算真实SDK回归 | 离线通过 |
| C22 | A2 schema 首次指令/真验证/合法 null/raw gate；B2 pixel；C 视频真实 schema/区间及逐模态覆盖 | 离线通过；schema合法不等同语义正确 |
| C23 | B2 Pro单/burst/Flash/MCP strictgate及三个opaque shell入口；E test_minimax_vision.py/test_image_processor_tool.py中央/真实tool/Explorer native-universal/cachehistory拒绝、声明/提示词过滤；test_minimax_task_runtime.py实际Replay success=false/零coder请求，独立SPEC重复coder与sync/async state验证 | 离线通过；固定CPU、legacy和用户低层入口保留 |
| C24 | B2 实际Actuator副作用后timeout只发送一次，unknown记录/观察，明确未发可有界重试 | 离线通过 |
| C25 | B1公共点/bbox；B2所有原始端点先验证、sharedround、实际wire点/bbox可表达/可见父级 | 离线通过；200目标真实精度未验证 |
| C26 | B1 版本 cache、旧历史保留/并发与失败不缓存 | 离线通过 |
| C27 | tests/unit/agents/test_minimax_video.py：原入口完成/partial/error、请求时段覆盖、缓存及消费者；C 独立真实入口正反路径通过 | 离线通过；真实视频理解效果未实测 |
| C28 | tests/unit/test_minimax_media.py 与 test_minimax_video.py：真实 SDK MockTransport/ffmpeg、ASR multipart/offset/限额、来源及重复取消清理 | 离线通过；ASR账号权限及效果未实测 |
| C29 | D3 test_minimax_d3_ingress.py 内部canonical端点实际catalog验证、不同alias/base/key的fallback、不相关host key不参与、同auth目标私有去重、default顺序、empty/invalid不误报verified；公开仅结论，未作推理调用 | 离线通过；未用真实账号作鉴权/能力验收 |
| C30 | A2 Messages 业务错误/429/SSE；C ASR aliases/HTTP200业务失败/媒体分类 | 离线通过 |
| C31 | Messages沿用SDK，厂商子类仅完整payload限额；nativeASR用量独立、不伪造texttokens；E明确pin LC-Anthropic1.5.6/Anthropic0.122.0，197包版本/source不变 ；安装环境Anthropic0.122.0/LC-Anthropic1.5.6/core1.7.3实际SDK正向与超限图片HTTP前拒绝 | 离线通过；实际安装私有hook验证，ASR计量独立 |
| C32 | D2全主备/default/nodes/utils/aux迁移、canonical未发布候选、保护备份/条件回滚、后续编辑和source/target读入竞态拒绝；root3282pass/8skip | 离线通过；逐文件原子+条件回滚，不是多文件事务 |

所有测试证据的命令、数量及失败修复见 90-evidence.md。最终需每行指向具体已执行测试/发行验证；不以此索引的建立代替实际验收。


附带回归修复不改变 C01–C32 的业务判定：MCP metadata/首次执行/awake 关闭契约、Windows FIFO/票据清理、Python3.12同 tick cache与旧视频fixture均已独立规格及质量复核，43个源路径冻结。full8两失败的后继修复经独立规格/质量闭环，root full9为3161passed8skipped388.95s，45源suite后hash保持；失败历史与待验证冷启动波动保留于90；未将局部通过计为最终发行验收。
