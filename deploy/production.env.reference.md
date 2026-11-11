# 生产环境变量总表

本文件是运维查阅入口。可复制模板分别是 `app.env.example` 和
`deployment.env.example`，值受代码校验约束。服务器运行配置由服务器持有，
不依赖 GitHub Environment。

## 配置放在哪里

| 配置层 | 位置 | 谁维护 | 用途 |
| --- | --- | --- | --- |
| 后端运行变量 | `/opt/zhiku/config/app.env` | 服务器运维 | API、Worker、预览 Worker 和预览网关读取 |
| 题库桥接密钥 | `/opt/zhiku/config/.env.question-workbench` | 服务器运维 | 题库工作台与知库服务之间认证；与 `app.env` 分开 |
| Compose 发布变量 | 每个 release 的 `.env.release` | 发布器自动生成 | 镜像 digest、PostgreSQL/RustFS 启动参数、版本与迁移参数 |
| Web 构建参数 | 构建 Web 镜像时的 `NEXT_PUBLIC_API_URL` | 本地构建命令或 CI | 编译进前端 bundle 的 API 地址；不是后端功能开关 |
| 产品后台配置 | PostgreSQL 中的版本化站点配置 | 站点管理员 | 模型、存储、云电脑、搜索和商业价格等动态配置 |

`app.env`、`.env.question-workbench` 和 `.env.release` 均为受保护文件，
权限应为 `600`。真实值不应提交 Git、发在聊天或写入发布记录。
`deployment.env.example` 只供 Compose 静态解析和配置契约验证，不能拿来
替代线上 `.env.release`，也不能把它复制成第二份人工维护的密钥文件。

## 后端服务器变量

状态说明：

- **必需**：部署校验要求显式填写；密钥类不得留空。
- **按功能启用**：相关功能启用时才需要配置；留空表示该功能未配置。
- **可选**：代码有默认值，省略不应导致发布失败。
- **历史兼容**：由 `Settings` 读取，但当前正式入口可能已改为后台数据库配置；
  不要仅因模板中存在就误认为线上功能依赖它。
- **派生**：发布器按 release 信息写入，不应由运维手动设置。

| 变量 | 状态 | 对应功能 / 用途 |
| --- | --- | --- |
| `DATABASE_URL` | 必需 | PostgreSQL 连接；包含用户名、密码、主机、端口和库名。Compose 中 PostgreSQL 的密码也由此解析生成。 |
| `REDIS_URL` | 必需 | Celery 任务队列和共享短期状态。 |
| `AUTH_SECRET_KEY` | 必需 | 登录会话及认证签名；生产需使用随机强密钥，至少 32 字符。 |
| `SITE_CONFIG_ENCRYPTION_KEY` | 必需 | 加密后台保存的存储、支付、邮件等站点凭据；必须单独备份，丢失会导致已存凭据无法解密。 |
| `MCP_CREDENTIAL_KEY` | 必需 | 加密 MCP 认证信息的独立密钥；与站点配置密钥分开保管。 |
| `FRONTEND_URL` | 必需 | 正式 Web 来源，用于回调、链接与来源校验。 |
| `API_PUBLIC_URL` | 必需 | 正式 API 地址，用于对外链接及来源校验。 |
| `AUTH_COOKIE_SECURE` | 必需 | HTTPS 站点设为 `true`，登录 Cookie 仅通过安全连接发送。 |
| `PREVIEW_ENABLED` | 必需 | 明确决定是否开启云电脑 HTTP 预览，填写 `true` 或 `false`。 |
| `PREVIEW_DOMAIN` | 必需 | 预览子域名后缀，例如 `preview.str1ct.top`。 |
| `NEXT_PUBLIC_API_URL` | 构建校验 | Web 镜像构建输入，必须指向生产 API；部署校验也检查它与发布包是否一致。它不会让后端自动切换 API 地址。 |
| `DEPLOY_CONTROL_TOKEN` | 发布器生成 | API loopback 部署维护接口使用。发布器会在服务器生成并跨 release 保留，不要手动发给浏览器或写入 GitHub。 |
| `APP_VERSION` | 发布器生成 | 当前发布版本，来自 `release.json`。 |
| `GIT_COMMIT` | 发布器生成 | 当前镜像对应的提交 SHA，来自 `release.json`。 |
| `CONFIG_CONTRACT` | 发布器生成 | 发布配置契约版本。 |
| `DEPLOYMENT_ROLLBACK` | 发布器生成 | 数据库迁移失败时的回滚策略，来自发布清单。 |
| `AUTH_SESSION_HOURS` | 可选 | 登录会话时长，默认 168 小时。 |
| `EMBEDDING_BASE_URL` | 按功能启用 | Embedding 服务接口地址；填入后还需要 API Key 和模型名，三项组成有效配置。 |
| `EMBEDDING_API_KEY` | 按功能启用 | Embedding 服务凭据。 |
| `EMBEDDING_MODEL` | 按功能启用 | Embedding 模型 ID；维度须与数据库向量列匹配。 |
| `EMBEDDING_DIMENSION` | 可选 | Embedding 向量维度，默认 1024；只有更换模型维度并完成索引兼容时才调整。 |
| `EMBEDDING_TIMEOUT_SECONDS` | 可选 | Embedding 请求超时秒数，默认 30。 |
| `LLM_BASE_URL` | 历史兼容 | 单一 LLM 备用连接；标准模型连接现由站点后台管理，不是多供应商配置入口。 |
| `LLM_API_KEY` | 历史兼容 | 上述备用 LLM 的凭据。 |
| `LLM_MODEL` | 历史兼容 | 上述备用 LLM 的模型 ID。 |
| `LLM_TIMEOUT_SECONDS` | 可选 | 备用 LLM 请求超时，默认 60 秒。 |
| `PDF_PARSER` | 可选 | PDF 解析方式：`local` 使用本地解析，`aliyun` 使用 DocMind；默认 `local`。 |
| `ALIYUN_ACCESS_KEY_ID` | 按功能启用 | 仅当 `PDF_PARSER=aliyun` 时使用的阿里云解析凭据。 |
| `ALIYUN_ACCESS_KEY_SECRET` | 按功能启用 | 仅当 `PDF_PARSER=aliyun` 时使用的阿里云解析凭据。 |
| `DOCMIND_TIMEOUT_SECONDS` | 可选 | DocMind 最长等待时间，默认 300 秒。 |
| `DOCMIND_POLL_SECONDS` | 可选 | DocMind 任务状态轮询间隔，默认 3 秒。 |
| `MCP_ALLOWED_PRIVATE_HOSTS` | 安全策略 | 允许 MCP 访问的私网精确主机名列表，逗号分隔；默认留空，不扩大私网访问面。 |
| `MCP_TIMEOUT_SECONDS` | 可选 | MCP 请求超时，默认 60 秒。 |
| `OIDC_NAME` | 可选 | OIDC 登录入口展示名称，默认“企业账号”。 |
| `OIDC_METADATA_URL` | 按功能启用 | OIDC discovery 地址；还需 Client ID、Secret 和认证密钥。 |
| `OIDC_CLIENT_ID` | 按功能启用 | OIDC 客户端 ID。 |
| `OIDC_CLIENT_SECRET` | 按功能启用 | OIDC 客户端密钥。 |
| `SMTP_HOST` | 按功能启用 | SMTP 服务器地址；后台邮件设置保存后，以后台版本化配置为准。 |
| `SMTP_PORT` | 可选 | SMTP 端口，默认 587。 |
| `SMTP_USERNAME` | 按功能启用 | SMTP 用户名。 |
| `SMTP_PASSWORD` | 按功能启用 | SMTP 密码或应用专用密码。 |
| `SMTP_FROM` | 按功能启用 | SMTP 发件地址。 |
| `SMTP_STARTTLS` | 可选 | 是否使用 STARTTLS，默认 `true`。 |
| `ZHIFUX_ENABLED` | 历史兼容 | 知付宝旧版配置迁移草稿；保存后台支付设置后数据库配置生效。 |
| `ZHIFUX_API_BASE_URL` | 历史兼容 | 知付宝旧版 API 地址。 |
| `ZHIFUX_MERCHANT_NUM` | 历史兼容 | 知付宝商户号。 |
| `ZHIFUX_SECRET` | 历史兼容 | 知付宝旧版商户密钥。 |
| `ZHIFUX_PAY_TYPE` | 历史兼容 | 知付宝旧版支付方式代码。 |
| `ZHIFUX_PAYMENT_METHODS_JSON` | 历史兼容 | 知付宝支付方式草稿 JSON。 |
| `ZHIFUX_CUSTOM_RECHARGE_ENABLED` | 历史兼容 | 知付宝旧版自定义充值开关。 |
| `ZHIFUX_CUSTOM_CREDITS_PER_YUAN` | 历史兼容 | 知付宝旧版每元积分数，默认 100。 |
| `RECHARGE_PACKAGES_JSON` | 历史兼容 | 旧版充值套餐 JSON 草稿；后台保存后数据库配置生效。 |
| `EZFP_ENABLED` | 历史兼容 | 易支付旧版配置迁移草稿。 |
| `EZFP_API_BASE_URL` | 历史兼容 | 易支付 API 地址，默认 `https://www.ezfp.cn/api/pay/create`。 |
| `EZFP_PID` | 历史兼容 | 易支付商户 ID。 |
| `EZFP_PAY_TYPE` | 历史兼容 | 易支付旧版方式代码，默认 `wxpay`。 |
| `EZFP_PAYMENT_METHODS_JSON` | 历史兼容 | 易支付方式草稿 JSON。 |
| `EZFP_PRIVATE_KEY` | 历史兼容 | 易支付签名私钥，仅旧版启动配置使用。 |
| `EZFP_PUBLIC_KEY` | 历史兼容 | 易支付验签公钥，仅旧版启动配置使用。 |
| `EZFP_RECHARGE_PACKAGES_JSON` | 历史兼容 | 易支付旧版套餐 JSON。 |
| `EZFP_CUSTOM_RECHARGE_ENABLED` | 历史兼容 | 易支付旧版自定义充值开关。 |
| `EZFP_CUSTOM_CREDITS_PER_YUAN` | 历史兼容 | 易支付旧版每元积分数，默认 100。 |
| `USAGE_AUTHORIZATION_HOLD_CREDITS` | 可选 | 模型计量请求最低预留积分，默认 1；不是图片价格或沙箱单价。 |
| `RESOURCE_CACHE_DIR` | 可选 | API/Worker 读取资源时使用的临时缓存目录，默认 `/tmp/rag-resource-cache`；不是云电脑工作目录。 |
| `RESOURCE_CACHE_MAX_BYTES` | 可选 | 资源缓存容量上限，默认 5 GiB。 |
| `RESOURCE_CACHE_IDLE_SECONDS` | 可选 | 资源缓存闲置清理时间，默认 24 小时。 |
| `PREVIEW_CACHE_MAX_BYTES` | 可选 | 资源预览 Worker 缓存上限，默认 5 GiB；发布器派生为 Compose 参数。 |
| `PREVIEW_CACHE_IDLE_SECONDS` | 可选 | 资源预览缓存闲置清理时间，默认 7 天；发布器派生为 Compose 参数。 |
| `WORKSPACE_EXECUTOR_URL` | 历史兼容 | 旧执行器 URL，默认不配置；不是阿里云沙箱连接配置。 |
| `WORKSPACE_RUNNER_IMAGE` | 历史兼容 | 本地 Docker 执行器镜像默认值；当前云电脑模板由后台配置。 |
| `WORKSPACE_FILE_RETENTION_DAYS` | 历史兼容 | 旧执行器文件保留期，默认 3 天。 |
| `WORKSPACE_FILE_QUOTA_MB` | 历史兼容 | 旧执行器文件配额，默认 500 MB。 |
| `QUESTION_WORKBENCH_URL` | 可选 | 题库工作台公网地址，默认 `https://q.str1ct.top`。 |
| `QUESTION_WORKBENCH_API_URL` | 可选 | 题库服务内网 API 地址，默认 `http://question-workbench-api:8100`。 |
| `QUESTION_WORKBENCH_SECRET` | 独立文件 | 题库桥接 Bearer 密钥，放在 `.env.question-workbench`，不得复制到 `app.env`。 |
| `S3_ENDPOINT` | 历史兼容 | 本地 RustFS/S3 兼容连接地址，默认 `http://localhost:9000`；正式存储连接由后台配置。 |
| `S3_ACCESS_KEY` | 历史兼容 | 本地 RustFS/S3 兼容访问密钥。 |
| `S3_SECRET_KEY` | 历史兼容 | 本地 RustFS/S3 兼容密钥。 |
| `S3_BUCKET` | 历史兼容 | 本地对象存储桶名，默认 `rag-documents`。 |
| `S3_REGION` | 历史兼容 | 本地对象存储区域，默认 `us-east-1`。 |

模板包含全部后端 `Settings` 字段，是可审计目录；**不是所有键都必须新增到
线上文件**。运行期采用代码默认值的可选键可以省略，已保存到数据库的后台
配置不应再复制进环境文件。

## Compose 发布器变量

这些变量由发布器在每个 release 中生成，运维不需要把它们填进 `app.env`。
`deployment.env.example` 为 Compose 校验提供占位值：

| 变量 | 生成来源 | 对应功能 |
| --- | --- | --- |
| `API_IMAGE` | Release manifest | API digest 固定镜像。 |
| `WORKER_IMAGE` | Release manifest | Celery Worker 固定镜像。 |
| `WEB_IMAGE` | Release manifest | Web 固定镜像。 |
| `APP_VERSION` | Release manifest | 运行版本标识。 |
| `GIT_COMMIT` | Release manifest | 代码提交 SHA。 |
| `CONFIG_CONTRACT` | Release manifest | 配置契约版本。 |
| `DEPLOYMENT_ROLLBACK` | Release manifest | 数据库迁移失败后的应用回滚规则。 |
| `MIGRATION_DATABASE_URL` | 发布时临时生成 | 隔离演练迁移的临时数据库地址。 |
| `POSTGRES_PASSWORD` | 从 `DATABASE_URL` 解析 | Compose PostgreSQL 容器初始化密码。 |
| `RUSTFS_ACCESS_KEY` | 从 `S3_ACCESS_KEY` 解析 | Compose 内置 RustFS 启动密钥。 |
| `RUSTFS_SECRET_KEY` | 从 `S3_SECRET_KEY` 解析 | Compose 内置 RustFS 启动密钥。 |
| `ZHIKU_APP_ENV_FILE` | 发布器固定值 | Compose 所加载的应用 env 文件名。 |

Release 入口还要求 `NEXT_PUBLIC_API_URL` 已在服务器 `app.env` 中配置，并与
Web 镜像构建时的值匹配。该变量在这里用于发布一致性校验，前端实际连接地址
已在构建时编入镜像。

## 不属于环境变量的后台配置

这些配置保存在 PostgreSQL 的站点管理配置中，不应在本表填第二份：

- 模型供应商、模型协议、对话和图片模型、图片规格与积分价格。
- 主对象存储供应商、COS/OSS 凭据、Bucket、地域及测试状态。
- 云电脑启停、沙箱凭据、地域、模板、角色 ARN、单价和生命周期。
- 网页搜索供应商与价格、存储/下载额度及超额处理规则。
- 管理员保存后的 SMTP、站点支付渠道、套餐和商业价格。

## 线上核对与审计

2026-11-11 通过服务器只读查询确认：基础 `app.env` 有 35 个键，权限为
`600`；题库桥接密钥在独立 `600` 文件中。部分模板键未显式出现在基础文件
里，但其中包括有代码默认值的可选配置和发布派生配置；不能据此断言对应功能
缺失。没有修改线上配置。

新 release 部署后可以只输出变量名与缺失项，不输出值：

```bash
sudo /usr/bin/python3 /opt/zhiku/current/validate_config.py \
  --audit-app-env /opt/zhiku/config/app.env \
  --audit-deployment-env /opt/zhiku/current/.env.release
```
