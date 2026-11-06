# 云电脑工具结果序列化修复

- 目标：修复文件已写入却导致对话失败的问题，区分异常中断与用户取消。
- 范围：云电脑结果的日期编码、异常日志、既有工具活动状态映射。
- 不包含：镜像制作、重新运行用户任务、修改历史消息或账单。
- 验收：工具执行结果可作为 JSON 交给模型；同轮后续工具可以执行；中断活动不显示为用户取消。
- 验证：模拟模型的 Agent 多工具回归、云电脑生命周期回归、前端状态映射检查及构建、git diff --check。

## 线上诊断

2026-11-06，会话 09d21388-731c-421b-bbfa-5403c8377b6a 的写文件命令成功，退出码为 0，未请求取消。
工具结果包含 cloud_computer.started_at 的 datetime 对象，无法由 Agent 的 json.dumps 编码。
用该会话云电脑记录进行只读序列化验证，复现 TypeError: Object of type datetime is not JSON serializable。
异常使剩余活动成为 interrupted，前端原映射将其错误显示成 cancelled。

修复云电脑对外日期为 ISO 8601 字符串（缺失时为 null），保留真实执行结果；中断活动显示结果待确认。
通用对话异常记录请求 ID 和服务端堆栈，用户界面不暴露内部错误。

## 本地验证

- Agent、云电脑生命周期、共享资源目录回归：50 passed。
- Node 工具活动状态回归：1 passed。
- Next.js 生产构建通过；git diff --check 通过。
- 未重跑用户任务，未调用真实模型，未修改线上消息或账单。

## 线上部署（2026-11-06）

- 修复提交：0a9d9f5；API、Worker、Web 均已切换到 cloud-tool-result-0a9d9f5 镜像。
- 备份：/data1/backups/cloud-tool-result-0a9d9f5，包含数据库自定义格式备份、配置及相关源文件；原镜像另存 pre-cloud-tool-result-0a9d9f5 标签。
- 服务器候选 API 镜像 Agent 回归：28 passed；新运行容器用原故障记录执行只读序列化验证通过。
- 公网首页和 API 健康接口均返回 200；Worker 就绪；公网活动组件脚本与本地构建逐字节一致，异常中断映射为结果待确认。
- 数据库无需新增迁移，当前仍为 0059_shared_resource_mount；未重跑用户模型任务，历史失败消息不改写。
- 发布文件与覆盖配置位于 /data1/releases/cloud-tool-result-0a9d9f5。回滚使用 current-directory-20261106 发布目录的 compose.yml，恢复其备份的相关源文件；无需数据库回滚。
