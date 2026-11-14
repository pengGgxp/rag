# Issue #100 本地验收运行说明

仅用于可丢弃的 Docker 项目；不用生产数据库、线上对象目录或付费环境。凭据、session state、临时配置与截图全部放在忽略的 `output/`，不能提交或粘贴到日志。

## 后端与数据库

隔离 API 容器挂载本分支 backend 到 `/app`，使用测试数据库、本地 Redis 和对象存储。运行任务记录中列出的 14 个 pytest 模块。内存存储夹具只证明应用的冲突、补偿和权限逻辑；OSS SDK 请求参数通过其单元测试核对，未请求真实 OSS。

在隔离网络内执行：

```text
python tests/run_workspace_acceptance.py
```

脚本只接受 host 为 `db` 的连接，用随机 `rag_auth_test_issue100_...` 空库执行完整 Alembic 升级两次及 PostgreSQL 并发验收，最后删除自己创建的库。它不删除浏览器临时库。

## 浏览器预览准备

1. 在同一隔离网络创建新的 `rag_auth_test_issue100_browser_<随机值>` 数据库，执行 `alembic upgrade head`。准备本项目测试配置，不复用线上凭据或业务存储。
2. 显式设置 `ISSUE100_LOCAL_PREVIEW=1`。选择 filesystem 时设置 `ISSUE100_LOCAL_STORE=filesystem`，为容器的 `/test-objects` 绑定本任务独立 named volume。选择本地 S3 时只允许 `http://minio:9000` 及专用 `issue100-workspace-acceptance` bucket。
3. 在该数据库中仅运行一次 `python tests/seed_workspace_preview.py`，建立脚本中明确标注的临时 owner/viewer、共享工作区、报告助手源包、项目资料与 505 项分页夹具。密码是公开测试夹具，不能用于真实账号。
4. 使用 `python tests/workspace_local_preview.py` 启动本地 8000 API；测试启动器校验数据库名称和 Docker 主机，注入显式测试存储，并禁止 cloud submit/execute。生产代码没有此适配器或自动回退。后端变动后重启该测试进程。
5. 前端以 `NEXT_PUBLIC_API_URL=http://localhost:8000` 在 3000 启动。Playwright CLI 打开本地页面并通过真实登录 UI 登录上述临时 owner。

本次验收采用 filesystem：本地 S3 单独写入探针也出现约一分钟延迟，避免将存储测试环境问题当作应用性能结论。最终浏览器对象位于本任务独立 Linux named volume，未使用 Windows 业务目录。

## 四组真实浏览器脚本

从仓库根目录，对已登录本地临时账号的 session 执行：

```text
npx --yes --package @playwright/cli playwright-cli -s=<session> run-code --filename frontend/tests/browser/workspace-files.acceptance.js
npx --yes --package @playwright/cli playwright-cli -s=<session> run-code --filename frontend/tests/browser/skill-library.acceptance.js
npx --yes --package @playwright/cli playwright-cli -s=<session> run-code --filename frontend/tests/browser/workspace-conflicts.acceptance.js
npx --yes --package @playwright/cli playwright-cli -s=<session> run-code --filename frontend/tests/browser/workspace-touch-viewer.acceptance.js
```

脚本拒绝非 localhost 页面和其他账号。普通文件写入唯一命名的临时目录；Skill 脚本重复运行会清理它自己已知的报告助手测试副本，禁止改为普通用户账号执行。触控脚本创建真实 touch context，另开 viewer context 验证只读。输入上传使用真实 file input；模型与云电脑不参与这些浏览器验收。

如本机下载管理器拦截 ZIP，使用 Playwright 独立 Chromium，保持应用 CORS 配置不变；本次下载已经以独立 Chromium 的真实按钮和 download 事件验证。不能只把 API 返回 200 当作按钮下载成功。

脚本通过后逐张查看 `output/playwright/issue100-*.png`：三种宽度的卡片/列表、触控菜单/编辑器、viewer、Skill 路径及冲突恢复。页面总宽度不得溢出，长名称仍可通过真实路径定位，焦点/键盘/错误恢复需通过交互断言。具体结果见 `issue-100-skill-decoupling-implementation.md` 与根 `design-qa.md`。

## 明确未验证的边界

真实模型选择 Skill 的效果、付费云电脑执行、真实 OSS 性能、挂载与网页恰好同时写入的结果均未验收。用户选择 A 后保留外部直写的并发窗口；本地测试不得被描述为跨入口原子替换/删除保证。
