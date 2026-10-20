# RAG Knowledge Workspace

一个以知识库为中心的 RAG 平台。当前仓库已经包含第一条可运行的垂直闭环：

```text
Docker Compose → 创建知识库 → 上传文档 → 异步解析 → 查看处理状态
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
- 带 `trace_id` 的知识库关键词检索
- OpenAI-compatible 云端 Embedding 适配器（未配置凭据时保持 `not_configured`）
- 健康检查、结构化错误状态和基础 CORS

## 当前边界

这个垂直切片还没有实现：

- 用户注册、OAuth 和权限模型
- 云端 Embedding 适配器已准备，但尚未接入文档索引任务
- 向量检索、混合检索、Rerank 和引用
- LLM 问答、SSE 和反馈

这些能力会沿着 `RAG_PLATFORM_FEATURE_BASELINE.md` 继续接入。当前实现优先保证导入和处理链路真实可运行，未完成的 RAG 能力不会被伪装成已完成。

## 相关文档

- [产品功能基线](./RAG_PLATFORM_FEATURE_BASELINE.md)
- [项目执行规则](./AGENTS.md)
- [技术决策记录](./docs/adr/0001-technology-baseline.md)
