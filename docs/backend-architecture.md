# 后端目录与模块归属

本文记录 Issue #146 已迁移的后端目录。后端按业务组织为模块化单体。
`app.main` 负责 FastAPI 装配，API、Celery Worker 和其他进程入口共用业务模块。

## 第五期最终目录

第五期收齐 #146 的剩余模块。已迁移的知识库、会话、Agent、文本协议、图片和身份模块不重做；仅调整它们与本期模块的接线，以及全站审计路由的归属。

```text
backend/app/
  __init__.py
  main.py, tasks.py, preview_gateway.py, executor_service.py, manage.py
  migrate_cos.py, migrate_knowledge_originals.py, migrate_shared_resources.py, migrate_storage.py
  models.py, storage_models.py                    集中 ORM 注册
  admin/          站点后台路由装配、全站审计查询
  identity/       登录、鉴权、账号管理
  knowledge/      知识库、检索、入库
  conversations/  会话、回答、附件、反馈
  agent/          执行循环、上下文、工具调度
  model_runtime/  模型调用协议与流式传输
  model_catalog/  模型目录、连接、可用性配置
  images/         生图、编辑及图片任务
  files/          文件、资源、预览、下载与额度
  cloud_computer/ 云电脑生命周期、供应商及预览授权
  skills/         Skill 目录、安装、上传与发现
  integrations/   MCP 与题目工作台等外部接入
  search/         联网搜索、配置及计量
  billing/        积分账本、定价、预留与结算
  payments/       订单、渠道、签名回调与充值配置
  site/           站点设置、公告与联系信息
  operations/     健康、就绪与内部部署维护
  infra/          配置、数据库及对象存储连接
```

`app/` 根目录的 Python 文件仅用于包初始化、既有进程或运维命令入口、集中 ORM 注册。业务实现按职责放入现有包：
上下文预算、窗口存取和工具回执归 `agent/`，会话产物保存归 `conversations/`，知识库操作工具归 `knowledge/`，
停用账号清理归 `identity/`。存储迁移实现归 `operations/`，旧命令由同名根目录入口转发。
独立 Skill 执行器实现归 `skills/`，根目录保留 ASGI 入口。

`admin/router.py` 统一登记后台入口；域内管理接口和业务规则留在各自的包，真正跨域的全站审计查询归 `admin/audit_routes.py`。后台不拥有业务规则，普通用户路由、业务模块和 Worker 不导入后台装配。站点管理员、工作区权限、支付回调和运维控制沿用各自的鉴权。模型配置与协议分开，新供应商或管理入口按实际归属扩展，不预建空模块。

验收以迁移前后的完整 OpenAPI、路由匹配顺序、权限、Celery 任务名/参数/队列、Alembic 元数据与既有启动命令为基线。针对支付回调幂等、账务结算、文件权限、云电脑预览与恢复跑回归；真实验收走 `test`，不在本次 PR 自行合并或部署。

| 模块 | 第五期主要入口 |
| --- | --- |
| 后台 | `admin/router.py` 登记各域的管理路由，`admin/audit_routes.py` 查询全站审计 |
| 文件 | `files/workspace.py`、`resources.py`、`shared.py` 管理工作区文件和共享对象，`quotas.py` 管理额度 |
| 云电脑 | `cloud_computer/runtime.py` 管理会话与命令，`adapter.py` 接入供应商，`preview.py` 管理预览授权 |
| Skill | `skills/catalog.py`、`personal.py`、`uploads.py` 管理平台目录、个人 Skill 和直传 |
| 外部接入 | `integrations/runtime.py` 装配会话可用工具，`mcp_client.py` 和 `question_workbench.py` 接入外部服务 |
| 模型目录 | `model_catalog/connections.py`、`shared_providers.py` 和 `providers.py` 管理连接、模型配置和选择 |
| 搜索 | `search/settings.py`、`web.py` 和 `billing.py` 管理配置、搜索和用量计量 |
| 积分与支付 | `billing/credits.py` 和 `workspace.py` 管理账本与计价，`payments/orders.py` 处理订单和签名回调 |
| 站点 | `site/settings.py` 和 `suggestions.py` 管理公告、联系信息与建议 |
| 运维 | `operations/health.py` 和 `deployment.py` 管理健康检查和维护，`site_audit.py` 记录审计事件 |
| 基础设施 | `infra/config.py`、`db.py`、`storage_backend.py` 管理配置、会话工厂和对象存储选择 |

新增管理端点时，在业务域中复用原有依赖和事务，随后把域的路由登记到 `admin/router.py`。新增模型连接或对象存储供应商时，分别从 `model_catalog/` 或 `infra/storage_backend.py` 接入，协议实现和调用方可独立演进。跨域调用直接依赖业务接口。保留现有进程和迁移命令，部署拓扑与集中 ORM 注册保持一致。

## 身份与账号模块

| 模块 | 职责 |
| --- | --- |
| `app.identity.auth` | 账号注册、登录、会话、访问身份、权限检查和 OIDC |
| `app.identity.email_auth` | 邮箱验证、邮件登录和注册流程 |
| `app.identity.wechat_auth` | 微信登录、身份绑定与解绑 |
| `app.identity.site_auth` | 站点认证配置、邮件服务、凭据加解密和管理员鉴权 |
| `app.identity.site_admin` | 站点用户、账号状态、会话撤销及账号相关管理路由 |
| `app.identity.schemas` | 身份、认证设置和账号管理 API 的 Pydantic 请求模型 |
| `app.identity.auth_logging` | 认证回调与供应商凭据的日志过滤 |

`app.main` 注册身份路由，保留 FastAPI 应用入口。其他业务模块直接从 `app.identity.auth`
导入 `Access`、会话和鉴权能力。用户创建仍在原事务中初始化积分账户；站点账号管理路由
继续调用现有账务能力处理积分调整和待结算余额，不改变事务边界与账务规则。

| 原模块 | 当前模块 |
| --- | --- |
| `app.auth` | `app.identity.auth` |
| `app.email_auth` | `app.identity.email_auth` |
| `app.wechat_auth` | `app.identity.wechat_auth` |
| `app.site_auth` | `app.identity.site_auth` |
| `app.site_admin` | `app.identity.site_admin` |
| `app.auth_logging` | `app.identity.auth_logging` |

所有仓库内调用方使用新模块路径。全局 SQLAlchemy 模型保持集中注册，站点审计记录归 `operations/site_audit.py`，配置与数据库连接归 `infra/`。

## 知识库模块

```text
app/
  main.py                         FastAPI 装配、中间件和路由注册
  knowledge/
    routes.py                      知识库 HTTP 路由与请求编排
    schemas.py                     请求/响应契约与文档响应序列化
    libraries.py                   知识库元数据、归档和恢复
    originals.py                   原件路径、引用解析、删除和恢复
    retrieval.py                   权限过滤后的检索和查询证据
    ingestion/
      admission.py                 上传准入、校验、发布和任务派发
      uploads.py                   分片上传生命周期和上传路由
      jobs.py                      入库任务状态、取消、重试和派发
      pipeline.py                  文档解析、切分和索引任务实现
      recovery.py                  Worker 声明与中断任务恢复
      failures.py                  安全错误信息和 Worker 诊断日志
      parsers.py                   文档格式解析
      chunking.py                  确定性切分和原文位置
      embedding.py                 Embedding 服务适配
      docmind.py                   DocMind 服务适配
      parsed_document.py           解析结果类型
  tasks.py                         Celery 应用、稳定任务入口和分派
  files/operations.py              跨业务共用的对象操作日志
```

知识库 HTTP 契约和请求编排放在 `knowledge/routes.py`。知识库状态操作按职责放入
`libraries.py`、`originals.py` 或 `knowledge/ingestion/` 下的具体模块。
请求/响应模型及文档序列化放在 `knowledge/schemas.py`。入库 Worker 的业务实现放在
`knowledge/ingestion/pipeline.py`；当已有队列消息依赖 `app.tasks` 中的任务名和参数时，
保留该入口并在执行时分派到业务实现。`files/operations.py` 提供多个业务域共用的对象操作日志。

## 旧路径映射

| 原模块 | 当前模块 |
| --- | --- |
| `app.document_ingestion` | `app.knowledge.ingestion.admission` |
| `app.document_uploads` | `app.knowledge.ingestion.uploads` |
| `app.chunking` | `app.knowledge.ingestion.chunking` |
| `app.docmind` | `app.knowledge.ingestion.docmind` |
| `app.embedding` | `app.knowledge.ingestion.embedding` |
| `app.document_failures` | `app.knowledge.ingestion.failures` |
| `app.ingestion_recovery` | `app.knowledge.ingestion.recovery` |
| `app.parsed_document` | `app.knowledge.ingestion.parsed_document` |
| `app.parsers` | `app.knowledge.ingestion.parsers` |
| `app.schemas` | `app.knowledge.schemas` |
| `app.knowledge_originals` | `app.knowledge.originals` |
| 原先定义在 `app.main` 的知识库路由与检索 | `app.knowledge.routes`、`app.knowledge.retrieval` |

已迁移的 Python 模块不保留旧路径兼容层；仓库内调用方使用新业务路径。
Celery 任务名仍注册在 `app.tasks`，其任务函数在运行时导入
`knowledge.ingestion.pipeline`。这样已投递的任务仍由原任务名寻址，
Worker 的业务代码也不再依赖 FastAPI 入口。

结构化入库日志的事件文本、字段和 logger 名 `app.document_failures` 均保持不变，
避免现有日志过滤器随模块迁移失效。

## 入库调用示例

知识库路由通过 `knowledge.ingestion.admission` 校验和暂存上传文件。
准入逻辑发布原件和持久化任务状态后，调用 `app.tasks.process_document`。
该稳定 Celery 入口将任务转交给 `knowledge.ingestion.pipeline`；
解析和切分完成后再派发索引任务。`knowledge.ingestion.jobs` 管理取消和重试状态，
`knowledge.ingestion.recovery` 负责领取任务及恢复中断的任务。

各层保持直接、可见的调用关系，不额外引入 service 或 repository 层。

## 会话模块

| 模块 | 职责 |
| --- | --- |
| `app.conversations.routes` | 会话、消息、上下文操作和事件订阅的 HTTP 路由 |
| `app.conversations.schemas` | 会话 API 的请求模型 |
| `app.conversations.messages` | 会话归属检查、输入引用解析和消息序列化 |
| `app.conversations.execution` | 执行回答并保存消息、工具结果和用量 |
| `app.conversations.jobs` | 持久化请求状态、自动命名、任务派发和中断恢复 |
| `app.conversations.stream` | SSE 编码与事件订阅 |
| `app.conversations.management` | 会话元数据和项目管理 |
| `app.conversations.attachments` | 会话附件的 HTTP 路由、校验和模型输入 |
| `app.conversations.feedback` | 消息反馈和问题报告 |
| `app.conversations.documents` | 会话产物保存到知识库的路由、校验和恢复 |

FastAPI 通过 `conversations.routes` 接收请求。Worker 通过原有的
`app.tasks.execute_conversation(identifier)` 和 `app.tasks.name_conversation(identifier)`
进入 `conversations.jobs`。回答任务直接调用 `conversations.execution`，HTTP 和 Worker
通过 `conversations.messages` 共用归属检查与序列化。

## Agent

| 模块 | 职责 |
| --- | --- |
| `app.agent.loop` | 模型轮次、工具选择与调度 |
| `app.agent.effective_context` | 有效上下文管理、滚动窗口与结果恢复 |
| `app.agent.conversation_context`、`app.agent.context_windows` | 上下文预算和消息构造、持久化窗口和协议投影 |
| `app.agent.tools` | 计划及工具结果读取所用的协议与分页 |
| `app.agent.tool_jobs` | 工具任务归属、活动状态与取消 |
| `app.agent.tool_detail`、`app.agent.tool_result_reads`、`app.agent.tool_errors` | 回执展示、结果读权限和可公开错误 |

`conversations.execution` 保存用户可见的回答与用量，并调用 `agent.loop`。
文件操作、云电脑、图片和知识库工具仍在其业务模块内执行。

## 图片生成与编辑

| 模块 | 职责 |
| --- | --- |
| `app.images.routes` | 图片任务与站点图片模型管理 HTTP 路由 |
| `app.images.schemas` | 图片任务、模型设置与结果核对请求模型 |
| `app.images.tasks` | 任务准入、幂等、状态恢复、供应商调用编排、积分结算与文件交付 |
| `app.images.adapters` | OpenAI Images、Responses 与 Gemini 图片协议和图片校验 |
| `app.images.presets` | 图片模型参数预设目录与规格校验 |
| `app.images.tools` | Agent 生图/编辑工具声明、参数适配与结果回传 |

`app.main` 注册 `images.routes`。原有 `app.tasks.process_image_task` 和
`app.tasks.recover_image_tasks` 保持任务名及参数不变，并在执行时调用
`images.tasks`，因此已投递的 Celery 消息仍使用原入口。会话恢复与 Agent
结果续接直接读取 `images.tasks`。`files/image_reading.py` 提供对话模型输入与云电脑预览共用的图像读取。

## 文本模型协议

| 原模块 | 当前模块与职责 |
| --- | --- |
| `app.llm` | `app.model_runtime.client`，文本客户端、OpenAI Chat Completions 和用量解析 |
| `app.llm_native` | `app.model_runtime.native`，Anthropic 和 Gemini 原生协议 |
| `app.llm_responses` | `app.model_runtime.responses`，Responses 请求、流式响应与续接 |
| `app.chat_sampling` | `app.model_runtime.sampling`，单轮采样和流式传输恢复 |
| `app.responses_socket` | `app.model_runtime.responses_socket`，Responses WebSocket 传输 |

`model_catalog/providers.py` 从站点模型和连接配置构造 `model_runtime.client.ChatProviderConfig`。
计费模块继续读取客户端测得的用量。`infra/upstream_retries.py` 同时服务文本、Embedding
和其他外部能力，归属共享基础设施。

## 第三期旧路径映射

| 原模块 | 当前模块 |
| --- | --- |
| `app.image_tasks` | `app.images.routes`、`app.images.schemas`、`app.images.tasks` |
| `app.image_adapters` | `app.images.adapters` |
| `app.image_presets` | `app.images.presets` |
| `app.image_tools` | `app.images.tools` |

结构化图片任务日志继续使用 logger 名 `app.image_tasks`。图片生成和编辑的 HTTP
路径、请求/响应结构及 Celery 任务清单保持不变。
