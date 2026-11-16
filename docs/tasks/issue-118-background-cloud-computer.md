# Issue #118：云电脑退到后台

## 目标与边界

聊天聚焦任务和真实成果，删除实例列表、费用、历史和全局状态错误。
预览只管理对应环境；积分页提供本人当前工作区的只读分页用量。
模型主动结束无后续用途的临时环境，保留交互预览。闲置从最后有效活动起计十分钟。

不新增公开接口或数据库迁移，不调整价格、预留、结算和最长运行规则。
已有环境沿用启动快照，不修改线上配置或用户数据。本轮提交 PR，不合并或部署。

## 验收方式

- 前端状态测试覆盖无全量查询、作用域隔离、结束幂等、迟到响应和撤权。
- 后端测试覆盖心跳、600 秒边界、活动命令、最长时限、配置版本及账务。
- 隔离 Docker、临时用户和工作区，模拟模型和供应商，浏览器验证成果、预览、
  结束、文件保留及积分分页；不调用付费服务。
- 检查 1440、700、390px 截图、键盘焦点、触控与 reduced motion。
- 执行相关回归、PostgreSQL 测试、类型检查、生产构建和 diff 检查。

## 执行记录

实施分支：`codex/118-background-cloud-computer`，基于 `origin/main` 的 `4e7ab101`。
2026-11-16 完成本地隔离验收：

- 云电脑、预览、多实例、结束原因及 PostgreSQL 回归合计 **65 passed**。
  PostgreSQL 使用 `rag_auth_test_issue118_suite`，迁移至当前 head；
  验证最后活动 + 600 秒、活动命令保护、最长时限、权限、配置快照及结算幂等。
- Agent 与预览相关测试组 **51 passed**（与上述存在交集，不累计计数）。
- 前端 `npm test` **47 passed**、`npm run build`、`npx tsc --noEmit` 通过。
- Docker Compose 配置检查、隔离 API/Web 镜像构建和 `git diff --check` 通过。
  构建使用本机缓存依赖基础镜像；夹具默认提供 Python/Node 基础镜像以便复现。
- 使用临时用户、独立工作区、24 条历史环境及多会话活动环境完成真实浏览器闭环。
  合成模型经真实 Agent/Worker 创建两台本地环境，共享写读文件，结束临时处理环境，
  保留交互页面；核对更新的生命周期提示词确实进入模型请求。
- 1440/700/390px 检查打开、点击交互、刷新、确认、Escape、焦点返回、关闭、
  独立窗口、查询失败及结束响应丢失重试；仅结束对应环境，其他任务继续运行。
- 结束后刷新历史预览不会重启；真实鉴权下载仍返回原文件。
  聊天无实例列表、catalog、历史命令请求，未打开预览不轮询。
- 积分页金额与接口逐条一致，展示未结算/已结算、时长秒单位、分页和只读详情；
  不展示其他用户记录或管理操作，浏览用量不更新闲置期限。
- 浏览器脚本最终完成且 `pageerror=[]`；13 张截图逐张审查，无横向溢出，
  手机相关触控目标至少 44px，启用 reduced motion。
  实机发现并修复嵌套确认 Escape 误关全屏预览和焦点返回导航的问题。
- 验收后通过本地鉴权结束剩余环境，模拟供应商累计创建/销毁均为 7，
  活动实例为 0；隔离数据库全部 32 条记录已结束，预留积分合计 `0.0000`。

## 复现入口

- [隔离 Docker 夹具与操作说明](../../backend/tests/browser/cloud-background/README.md)
- [浏览器验收脚本](../../frontend/tests/browser/cloud-background.acceptance.js)
- [后端生命周期测试](../../backend/tests/test_cloud_background.py)
- [PostgreSQL 账务测试](../../backend/tests/test_cloud_background_postgres.py)

## 截图

| 宽度 | 聊天 | 预览 | 用量 |
| --- | --- | --- | --- |
| 1440 | [截图](../assets/cloud-background/issue118-1440-chat.png) | [截图](../assets/cloud-background/issue118-1440-preview.png) | [截图](../assets/cloud-background/issue118-1440-usage.png) |
| 700 | [截图](../assets/cloud-background/issue118-700-chat.png) | [截图](../assets/cloud-background/issue118-700-preview.png) | [截图](../assets/cloud-background/issue118-700-usage.png) |
| 390 | [截图](../assets/cloud-background/issue118-390-chat.png) | [截图](../assets/cloud-background/issue118-390-preview.png) | [截图](../assets/cloud-background/issue118-390-usage.png) |

[手机确认](../assets/cloud-background/issue118-390-confirm.png)、
[桌面已结束](../assets/cloud-background/issue118-1440-ended.png)、
[手机已结束](../assets/cloud-background/issue118-390-ended.png)、
[独立窗口](../assets/cloud-background/issue118-standalone.png)。

## 验收边界与后续发布

模型和云供应商为合成适配器；命令/HTTP 子进程、平台鉴权、Agent、Celery、
数据库、账务和文件下载为真实本地代码。未调用真实付费模型、云沙箱或 OSS，
不将该结果描述为供应商网络、挂载或真实模型自主决策验收。
预览隔离域名的浏览器传输转到本地 HTTP 服务，未复测线上泛域名网关。
后台/断线后的 600 秒回收使用可控时钟测试，不通过真实等待十分钟替代边界断言。

没有新增迁移或公开接口，未修改线上数据、配置、单价或部署。
后续发布需通过现有版本化 GET/PUT 将后台 `idle_seconds` 保存为 600，
保留其他字段，版本冲突时重新读取；既有实例继续使用原快照。
