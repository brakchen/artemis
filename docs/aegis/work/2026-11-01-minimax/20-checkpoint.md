# 实施检查点

## 当前状态

- 用户授权完整六模块 MiniMax v1.2，任务 active。A1/A2/B1/B2/C/D1 已提交推送；D2两级复审、当前离线UI及root完整单测通过，正进入显式提交/推送。D3/E尚待，不得以D2结束整个任务。
- 工作区 D:/Codex Project/artemis，feat/model-config，D2基线HEAD c80db73852c0a8bdf036d81f34322ae639e6cf65。root唯一Git/docs/deps owner，无source writer，作者/全部SPEC/QUALITY/root测试已结束；root preview Chrome/server/named harness均关闭。精确D2集成HEAD由Git readback及下一packet给出。
- D2最终manifest scratch/d2-source-manifest-v4.json SHAe958be4fe57fb964155cc64a910b59b71ef774a892151ec39f4f5afee02fdac3，19源identity4952bd63559461372e6da5c0016cfe1350cabfc75c569e9897246c30fadcbec5。root suite前后19hash/scope/HEAD/Ruff/format/diff通过；root完整tests/unit 3282passed/8skipped/88既有warnings384.46秒exit0，scratch/d2-final-unit-1-py312.log，session22209已回收。
- root D2汇总scratch/d2-root-final-receipt.json SHAd985e110b4b39c708d52588ebbab16cd806ad18a49b3e42f63a311f31abf8863。SPEC最终receiptf7e48da6bc1a54355d4c9dad4fc48159e3dad9a18a5c7b044426c1842280c8db，QUALITY最终d9145231ee0a28c120ae57c900abf46618fc89aba7b76acbe2d32abeb7ba714e；v1–v3及已关闭失败反例均保留于90/scratch。

## D2已实现及审查闭环

- HTTP/CLI/UI共享完整candidate，kind/vendor/base显式draft优先，省略隐藏metadata保留，空Key编辑在原registry锁内fresh merge，避免重放旧secret；明确clear/disable确认。known协议catalog鉴权/分页、自定义Anthropic地址、MiniMax缺base本地拒绝；真实ASGI422与transportRuntimeError/chain不反射Key。
- 完整MiniMax init迁移primary/fallback/default/nodes/utils/explicitaux；canonical未发布registry候选先校验，保护备份/逐文件atomic+条件回滚保留后编辑。process MINIMAX_*仅显式导入，已有Key优先，--import-env --replace-key才替换，空/whitespace/template源预发布拒绝。capture同source字节parse+早捕获source/target baseline，变更在backup/publish前拒绝；Unicode+spaces dotenv pointer经过真正下一次选择/加载验证。
- UI MiniMax preset/协议vendor/手填/编辑/空Key/clearcancel/failretry，无rawKey回填/无autoSkip。目录不可用时可显式unverified save，文案保持未验证；不把provider保存当完整taskready。D3 backend readiness源头脱敏尚待。CLIpure实际profile preflight在device/daemon/queue前，唯一selector保持。
- 当前UI Angular15SUCCESS（7Home+8SystemService），productionbuild11.797秒，app/specTSnoEmit通过。获取modelConfigEnv失败清null、重试恢复、knownpartial字段保持；无数据不虚构Gemini，协议/Key registry说明准确。旧file-name-only路径placeholder为非阻塞观察，不宣称穷尽UI错误路径/无障碍/跨浏览器。
- root实际offline browser仅ownfakeAPI和隔离Chrome/BH_HOME；1440最终preset anthro/minimax、Key0、Unavailable模型/双协议registry说明/无横向溢出；旧同layout1024与edit/未验证交互亦检。证据scratch/ui-d2-root-preview及-v4，截图/录制为合成fixture，无真实Key/用户browser/设备/API。按保存PID/创建时间/cmdline确认server归属关闭，Browser.close+named --reload，未删任何用户目录。

## 已交付基线与保留合同

- D1 c80db73已push/readbackclean0/0，36路径（30源+6docs）。任务配置typed copy/fields_set/callback能力身份，pure Agent.prepare_task_config(profile)，actual单selector；aux同registryrevision/正确主备参数/每job预算与terminal不复活/usage一次，public历史snapshot/Replay。root3220pass8skip373.52秒，两级各16定向通过，rootreceipt4d4ed639…3934。全部旧失败/负control留90。
- D1私有实际config/LLM对象身份仅证明SDKcopy，公开model_snapshot不能跳过预检/复制。已有非空_llm_endpoint_snapshot仍是单task authority（包括先OCR的standalonectx）；手工同ctx换typed对象不重绑端点，新task用新ctx。prepared私有含Key，ONLY public model_snapshot可API/持久化。
- C 274db20（51路径），root3161pass8skip88warnings388.95秒；视频/ASR范围和逐模态覆盖、payload限额、VFR/取消join、UUID来源缓存、MCP metadata零Root/earlyawakeoff/root-before-leaf、WindowsFIFO原生FileLock/deadline/cleanup保持。C45源完整历史manifest与全部QA记录见90。
- A/B已提交，xy/LTRB有限数/provenance/快照失效/Pro和Flash统一gate/unknown不重放保持。B2只覆盖三个shellfactory并不能证明ImageProcessor动态Python安全，此已证gap本次E必须修，不能留作后续。

## 后继任务（继续到底）

1. D3 fresh唯一writer，scratch/d3-context-packet.md（root提供准确D2 HEAD）：diagnostics源头无Key/内部canonical鉴权、API/queue/MCP父入口预检、最小选中provider秘密readonly注入/APP_DIR可写/host映射、根wheel+UI runtime复用。MCP worker现resolve_profile_file/from_file在device探测后，parent及worker预检必须共用实际legacyoverride/explicitmain选择，不能泛用defaultbuilder忽略它。新增Dockerfile/manager配置/compose/requirements scope已登记handoff。
2. E fresh writer，scratch/e-context-packet.md：必须修ImageProcessor生成Python旁路（MM声明过滤+真实Agent入口前拒绝，不造ASTsandbox；固定CPU和legacy保持），修真实pyright-core6错及ratchet broad754/silent0基线债务，禁止baseline提高/ignore/异常别名游戏；修旧video fixture全局Path.stat模拟与logger线程race（D1出现、D2未出现不等于已修）。补六模块+aux实际SDK/Google拒绝矩阵。
3. root最后defaultpytest（含tools/client）、UI全测试/build、Ruff/format/ratchet/pyright、新wheel/sdist清洁安装/非repo cwd/资源及真实SDKMock seam，按实际版本约束privateSDKhook依赖（当前LC-Anthropic1.5.6/Anthropic0.122.0），README/CN/设计Changelog/C01–C32全部对账，feature分支精确提交push。无main合并/部署。

## 环境/真实验证限制

- Python3.12.2 scratch/ci-py312沿原uv.lock frozen；原.venv3.14.6不动。unit仅LOCALAPPDATA=scratch/test-localapp，不全局ARTEMIS_APP_DIR；fixture/发行另用隔离appdir。ffmpeg/ffprobe8.0.1只合成媒体；Chrome可用；Dockerdaemon ServerAPI500，不启动/重启容器。
- 无真实Key/registry/用户媒体/paidHTTP/SDK.initialize/真实设备；实际SDK仅MockTransport。TDD off/proportional，original meaningful断言与Host/Origin不放宽。真实账号图像/搜索/ASR权限、200标注定位精度和实机闭环仍needs-verification，不编造99%/延迟成果。
- Root scratch已有release-wheel-smoke.py（未运行，须清洁wheel解释器+非repo cwd）、ui-offline-preview/launch helpers（已跑仅fixture）。无常驻helper或测试进程。初次旧UI录制10frames、最终8frames均保留精确返回目录，不当真实用户或端到端证明。
- 曾有自动审批拒绝删除自有合成测试临时目录，reason仅blocked by policy，目录保留、不绕过；最终答复独立短段说明。曾误采不相关Python进程只读stack已更正，无locals/Key/修改/kill，该日志不是Artemis证据，不再查其输入，只管理确认归属的自启进程。
- 四public initializer只同blob索引刷新、原物理字节保持，不再做包lazy export框架。root soleGit，逐路径stage，不blanketadd/force/stash/foreign edits。tower/Pi/origin-master环境例外见handoff，Codex UUID01a1957d-26fb-7173-abdc-ba44f87b31d0，origin/main同步基线e7cbe5950fba16b7d31c852aa1e92309d7d6405c；保留feat/model-config，无main merge授权。
