# Issue #107: 云电脑费用与操作区

## 目标与边界

按用户确认的 Figma `B5D2oFRwyBudbCi0ZnZhy8 / 13:212` 改造成果入口、
输入区控制栏、按需费用详情和预览。分支 `codex/107-cloud-computer-experience`，
起点 `origin/main@a1996846d421e87d4afec559c36a9d1013bf13aa`。

不修改数据库、公开接口、商业价格、账务、闲置/最长存续、预览鉴权或资源持久化。
本轮交付 PR，不合并、不部署，不新增真实模型或收费沙箱调用。

## 实现与验收

- 按用户、工作区和会话隔离共享状态；前台五秒轮询，恢复可见时立即核对。
- 预览可见时二十秒心跳；查询和费用展开不续时，结束和撤权优先于迟到响应。
- 成果区域提供就绪预览、真实文件入口并保留原下载能力。
- 输入框上方显示状态、费用详情和结束；详情采用服务端实际积分及冻结单价。
- 关闭预览、停止命令和结束环境分别处理；服务端确认结束后才显示最终消费。
- 单元测试覆盖竞态、隔离、失败与重复结束；本地临时容器和用户完成浏览器闭环。
- 1440/700/390px 截图逐张检查，包含键盘、焦点、错误、长标题及 reduced motion。
- 执行前端测试、类型/构建、云电脑与预览后端回归、`git diff --check`。

Figma 官方 MCP 读取触发 Starter 额度上限，使用此前确认并导出的设计稿继续实现。
模拟验收与真实云付费链路验收分别记录，后者不属于本轮。

## 2026-11-16 验收记录

### 自动化检查

- 前端 `npm test`：40 项通过，其中共享状态 15 项，覆盖重复结束、作用域隔离、
  撤权、迟到响应、终态保护、冻结单价和心跳不覆盖费用。
- `npx tsc --noEmit`、Next 生产构建通过。构建使用独立
  `NEXT_DIST_DIR=.next-cloud-experience`，没有提交构建器对常规类型配置的临时改写。
- 后端 `test_cloud_computer.py`、`test_cloud_preview.py`、
  `test_cloud_computer_mount.py`：32 项通过，1 项收费挂载验收按显式 opt-in 规则跳过。
- 隔离 PostgreSQL 16 + pgvector 数据库完成 `alembic upgrade head`，当前
  `0071_agent_tool_executions`；云电脑并发/账务与预览唯一约束测试共 3 项通过。
  本次未增加迁移。
- 本地验收 Compose 配置检查和 `git diff --check` 通过。
- GitHub Actions 受仓库账户账单/额度限制，任务未启动；不将本地通过描述为远程 CI 通过。
  验收夹具的加密键在临时容器启动时随机生成，不把固定密钥提交到仓库。

### 真实本地浏览器流程

使用临时 Docker 项目 `cloud-107-acceptance`、临时用户和独立工作区。
API 使用当前源码，数据库在容器内 `/tmp/cloud-107-acceptance.db`；
不读取线上环境文件，不连接线上数据库或 OSS，不使用真实模型凭据。

`cloud-experience.acceptance.js` 通过实际登录、对话、环境查询、授权、结束、
资源下载接口完成“打开成果 → 交互/刷新 → 费用详情 → 结束 → 查看文件”。
确认五秒轮询保留 iframe 交互状态、费用显示冻结单价和服务端四位精度、
关闭不销毁、结束提交一次、确认终态前不显示最终费用、结束后授权返回 410、
原文件结束前后仍可计量下载。没有未处理的页面 JavaScript 异常。

`cloud-experience-states.acceptance.js` 模拟未启动、启动中、启动失败、
积分不足、状态 503、结束响应丢失及权限 403；验证命令取消与独立预览分离、
独立窗口操作、隐藏/关闭不续时、恢复前台立即刷新和新对话拒绝旧响应。
故障测试中的 503/403 及未登录时的 401 属于预期 HTTP 错误。

1440、700、390px 截图共 18 张逐张审查；检查长标题省略、正文留白、无横向溢出、
手机 44px 操作区域、键盘 Enter/Escape、焦点返回和 reduced motion。
费用内容不设置持续读屏播报。截图保存在
`docs/assets/cloud-computer-experience/`，仅含合成文件和临时身份。

原有 `tool-process.acceptance.js` 更新就绪环境夹具后，在同一构建产物上回归通过：
参数反馈保持中性、过程折叠和刷新恢复稳定、所有文件下载保留、生图运行/成功/失败
独立展示，1440/700/390px 共 8 张相关回归截图也已逐张检查。

### 复现方式

从仓库根目录执行（Node/npm、Docker 和 Playwright 浏览器需可用）：

```powershell
Push-Location frontend
$env:NEXT_PUBLIC_API_URL = 'http://localhost:8127'
$env:NEXT_DIST_DIR = '.next-cloud-experience'
npm run build
Pop-Location
docker build -f frontend/tests/browser/cloud107.Dockerfile -t local/zhiku-cloud-107-web:test .
docker compose -f backend/tests/browser/compose.cloud107.yml up -d
npx --yes --package @playwright/cli playwright-cli -s=cloud107-review open http://localhost:3127
npx --yes --package @playwright/cli playwright-cli -s=cloud107-review run-code --filename frontend/tests/browser/cloud-experience.acceptance.js
npx --yes --package @playwright/cli playwright-cli -s=cloud107-review run-code --filename frontend/tests/browser/cloud-experience-states.acceptance.js
```

主流程使用固定的合成登录身份，异常流程接着使用同一浏览器登录态。主流程结束环境后，
重跑需先 `docker compose -f backend/tests/browser/compose.cloud107.yml up -d --force-recreate api`
重建临时容器数据库，并等待 API 启动。异常脚本会控制浏览器时间；之后重跑主流程应换一个
新的 Playwright 会话，避免测试时钟残留影响动画。关闭验收环境使用同一 Compose 文件的
`down`，不要操作其他 Docker 项目。

### 验收边界

浏览器运行真实构建产物，账户、资源、费用预留/结算及状态 API 是本地真实实现；
云供应商适配器、对象存储和预览页面传输为模拟。未执行真实沙箱创建、OSS 挂载、
公网预览网关或模型调用，因此不将本次测试称为真实付费云链路验收。
未修改线上配置、用户目录、数据库或服务；PR 等待人工审查，不自动合并或部署。

### 截图索引

| 视口 | 对话与控制栏 | 费用详情 | 交互预览 |
| --- | --- | --- | --- |
| 1440 | [对话](../assets/cloud-computer-experience/cloud107-1440-chat.png) | [浮层](../assets/cloud-computer-experience/cloud107-1440-fees.png) | [侧栏预览](../assets/cloud-computer-experience/cloud107-1440-preview.png) |
| 700 | [对话](../assets/cloud-computer-experience/cloud107-700-chat.png) | [Dialog](../assets/cloud-computer-experience/cloud107-700-fees.png) | [全屏预览](../assets/cloud-computer-experience/cloud107-700-preview.png) |
| 390 | [对话](../assets/cloud-computer-experience/cloud107-390-chat.png) | [Dialog](../assets/cloud-computer-experience/cloud107-390-fees.png) | [全屏预览](../assets/cloud-computer-experience/cloud107-390-preview.png) |

另含启动中、启动失败、余额不足、状态更新失败、结束中、已结束、撤权、
文件保留和独立窗口截图。
