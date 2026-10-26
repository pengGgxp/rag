# 设置层连续导航回归验收

## 目标与范围

关联设置层重构 Issue #31。修复设置内部导航不断新增浏览历史，导致关闭按钮
只返回上一个设置菜单的问题。只调整前端导航，不改权限、后端 API、数据库和计费。

## 原因与修复

工作台打开设置应新增一条历史记录；设置内部切换 section 必须替换这条记录。
原侧栏 Link 和概览按钮使用 push，拦截层关闭时的 back 因而只能退回上一 section。
侧栏统一使用 Link replace，概览按钮统一使用 router.replace；继续复用现有设置 Dialog。
全屏设置的关闭按钮不再嵌套 Tooltip，避免自动聚焦时提示层拦截 Escape；保留无障碍名称和原生提示。

## 验收标准与验证方式

- 从知识库、问答、Agent 打开设置后，连续切换侧栏菜单与概览入口，DOM 中始终只有一个设置 Dialog。
- 一次关闭、Escape 或浏览器后退回到原工作台，不停留在上一设置 section。
- 前进可以重新打开最后访问的设置 section，URL 与内容一致。
- 直接打开或刷新设置链接后，连续切换菜单仍只有一个 Dialog，一次关闭按 returnTo 返回。
- 工作台已有输入在设置内部切换与关闭后保留。
- 1440px 与 390px 检查窗口数量、导航和关闭行为。
- 执行生产构建、git diff --check、Playwright 回归，再同步线上仅重建 web 并复查真实会话。

## 回滚边界

修复提交可单独回退；部署前保存现有 Web 镜像和前端源码。服务器 .env 与数据卷不参与同步。

## 本地验证结果（2026-10-26）

- Docker Web 构建中的 `npm run build` 与 TypeScript 检查通过。
- Playwright 使用隔离的管理员 API fixture，不写入本地或服务器业务数据。
- 1440 × 900：六个侧栏菜单连续切换后只有一个设置层，一次关闭退出，知识库搜索输入保留。
- 问答页：概览按钮与侧栏切换后 Escape 关闭，问答草稿保留。
- Agent 页：后退关闭、前进打开最后 section、关闭返回原页，扩展搜索保留。
- 六个设置路由均验证直接访问、刷新、继续切换菜单及一次关闭按 returnTo 返回。
- 390 × 844：六个菜单连续切换后一次关闭，没有页面横向溢出。
- 上述回归中没有浏览器未捕获异常。

复现命令（本地 web 已启动，仓库根目录执行）：

```powershell
npx --yes --package @playwright/cli playwright-cli --session settings-regression open http://localhost:3000/
npx --yes --package @playwright/cli playwright-cli --session settings-regression run-code --filename=scripts/check-settings-navigation.js
```

脚本仅拦截 localhost:8000 的测试 API；线上验收使用真实登录会话另行验证。
