# 云电脑

## 接入约定

执行使用阿里 Agent Sandbox 极速版，锁定官方入门示例的 `e2b==2.31.0`。地域决定 `https://api.<region>.sandbox.aliyuncs.com` 与 `<region>.sandbox.aliyuncs.com`；不允许任意服务地址。参考：

- https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk
- https://help.aliyun.com/zh/agent-sandbox/console-management-custom-templates

使用 `backend/cloud-computer.Dockerfile` 在阿里自定义模板流程构建模板。模板规格在供应商侧固定，后台规格字段为核对记录，不通过模型参数调整。构建后记录模板不可变 ID、实际 CPU/内存、地域和镜像摘要，再填写后台。Dockerfile 为待真实云构建验收的定义，不等于可用模板。

服务端持有 API Key，沿用 SITE_CONFIG_ENCRYPTION_KEY。后台读取不返回凭据；密钥留空保留、明确清除。禁止向沙箱注入业务凭据或对象存储权限。

创建要求 secure、非公开访问及网络拒绝列表。拒绝私网、元数据、业务域名及管理员补充的业务公网 IP。若阿里不支持传入网络策略，创建必须失败，不能去掉策略重试。真实验收需实测 DNS 重绑定、IPv6、内网及公网业务 IP；未通过不能上线。

## 生命周期和资源

一个用户仅一个活跃环境，绑定工作区和会话；默认站点上限 5。默认闲置 600 秒，最长 3600 秒。命令执行中不算空闲，供应商硬期限始终生效。

`run_command(command, inputs, skill_ids)` 保持命令和资源输入接口，增加可选 Skill ID 列表；命令中明确引用 `/skills/{id}/` 时也准备对应包。不要求先读取 Skill，不全量复制个人 Skill。输入按资源固定版本从计量缓存读取；工作目录 `/home/user/workspace`，Skill `/skills`，依赖 `/home/user/.cloud-computer/deps`。只同步工作目录内普通文件，拒绝符号链接，排除 node_modules、虚拟环境和缓存目录，限制单文件 200 MiB、总计 512 MiB、2000 个文件。沙箱删除不删除资源；资源冲突不覆盖。

API 将命令持久化后投递现有 Celery Worker；Agent 等待结果，页面独立查询。API 每 15 秒维护活跃环境。创建或命令结果未知不重跑；按元数据查找实例，保留准入槽位直到确认销毁或最大期限经过。供应商硬期限限制 API/Worker 同时离线产生的损失。

结束、空闲和余额不足时先停止命令并尽力同步，再销毁和查询确认。平台回收延误不转嫁用户。所有服务重启后必须优先运行维护核对，再允许新的执行。

## 计费

计费主体沿用现有工作区积分账户，记录操作用户。经用户授权，初始配置默认每分钟 1 积分，管理员可调整，默认不启用。按存续秒数累计，包含命令间等待；累计总额统一保留四位小数，周期入账仅记累计差额。首次预留一个闲置窗口，每分钟结算并补足预留；不足时结束，不允许透支其他任务的预留。

预留及释放复用原账本，不计入最终消费；费用类别为 `cloud_computer`。每个环境冻结售价和配置版本，后台调价不改变活跃环境。模型 Token、资源存储及下载费用继续独立核算。云侧实际成本需账单核对，不能用用户积分流水冒充云账单。

## API

- `GET/PUT /site/admin/cloud-computer`：版本化设置，管理员权限。
- `POST /site/admin/cloud-computer/check`：只验证列表连接，不创建实例、不验证模板。
- `GET /cloud-computer/catalog`：可用性、单价、闲置和最长时间。
- `GET /conversations/{id}/cloud-computer`：当前或最近环境状态和费用。
- `POST /conversations/{id}/cloud-computer/end`：请求结束。
- `GET /conversations/{id}/cloud-computer/commands`：最近 20 个命令与产物，不返回云凭据或模型输入。
- `POST /cloud-computer/commands/{id}/cancel`：停止对应命令。
- `POST /cloud-computer/commands/{id}/retry-save`：只重试同步，不执行命令。

所有用户接口重新验证登录、工作区、会话所有权；写操作要求编辑权限并沿用全局 CSRF/来源校验。

## 上线与验收

新增迁移 `0056_cloud_computer`，不改历史账单。先完成模拟和 PostgreSQL 测试，再确认真实云预算、模板、网络策略、销毁及云侧用量。真实模型调用不在当前授权范围内。

上线前备份数据库、配置及镜像；确认旧 Docker 执行没有运行任务。迁移后协调更新 API/Worker/Web，配置真实连接、模板和经确认的售价并完成实际启用验证，再停止旧执行服务。没有真实凭据、模板或售价时不得宣布上线完成，也不自动退回 Docker。
