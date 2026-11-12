# Issue #93：Agent 知识库基础管理

## 目标、范围和验收

基线 main@4cd9ca0；领取评论 6825915273。补齐创建知识库、修改名称/描述/标签、按需查询有权访问的知识库和文档处理状态。身份绑定服务端当前用户/工作区，沿用现有 editor/admin 与知识库授权。用户明确要求且信息充分时直接执行；目标或内容不明先澄清。普通问答、资料、附件、Skill、网页或工具结果不能授权写入。

范围外：上传/导入、删除/归档、权限管理、账户或管理员操作；不修改发现、检索、@、引用、拒答或 save_to_knowledge_base 策略，不处理 #96。禁止真实付费模型、OCR、沙箱及业务数据操作；完成后提交 PR 等待人工 review，不合并、不关闭 Issue、不部署。

验收与验证：隔离数据库、临时用户及目录覆盖跨用户/工作区、受限知识库、只读成员、撤权、参数验证、重复调用/重建运行时/并发幂等与冲突、文档真实处理状态/活动版本/归档/删除可见性。覆盖四种供应商协议的工具定义与调用、Agent 活动及持久化恢复，复用现有活动组件验证入口、失败和窄屏。执行相关后端回归、前端测试/类型检查/构建及 git diff --check。模拟供应商与真实模型效果分别记录。

## 实现取舍

- 新增小型知识库写入回执表（0066_knowledge_base_tools）。以回答 request_id 和稳定活动 call_id 派生键，同一调用参数变化拒绝，修改与回执在同一事务提交；锁定原 assistant message 串行准入，不依赖内存缓存或供应商调用 ID 全局唯一。
- 不为历史调用补写回执，不增加过渡开关。迁移仅建空表；失败事务整体回滚。降级会删除幂等回执，必须停止相关 Agent 写入并保留备份，不能在有未完成重投递时降级后再次执行原请求。
- 创建字段沿用 KnowledgeBaseCreate，编辑参数继承 KnowledgeBaseUpdate 验证，只校验并更新明确字段；不清空省略字段，也不因旧描述超出现有编辑长度而阻止单独改名。审计沿用知识库创建/更新事件。
- 读工具为只读成员开放，分页且不返回正文、存储路径或内部错误。文档查询包含未完成的替换版本和历史版本，只有活动 ready 版本且知识库未归档才标记 searchable。
- 语义上的用户写入意图沿用现有 Agent 系统提示与工具说明约束，不新增关键词授权器；服务端独立负责身份、权限、对象和调用幂等校验。模拟协议测试不能证明真实模型在所有 prompt injection 下的意图判断，需要明确记录此限制。
- 前端扩展已有 ConversationActivity 和 activityItem，复用 Link、Lucide 与现有活动样式，不创建重复结果组件。入口由真实 ID 构造，不渲染模型提供的任意 URL。

## 验证记录

2026-11-12 在独立 worktree 执行，数据库、账号和资料均为合成数据：

- 后端相关回归 198 项通过，耗时 184.56 秒；覆盖新增工具、原成果保存、Agent、扩展、账号权限、知识库生命周期/标签、会话、资源 @、检索及四种模型协议。新增工具测试 38 项通过，包含 Chat Completions、Responses、Anthropic 和 Gemini 各四个工具的定义、调用解析及结果续传。随后新增的跨库文档 ID 隔离断言单独复跑通过。
- 独立 pgvector/PostgreSQL 15 容器、空测试库 `rag_auth_test_issue93` 全量迁移至 0066；新增四线程并发创建/更新、撤权回放与原成果保存并发回归共 2 项通过。降级至 0065 后重新升级通过；未操作开发或业务数据库。
- 前端 `npm test`：15 项通过；`NEXT_PUBLIC_API_URL=http://localhost:18193 npm run build` 中 TypeScript 与 Next.js 生产构建通过。`node --check scripts/check-knowledge-base-tools.js` 和 `git diff --check` 通过。
- 本地真实 Chrome 使用真实 API/Worker、临时 SQLite 与脚本模型，完成创建、只修改描述、查询文档状态、列出目录、普通问答与不可访问对象失败。共 5 次活动、2 条写入回执；页面刷新后仍为 2 条，不产生额外知识库。排队资料返回 searchable=false，就绪活动版本返回 true。
- 已实际点击/键盘打开真实知识库入口，查看 1440px 与 390px 截图，长名称无横向溢出，移动入口至少 44px，失败活动没有成功入口且给出重新选择的下一步；检查活动持久化恢复、标题编辑取消、手机导航/账号菜单及 Escape。复用原活动 UI，没有增加技术说明文案。截图位于忽略目录 `output/playwright/issue93-{created,completed}-{1440,390}.png`。

扩展回归初跑曾遇到旧保存夹具的随机 UUID 被 SQLite 数值亲和转换成浮点值，发生在测试准备阶段；本工具测试改用独立夹具，并为新建的合成消息/请求 ID 加字母前缀，之后上述相关回归通过。生产 PostgreSQL UUID 模型未作改动。

本轮未调用真实付费模型、OCR、Embedding 服务、对象存储或云电脑，未产生真实费用。四种协议和浏览器用的都是替身模型；普通问答/恶意资料不触发写入的模拟检查及提示词断言，不替代真实模型的意图判断与抗注入验收。真实模型效果仍待维护者 review 后在明确预算内另行验收，不能据本轮结果声称已完成商业供应商端到端验证。

## 复现方式

后端使用仓库已有 Python 环境，从 backend 运行：

```powershell
python -m pytest tests/test_knowledge_base_tools.py tests/test_conversation_documents.py tests/test_agent.py tests/test_extensions.py tests/test_auth.py tests/test_knowledge_base_lifecycle.py tests/test_knowledge_base_tags.py tests/test_conversations.py tests/test_resource_mentions.py tests/test_llm.py tests/test_llm_native.py tests/test_llm_responses.py tests/test_retrieval.py -q
```

PostgreSQL 测试先准备独立空库并迁移，显式设置 AUTH_TEST_DATABASE_URL；夹具拒绝数据库名不以 `rag_auth_test_` 开头的连接，然后运行 `tests/test_knowledge_base_tools_postgres.py tests/test_conversation_documents_postgres.py`。

浏览器验收从 backend 运行 `python tests/knowledge_base_tools_preview.py`，仅绑定 127.0.0.1:18193；从 frontend 按上述 API 地址构建并以 `next start --hostname 127.0.0.1 --port 3193` 启动。根目录创建 `output/playwright` 后执行：

```powershell
npx --yes --package @playwright/cli playwright-cli -s=rag93 open http://localhost:3193 --headed
npx --yes --package @playwright/cli playwright-cli -s=rag93 run-code --filename scripts/check-knowledge-base-tools.js
```

每次重跑必须重启后端夹具，以获得全新临时目录和空写入回执。夹具只供本地验收，不用于部署。
