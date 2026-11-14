# 对话执行过程呈现

## 目标与边界

连续工具操作与其间的思考合并为默认收起的过程入口，保留正文前后顺序和全部执行详情。
模型参数校验反馈与实际任务失败分开表达；权限、余额限制及结果不确定仍可见。
图片、文件、知识库、网页预览等成果入口保持可直接使用，独立异步任务继续显示真实进度。

不修改工具执行权限、重试策略、费用、任务生命周期或数据库结构，不部署线上。

## 源码依据

对照 `openai/codex` 提交 `b1573f102413e96a6481087922bf425231222dee`：

- `codex-rs/core/src/tools/registry.rs`：`RespondToModel` 将可纠正的调用问题反馈模型，与 `Fatal` 分开。
- `codex-rs/tui/src/history_cell/activity_group.rs`：保存有序调用及详情。
- `codex-rs/tui/src/chatwidget/activity_groups.rs`：吸收相邻过程详情。
- `codex-rs/tui/src/chatwidget/activity_presentation.rs`：完整记录与紧凑展示分离。

采用其机制，适配当前 Python 事件流与 React 界面，不照搬终端组件。

## 事件与展示契约

`tool_result.outcome` 随已有 `tool_events` JSON 保存：`success` 为成功回执，
`feedback` 为参数反馈，`blocked` 为权限、额度或能力限制，`failed` 为工具操作失败，
`uncertain` 为结果待核对。不修改原始 `status` 或发给模型的错误观察；
优先保留不确定状态，不根据错误文案猜测分类。
没有新字段的旧记录仍按其真实状态展示详情，不反推历史错误类型。

`display_blocks` 继续保存原始顺序，前端只生成过程分组视图。
正文切断分组，新增调用保留原过程 ID；不把工具失败数量当作整轮失败。
图片、资料保存等异步任务的回执和进度不依赖过程展开。

## 验收

- 参数错误反馈模型后可自行改正并完成；不显示红色任务失败。
- 连续操作和思考保持有序、默认折叠；正文构成分组边界，用户展开状态不被更新覆盖。
- 实时事件和刷新后的持久记录分类一致，不重复执行工具。
- 权限、额度和不确定结果不被隐藏为成功；独立图片任务失败仍可见。
- 多个文件下载、异步进度、来源和预览不被分组吞掉。
- 后端回归、前端单元测试、类型检查与构建、1440/700/390px 浏览器截图及键盘检查。

## 验证记录

- 最终后端相关回归 199 项全部通过，覆盖 Agent、持久化会话、任务管理、题目工作台、
  上游重试、错误分类、扩展及个人 MCP。初轮两处新增测试误用结束事件名，已修正并全量复测。
- 前端单元测试 25 项通过，Next.js 生产构建及 TypeScript 通过。
- Playwright 载入真实生产构建，使用完全拦截的合成 API 数据，验证分组、纠参、顺序、按需读取思考、
  键盘展开、两个文件下载、预览入口、图片在回答结束后的完成/失败、刷新及展开状态、额度/不确定/整轮失败。
- 已逐张检查 1440、700、390px 收起/展开截图、图片完成截图和手机错误截图，共 8 张；
  无横向溢出、遮挡或浏览器异常，手机入口至少 44px，并覆盖 reduced motion。
- 浏览器复现脚本：`frontend/tests/browser/tool-process.acceptance.js`。
  构建时使用 `NEXT_PUBLIC_API_URL=http://localhost:8127`、`NEXT_DIST_DIR=.next-tool-process`；
  从仓库根目录执行 `playwright-cli --session tool-process run-code --filename frontend/tests/browser/tool-process.acceptance.js`。
  脚本通过 Playwright 读取静态构建文件，无须本地 HTTP 服务，全部 API 数据均为夹具。
- 本地预览服务启动被自动审批拦截，工具未返回具体原因；改用浏览器拦截读取本地生产构建完成验收。
- 沿用 `ConversationActivity` 和 `ConversationThinking`，仅增加有序详情入口及结构化展示分类；
  未增加依赖、数据库迁移或测试用产品路由。线上未部署；未调用真实模型或启动收费沙箱。
