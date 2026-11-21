# 知识库上传恢复（#145）

## 发布

1. 停止旧 API 接收写入，等待旧 Worker 排空，避免新旧 Worker 同时处理文档。对未排空的付费处理任务保留日志和上游任务标识。
2. 备份数据库，应用 `0079_document_recovery`。迁移只新增列和约束，不改写使用量事件或积分账本。旧的运行中索引任务没有可靠的逐批提交记录，会进入待核对；有 OCR 任务标识的解析任务继续查询原任务。
3. 同步启动新 API、Worker 和前端。部署检查应包含创建、上传、校验和检索的完整操作，而不只是健康检查。回滚须先停止写入和 Worker；降级迁移会丢失新增的幂等和处理检查点，不应在有未完成付费任务时直接执行。

## 运维检查

- 上传重放用同一 `Idempotency-Key`；尚未拿到会话 ID 的请求可以重放创建。收到会话 ID 后用原 ID 查询、续传和合并。对象存储需保留列举未完成分片上传的权限。
- 按 `stage`、`code` 和 `retryable` 统计 `document_request_failed`、`document_worker_failed`，用 `request_id`、`upload_id` 和 `job_id` 关联复查。关注正在退避、已恢复和 `needs_reconciliation` 的数量。
- `needs_reconciliation` 表示付费请求可能已被接收，但当前没有可安全继续的结果。核对上游任务或账单前不得重复提交；不要在工单中粘贴签名 URL 或文档内容。
- 原线上 `Internal S…` 响应的来源须结合网关与 API 日志另行确认。
