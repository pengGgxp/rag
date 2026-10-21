# RAG Knowledge Workspace

一个以知识库为中心的 RAG 平台。当前仓库已经包含第一条可运行的垂直闭环：

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
- 文档向量化任务、失败状态、重试和手动重建索引
- 健康检查、结构化错误状态和基础 CORS

## 当前边界

这个垂直切片还没有实现：

- 用户注册、OAuth 和权限模型
- Rerank、引用展示和可验证的回答证据
- LLM 问答、SSE 和反馈

这些能力会沿着 `RAG_PLATFORM_FEATURE_BASELINE.md` 继续接入。当前实现优先保证导入和处理链路真实可运行，未完成的 RAG 能力不会被伪装成已完成。

## 相关文档

- [产品功能基线](./RAG_PLATFORM_FEATURE_BASELINE.md)
- [项目执行规则](./AGENTS.md)
- [技术决策记录](./docs/adr/0001-technology-baseline.md)
