# RAG 知识库平台功能基线

> 本文档是平台的正式功能基线。后续架构、数据库、接口和代码实现都应围绕本文档展开，不能退化成只有“上传文件 + 聊天框”的简单 Demo。

## 一、产品定位

这是一个以知识库为中心的 RAG 平台，完整覆盖：

> 资料导入 → 文档处理 → 知识库构建 → 检索调试 → 知识问答 → 引用追溯 → 反馈评估 → 持续维护

聊天只是使用入口，不是产品主体。

## 二、功能清单

### 1. 用户、工作区与权限

- 用户注册、登录、退出
- 工作区创建与成员邀请
- 角色：
  - 管理员
  - 知识库编辑者
  - 普通使用者
- 知识库级访问权限
- 所有数据强制按工作区隔离
- 工作区积分账户与用量流水
- 平台统一配置模型，用户不管理模型 API Key
- 操作审计日志
- 登录、成员变更、文档增删改、知识库发布等操作留痕

### 2. 知识库管理

- 创建、编辑、归档、删除知识库
- 知识库名称、描述、标签
- 知识库状态：
  - 草稿
  - 处理中
  - 可用
  - 处理失败
  - 已归档
- 文档数量、分块数量、索引状态、最近同步时间
- 知识库默认模型和检索配置
- 知识库版本与索引版本
- 发布、回滚、重新构建索引

### 3. 数据源与文档管理

支持：

- PDF
- DOCX
- Markdown
- TXT
- HTML
- CSV/XLSX

功能包括：

- 单文件上传
- 批量上传
- 文件大小、类型、安全检查
- 文件 hash 去重
- 原始文件保存
- 文档版本管理
- 文档标题、来源、作者、标签、更新时间
- 文档预览
- 解析结果预览
- 分块结果预览
- 文档重新处理
- 文档软删除、恢复、彻底删除
- 删除后同步清理检索索引
- 支持扫描 PDF 的 OCR 解析适配器

原始文档不能被直接覆盖，每次更新都产生新的文档版本。

### 4. 文档处理流水线

完整处理流程：

```text
上传文件
  → 文件校验
  → 原文存储
  → 文档解析
  → 内容清洗
  → 结构化规范化
  → 文本切分
  → 元数据提取
  → Embedding
  → 向量索引
  → 全文索引
  → 知识库可用
```

每一步都必须具备：

- 状态
- 进度
- 日志
- 错误原因
- 自动重试
- 手动重试
- 取消任务
- 幂等处理

统一任务状态：

```text
queued → running → succeeded
                 ↘ failed
                 ↘ cancelled
```

### 5. 文本切分

默认采用结构感知切分：

- 优先按标题、段落、列表切分
- 长段落再按句子拆分
- 默认每个 chunk 约 400～700 tokens
- 重叠约 50～80 tokens
- 保留标题路径
- 保留页码、段落、字符偏移
- 保留文档版本和来源信息
- 记录 chunker 版本
- 支持相邻 chunk 关联

不能只保存纯文本，必须保存原文定位信息，否则无法实现可靠引用。

## 三、核心 RAG 系统设计

### 1. 离线知识构建链路

```text
Document
  → Parser
  → Normalizer
  → Chunker
  → Metadata Enricher
  → Embedder
  → Vector Index
  → Keyword Index
```

默认技术方案：

- PostgreSQL：业务数据
- pgvector：向量检索
- PostgreSQL FTS/BM25：关键词检索
- 对象存储：原始文件和解析文件
- Redis 或消息队列：异步任务
- 模块化单体 API + 独立 Worker

所有 Parser、Embedder、VectorStore、LLM 都设计成可替换适配器。

### 2. 在线检索链路

```text
用户问题
  → 身份鉴权
  → 知识库权限过滤
  → 查询规范化
  → 向量召回
  → 全文召回
  → RRF 融合
  → 去重与邻接扩展
  → Rerank
  → 证据阈值判断
  → 上下文组装
  → LLM 生成
  → 引用校验
  → 返回答案
```

默认策略：

- 向量召回 Top 50
- 全文召回 Top 50
- 使用 RRF 融合
- Rerank 候选 20～40 条
- 最终上下文 6～12 个 chunk
- 严格限制上下文 token 数
- 按来源多样性去重
- 低于证据阈值时拒答

### 3. 生成与回答

每次回答必须返回：

- 答案正文
- 引用列表
- 引用文档
- 文档版本
- 页码或段落
- 原文片段
- chunk ID
- trace ID
- 是否拒答
- 拒答原因

系统提示词必须明确：

> 只能依据检索到的知识回答；没有足够证据时必须明确说明，不得编造。

引用必须经过校验，不能出现模型自行生成的虚假引用。

### 4. Prompt Injection 防护

文档内容一律视为不可信数据：

- 文档中的指令不能覆盖系统规则
- 默认禁止根据文档调用外部工具
- 用户输入和文档上下文严格分隔
- 对可疑内容记录安全事件
- 支持敏感信息和 PII 检测
- 禁止跨工作区、跨知识库泄露内容

## 四、用户使用功能

### 1. 知识库问答

- 选择一个或多个知识库
- 创建、切换、删除会话
- 多轮对话
- 流式回答
- 查看引用
- 点击引用跳转到原文
- 复制答案
- 导出会话
- 点赞、点踩
- 标注问题类型：
  - 资料缺失
  - 检索错误
  - 回答错误
  - 引用错误

### 2. 全文与语义搜索

- 关键词搜索
- 语义搜索
- 混合搜索
- 标签和元数据过滤
- 按知识库过滤
- 按文档、来源、时间过滤
- 查看匹配片段和分数
- 跳转到原文位置

### 3. 检索 Playground

管理员和编辑者可以查看：

- 查询改写结果
- 权限过滤条件
- 向量召回结果
- 全文召回结果
- 融合结果
- Rerank 前后排序
- 每个 chunk 的分数
- 最终上下文
- 使用的模型和配置
- token、延迟、成本
- 最终答案和引用

普通用户只看到最终答案和引用，不暴露底层调试信息。

## 五、评估与可观测性

每次问答生成完整 Trace：

- 原始问题
- 查询处理结果
- 检索过滤条件
- 召回列表
- 各阶段分数
- 最终上下文
- Prompt 版本
- 模型版本
- 输入输出 token
- 延迟
- 成本
- 错误
- 引用覆盖情况

评估能力包括：

- 固定问题集
- 期望答案
- 期望文档
- 不可回答问题
- Recall@K
- MRR
- nDCG
- Citation Precision
- Citation Recall
- Groundedness
- Answer Correctness
- 拒答准确率
- P50/P95 延迟
- 单次请求成本

积分与用量要求：

- 问答生成前检查并预扣工作区积分
- 失败、取消和证据不足时释放预扣积分
- 成功回答保留扣费流水
- 余额和流水按工作区隔离，管理员可充值
- 扣费引用必须幂等，不能因重试或并发请求重复扣费

## 六、核心数据模型

必须包含：

- Workspace / Tenant
- User / Membership / Role
- KnowledgeBase
- Source
- Document
- DocumentVersion
- IngestionJob
- Chunk
- Embedding
- IndexVersion
- Conversation
- Message
- Citation
- QueryTrace
- Feedback
- CreditAccount / CreditLedgerEntry
- EvalDataset
- EvalRun
- ModelConfig
- AuditLog

关键约束：

- 所有业务数据带 workspace_id
- 所有文档版本不可覆盖
- 索引版本原子切换
- 删除采用 tombstone，再异步物理清理
- 老任务不能覆盖新版本
- 所有异步任务可重试且幂等
- 所有问答统一 trace_id

## 七、对外 API

至少包括：

```text
POST   /knowledge-bases
GET    /knowledge-bases
POST   /knowledge-bases/{id}/sources
POST   /documents/upload
GET    /documents/{id}/preview
GET    /documents/{id}/chunks
DELETE /documents/{id}

GET    /jobs/{id}
POST   /jobs/{id}/cancel

POST   /knowledge-bases/{id}/search
POST   /conversations
POST   /conversations/{id}/messages
GET    /traces/{id}
POST   /feedback

POST   /eval-datasets
POST   /eval-runs
GET    /usage
POST   /usage/credits
GET    /audit-logs
```

问答支持 SSE 流式输出，所有异步操作返回 `job_id`。

## 八、验收标准

这几个条件必须全部满足，平台才算可用：

1. 上传后的文档处理状态清晰可见。
2. 处理失败能定位原因并重新执行。
3. 原文、解析内容、chunk、索引之间可以追溯。
4. 每个回答都有可验证引用，或明确拒答。
5. 引用可以定位到文档页码、段落或字符位置。
6. 没有权限的文档绝不能被召回。
7. 删除文档后不能继续出现在检索结果中。
8. 文档更新不会破坏未变化的内容。
9. 每个问题都能通过 trace 复盘。
10. 固定评测集的检索和回答质量可以回归验证。
11. 任务重复执行不会产生脏数据。
12. 模型、Embedding、切分规则变化都能被记录和审计。

## 九、当前明确不纳入范围

为了保持产品边界稳定，当前不把以下能力作为核心要求：

- 自动 Agent 工作流
- GraphRAG
- 自动训练模型
- 视频、音频多模态知识库
- 支付网关、套餐、发票等复杂计费系统
- 面向用户的模型 API Key 管理
- 插件市场
- 十几种第三方连接器
- 过度复杂的企业级 IAM

