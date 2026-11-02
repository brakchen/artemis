# 后继审查提示

规格唯一来源仍为技术方案；以下只记录需用实际代码/测试核实的边界，不是预判缺陷。

## B2

- 检查真实 Pro 单动作/burst 和 Flash/MCP adapter，而不是仅调用新 helper；旧索引不能换页后重映射。StateView writes 要实际可写，不吞异常。
- 队列最后校验/失效必须发生于真正 call_tool 前；首次 concurrent get_action_session 只有一个 owner。取消前未派发与取消后可能已发分开，unknown 不重发且观察/记录状态，不能仅让 helper 返回枚举。
- 强 XML 路径要用 fresh live capture/树判断可见、可操作与适当父级，遮挡/多子容器/零面积不得凭 text 唯一就 local allow；没有强证据才走 typed pixel，invalid metadata 直接 block。
- 检查 MiniMax Agent shell binding 的拒绝确实涵盖 Pro 三个 factory/Diagnoser短命令 alias，Flash未知工具不退回任意shell。model不能传origin参数放行；direct用户与legacy仍可调用。
- 原始用户命令 tracked 前后台/cloud生命周期保持屏障，但 opaque detached child 无法由宿主进程完成证明终止；此为文档明确边界，不据此构造通用 shell parser。
- 观测/等待/搜索不应无故改变决策capture；confirmed not_dispatched版本恢复不得覆盖并发的新观察。typed DispatchState 输出 additive 会同步12个action schema；检查fixture与canonical一致，不能放宽测试。

## C/D/E

- 媒体完整请求限额、真实时间轴、必需语音、状态与清理；aux任务配置显式性/永久错误；真实ASGI验证错误、全报告秘密、历史实际模型；详见30-next-slices与40-acceptance。
