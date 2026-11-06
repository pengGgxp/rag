# 云电脑

## 接入约定

执行使用阿里 Agent Sandbox 极速版，锁定官方入门示例的 `e2b==2.31.0`。地域决定 `https://api.<region>.sandbox.aliyuncs.com` 与 `<region>.sandbox.aliyuncs.com`；不允许任意服务地址。参考：

- https://help.aliyun.com/zh/agent-sandbox/using-the-cloud-sandbox-via-the-sdk
- https://help.aliyun.com/zh/agent-sandbox/console-management-custom-templates

使用 `backend/cloud-computer.Dockerfile` 在阿里自定义模板流程构建模板。模板规格在供应商侧固定，后台规格字段为核对记录，不通过模型参数调整。构建后记录模板不可变 ID、实际 CPU/内存、地域和镜像摘要，再填写后台。Dockerfile 为待真实云构建验收的定义，不等于可用模板。

服务端持有 API Key，沿用 SITE_CONFIG_ENCRYPTION_KEY。后台读取不返回凭据；密钥留空保留、明确清除。不向沙箱注入业务凭据或长期存储密钥。OSS 挂载由供应商使用 RAM 角色签发限定挂载路径的临时身份，不把整个 Bucket 或其他工作区目录暴露给环境。

创建要求 secure、非公开访问及网络拒绝列表。默认拒绝私网和元数据地址，允许访问知库、题目工作台等公开域名；沙箱不持有用户 Cookie 或业务凭据，接口仍须独立鉴权。管理员可按需补充拒绝地址，默认列表为空。迁移 0057 移除旧配置中自动加入的两个业务域名，保留其他管理员规则，不改历史会话快照；已有实例需结束后重建才能应用新策略。真实验收需实测 DNS 重绑定、IPv6、内网及元数据；未通过不能宣称隔离验收完成。

## 生命周期和资源

一个用户仅一个活跃环境，绑定工作区和会话；默认站点上限 5。默认闲置 600 秒，最长 3600 秒。命令执行中不算空闲，供应商硬期限始终生效。

`run_command(command, inputs, skill_ids)` 保持命令和资源输入接口，增加可选 Skill ID 列表；命令中明确引用 `/skills/{id}/` 时也准备对应包。不要求先读取 Skill，不全量复制个人 Skill。输入按资源固定版本从计量缓存读取；工作目录 `/home/user/workspace`，Skill `/skills`，依赖 `/tmp/zhiku-cloud-computer/deps`（运行期临时盘，实测 `/home/user/.cloud-computer` 对 pip 解包和文件写入返回 EIO）。只同步工作目录内普通文件，拒绝符号链接，排除 node_modules、虚拟环境和缓存目录，限制单文件 200 MiB、总计 512 MiB、2000 个文件。沙箱删除不删除资源；资源冲突不覆盖。

API 将命令持久化后投递现有 Celery Worker；Agent 等待结果，页面独立查询。API 每 15 秒维护活跃环境。创建或命令结果未知不重跑；按元数据查找实例，保留准入槽位直到确认销毁或最大期限经过。供应商硬期限限制 API/Worker 同时离线产生的损失。

### OSS 资源挂载

主存储为 OSS 时，在后台填写可由 `fc.aliyuncs.com` 扮演的 `oss_role_arn`。每个环境使用独立前缀 `workspaces/{workspace_id}/cloud-computer/{computer_id}/workspace/`，挂载到 `/home/user/workspace`。通过官方动态挂载 metadata 指定 Bucket、地域 Endpoint、前缀和角色；网络规则明确允许 Bucket Endpoint。当前杭州模板连接北京 Bucket 已实测。

挂载启用后，每次执行前准备当前工作区全部可见资源；显式 `inputs` 固定版本优先。初次准备走现有资源读取缓存及下载额度计量，相同版本不重复准备。资源对象以逻辑路径复制成工作副本，不可写挂载原始版本对象。仍受单文件 200 MiB、总计 512 MiB、2000 文件的执行限制；超过时明确拒绝启动，不部分挂载。

命令间保留工作副本，后续新增或修改资源会在下一次执行前刷新。命令完成后以原有版本冲突检查保存变更，不把沙箱删除映射成资源删除。只有确认环境销毁且产物保存成功后才清理会话前缀；失败保留副本供恢复。实例意外消失时可直接从 OSS 恢复产物，不重放命令。临时副本是运行基础设施，不重复创建资源计量记录；保存后的新版本沿用实际存储计量。

角色权限示例与阿里官方说明见任务记录，必须同时具有 Bucket 列举及目标前缀对象读写权限。角色权限是上限，供应商进一步限定单次挂载路径。真实跨前缀越权攻击、IPv6 和 DNS 重绑定尚未完成专项验收，不能将普通隔离测试称为完整安全审计。

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

上线前备份数据库、配置及镜像；确认旧 Docker 执行没有运行任务。管理员在供应商控制台先创建模板并将模板 ID 填入站点后台；平台不构建或自动创建供应商模板。迁移后协调更新 API/Worker/Web，配置真实连接、模板和经确认的售价并完成实际启用验证，再停止旧执行服务。没有真实凭据、模板或售价时不得宣布上线完成，也不自动退回 Docker。
