# Issue #93：Agent 知识库基础管理

## 目标、范围和验收

基线 main@4cd9ca0；领取评论 6825915273。补齐创建知识库、修改名称/描述/标签、按需查询有权访问的知识库和文档处理状态。身份绑定服务端当前用户/工作区，沿用现有 editor/admin 与知识库授权。用户明确要求且信息充分时直接执行；目标或内容不明先澄清。普通问答、资料、附件、Skill、网页或工具结果不能授权写入。

范围外：上传/导入、删除/归档、权限管理、账户或管理员操作；不修改发现、检索、@、引用、拒答或 save_to_knowledge_base 策略，不处理 #96。禁止真实付费模型、OCR、沙箱及业务数据操作；完成后提交 PR 等待人工 review，不合并、不关闭 Issue、不部署。

验收与验证：隔离数据库、临时用户及目录覆盖跨用户/工作区、受限知识库、只读成员、撤权、参数验证、重复调用/重建运行时/并发幂等与冲突、文档真实处理状态/活动版本/归档/删除可见性。覆盖四种供应商协议的工具定义与调用、Agent 活动及持久化恢复，复用现有活动组件验证入口、失败和窄屏。执行相关后端回归、前端测试/类型检查/构建及 git diff --check。模拟供应商与真实模型效果分别记录。

## PR #97 元数据刷新跟进（2026-11-13）

目标：修复评审评论 6840090125 指出的同一 SPA 内 Agent 创建后误报无权访问、修改后详情仍显示旧描述的问题。范围仅为知识库页面元数据重新读取、加载和并发响应保护，以及隔离浏览器回归。范围外：后端工具、权限规则、检索策略、真实付费模型、部署及 #96。

验收：先加载已有目录，通过站内导航进入聊天，创建后立即键盘打开活动入口；返回原聊天，仅改描述后立即打开更新入口。全程保持同一 document，详情、侧栏及目录读取服务端最新名称、描述和标签；读取未完成时不误报无权访问，读取失败可重试，真正不可访问仍显示原反馈。验证 1440/390px、前端测试、类型检查、生产构建和 diff 检查，不使用页面刷新掩盖缓存问题。

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
- 当时入口检查前执行了页面刷新，仅证明刷新后的入口、活动恢复及布局可用，未覆盖同一 SPA 内创建/更新后立即打开，不能作为该流程已通过的证据；评审评论 6840090125 发现了此遗漏。1440/390px 长名称、手机入口 44px、失败无成功入口、标题编辑取消及菜单/Escape 的原检查保留；无刷新流程的修复与复验见下文。

扩展回归初跑曾遇到旧保存夹具的随机 UUID 被 SQLite 数值亲和转换成浮点值，发生在测试准备阶段；本工具测试改用独立夹具，并为新建的合成消息/请求 ID 加字母前缀，之后上述相关回归通过。生产 PostgreSQL UUID 模型未作改动。

本轮未调用真实付费模型、OCR、Embedding 服务、对象存储或云电脑，未产生真实费用。四种协议和浏览器用的都是替身模型；普通问答/恶意资料不触发写入的模拟检查及提示词断言，不替代真实模型的意图判断与抗注入验收。真实模型效果仍待维护者 review 后在明确预算内另行验收，不能据本轮结果声称已完成商业供应商端到端验证。

### 2026-11-13 元数据刷新复验

- 在未修改的 b4c8bde 界面运行纠正后的入口检查，先加载目录、创建后立即键盘打开；等待真实标题超时，快照显示“无法访问这个知识库”及旧侧栏。证据：`output/playwright/issue93-before-refresh-fix.png`。
- 页面按目录/聊天/知识库目标重新读取 `/knowledge-bases` 授权列表；读取完成前复用原加载组件，失败走原错误和重试入口，最新授权结果缺项才显示不可访问。按请求顺序及当前目标丢弃过期响应，不把活动中的对象当作权限依据；切换文档/配置等同库视图不重复读取。
- 新生产构建下，真实 Chrome + 隔离 API/Worker + 临时 SQLite/账号 + 脚本模型完成无刷新创建、部分更新、立即打开、侧栏/配置/目录同步；document 标记确认关键流程没有 reload。1440/390px、手机实际打开和导航、键盘入口通过，名称/标签保留且描述最新。
- 浏览器控制响应验证：暂停列表读取时显示加载态且没有权限误报；延迟的旧聊天列表在最新详情之后返回不会覆盖更新；503 错误可重试且不误报权限；模拟授权列表不包含该对象时隐藏旧详情并显示不可访问。503/授权缺项是前端响应模拟，不能替代真实服务端权限测试。
- 原状态/目录查询、普通问答、失败活动和刷新恢复继续通过：5 条工具活动、2 条写回执，ready 可检索、queued 不可检索，切换与刷新均未增加写回执。前端 15 项测试、TypeScript/Next 生产构建、脚本语法及 `git diff --check` 通过。逐张查看创建活动、更新配置、结束活动的桌面/手机截图，共 6 张；另保存立即打开的新库截图。
- 本次仅修改前端刷新及浏览器验收/文档，未重跑既有 198 项后端、2 项 PostgreSQL 与迁移回归；这些仍是前次证据。本次未调用真实付费模型、OCR 或云电脑，费用为 0。评审评论 6840090125 在旧 SHA 的真实模型验收属于评审者独立证据：7 条用户请求、两条写回执及有限意图/权限样本通过，同时发现前端缺陷；不冒称为本次重跑或全部抗注入已验收。新 SHA 的真实商业模型复验仍由维护者决定。

截图位于忽略目录 `output/playwright/issue93-{created,updated,completed}-{1440,390}.png` 和 `issue93-created-library-1440.png`。未新增界面文案或组件，保留现有反馈与服务端权限边界。

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
