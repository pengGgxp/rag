# 有效上下文窗口（Issue #96）

回答收尾保存的加密 continuation 仅供审计，序列化前单独将工具图片转换为安全描述，不保存图片字节。转换不修改 Agent 当前项或权威窗口的完整图片快照，也不删除仍有效的工具配对、reasoning 或协议签名。刷新和 Worker 恢复仍从权威窗口加载图片，不从审计描述恢复。

模型输入的唯一历史来源是 `ConversationContext`。没有活动检查点时，窗口是加密 suffix；安装检查点之后，恢复公式固定为 **checkpoint.encrypted_replacement + context.encrypted_suffix**。系统提示每次从当前配置构造，不写入窗口。原始 `ChatMessage`、SSE 事件、旧 continuation 和前端显示状态均不能决定恢复边界。

`context_windows.py` 负责项标识、加密序列化、工具配对、协议投影和数据库读写。`effective_context.py` 提供统一的加载、当前输入追加、模型/工具追加和本地摘要安装。项保存来源、原消息/请求归属和工具调用 ID，工具图片与交接不会被当作真实用户消息。临时工具图片保存为带稳定 snapshot_id 和 SHA-256 的加密不可变快照；加载时完整恢复并校验字节，鉴权始终依附所属会话和工作区。上传资源图片仍逐次鉴权，实际字节必须匹配窗口记录的指纹，资源被修改或删除时失败。

摘要生成在安装事务外。所有写入先锁定 Conversation，安装再比较 window_id、revision、请求和权限范围，同一事务写 replacement 并切换指针。提交后将同一状态安装进 Agent，最后发 completed。手动操作结果也在该事务中保存，事件丢失或进程退出不撤销已安装窗口。追加和安装使用稳定 ID；修订竞争失败，不复用过期候选。自动压缩在相同未变化修订上只尝试一次。

预算参考 Codex：自动阈值不高于 `min(0.9*C, H)`，硬输入上限 `H=min(floor(0.95*C), C-O)`。估算通过四协议的实际请求投影，覆盖工具、系统、图片及不透明字段；计费仍使用既有用量规则。保留用户项从新到旧选择，历史用户预算上限 20,000 tokens，同时服从硬容量；当前输入、待处理的最后完整工具组和未完成后缀整体保护。摘要输入超容量时，逐个移除最老可移除完整组；候选有效、严格缩小且可容纳才安装。没有固定保留轮数、60% 目标或固定摘要输出限额。

协议切换只转换当前项。已完成历史按规范化字段投影，移除其他模型的 reasoning、原生签名和 Responses output；必需图片同步转换。不能合法转换的未完成工具后缀返回 `context_suffix_incompatible`。窗口不可恢复返回 `context_window_unrecoverable`，容量不足返回 `context_capacity_exceeded`，安装竞争返回 `context_revision_conflict`。HTTP 错误与执行中的 context_error 事件均携带稳定错误码；上下文事件携带 window_id/context_revision。

权限、资料状态或扩展版本变化后，已有交接或私有工具资料不能证明安全时阻断续接，保留原记录并提示新建会话；不读取旧聊天来恢复内容。尚未取得私有证据的发现流程继续由既有工具重新鉴权。worker 中断仍停止执行，用户显式重试；未配对调用不自动重放，也不伪造结果。已完成工具后缀可供显式重试继续使用。

0066 迁移新增表和检查点字段，不修改原始聊天。加密旧数据在第一次授权加载时初始化一次。未压缩会话使用当时保存的完整项；旧摘要只能在原范围摘要指纹、完整前缀和后缀可证明时转换。旧同轮检查点、损坏的 continuation 或缺失工具后缀会阻断。初始化之后不会因原始聊天被修改而改用旧边界。

验收以最终 HTTP JSON 为准：`test_context_replacement.py` 使用本地 HTTP 假供应商覆盖四协议，检查被移出的消息、参数、工具结果和不透明字段在恢复、后续追加和第二次 compact 时不回流。另有取消、CAS、完成事件丢失、工具配对、图片快照、旧数据转换及 PostgreSQL 并发安装测试。PostgreSQL 测试只接受 `rag_auth_test_` 前缀的临时数据库。真实模型/云沙箱验收需单独预算授权，本次不部署。

实现和交接提示的结构参考 OpenAI Codex 提交 `c0cc9fc820f62e4dbdde68214dd6d792d6c8a7ee`，尤其 compact.rs、context_manager/history.rs、compact_remote_v2_installation.rs 及 compact/prompt.md。本项目采用本地摘要的替换、安装和恢复机制，没有引入远程 compact 服务。Codex 为 OpenAI 版权所有，采用 Apache-2.0 许可；这里使用独立 Python 实现和中文交接说明。
