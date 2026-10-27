# 账号与工作区设置重构

## 目标与边界

将全屏设置改为桌面有边界、手机全屏的统一设置窗口，拆分个人资料与登录安全；工作区保留协作和消费，模型及定价移入站点后台按工作区管理。

复用现有 Radix Dialog、业务面板、导航样式与 Token，不新增依赖。不修改当前价格、余额、历史账单、数据库结构、RAG 或 Agent 编排。缓存计费由 Issue #36 独立处理。现有 `output/` 不提交。

## 实现约定

- 桌面窗口最大 1120px，视口两侧至少 24px，高度 `min(760px, 88dvh)`；700px 以下全屏。固定侧栏、内容标题和关闭按钮，正文单独滚动。
- `/settings` 个人资料，`/settings/security` 登录与安全；`/settings/workspace` 基本信息，现有 billing、members、audit 分别为积分与充值、成员管理、操作记录。
- 设置布局持续挂载，内部路由 replace；直接访问使用同一窗口及中性背景。关闭优先恢复来源工作台，安全校验 returnTo。未知 section 不静默落入账号页。
- 未保存输入在内部导航、关闭及 Escape 前使用内联确认；刷新使用原生离开提示；密码不持久化。
- 后台新增 `/admin/models`、`/admin/pricing`，以 `workspace` 参数明确目标；不自动选择第一个工作区，不切换管理员自己的会话。
- 新管理 API 位于 `/site/admin/workspaces/{workspace_id}/llm-providers` 和 `/usage-policies` 及现有操作子路径；站点管理员可管理未加入的工作区，供应商必须属于目标工作区。
- 新旧定价与供应商接口均校验数据库中的站点管理员权限；兼容接口仍针对会话当前工作区。审计不包含凭据。
- 保留所有已有工作区规则和供应商。旧 policies 页面仅向站点管理员跳转对应后台，其他用户无权访问。

## 提交与验收

1. `fix(admin): restrict workspace pricing and provider management`：权限、显式目标 API、审计和后端回归。
2. `refactor(settings): rebuild account and workspace settings`：设置容器、信息架构、稳定路由、公共导航样式和设计规范。
3. `feat(admin): manage workspace models and pricing`：后台目标选择、模型及定价面板、浏览器验收记录。

执行后端相关测试、前端构建、diff 检查；Playwright 检查 1440px / 390px、路由刷新、前进后退、焦点、未保存提示、错误重试和设置恢复。新旧 API 验证普通用户、工作区管理员拒绝，站点管理员可跨工作区管理；历史快照与价格保持不变。

通过后备份镜像、配置及数据库，更新 API 和 Web，不重建数据服务、不修改 .env。线上复查设置和权限，未通过不关闭 Issue。界面回滚仍保留服务端权限修复。
