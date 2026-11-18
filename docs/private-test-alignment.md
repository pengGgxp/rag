# 历史 fsl1 配置对照与验收

> 2026-11-18 更新：本文是此前 fsl1 环境的配置与验收证据。
> 用户已确认当前测试服务器为 `103.97.177.61`，沿用 `https://zk.str1ct.top`。
> 当前生产入口为 `https://syfc.moonlitstarway.top/`；测试入口为
> `https://zk.str1ct.top/`。
> 本文中的旧测试域名、目录及验收结果不代表当前服务器状态；
> 当前部署目标见 [测试发布文档](../deploy/testing/README.md)。

## 目标与边界

关联 PR #119、Issue #118。目标是在 fsl1 将独立测试站的功能、运行参数及外部
服务配置对齐生产，让用户通过约定域名验收真实流程。
范围包括配置盘点、测试端备份、导入重加密、域名来源校验、资源和浏览器验证。
不修改生产容器或业务数据，不创建真实支付订单、不发通知、不追加收费模型或沙箱调用。
题库按用户 2026-11-17 的明确要求不启用。

验收标准为配置差异可核对、独立数据边界保留、登录至成果的实际流程可用。
验证分为逐键比较、凭据解密检查、源站 API 行为、OSS 真实读写和 Playwright
公网页面检查；尚未达到完整交付标准。

## 配置基准

2026-11-17 只读盘点生产 `/opt/zhiku/current`，指向 `v0.1.27`，
API 容器为 `rag-platform-api-1`。测试运行应用提交
`233a4524c8ce30144c5e7201d0b198df842ff884`。
比较实际 `Settings` 的全部 79 个键，而非只比较 env 文件中显式填写的键。
下文只记录键名、类别和状态，不记录配置值或凭据。

| 类别 | 对照结果 |
| --- | --- |
| 站点 `SiteSettings` | 1 条，字段一致 |
| 搜索 `SiteSearchSettings` | 1 条，开关、参数、价格及解密后的凭据一致 |
| 认证 `SiteAuthSettings` | 1 条，SMTP、微信及验证政策一致，凭据已重加密 |
| 支付 `SitePaymentConfig` | 5 个版本，业务字段及解密后的凭据一致，创建者映射到测试管理员 |
| 充值 `SiteRechargeConfig` | 1 条，报价及付款方式一致，创建者映射到测试管理员 |
| 云电脑 `CloudComputerSettings` | 1 条，配置一致，闲置窗口 600 秒 |
| 图片 `ImageModelSettings` | 1 条，字段一致 |
| 模型计费 `ModelBillingSettings` | 1 条，默认模型及字段一致 |
| 存储 `SiteStorageSettings` | 2 条，业务字段及解密后的凭据一致，OSS 为活动存储 |
| 模型 `SiteModelProvider` | 12 条，字段一致 |
| 连接 `ModelConnection` | 8 条，业务字段及解密后的凭据一致，重加密导入导致 `updated_at` 不同 |

环境配置 65 个键相同（主站启用 HTTPS 后，`AUTH_COOKIE_SECURE` 已对齐生产）：

```text
REDIS_URL
S3_ACCESS_KEY S3_SECRET_KEY S3_BUCKET S3_REGION
EMBEDDING_BASE_URL EMBEDDING_API_KEY EMBEDDING_MODEL EMBEDDING_DIMENSION
EMBEDDING_TIMEOUT_SECONDS LLM_BASE_URL LLM_API_KEY LLM_MODEL LLM_TIMEOUT_SECONDS
MCP_ALLOWED_PRIVATE_HOSTS MCP_TIMEOUT_SECONDS
PDF_PARSER ALIYUN_ACCESS_KEY_ID ALIYUN_ACCESS_KEY_SECRET
DOCMIND_TIMEOUT_SECONDS DOCMIND_POLL_SECONDS
PREVIEW_ENABLED AUTH_SESSION_HOURS AUTH_COOKIE_SECURE
OIDC_NAME OIDC_METADATA_URL OIDC_CLIENT_ID OIDC_CLIENT_SECRET
SMTP_HOST SMTP_PORT SMTP_USERNAME SMTP_PASSWORD SMTP_FROM SMTP_STARTTLS
ZHIFUX_ENABLED ZHIFUX_API_BASE_URL ZHIFUX_MERCHANT_NUM ZHIFUX_SECRET
ZHIFUX_PAY_TYPE ZHIFUX_PAYMENT_METHODS_JSON RECHARGE_PACKAGES_JSON
ZHIFUX_CUSTOM_RECHARGE_ENABLED ZHIFUX_CUSTOM_CREDITS_PER_YUAN
EZFP_ENABLED EZFP_API_BASE_URL EZFP_PID EZFP_PAY_TYPE EZFP_PAYMENT_METHODS_JSON
EZFP_PRIVATE_KEY EZFP_PUBLIC_KEY EZFP_RECHARGE_PACKAGES_JSON
EZFP_CUSTOM_RECHARGE_ENABLED EZFP_CUSTOM_CREDITS_PER_YUAN
USAGE_AUTHORIZATION_HOLD_CREDITS WORKSPACE_EXECUTOR_URL
RESOURCE_CACHE_DIR RESOURCE_CACHE_MAX_BYTES RESOURCE_CACHE_IDLE_SECONDS
WORKSPACE_RUNNER_IMAGE WORKSPACE_FILE_RETENTION_DAYS WORKSPACE_FILE_QUOTA_MB
CONFIG_CONTRACT DEPLOYMENT_ROLLBACK
```

环境配置 14 个差异键：

| 键 | 差异原因及实际影响 |
| --- | --- |
| `DATABASE_URL` | 独立测试数据库，不复制生产连接 |
| `S3_ENDPOINT` | 未连接生产内网旧 S3；保留本地默认值。当前活动存储为数据库配置的 OSS，不走旧 S3 |
| `MCP_CREDENTIAL_KEY` | 独立凭据加密密钥，8 个连接已重加密且可解密 |
| `QUESTION_WORKBENCH_URL`、`QUESTION_WORKBENCH_API_URL`、`QUESTION_WORKBENCH_SECRET` | 用户要求不启用题库，三项均为空；测试用户题库权限关闭 |
| `FRONTEND_URL`、`API_PUBLIC_URL` | 使用测试主站 `https://test.zk.moonlitstarway.top` 和同源 `/api`，HTTP 80 跳转到 HTTPS；支付 HTTPS 条件已满足 |
| `PREVIEW_DOMAIN` | 使用 `test-zk-preview.moonlitstarway.top`；已接通独立证书和 HTTPS 网关 |
| `AUTH_SECRET_KEY`、`SITE_CONFIG_ENCRYPTION_KEY`、`DEPLOY_CONTROL_TOKEN` | 测试独立密钥；数据库配置已重加密，生产密钥不复用 |
| `APP_VERSION`、`GIT_COMMIT` | 标识 PR 测试构建，与生产版本不同 |

Redis 连接字符串相同，但所在 Compose 网络、服务实例和卷独立。
缓存路径和参数一致，但卷独立。支付和充值配置的创建者映射只作用于测试数据库，
没有导入对应生产用户。测试 OSS 文件仅使用新工作区前缀，不复制生产资源目录。

## 已执行验证

| 验证 | 实际结果 |
| --- | --- |
| 备份 | 测试 env、Nginx、完整测试数据库已备份；目录 700、配置 600 |
| 进程与健康 | 七个服务运行；health/ready、两个 Worker 心跳正常；Nginx 校验通过 |
| 域名来源校验 | 源站使用测试域名 Origin 登录 200；错误 Origin 登录 403 |
| 模型选择 | 可用模型接口 200；4 个对话模型可选；未发送收费推理请求 |
| 会话与积分 | 接口 200；测试积分 1000，支出和待结算均为 0 |
| 资源闭环 | 实际上传 201、同请求重试命中同一文件、下载 200 且内容一致、目录可见 |
| 资源权限与删除 | 匿名下载 401；删除 204；删除后下载 404；本轮测试文件已删除 |
| 配置凭据 | 搜索、SMTP、微信、云电脑、存储、模型连接和支付凭据在测试端可解密 |
| 预览鉴权 | 网关对未授权访问及无效票据返回 401 |
| 题库 | 状态接口 200，入口和工具均未启用，符合用户最新要求 |
| 新域名公网登录 | `http://test.zk.moonlitstarway.top` 真实浏览器密码登录 200，知识库首页正常，刷新后会话和知识库接口 200；页面和静态资源 200，错误 Origin 登录 403，公开 internal 路径 404 |
| 预览 HTTPS | 独立 Let's Encrypt 泛域名证书已签发并安装，有效期至 2027-02-15；外部 curl 及 Playwright 均通过证书校验，缺少授权和无效票据返回 401 |
| 主站 HTTPS | 独立证书有效期至 2027-02-15，HTTP 308 保留路径及查询参数；HTTPS 重新登录与刷新通过，Cookie Secure/HttpOnly=true；主站模拟自动续期和重载钩子通过 |
| HTTPS 充值目录 | 200、enabled=true、3 个套餐；未创建订单 |

## 未完成与阻塞

| 项目 | 证据与剩余工作 |
| --- | --- |
| 真实成果预览 | `*.test-zk-preview.moonlitstarway.top` 的证书与网关已接通；未创建收费沙箱，真实任务的票据兑换、Cookie、刷新与结束运行仍待验证。证书续期仍需人工添加新的 DNS TXT |
| 支付 | HTTPS 配置已满足现有校验，测试充值目录已启用；未创建真实订单，支付跳转及回调尚未验收 |
| 邮件与微信 | 配置及解密通过，未发信、未触发外部授权回调；测试域名回调还需在公网入口恢复后验证 |
| 搜索、解析、图片、真实问答与云电脑 | 配置对照已完成，未触发可能收费的真实任务；不能由健康检查推断这些任务成功 |
| 知识库 RAG 闭环 | 本轮验证资源读写，不等同于文档解析、索引、检索、引用及无证据拒答验收；未新建收费 Embedding/解析任务 |

Playwright 截图保存在忽略的 `output/playwright/fsl1-domain-blocked.png`。
不使用隧道、IP 登录、浏览器请求拦截替换或模拟上游冒充公网验收。
不关闭 Issue，不合并 PR，不部署生产。
