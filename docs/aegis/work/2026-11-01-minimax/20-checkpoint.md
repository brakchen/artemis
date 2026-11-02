# 最终实施检查点

## 交付状态与边界

- 用户授权的 MiniMax v1.2 六模块代码开发与本地离线验证完成，后续已按用户授权合入 main，保留 feat/model-config 开发历史；最终主线提交以 Git 记录为准。没有部署。
- 验证时 HEAD 为7cc6e841ce26c3aab8d396ba002edb23a738741d，实际TaskStart8e41b19e710abb19ba0304f15ddb542f7a0f6dd7。E改动与SDK pins按物理哈希验收后提交；文档收尾不改变源码。
- root唯一Git/docs/deps owner。所有作者、规格/质量/整体审查、Python/前端/打包/安装CLI检查进程均已结束；没有运行中的测试、浏览器预览或任务worker。没有真实Key、用户媒体、付费模型请求、SDK.initialize、真实设备或Docker运行。
- 真实账号的图像/搜索/ASR权限、200标注目标精度、延迟/成本与设备反馈闭环未验证；本机Docker daemon Server API500，未build/start真实镜像。代码/模拟HTTP通过不表示这些业务能力已验收。

## 最终源码与独立复核

- 全任务172源码/配置/提示词/依赖冻结：scratch/final-feature-source-manifest-v1.json SHA7a5f1fe0b41a5714962c74e53e881995994360983045cc9aba6a0517ee75bb70，identity364dbbffef50eded52baaf3fb92b3eff87794390111a205a7c81d63220a8c58b。包含四public initializer及质量/pyright基线原样检查；测试和打包后172hash保持。
- E最终22源manifest scratch/e-source-manifest-v3.json SHAfe5322094847ae2c357baa53ffc50fa11ba43140db59916d556145bab7657437，identityaa4c33634bbac6dd9a997a7bc75793be5258a4f43580428a2077673de45aa7fd。receipt6e7992fd7a93303d58630a826e34a18111cde977023abb1e6f68ea2f3db0a704；冻结相关208pass12.07秒，旧v2未改主体964pass106.67秒证据及全部失败历史保留90。
- E最终SPEC43PASS，receipt9f8aac8e4627def3d20ca57d8f9ac01dfb052b644fc36b7f310b890a2eb0a9f3；QUALITY33PASS，receipt748048b17d21cc4f8d8e318d6f472825af01d63983cab17852b595a52b1e8f5b。最终跨模块36PASS11.11秒/no actionablefinding，receipt689e2a6b158f7d753e12e9ce00116cbac9dcf3ca1a82dc972a3d876fbff5d7fc。均SOURCE STOP，修复后对应原反例独立重验。
- D3提交7cc6e84、D2 7edf09a、D1 c80db73、C 274db20及A/B已推送；D3完整3343pass时旧视频线程警告再次出现，E已收窄全局stat/exists模拟并保留原29AST断言及无关目录/读取正对照，最终默认suite线程warning设error通过。

## root最终检查

- 默认pytest含unit/tools/client：3427passed、9skipped、8deselected、88既有普通warnings，389.76秒，exit0；PytestUnhandledThreadExceptionWarning设error，未出现该警告。scratch/final-default-pytest-1.log，session68396终止。
- 全Ruff PASS、format674alreadyformatted、受保护核心pyright0errors0warnings、qualityratchet754broad/0silent/16ignore（原基线754/0/18未改）、公开PyPI+npm源检查PASS。仅pyright工具新版提示，未升级。
- 前端完整145SUCCESS，productionbuild12.287秒；scratch/final-ui-tests.log/final-ui-build.log，session22522终止。前次隔离Chrome/fakeAPI预览的布局、preset/edit/Key0/未验证交互证据保留，未使用用户浏览器配置。
- 新client/core wheel和sdist构建、sdist单独重建wheel成功；scratch/final-distribution-inspection.json确认393成员corewheel配置/UI与新build一致，无scratch/cache/registry/.env/node_modules。首次offline corebuild缺Cython缓存，改公开PyPI构建依赖成功，运行时lock未改，原失败日志保留。
- 仓库外空Python3.12环境D:/Codex Project/artemis-release-01a1957d/venv安装重建wheel；175依赖check兼容，实际console --version/--help、CLI Minimax init、中文state路径/注册表/随包模板不变通过。scratch/final-installed-wheel-smoke.log真实SDK MockTransport一请求+超限图片HTTP前拒绝；Anthropic0.122.0/LC-Anthropic1.5.6固定，源码LCcore1.5.6和cleanwheel core1.7.3实际记录。没有真实模型HTTP。
- SDK pins root pyproject SHA c06e100d157702ed74f8793dd1565d5ab50016ce23dffa4c4b399451875b2bbb、uv.lock SHA56477dd8684d36ca127c54bc59161430d0baa3f2d7c9b50e3e49eb78bcc98dd7；197包版本/source不变，仅根metadata约束，已核对MIT。原.venv3.14.6未改，source tests使用scratch/ci-py3123.12.2。
- 最新fetch origin/main及merge-base均e7cbe5950fba16b7d31c852aa1e92309d7d6405c，已包含该基线，没有main合并。最终只精确stage本任务文件，commit/push feature，禁止blanketadd/force/stash/foreign edits。

## 保留契约与后续操作

- MiniMax模型任务以canonical endpoint/typed config/同registry revision固定全部角色主备、utils/aux；仅public model_snapshot可公开，privateprepared含Key。新task用新ctx，已有snapshot是单task权威，不能靠公开历史字段跳过预检。
- CLI/API/Queue/MCP新任务纯预检先于trace/worker/daemon/device，已接受任务幂等保持；MCP parent/worker实际legacyoverride/explicitmain一致。Queue提交校验与worker最终pin不构成排队期间跨进程秘密原子快照。
- MM生成Python/任意脚本真实入口拒绝，固定CPU和用户低层/legacy保留；真实Operator systemprefix及transcriptbase/limitreminder已修，schema/stop/target/provenance/unknown不重放合同保持。legacyOCR/pool恢复回归在parser类型/shape处关闭，不放宽catch或基线。
- 容器最小选中secret/config文件RO、APP_DIR其他可写、403、host映射与按containerID清理；取消先settle真worker，remove失败留bindfiles；崩溃/外部生命周期orphan受保护目录保留，未加GC/TTL。Google SDK/旧厂商与无关GCPinfra保留兼容，退休触发需项目明确弃用对应vendor，不是本次删依赖目标。
- 安装/源码配置入口：artemis init --provider minimax（源码用uv run）。Key隐藏输入、仅注册表保护写入；--import-env显式导入，已有Key默认保留，--replace-key才替换。长期进程切配置后重启，当前运行任务不热换凭据。真实测试另需账号和授权设备/截图。
- 复杂度收尾：既有大source/test文件局部接线与修复，协议/坐标/媒体责任用专属现有owner；没有第二注册表、执行器、重试或治理框架。复用同SDK/decoder/drain/fixture，长证据保留90，当前检查点/验收表为入口。状态exceeded-and-governed，不以删必要异常或加ignore换计数。
- docs/current/ADR系统不存在，已批准技术方案是需求/架构owner，避免重复ADR；Aegis workspace helper未配置且未定位，结构手工核对10/20/30/40/90与文档链接/JSON，不宣称helper bundle/check已运行。详情及全部反例链见90。
- 自动审批曾拒绝删除C:/Users/chen/AppData/Local/Temp/c-v3-spec-recheck-2eprfb6e，reason仅blocked by policy；目录保留、不重试绕过，最终答复需单独短段说明。曾误采不相关Python进程只读stack已更正，无locals/Key/修改/kill，不再查看其输入；只管理确认PID/creationTime/cmdline归属的自启进程。

## 后续主线集成（2026-11-02）

- 用户明确授权合并主线和推送，main实际为主分支，无master。功能tip e3ceb990c2e8f1b06c1ed9108d0a08f8dd4f5793、基线 e7cbe5950fba16b7d31c852aa1e92309d7d6405c、非FF合并 13fdd03e1d9578a9664f5949c9a9ebd75f25edd6；候选树与功能tip相同。
- 合并后默认完整测试 3427 passed, 9 skipped, 8 deselected, 88 warnings in 385.21s (0:06:25)，threadwarning=error；UI生产build 18.754秒通过。之后仅README/方案/work/登记元数据改变，功能代码不变。
- 桌面worktree工具拒绝嵌套仓库，已使用当前干净checkout的临时集成分支并持有本机TEMP/artemis-master-merge.lock；源分支和原main保持至验证通过。Git收尾推送main后保留源分支、清理临时分支，结果见会话回读。
