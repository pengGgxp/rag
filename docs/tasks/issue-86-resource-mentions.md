# Issue #86：消息级资源文件提及

## 目标与范围

- 在现有 `@` 补全中增加独立“资源文件”分组，按文件名/路径筛选，使用不同标记区分知识库与文档；覆盖选择、移除、草稿恢复、消息保存和失败重试。
- 服务端按当前工作区成员权限生成候选，发送前和 Worker 处理时重新校验文件存在及可用状态，只传定位元数据，不转成附件或预读正文。
- 用户已在 Issue 评论确认：选择/发送资源引用不启动云电脑；Agent 实际执行文件操作时沿用现有启动、权限和计费规则。
- 保留知识提及的方案 B 与当前资源库的工作区共享、读写权限边界；不修改商业价格、工具收费规则、云电脑生命周期或知识引用模型。

## 验收与验证

- 混合三类提及可筛选、键盘选择、移除和刷新恢复；同名资源使用路径辨认，手机无横向溢出，加载/错误/无结果状态准确。
- 资源引用仅保留元数据，普通回复不调用文件读取、不创建云电脑或其费用；实际文件工具继续走现有执行链路。
- 删除、不可用对象、跨工作区 ID、成员撤权在发送/运行/重试时重新校验；拒绝发送发生在消息和模型扣费请求创建前。历史资源标记保留，重试跳过失效资源。
- 执行后端权限/对话/文件/重试定向回归、隔离 PostgreSQL 迁移默认值和数据保留验证、前端测试/构建、真实页面 1440/700/390px 及 reduced motion 验证、`git diff --check`。
- 不执行真实付费模型或云电脑调用、不操作业务数据、不部署、不合并或关闭 Issue。

## 分支

沿用已有独立 worktree 和开放 PR #87 的 `codex/issue-86-workspace-knowledge-search` 分支。开始时主工作区与该 worktree 均干净，已合入默认分支最新的 PR #90 上游重试和超时修复。

## 实现与验收结果（2026-11-12）

- 复用 `DocumentMentionMenu` 与既有输入标记，新增独立资源文件分组、图标及相对路径，三类共享 8 项上限。选择、删除、草稿刷新、混合发送和历史刷新均已验证；同时修复恢复草稿时初始空状态覆盖快照，以及手机错误通知超出右边界的问题。
- 资源目录使用现有对象元数据索引；发送前和 Worker 开始时复核当前工作区成员与资源。只保存 ID、文件名、相对路径、类型及大小，不预读正文，不将引用转为附件。失败重试保留历史快照，向 Agent 提供当前有效元数据，跳过失效文件。
- Agent 按实际任务使用原有文件工具，资源 ID 不作为知识文档 ID 或引用证据。云电脑关闭和开启配置下，普通确认回复均未读取正文、创建云电脑或命令；实际模型用量仍按原有规则结算。资源引用并不免除后续模型或文件操作费用。
- 新增迁移 `0065_resource_mentions`。隔离 PostgreSQL 15 验证空库升级、`0063 → head`、重复升级、回退再升级、旧消息和新消息默认空列表、资源快照写入，既有会话/消息/引用/提及/账务快照保持完整；版本列仍为 `VARCHAR(32)`。

### 自动测试与构建

在隔离 PostgreSQL 配置 `AUTH_TEST_DATABASE_URL` 后，从 `backend` 运行：

```text
python -m pytest tests/test_resource_mentions.py tests/test_conversations.py tests/test_auth.py tests/test_resources.py tests/test_cloud_computer.py tests/test_cloud_computer_mount.py tests/test_agent.py tests/test_context_budget.py tests/test_knowledge_mentions_migration.py tests/test_conversation_management_postgres.py tests/test_upstream_retries.py -q
```

结果为 **210 passed、1 skipped**。跳过的是需要显式付费授权的真实云挂载验收；新增资源引用回归 **16 项通过**，包含元数据隔离、重复去重、混合上限、无工具模型、删除/缺失/不可用/跨工作区拒绝、提交后 Worker 撤权/删除、重试时保留历史并刷新当前元数据，以及 Agent 经现有文件工具按 ID 读取。

前端 `npm test` **11 项通过**，TypeScript 检查、`npm run build` 与 `git diff --check` 通过。没有复跑全量后端。

### 实际页面验收

使用 `tests/resource_mentions_preview.py` 启动真实 API、一次性 SQLite、内存对象存储、独立 Worker 执行及本地模拟模型；前端先在开发模式检查，再在生产构建检查。测试用户和资料均为合成，没有真实付费上游或云电脑操作。

可重复验收方式：

1. 从 `backend` 运行 `python tests/resource_mentions_preview.py`，本地 API 为 `http://localhost:8126`。
2. 从 `frontend` 设置 `NEXT_PUBLIC_API_URL=http://localhost:8126`，运行 `npm run dev -- --port 3110`，或构建后用相同端口启动生产页面。
3. 打开 Playwright CLI 会话，再执行 `run-code --filename scripts/check-resource-mentions.js`。该脚本会删除本夹具的合成对象，重复运行前需重启夹具。

**1440 / 700 / 390px** 均验证三类分组、同名区分、文件名筛选、方向键/Enter/Tab/Escape、移除、刷新保留文本与 ID、混合三类发送、历史恢复、加载/失败重试/空结果、对象失效后 409 与草稿保留、reduced motion 及无横向溢出。最终八张生产页面截图已逐张查看，保存在忽略目录 `output/playwright/issue86-resource-*.png`；手机错误通知完整可见。

### 限制与未验收项

- 本轮模拟模型确定性验证工具选择及接口行为，没有重新验收商业模型自主选择资源、实际云端读取/修改或供应商账单；实际文件工具到云电脑的委派在测试中拦截，既有云电脑/挂载回归使用 FakeCloud。此前知识检索的真实模型验收仍见 `docs/workspace-knowledge-acceptance.md`，不能据此宣称新增资源功能已完成真实付费验收。
- 不改变文件工具的既有角色边界；资源目录与引用按工作区成员可见，实际工具可用性继续遵循当前云电脑配置和角色。
- 需先升级数据库再启动新 API。未执行线上迁移、部署、业务数据操作、商业价格修改、合并或关闭 Issue。PR 与 Issue 保持开放，等待人工复核。
