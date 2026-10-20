# 文档版本验收记录

## 目标

同一份资料的更新保存为不可覆盖的版本链，并明确当前参与检索的版本。

## 实现范围

- `documents.version_group_id` 标识同一份资料的版本组。
- `version_number` 从 1 开始递增，`supersedes_id` 保留版本关系。
- `is_active` 标识当前生效版本。
- 新版本处理期间不会影响旧的生效版本。
- 只有已就绪版本可以切换生效。
- 删除当前版本时，自动选择同组中最新的已就绪版本。
- 检索和问答只召回已就绪且当前生效的版本。
- 引用和检索 trace 返回版本号。

## API

- `GET /documents/{id}/versions`
- `POST /documents/{id}/activate`
- 上传接口通过 multipart 字段 `version_of` 创建新版本。

## 验证

- 后端测试：55 passed。
- Docker Alembic 当前版本：`0010_add_document_versions`。
- 前端生产构建通过。
- 版本专项测试覆盖旧版本保留、检索过滤和激活切换。

## 未包含

- 文档差异对比。
- 自动合并。
- 多分支版本工作流。
