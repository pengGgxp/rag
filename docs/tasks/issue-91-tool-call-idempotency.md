# Issue #91：云命令按单次工具调用幂等

## 目标与范围

修复同一回答内读取、修改、相同参数再次读取时错误复用旧命令的问题。将 Agent 已有的稳定活动标识（轮次与工具调用 ID）与回答 request ID 组合为 UUIDv5，作为云命令的调用请求 ID；沿用现有持久化表、准入锁和 Worker 状态机。查找原调用时不再以参数摘要作为筛选条件，同一调用参数不同明确返回 409。

范围覆盖文件工具、命令、图片元数据检查及成果预览启动；不改变远程 MCP、生图等其他工具的幂等设计。没有新表或业务数据迁移，不改变挂载、文件版本、权限、取消、不确定结果、收费或模型策略，不部署、不合并、不关闭 Issue。

## 授权与验收

用户在当前任务回复“可以。去做”，同意先完成本地验证并提交 PR，真实沙箱完整闭环由维护者另行验收。本轮禁止真实付费模型及沙箱调用。领取评论：6818022872；基线：main@2165ec72b446aa74b2069412cef954ab3800cdb8。

- 同一回答读取 17、修改为 23、相同参数再次读取，得到新正文与一致摘要；不同轮次复用供应商调用 ID 仍作为不同调用。
- 同一调用重复提交、响应丢失、重新创建运行时/数据库会话或 Worker 重启，不重复执行、预留或扣费；不同参数返回冲突。
- 覆盖取消、撤权、跨用户/工作区/会话、不确定副作用命令和已结束云电脑的回放。
- 隔离 PostgreSQL 并发准入和恢复/账务幂等；针对性后端回归；本地 API 与真实浏览器文件操作闭环；前端生产构建；git diff --check。
- 在 PR 逐项记录结果和限制。真实沙箱/OSS/商业模型与供应商账单尚待维护者验收，本地替身测试不替代实机验收。

## 实现约束

派生 ID 不使用参数、时间戳或随机数，不把回答 ID 直接当作单次云调用 ID。缺少稳定回答/调用标识时拒绝发起云调用。原始回答 ID 保留在会话执行记录；命令 request_id 为可由回答 ID 与活动标识重算的调用请求 ID。旧命令继续由原 command ID 恢复与核对，不追溯重写或重放。Worker 不自动重新运行 Agent 回合，保持既有中断恢复规则。

## 本地验收方式

从 backend 使用已有 Python 环境执行：

    python -m pytest tests/test_cloud_tool_idempotency.py tests/test_cloud_computer_postgres.py -q

PostgreSQL 测试需要显式设置 AUTH_TEST_DATABASE_URL，仅允许数据库名以 rag_auth_test_ 开头的一次性迁移测试库。本次采用独立 PostgreSQL 15 / pgvector 容器、全新空库升级至 0065_resource_mentions。没有业务数据迁移。

全量测试每次使用全新测试库：既有 PostgreSQL 夹具会保留合成数据和设置，复用上轮全量的数据库可能引入配置残留；不能以这种复跑的额外失败判断代码回归。

浏览器夹具：从 backend 执行 python tests/cloud_tool_idempotency_preview.py；它只绑定 127.0.0.1:8131，创建临时 SQLite、内存对象存储、本地文件执行器和独立线程 Worker。前端使用 NEXT_PUBLIC_API_URL=http://localhost:8131 构建，生产服务只绑定 127.0.0.1:3191。通过 Playwright CLI 执行 scripts/check-cloud-tool-idempotency.js；每次重跑需重启夹具，以恢复合成原始文件。

在仓库根目录打开浏览器会话后运行：

    npx --yes --package @playwright/cli playwright-cli -s=rag91 open http://localhost:3191 --headed
    npx --yes --package @playwright/cli playwright-cli -s=rag91 run-code --filename scripts/check-cloud-tool-idempotency.js

验收脚本通过 @ 选择资源，发送读改再读请求，核对三个持久化命令、相同读取参数摘要及不同调用请求 ID、模型所见正文、资源下载、真实浏览器下载，检查 1440/700/390px 与刷新无重放。截图和报告保存在忽略目录 output/playwright/issue91-*。SHA-256 为 1a2124e8ef4a4009dbc50a73821d81cc7eae6131e025b8daad93162ad683d09e，修改后内容为 quantity 23 加 checked 行（各含行尾换行）。

共享文件索引在 HEAD 刷新发现变化时可能将尚未重算的摘要留空，沿用现有语义；回归同时核对更新操作返回的摘要、再次读取的正文/长度和下载内容，不将空摘要当成已校验。真实供应商存储、挂载、沙箱、商业模型与账单未执行，仍待维护者另行验收。

## 已执行验证与限制

- 全新隔离 PostgreSQL 测试库下最终后端全量：1170 项通过、16 项跳过、2 项既有失败，耗时 534.37 秒；失败详情及基线复现见下文。
- 云命令与 PostgreSQL 针对性回归：22 项通过；四线程并发提交、四 Worker 重投递、参数冲突、不同调用相同参数、环境结束后的重放与账务核对均通过。
- 扩展回归：228 项通过、1 项跳过，覆盖 Agent、文件、挂载、预览、图片、资源引用、扩展、模型计费与上游重试。
- 更新旧 Skill 测试的调用身份后，Skill 与新增幂等回归复跑：43 项通过、2 项跳过。没有为旧测试添加生产环境身份回退。
- 前端测试：11 项通过；Next.js 生产构建与 TypeScript 检查通过。
- 真实 Chrome 使用本地模型、文件执行器与内存存储完成 @ 选择资源、读改再读、API 下载及浏览器点击下载；1440、700、390px 无横向溢出，刷新后仍只有三次文件执行。
- Python 编译、浏览器脚本语法检查与 git diff --check 通过。

全量初跑出现的五个 Skill 失败已由上述回归修复。另两项 PostgreSQL 失败已在未修改的 main@2165ec7 导出副本、同一隔离测试库中独立复现，未扩大本次范围去改迁移测试：

- test_decimal_credits_postgres.py::test_downgrade_refuses_to_destroy_fractional_credits：期待 Decimal credits exist，但降级先触发 0053_resource_library 的 Shared message references 防丢失保护。
- test_oss_postgres.py::test_schema_preservation_unique_active_and_serialized_settings：写死迁移版本 0058_oss_storage，当前 head 为 0065_resource_mentions。

跳过项包括未安装的 markitdown 解析测试、未配置的一次性支付测试库、Windows 无法执行的 Linux launcher，以及需显式开启的真实沙箱、挂载、图片和 Docker runner 验收。本轮没有开启付费调用。真实沙箱完整文件操作闭环仍是待维护者验收项，不能将本地通过描述为该项已完成。
