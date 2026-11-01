# 实施检查点

## 当前状态

- 用户已授权完整六模块 MiniMax v1.2 开发，任务仍 active。A1/A2/B1/B2 已提交推送；C及QA仍未提交；D1/D2/D3/E尚未开始。不得以局部通过结束。
- 工作区 `D:/Codex Project/artemis`，分支 `feat/model-config`，HEAD `d5ff564e8a5aa30a46f214c33cf02a0b9d5baff6`。root唯一Git/docs/deps owner，source串行writer；当前所有作者与审查进程STOP，无source writer。
- **root full9已通过且session86965 closed**：3161 passed/8 skipped/88既有warnings388.95s/exit0，`scratch/c-final-unit-9-py312.log`；`uv run --frozen pytest tests/unit -q`，Python3.12.2独立env `scratch/ci-py312`，LOCALAPPDATA=`scratch/test-localapp`，移除ARTEMIS_APP_DIR。本轮无并行其他测试/诊断/源码写入；suite后45源hash仍与冻结manifest一致。当前准备显式C提交推送，再D1。
- full9前root fresh 45源物理hash、branch/HEAD/scope、Ruff/check/format/diff均PASS，`scratch/c-root-pre-full9-receipt.json` SHA087c6ec6178e8173308373289bbc73090b7df316195c96c1fd149cb144c144ed。统一helper `scratch/c-root-pre-full9-verify.py`已执行，不能把预检当完整测试通过。
- 当前45源冻结由C23、startup其余14、312QA3、本次QA5组成。最终QA manifest `scratch/full8-qa-source-manifest-v3.json` SHA12517d52f151522816ec116d3a22f8b62b74d5f60e419b47bc29f2cb51ab24ad；五源identity33be5923a9bb4232ca99d1a85c5887ddcfece7bc043359f7af8c90f8976c2302；总45源identity454b278bf6a056ee1a23a6ecc7f3d9c45ff038907ce749e27b309cc6a8af0a48。manifest的all_45_sources是当前完整路径/物理SHA清单。
- 显式后继：queue v3两源及startup v5的`tests/unit/test_minimax_vision.py`源条目已由上述QA5替代；历史manifest/证据不覆写，不能继续要求这3条旧源hash匹配。其余40源保持。另3owned perception/latency/vision在QA v1→v3未再改字节。
- 最新SPEC v3 PASS/STOP：`scratch/spec-full8-qa-review-v3.json` SHA492f78881cf513ec3efcf21065d7ed83f2b852883d4f058645ed1ef2c53774cb；12代表case、7真实holder+3即时对照及原质量反例通过，45源与107记录对象前后同。
- 最新QUALITY v3 PASS/STOP：`scratch/quality-full8-qa-review-v3.json` SHA4e162a5d80a49cac5b13e53dbc4244f37f3749831302a4a8249f964baef0eb76；10代表case、原P2反例/异常cause/清理隔离通过，45源与117记录对象前后同，root过程docs授权追加单列。此前v1 SPEC P1及v2 QUALITY P2均已按冻结v3独立重审关闭，不用旧版本PASS代替新源验收。
- 完整失败、反例、fixture修正、命令与范围变更保存在90-evidence.md，不再在本检查点重复全时序。full8为2failed/3111passed/8skip432.53s；两原例隔离2pass，随后确定性证明同tick排序和未登记保存隐患，不冒称取得原失败现场/唯一原因。
- 后继：full9 PASS且fresh hash通过→显式45源+本任务docs/handoff提交推送→D1→D2→D3→E。无blanket add、无force、无main合并/部署。

## 已完成片段及稳定契约

- A1 `3d5da4e`：kind/vendor、Key保留/clear、进程锁/原子保存/Windows ACL、规范化与角色主备快照；root全量2602pass8skip。
- A2 `fb7389c`：raw gate/schema、共享调用预算、同wire去重、独立搜索及业务错误；root全量2713pass8skip。
- B1 `7e586d4`：MiniMax utility定位/default OCR、typed结果/分区/持久cache、SnapshotRef/provenance及SDK OCR三态；root全量2802pass8skip。
- B2 `d5ff564`：严格目标门禁/派发失效、unknown不重发、Proburst/Flash整turn屏障、Agent opaque shell阻断、12动作输出schema additive；root全量2943pass8skip89旧warnings202.46s，21源冻结/独立复核通过，提交后clean/upstream0/0。
- `get_effective_endpoint(ctx,name,is_utils=False,use_fallback=False)`：default全局，object_detector/video_analyzer为utility，summarizer为agent。单registry快照固定任务角色主备与凭据，MM混vendor预检失败；不另建resolver/cache。
- Registry除Key/enabled外metadata为完整replacement；D入口构造完整候选。空/省略Key编辑保留、首次拒绝、clear独立禁用，不从alias/model/host猜vendor。
- `logical_call_scope`默认8次/180秒，nested共享；视频整job专有有界预算含ASR/repair/fallback。SDK retry=0，永久业务错误不能被外层循环复活。
- Snapshot仅真实capture生成，None unbound；invalidate递增version并清current。数学bbox/变换可有限float，Agent/wire动作先验证整数再映射，不先截断或clip坏输入；index选择bbox内可表示wire点。
- `uses_minimax`真实pinned endpoint优先，仅具体typed config或真实Context+None做canonical；opaque注入不解析，真实错误不吞成legacy。
- XML身份改变/消失/不唯一须新观察，uncertain/ambiguous/旧来源不能升级动作；unknown副作用只观察。Agent通用shell阻断仅MM Agent三个现导出factory，direct用户/legacy/status/kill保持；detached外部child边界已文档化。

## C当前冻结实现

- C23 `c-source-manifest-v3.json` SHA d763fb008d40a829b95d9f8d36df603bdf42308dca5c6c66148c959a01f8e6b9：actual MiniMax utility视频入口、逐模态覆盖/缺口、native ASR、媒体payload限额、全job预算、VFR PTS、UUID/generation缓存与media_facts_json、重复取消join再清理。作者453/root68/独立实际入口及质量反例通过。
- Startup v5 `startup-source-manifest-v5.json` SHA8e1a041ece83f7eb6f00bc9d1013f2e561409fdf188938ce88cd24eabc0e3264（vision fixture源条已后继）：MCP metadata零Root，执行root-before-leaf，KeepAwake=false早gate且已用runtime仍清理；公开四initializer内容保持HEAD，不改包加载框架。原6s/4s/45s时限不延长，未知冷启动波动历史保留，不声称根除。
- Python312 QA3 `py312-qa-source-manifest-v1.json` SHA d816fefef103db387ad7ec05055e47b2077840f03a79d9d18b5dd662aec2ed23：readiness以进入时报告对象身份coalesce，避免GetTickCount64粗tick；旧视频fixture真实借入文件保留confidence/signature断言。312相关155、双版本关键/独立规格质量通过。
- 当前QA5：queue reserve使用既有FileLock原生admission，现票maxprefix保证同tick/倒退下跨线程进程顺序，temp完整短写后publish。所有acquire nonblocking/总deadline/cancel贯穿claim/admission，late gate/剩余poll；release失败清本人publish票，body原异常object/cause优先附notes。保留原unknown严格IO/FIFO/pending parking/capacity/literal token、3x20ms helpers。perception改用DataEngine现后台owner登记，vision两fake仅补owner方法，无生产fallback。
- QA作者最终相关131pass14.78s、314关键52pass9.04s；P1/P2控制与全部原assert保存，native释放异常是实际unlock/close后的受控fault injection，不冒称自然OS事故。永久OS清理拒绝可能留下record并附notes；运行中to_thread取消沿用原DataEngine语义，不虚称能抢占线程/同步syscall。

## 后继地图与最终门禁

- D1完整细节见30-next-slices：取消非必需Google预热；actual selected profile后、Task/queue/session前固定typed AgentConfig/LLMConfig并预检，保留fields_set和live callback内部身份（可含锁）；全执行期不用self._config或reload漂移。typed memory/builder透传，aux从同pin summarizer主备派生，显式M3/Flash重建参数不忽略、不混Google；整后台job预算/永久失败不复活，usage一次且ledger compaction base不变；selected config helper、public模型快照/历史、MM Replay无Google patch。
- D2：API/CLI/UI完整draft candidate，kind/vendor/base显式优先、空Key保留/clear；真实ASGI422 input/ctx与upstream echo脱敏；MiniMax init手动/显式env bootstrap、全主备/aux迁移候选、保护备份/本次写入条件回滚、不覆盖.env无关内容；UI t/zh、有效表单/service测试与build。
- D3：diagnostics/readiness/doctor无原始Key，MCP内部canonical凭据验证非空目标；最小选定角色Provider子集/主配置readonly容器注入、APPDIR运行态可写、host路径映射明确、无效注入先于old.remove。须登记新增backend config/compose/Dockerfile等路径后再改。Docker daemon实际500，不能把契约mock当真实容器通过。
- E：修本次pyright-core 6 typing错误/quality ratchet实际新增broad/silent，不提高baseline/加ignore/Exception别名。六模块+aux残留GGKey/raw Gemini/shared client下实际SDK MockTransport及Google拒绝spy。默认pytest（含tools/client）、全Ruff/format/ratchet/pyright、UI build/tests、public依赖源、fresh wheel/sdist/清洁安装/非repo cwd资源验证；私有LC payload hook声明范围与已测试1.5.6相符。
- README/CN真实使用说明、技术设计Changelog、40验收每行具体证据、90结果与限制同步。真实账户权限、200标注定位精度与设备闭环needs-verification，无Key/实际设备不编造成功率。

## 环境与安全边界

- 原.venv Python3.14.6未动；主参考env scratch/ci-py312 Python3.12.2沿原uv.lock frozen sync，无依赖升级。unit只设进程LOCALAPPDATA，不全局ARTEMIS_APP_DIR；packaging smoke另用隔离appdir。
- ffmpeg/ffprobe8.0.1可用，仅合成媒体；Chrome可用；Docker CLI存在而daemon Server API500，不启动/重启daemon或容器。
- 所有模型/SDK测试用MockTransport/fake controller，不读真实Key/registry/用户媒体，不付费HTTP、不SDK.initialize、不真实设备/模拟器。TDD off/skipped，无strict要求；有意义离线回归不放宽业务/Host-Origin。
- 四public initializer在git status可见M但git diff内容0，源字节为HEAD LF；根本不含feature修改。root可在完整检查后对四路径显式Git索引刷新同blob，避免误stage成feature或改其包加载行为。
- 之前自动审批曾拒绝删除一个自有synthetic temp（reason仅blocked by policy），目录保留，不重试/绕过。最后final须独立短段如实说明（详90）。
- 之前root误采另一项目PID的只读stack已更正（无locals/Key、未改/杀进程），该日志明确不是Artemis证据，不再查对应文件/输入。后续只管理自启Popen保存对象/PID，不以时序猜归属。
- tower/Pi/origin-master环境例外及owned scope见handoff/ACTIVE.md；Codex实际UUID，origin/main同步基线e7cbe595…，无main合并授权。当前唯一工作区复用、root sole Git owner，显式stage不blanket add、不动他人WIP。
