# 实施检查点

## 当前状态

- 用户授权完整六模块 MiniMax v1.2，任务 active。A1/A2/B1/B2/C/D1/D2已提交推送，D3验证完成准备精确提交；E仍必须完成，不得以D3结束整个任务。
- 工作区 D:/Codex Project/artemis，feat/model-config；D3验证基线HEAD7edf09aedd6b16269f24ba5e587d476ac61fe484。root唯一Git/docs/deps owner，作者与独立审查串行；当前所有作者/审查/root测试、preview进程结束，无source writer。
- D3最终23manifest scratch/d3-source-manifest-v3.json SHAb4e5a5477ef58c993bddc909f5f397d6b8163785fd5bb0050ea02840408bba08，identitye33143df004b3351f8cdd0ce0e8ac40e9e20e9c01c3eec3dd018f41367913023。SPEC17pass3.45秒receipt1aaf0920d8322c1c6622f2d70716fe4d6d60e97bb80b4e5e5f67c9bd84bf6c0d；QUALITY18pass3.20秒receipt2dd98c4d42cf9a607595a130363dfefdd880ab22dc75821180865ce2d223dd78，均STOP。
- root完整tests/unit最终3343passed/8skipped/89warnings386.09秒exit0，scratch/d3-final-unit-1-py312.log，session21104已关闭；suite前后23物理hash/HEAD/范围/Ruff18/format/diff通过，scratch/d3-root-{pre,post}-full-check.json。旧Path.stat视频fixture线程警告再次出现，E必须修，不能以suite exit0跳过。

## D3已实现与保留边界

- 凭据探针源头无rawKey，fresh/cache/nested/readiness/device公开报告脱敏；实际Agent配置错误input由canonical hide_input_in_errors隐藏。MCP内部canonical按真正使用的default/角色主备auth目标验证，空/无效不能误报verified，无自动付费推理。
- API/Queue/MCP父入口及实际worker在trace/worker/daemon/device前纯预检，复用SDK唯一profile selector；MCP实际legacyoverride与explicitmain选择一致，已接受任务幂等保持。仅public model_snapshot可出server；prepared对象与registry含私有Key，不输出/持久化。
- 容器导出选中角色主备/utils/aux实际provider子集和legacy dotenv，保持Agent fields_set/implicit aux及独立legacy runtime设置；源revision变更拒绝，registry私有同revision。只读file mounts、APP_DIR其他状态可写、HTTP修改403；manager-visible root与Docker-host root显式映射。
- 正常stop/失败/取消清理按container ID+generation归属；shield并settle真实worker及独立cleanup，重复cancel保留原错误，remove失败留bindfiles。manager崩溃/外部生命周期orphan受保护目录保留，无新GC/TTL。两Playground镜像复用根UI/client/core wheel runtime，无真实Docker build/run验证。
- v1/v2/v3全部失败与修复链留90：配置错误公开反射、legacy custom/ollama/vllm选中Key丢失、启动取消晚失败/晚成功。原QUALITY late-success脚本finally在awaitcancel之后释放worker，与正确settlement不相容，原脚本/非PASS结果保留；独立释放事件的新反例保持resource/remove/ownership断言，独立复核通过。
- Queue提交检查提交时配置，SDKworker启动固定最终实际配置；无跨进程排队期间秘密配置原子快照保证。Host/Origin及无关云认证/BigQuery未变。

## 已交付基线

- D2 7edf09a已push；HTTP/CLI/UI完整candidate、锁内fresh空Key合并/确认clear、真正协议catalog、全角色MiniMax init、受保护备份/逐文件atomic与条件回滚、Unicode dotenv next-load。root3282pass8skip384.46秒；最终19源manifest e958be4f…fac3，rootreceipt d985e110…8863，独立SPEC/QUALITY通过。UI Angular15/build11.797秒/TSnoEmit，root隔离Chrome+fakeAPI preset/edit/Key0/未验证/1440与1024布局证据；所有ownbrowser/server关闭，旧filenameplaceholder非阻塞不宣称全面a11y。
- D1 c80db73已push；typed任务copy/fields_set/live callback身份、pure Agent.prepare_task_config(profile)、aux同registryrevision及预算/terminal/usage一次、public历史/Replay。root3220pass8skip373.52秒，两级各16项。已有非空endpoint snapshot是单task ctx权威，新task用新ctx，公开snapshot不能跳过预检；全部旧失败/negativecontrol见90。
- C 274db20已push；root3161pass8skip388.95秒，媒体范围/逐模态覆盖、payload限额/VFR/取消join、UUID来源缓存、MCP metadata零Root/awakeoff/root-before-leaf、WindowsFIFO/FileLock/deadline/cleanup。A/B已push，xy/LTRB/provenance/统一gate/unknown不重放保持。

## 必须继续的E与最终交付

1. root先将现已验证langchain-anthropic固定1.5.6、显式anthropic0.122.0（均MIT），uv lock --offline仅metadata约束、无依赖升级；root独占deps，E作者需知晓此root WIP和hash。
2. fresh E唯一writer读scratch/e-context-packet.md：关闭已证明ImageProcessor生成Python旁路（MiniMax声明/提示词过滤+真实central/Agent入口前拒绝，无ASTsandbox，固定CPU及legacy/用户低层PythonExecutor保持）；修实际pyright-core六错/quality ratchet新增broad/silent债务，不提高baseline/加ignore/别名绕过；收窄旧视频fixture全局Path.stat/exists/prompts模拟，保留所有原语义断言；补六模块+aux/history/replay/parentingress真实SDK MockTransport+Google构造/HTTP拒绝矩阵。不可仅靠三个shellfactory已测宣称ImageProcessor安全。
3. E SOURCE STOP→fresh SPEC→fresh QUALITY→最终整体独立review→root默认pytest（含tools/client，threadwarning设error）、UI全部测试/build、全Ruff/format/ratchet/pyright/publicsources、新client/core wheel+sdist清洁安装、非repo cwd资源与实际CLI init/SDK媒体限额seam。root更新README/CN/技术方案Changelog/C01–C32、精确stage/commit/push feature；不main merge/部署，不以局部PASS结束。
4. root scratch/release-wheel-smoke.py尚未执行。清洁venv及cwd计划D:/Codex Project/artemis-release-01a1957d（创建前确认无foreign内容），配置state隔离repo scratch；helper要求cwd/installed module均在checkout外，导入前deny真实httpx transport，实际CLI仅fakekey/env导入，模板不改，后实际SDK MockTransport及oversizedmedia前置拒绝。

## 环境与真实验证限制

- Python3.12.2 scratch/ci-py312 frozen；原.venv3.14.6不动。unit仅LOCALAPPDATA=scratch/test-localapp，不全局ARTEMIS_APP_DIR；fixture/发行独立AppDir。ffmpeg/ffprobe8.0.1仅合成媒体，Chrome可用，Docker daemon ServerAPI500，不启动/重启。
- 无真实Key/registry/用户媒体/paidHTTP/SDK.initialize/真实设备，实际SDK仅MockTransport。TDD off/proportional，不放宽原断言/timeout/Host-Origin。真实图像/搜索/ASR权限、200标注目标精度和设备闭环未验证，不编造99%/延迟成果。
- 曾有自动审批拒绝删除自有合成测试临时目录，理由仅blocked by policy；目录保留，不重试绕过，最终答复需单独短段说明。曾误采不相关Python进程只读stack已更正，无locals/Key/修改/kill，不再检查其输入；仅管理保存PID/creationTime/cmdline匹配的自启进程。
- 四public initializer原物理字节/同blob保持，不再改lazy exports。root唯一Git，显式路径stage，无blanketadd/force/stash/foreign edits。tower/Pi/origin-master环境例外见handoff；Codex UUID01a1957d-26fb-7173-abdc-ba44f87b31d0，origin/main同步基线e7cbe5950fba16b7d31c852aa1e92309d7d6405c，feature分支交付不合并main。
