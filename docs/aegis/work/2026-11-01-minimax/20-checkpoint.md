# 实施检查点

## 当前状态

- 用户批准按技术方案 v1.2 完整实施六模块；整项 active。A1/A2/B1 已提交推送，B2 已独立审查及最终全量通过，准备显式提交推送；之后 C → D1/D2/D3 → E，不以局部完成结束。
- 分支 feat/model-config，B2 基线/当前HEAD 7e586d4；源码及root文档未提交。root 唯一 Git owner，source始终单writer；当前全部作者/审查者停止，准备root commit，再fresh C writer。
- 唯一规格：技术方案；具体地图30-next-slices；C01–C32对账40-acceptance；审查提示50-review-notes；完整过程/反例/命令90-evidence。tower/Pi/origin-master例外见handoff/ACTIVE.md。

## 完成证据

- A1 3d5da4e：Provider kind/vendor、秘密保留/清除、锁/原子保存/Windows ACL、角色解析与任务快照；全量2602pass/8skip。
- A2 fb7389c：raw gate/schema、共享调用预算、同wire去重、独立搜索/业务错误；全量2713pass/8skip。
- B1 7e586d4：MiniMax utility定位/default OCR、分区/typed状态/版本cache/共享取消、来源变换及SDK OCR三态；全量2802pass/8skip。
- B2 最终root全量2943 passed/8 skipped/89上游warnings/202.46s/exit0，日志scratch/b2-final-unit-2.log，session74187已结束；Ruff/format20Python/diff、21项冻结hash通过。全量首轮注入兼容回归已修，原Diagnoser源码/测试diff0。
- B2 final manifest scratch/b2-source-manifest.json，21路径，identity6d53f5bbef531c61f8f84e73739ccab29cb18220ad6f000edbff79aacd847a87，manifestSHA47be22a5ab5ddcf23b23ae6f2785fc4ebb55c4b009115a8dbde9da76d58bc0db。独立规格/质量全部实际问题闭环；最后兼容定向262pass/4skip，原动作/视觉221pass、独立18actualcase通过。
- B2已补typed gate/真实XML与capture/最终wire点、队列前失效和单owner初始化、unknown不重放、Proburst/Flash整turncapture+version；Agentopaque shell拒绝；原XML身份任意改变/消失/移走/不唯一要求新观察；purewait正常。所有详细失败历史在90，不作为当前阻断。
- B2 commit body须解释12个ActionResult outputSchemas additive DispatchState/dispatch_state；输入/name保持。只stage manifest21路径及本任务docs/handoff，不用blanket add。

## 稳定契约

- services.llm.get_effective_endpoint(ctx,name,is_utils=False,use_fallback=False)：default全局；object_detector/video_analyzer utility；summarizer agent。私有snapshot固定registry/角色主备，MM混vendor启动前失败；不另建resolver/cache。
- llm.minimax normalize_minimax_error/validate_minimax_response/minimax_wire_target；reliability.logical_call_scope默认8次/180秒，嵌套复用。web_search角色授权/default pinned wire。
- Registry除Key/enabled外metadata完整替换，D各入口构造全候选；Key空/省略保留，首次拒绝，clear独立。不可从alias/model/host推vendor。
- create_snapshot仅真实capture；snapshot_for_image明确origin才能复用，None unbound。invalidate_snapshot递增version清current；immutable capture字段也参与Flash同turn屏障。
- bbox/变换数学支持finite float；现有Agent/wire动作坐标整数，非法fraction不先截断；index选bbox内可表达wire点并验证实际pixel，无可表达点block。B1新增MM小数actioncase保留并改为拒绝；legacy/公共数学断言保持。
- uses_minimax只对具体typed配置或真实Context+None做canonical；opaque注入不解析。真实配置错误必须传播，pinned真实endpoint优先，不能catch当legacy。
- MM Agent现有三个shell导出factory拒绝opaque执行/send_input；status/kill、direct用户/legacy保留。tracked原始用户脚本生命周期屏障结束后要求fresh observe；detached child属于外部动态，已文档明确。

## 后继路线

- C fresh implementer：消费30-next-slices及prepare_media_contract事实，复用universal与现工具；actual utility vendor、所有run raw model覆写、ASR native multipart/500s50MB/时间与权限隔离、全时段采帧/nativevideo、typed完成状态/consumer、同logical budget、媒体所有权/取消、完整SDK payload限额。源/测scope已含controllers unified与vendored视频utils。
- C本机可用ffmpeg/ffprobe8.0.1；scratch/minimax-synthetic有60s无音轨与2s音调合成视频（不是人声），只作本机实验，不是可提交单测夹具。实际旧max30抽帧只到29s，显式timestamp能取59.5s。无真实录屏/设备。
- D1 readonly准备已完成：取消init可选Google ping；typed memory/builder/task copy及aux fields_set/模型参数重建、selected config helper、Session公共模型snapshot/历史/Replay；不要dump/revalidate洗显式性。
- D2/D3：ProviderAPI/CLI/UI全草稿、init迁移/环境变量、诊断去秘密与真实ASGI错误、最小容器注入。CLI真实路径interfaces/cli/commands/{providers,init,run}.py已owned；scripts无Key专用逻辑，暂不改。模型目录不等于视觉/工具能力认证。
- E：六模块+aux Google拒绝spy，混合残留配置，README/CN/Changelog、UI build、wheel/非repo cwd/资源/容器契约与C01–C32对账。Docker CLI存在但Server version实际500，未起/重启daemon；不要声称真实容器通过。

## 环境与边界

- uv/.venv Python3.14.6；filelock/pywin32/jsonschema/referencing直接依赖沿用已有锁版本，无升级。
- full unit仅进程LOCALAPPDATA=scratch/test-localapp，Remove-Item Env:ARTEMIS_APP_DIR（全局设后者会破坏platform原测试）。
- UI基线npm ci/build过，D修改后必须重建；最终wheel安装/资源/非repo cwd仍待E。
- 不读/输出真实Key，不发付费请求，不跑SDK.initialize或真实设备。实际SDK用httpx.MockTransport，控制器fake；真实账户权限/视觉精度/实机闭环needs-verification。
- TDD off / Decision skipped，无strict要求，保持有意义的离线回归，不弱化测试/Host-Origin。
- DriftCheck continue：全六模块MM、legacy兼容、复用既有owner，不移除Google SDK、不部署/合并。恢复先查Git/代理与active slice，不重做已完成片段。
