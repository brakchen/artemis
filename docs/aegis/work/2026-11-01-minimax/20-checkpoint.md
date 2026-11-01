# 实施检查点

## 当前状态

- 用户已授权完整六模块 MiniMax v1.2 开发，任务仍 active。A1/A2/B1/B2/C 已提交推送；D1 两级独立复审和 root 完整单测通过，进入 root 显式提交/推送；D2/D3/E 尚待，不得以 D1 局部收尾结束整个任务。
- 工作区 D:/Codex Project/artemis，分支 feat/model-config，D1 基线 HEAD274db20978d97193aa9c950a51f747bd49851748；精确集成 SHA 以 Git readback 为准。root 唯一 Git/docs/deps owner，串行 source writer；当前作者/规格/质量及 root 单测所有进程已结束。
- D1 冻结 manifest scratch/d1-source-manifest-v3.json，SHA29fb1c649242d5018930caecfde553115ac31b738f34d518d03bf25379a6877f，30源 identity56ff4982bedae5cfdf27670747ba91f295b97cb91a999c99fa5acc1cd73979c8。root suite 前后源哈希、scope、HEAD、Ruff/format/diff通过；C原45源仅config/agent.py授权后继，其余44保持。
- root 完整 tests/unit：3220passed、8skipped、89warnings，373.52秒/exit0，scratch/d1-final-unit-1-py312.log，session80574已关闭。独立规格16例及质量16例通过，作者相关446passed/1skipped26.23秒。root汇总scratch/d1-root-final-receipt.json SHA4d4ed639758bdbbb9f4337ff63ec954dffcc5243d72fa11113bf9c311fbc3934。
- 新增1条线程警告：旧视频test全局Path.stat模拟把目录视为普通文件，logger后台读取IPC创建temp_dir时出现FileExistsError；该文件隔离并将线程warning升error后7pass/3正常warn2.75秒，不证明整套race消失。E已登记收窄fixture，仅假video/prompt路径模拟，保留全部旧语义断言；不吞warning、不改Windows目录/日志实现、不删用户temp。

## D1 已实现边界

- 不做profile未知时Google付费预热。实际profile选定后、Task发布前复制typed AgentConfig/LLMConfig并canonical preflight；callback capability身份与nested fields_set保留，memory/runtime/pro.execution/callbacks读任务副本。
- Agent(config=typed).prepare_task_config(profile=None)是无initialize/client/device副作用的父入口预检。唯一选择规则AgentBase._select_agent_profile；prepared含私有端点/凭据，ONLY model_snapshot可公开。
- 辅助主备在同registry快照派生，implicit旧Gemini默认继承摘要端点，explicit模型按M3/Flash参数校验。后台同key不重复启动、terminal不复活，各job独立预算，job内部recovery/fallback共享预算；usage一次，保留TranscriptLedger compaction base。
- get_main_config_path显式JSONC/JSON共同优先；无explicit主文件时独立legacy LLM/Agent配置路径不混用。当前展示与历史实际public snapshot区分；MiniMax Replay用标准trace，只有有效Google路径patch Google。
- SPEC P2 generic legacy pin以context已安装对象身份修复；QUALITY P2公开snapshot跳过clone/profile选择以SDK私有实际对象绑定修复。公开metadata永远不能证明prepared，canonical allowlist仍重建，invalidaux即使traceFalse也在路径/session前失败。
- 已存在非空_llm_endpoint_snapshot保留A1/C单task authority，包括先OCR的standalone context；同ctx手工换typed对象不视作新task/重选端点，新task使用新ctx。不添加reset/resolver/cache框架。
- SPEC回执scratch/spec-d1-runtime-review-v3.json SHA1ed562bc2fd306580a2559ea85192845d1a9245c3e650e10f9091b040a88fbf1；QUALITY scratch/quality-d1-runtime-review-v3.json SHA510a8a1bbfbe75942d049420d802e391179f956b9b0d08eb535a94470b0b33be。旧v1/v2失败、内存负控制及所有原assert证据保留90和scratch，不覆写。

## 后继交接

1. D2 fresh writer：scratch/d2-context-packet.md，HTTP/CLI/UI协议与完整draft、blankKey保留/显式clear、422/上游错误脱敏、完整MiniMax init迁移与保护备份/条件回滚、CLI父预检、UI无rawKey回填/无自动skip；有意义Python+renderedAngular测试/build。root集成D1后发准确HEAD，不提前source写。
2. D3 fresh writer：scratch/d3-context-packet.md，diagnostics源头脱敏和内部鉴权、MCP/API/queue新任务父预检、最小选定provider秘密readonly注入/APP_DIR可写/host路径映射、复用根wheel+UI runtime镜像。根及playground Docker相关新增owned路径已登记handoff，未编辑source。
3. E fresh writer：scratch/e-context-packet.md，必须修已证ImageProcessor生成Python动作旁路（MM声明过滤+真实入口前阻断，固定CPU操作/legacy保持），修真实pyright/ratchet债务及上述全局stat fixture，补实际SDK MockTransport/Google拒绝整链。root最后完整defaultpytest、UI测试/build、lint/format/ratchet/pyright、wheel/sdist清洁安装/非repo cwd资源、README/CN/设计Changelog/C01–C32对账。

## 已交付基线与规则

- A/B 片段已提交；C为274db20（51内容路径），root完整3161pass8skip88warnings388.95秒、45源suite后hash保持；证据scratch/c-root-full9-final-receipt.json SHA7716c545ea1860ca24b3f3fd3f3b42f4b564454144fdd1344bc2ef34f2790b2c。C媒体范围/模态覆盖、ASR、payload限额、VFR/取消join、跨录屏缓存、MCP metadata/earlyoff/root-before-leaf、FIFO原生FileLock与deadline/cleanup合同保持。详细历史见90，不要求后继重读巨量旧日志。
- Pro/Flash目标门禁、xy/LTRB有限数/provenance/快照失效/unknown不重发保持；三个shell入口检查不能代替ImageProcessor缺口。真实定位精度/200标注数据/账号权限/设备闭环仍needs-verification。
- Python3.12.2 scratch/ci-py312沿原uv.lock frozen；原.venv Python3.14.6未动。unit只设进程LOCALAPPDATA=scratch/test-localapp、不全局ARTEMIS_APP_DIR。ffmpeg/ffprobe8.0.1只合成媒体；Chrome可用，Docker daemon ServerAPI500，不启动/重启。
- 无真实Key/registry/用户媒体、无paidHTTP/SDK.initialize/真实设备；实际SDK仅MockTransport。TDD off/proportional，原有断言和Host/Origin不放宽。ONE writer→SPEC→QUALITY→rootfull→显式commit/push；无main合并/部署。
- 四public initializer只同blob索引刷新，物理原字节保持，C提交后clean；不要再构造包lazy export兼容框架。Root sole Git，逐路径stage，不blanket add/force/stash/foreign edits。
- 曾有自动审批拒绝删除自有合成测试临时目录，reason仅blocked by policy，目录保留且不绕过；最终答复独立短段说明。曾误采不相关用户Python进程只读stack已更正，无locals/Key、无修改/kill，该日志不是Artemis证据，不再访问对应输入。只管理自启并确认归属的进程。
- tower/Pi/origin-master环境例外见handoff：实际Codex UUID01a1957d-26fb-7173-abdc-ba44f87b31d0，origin/main基线e7cbe5950fba16b7d31c852aa1e92309d7d6405c；用户要求继续feat/model-config，无main合并授权。最终精确branch状态与同步基线如实对账。
