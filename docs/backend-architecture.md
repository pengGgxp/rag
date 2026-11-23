# 后端目录与模块归属

本文记录 Issue #146 已迁移的后端目录。后端按业务组织为模块化单体。
`app.main` 负责 FastAPI 装配，API、Celery Worker 和其他进程入口共用业务模块。

## 知识库模块

```text
app/
  main.py                         FastAPI 装配、中间件、健康检查和路由注册
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
  file_operations.py               跨业务共用的对象操作日志
```

知识库 HTTP 契约和请求编排放在 `knowledge/routes.py`。知识库状态操作按职责放入
`libraries.py`、`originals.py` 或 `knowledge/ingestion/` 下的具体模块。
请求/响应模型及文档序列化放在 `knowledge/schemas.py`。入库 Worker 的业务实现放在
`knowledge/ingestion/pipeline.py`；当已有队列消息依赖 `app.tasks` 中的任务名和参数时，
保留该入口并在执行时分派到业务实现。`file_operations.py` 仍属共享基础设施，
因为知识库以外的业务也使用它。

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

FastAPI 通过 `conversations.routes` 接收请求。Worker 通过原有的
`app.tasks.execute_conversation(identifier)` 和 `app.tasks.name_conversation(identifier)`
进入 `conversations.jobs`。回答任务直接调用 `conversations.execution`，HTTP 和 Worker
通过 `conversations.messages` 共用归属检查与序列化。
