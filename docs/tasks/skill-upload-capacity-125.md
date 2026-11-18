# Skill 包容量统一（Issue #125）

## 目标与范围

用户于 2026-11-18 确认上传包、单文件、解压后总量统一为 200 MB。沿用项目既有二进制计算口径，实施为 200 MiB（209,715,200 字节），提示明确使用 MiB。

- 管理端导入预览、检查、创建和更新，以及工作区 ZIP、SKILL.md、文件夹导入共用容量策略。
- 不限制 Skill 文件数量或 ZIP/TAR 条目数量；保留 200 MiB 单文件/总量/上传包容量上限，以及路径、链接、特殊文件、加密条目等完整性校验。
- SKILL.md 的管理端正文长度和工作区发现大小约束保持现状。
- 不涉及 #124、部署、合并、数据迁移或存储额度调整。

## 实现

- `skill_files.MAX_SIZE_MIB` 统一派生三个后端字节上限，管理端上传不再硬编码旧阈值。
- 管理端 Base64 字段容量使用 `4 × ceil(单文件字节数 / 3)`，解码后的文件和总量仍按原始字节校验，包含 SKILL.md。
- 两个前端入口共用 `skill-upload.ts`，在传输前拒绝超限包、单文件及文件夹总量，保留已导入内容。
- 主站、主站脚本副本及测试站 Nginx 模板预留 268 MiB 请求体，容纳 200 MiB 文件的 Base64/JSON 和 multipart 开销；应用容量上限仍为 200 MiB。
- 原来使用旧 5 MiB 阈值的超限测试改为缩小测试边界，另用真实 200 MiB 数据验证产品上限。恶意 ZIP 夹具保留原始反斜杠，避免 Windows 的 ZipInfo 自动规范化后失去测试意义。

## 验证记录

在本 Issue 独立工作树执行，未访问线上用户数据或外部模型。

- 前端 `npm test`：52 项通过，包含上传边界、文件夹总量/数量及代理编码开销回归。
- 前端 `npx tsc --noEmit` 通过。`npm run build` 的编译、类型检查和页面生成完成，但最终 Next.js 16.4.2 安全检查报 `SecurityFatalError: Your version of Next.js is affected by a known security vulnerability`，进程退出码为 1，不能算构建通过。此次未更改依赖和锁文件，也未绕过安全检查或扩大范围升级依赖。
- 后端新增容量用例 14 项：真实 200 MiB 文件集合和 ZIP、上限加 1 字节、Base64 padding/清单计量、ZIP/TAR 解压约束、两个 HTTP 上传入口、文件夹失败前置校验。
- 26 MiB 未压缩 ZIP 及文件夹经工作区真实路由导入、下载并校验 SHA-256；管理端经导入、检查、创建、更新、下载和安装路由验证资源字节不变。
- Linux 原基线镜像挂载本次后端代码执行扩展回归：81 项通过，1 项失败；后续增加的真实 200 MiB ZIP 用例另验通过，合计 82 项通过。范围为 `test_skill_upload_limits.py`、`test_site_skill_packages.py`、`test_skill_resources.py`、`test_workspace_mutations.py`、`test_issue_100_skill_library.py`、`test_personal_extensions.py`、`test_skill_context_integration.py`。
- 唯一失败为 `test_each_message_and_compact_recovery_uses_current_skill_metadata`：会话返回 failed，模型夹具未被调用。在未挂载本次代码的原始 `ed037b3` 镜像中单独复现相同失败，未纳入 #125 修复范围。未将整组回归描述为全绿。
- 三份 Nginx 模板使用临时自签证书在隔离容器中逐份执行 `nginx -t`，全部通过；编码后请求容量另有前端静态回归。
- Playwright 使用本地已生成的页面产物和真实 API 路由、SQLite 及隔离内存存储夹具验证：26 MiB 包管理端导入并保存启用；工作区 ZIP/文件夹导入；超限包和文件夹在网络传输前拒绝；损坏包报错后重试成功。页面可运行不代表上述构建安全检查已通过。
- 1440、700、390px 截图及横向溢出检查通过。截图位于忽略目录 `output/playwright/issue125-*`，验收夹具位于 `output/issue125/`，不提交账号会话、临时数据库、压缩包和私钥。
- 配置校验通过（86 项环境变量），`docker compose ... config --quiet` 通过。部署脚本测试共 88 项，其中 85 项通过、1 项跳过、2 项失败：环境变量目录缺少 `PREVIEW_CACHE_IDLE_SECONDS` / `PREVIEW_CACHE_MAX_BYTES`，以及测试仍引用已不存在的 `preview-worker`。相关测试、目录和 Compose 文件未被本次修改，未纳入 #125 修复。
- PR #127 远端 CI 在步骤启动前因 GitHub 账户账单/消费额度限制失败，未实际执行；镜像作业跳过。GitGuardian 检查通过。不能把远端检查描述为通过。

## 验证边界

没有部署或修改线上生效的 Nginx 配置，没有使用真实云存储验证，也没有进行多并发大包压力测试。当前保留有界内存处理及管理端 Base64 传输方式，单请求内存占用会随包大小增加。线上启用时需同步应用 API、Web 和对应 Nginx 模板，不能只改页面提示。

Issue 保持打开，供后续代码审查与交付核对。
