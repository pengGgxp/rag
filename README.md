# RAG Knowledge Workspace

一个以知识库为中心、支持用户管理 MCP 和 Skill 的 Agent 平台：

```text
Docker Compose → 创建知识库 → 上传文档 → 异步解析 → 1024 维向量化 → 混合检索
```

## 本地运行

要求：

- Docker Desktop
- Docker Compose

启动：

```bash
docker compose up --build
```

打开：

- Web：<http://localhost:3000>
- API 文档：<http://localhost:8000/docs>
- RustFS 控制台：<http://localhost:9001>

停止：

```bash
docker compose down
```

## 当前实现

- Next.js + TypeScript Web 工作台
- FastAPI 模块化单体 API
- PostgreSQL + pgvector 镜像
- Redis + Celery 异步任务
- RustFS S3 兼容对象存储
- Alembic 数据库迁移
- PDF、DOCX、Markdown、TXT、HTML、CSV、XLSX 文本解析
- 文件 hash 去重
- 文档上传、对象存储、解析和 Job 状态
- 确定性文本分块，保留字符偏移和标题路径
- 可查看文档 chunks
- 带 `trace_id` 的知识库混合检索（PostgreSQL 全文检索 + BM25 + pgvector cosine + RRF）
- 持久化检索 Trace，可按 `trace_id` 复盘查询、召回方式和排序结果
- 基于证据阈值的问答接口，返回拒答状态、原因和可定位引用
- OpenAI-compatible 云端 Embedding 适配器，当前默认维度为 1024
- OpenAI-compatible LLM 适配器（配置 `LLM_BASE_URL`、`LLM_API_KEY`、`LLM_MODEL` 后启用问答生成）
- LangGraph 受控 ReAct Agent，可按需调用知识库检索工具并继续检索
- 用户管理的 MCP 连接、逐工具授权和会话级扩展选择
- 多文件 Skill 文件夹/ZIP 导入、文件管理、完整 ZIP 导出及 Agent 渐进加载
- 文档向量化任务、失败状态、重试和手动重建索引
- 健康检查、结构化错误状态和基础 CORS

## Agent 扩展配置

认证 MCP 的请求头使用独立密钥加密保存。首次使用前生成并持久化到本地 `.env`：

```powershell
docker compose run --rm --no-deps -v "${PWD}:/workspace" api python /workspace/tools/configure_mcp.py
docker compose up -d api
```

安全备份 `.env` 中的 `MCP_CREDENTIAL_KEY`；不要提交或随意替换，丢失后已有 MCP 凭据无法恢复。
未配置认证头的连接不依赖加密密钥。

MCP 默认禁止连接内网和回环地址。管理员需要接入可信内网服务时，可在
`MCP_ALLOWED_PRIVATE_HOSTS` 中设置逗号分隔的精确主机名，并重新创建 API 容器。
不要对普通用户开放该配置。连接和工具授权只属于当前工作区中的本人。

## 当前边界

- 尚未引入 Rerank；Agent 可以自主多步检索。
- MCP 支持远程 Streamable HTTP 和 SSE 工具，不支持任意 stdio、MCP OAuth、resources/prompts。
- Skill 支持 SKILL.md 和 references/scripts/assets 等附属文件，Agent 可按需读取 UTF-8 文本。
  二进制资源原样保存并可下载；不运行技能包脚本、不安装依赖、不自动解析二进制资源。
- MCP 授权可能允许外部写入，当前不提供逐次操作审批，也不自动重试外部操作。

## 相关文档

- [产品功能基线](./RAG_PLATFORM_FEATURE_BASELINE.md)
- [项目执行规则](./AGENTS.md)
- [技术决策记录](./docs/adr/0001-technology-baseline.md)
- [Agent 扩展边界与验收](./docs/agent-extensions-acceptance.md)
- [多文件 Skill 设计与验收](./docs/skill-bundles-acceptance.md)
