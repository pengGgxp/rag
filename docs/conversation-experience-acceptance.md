# 问答体验验收

2026-11-07：Issue #76 的本地改造及验收记录见 `conversation-management.md` 和 `tasks/conversation-experience.md`。当前已具备用户、工作区及知识库权限；Markdown 导出入口已移除，回答与 HTTP 订阅解耦。以下保留 Issue #13 的历史记录，不代表现行接口。

对应 GitHub Issue #13。问答现在是持久化的知识使用工作流，不再只保留当前
页面的一次请求。

## 已实现

- 独立的问答工作区和历史对话列表。
- 新建、切换、搜索、重命名、软删除对话。
- 一个对话可固定选择多个知识库；创建后资料范围不可悄悄改变。
- 每轮用户问题和助手回答写入数据库，刷新后恢复。
- 追问会使用历史消息改写检索问题，但每轮仍重新检索当前知识库；
  历史回答不是新的证据来源。
- OpenAI-compatible `stream=true` 流式回答；前端增量渲染。
- 停止回答、断线失败、部分内容保存和最后一条回答重试。
- 多知识库回答的来源标注知识库名称，每个知识库保留独立检索 trace。
- 引用继续打开原文片段，Trace 继续按知识库查看。
- Markdown 对话导出。
- 文档删除后，后续追问重新检索，不会使用上一轮的旧来源。
- 服务重启时遗留的 `streaming` 消息会标记为 `interrupted`，可以重试。

## API

```text
POST   /conversations
GET    /conversations
GET    /conversations/{id}
PATCH  /conversations/{id}
DELETE /conversations/{id}
POST   /conversations/{id}/messages
POST   /conversations/{id}/stop
GET    /conversations/{id}/export
```

消息接口使用 Server-Sent Events 事件：`message`、`phase`、`sources`、
`delta`、`done`。每个请求必须携带客户端生成的 `request_id`，服务端使用
它保证重复提交不会新增重复消息。

## 边界

当前会话仍未接入用户身份、工作区成员权限和跨用户隔离；这些属于认证与多租户
范围，不在本次问答体验改动内。流式回答使用兼容 OpenAI Chat Completions 的
接口，供应商返回非流式 JSON 时也会降级为单段回答。

## 验证

- `npm run build` 通过。
- Docker API 测试：29 passed。
- 真实云端 Playwright：两个知识库回答、24 次增量 DOM 更新、来源、两个 trace、
  追问检索改写、刷新恢复、重命名、Markdown 导出、390px 移动布局。
- 测试覆盖：多库隔离、删除资料后的拒答、空证据、失败重试、停止回答、
  重复请求、服务重启恢复和迟到响应不能覆盖取消状态。
