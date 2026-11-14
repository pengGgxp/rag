# 工作区接口与执行边界

工作区页面、Skill 管理、云电脑和模型文件工具使用同一份真实文件。`/resources` 作为内部路由保留，用户可见名称统一为“工作区”。不增加发布副本、同步目录或正文历史。预览见 `resource-content-preview.md`，文件工具见 `tasks/unified-cloud-file-tools.md`。

## 文件、目录与权限

文件位于 `workspaces/<workspace-id>/resource-directory/`，数据库仅保存当前展示索引。浏览和搜索前刷新真实文件增改删；空目录使用同前缀下 `.rag-directory` 标记，标记不作为文件展示，不进入模型列表或重复计入容量。隐式目录由文件路径推导，目录操作处理全部子项和标记。

viewer 可浏览、搜索、预览和下载；editor/admin 可新建、上传、编辑、复制、移动和删除。所有访问限定当前工作区。工作区上传允许脚本、压缩包和普通二进制，严格落在当前目录；不自动解包或执行。聊天附件保留自己的格式和大小限制。文件操作不启动云电脑，也不创建知识库文档、OCR 或向量索引任务。

## API

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/resources/browse?path=&q=&sort=&direction=&offset=&limit=` | 直接子项、面包屑、完整路径、revision、total、has_more；非空 q 全工作区搜索，单页最多 500 项 |
| GET | `/resources?q=&source=&offset=&limit=` | 平面文件选用入口，分页返回 has_more |
| POST | `/resources` | file、directory、conflict、revision、request_id；error/keep_both/replace，替换需旧 revision |
| GET | `/resources/{id}` | 刷新真实对象元数据 |
| GET/HEAD | `/resources/{id}/download?preview=true` | 鉴权下载、Range、用户出口计量 |
| POST | `/resources/{id}/attachment` | 创建聊天引用，不复制文件 |
| POST/PUT/DELETE | `/resources/directories` | 创建、重命名、递归删除；禁止根目录破坏 |
| PUT | `/resources/files/{id}` | 重命名，携带 revision/request_id |
| POST | `/resources/operations/copy`、`/move` | paths、destination、expected、conflict、逐项 resolutions、request_id；整批预检 |
| POST | `/resources/operations/delete-preview` | 实际文件/目录数量及 confirmation |
| POST | `/resources/operations/delete` | confirmation 必填，变化返回 409 与新范围 |
| GET | `/workspaces/{workspace}/files/{id}/text` | UTF-8 文本及当前 revision |
| PUT | `/workspaces/{workspace}/files` | 创建文本或携带 revision 保存；request_id 防重 |

冲突响应提供路径和当前 revision；异类冲突不能替换，目录合并逐文件处理。拒绝路径穿越、来源与目标重叠、移动到自身内部。预览 `revision` 用于渲染缓存，`content_revision` 才是文件内容标识，不能混用于编辑。

## 并发、重试和恢复

revision 是当前 ETag 与大小生成的不透明标识，旧保存返回 409 并保留输入，不保存正文历史。新建使用存储禁止覆盖。平台同工作区写入使用进程锁及 PostgreSQL session advisory lock 串行，日志提交期间持锁。

通用操作记录以 `(workspace_id, request_id)` 防重；同 ID 不同参数返回冲突。完整检查来源、目标、权限、容量和重叠路径后，准备 staging/backup，再修改对象和索引。临时材料位于挂载根之外的 `workspaces/<workspace-id>/file-operations/<operation-id>/`，成功后清理；失败补偿，进程中断通过现有维护入口恢复。恢复遇外部新内容时保留 recovery_required 和材料，不伪报成功；未完成恢复阻止后续写入。

**已确认选择 A 的限制**：保留 OSS 与云电脑任意挂载直写。API 锁只约束参与协议的写入，保存前 revision 检查不能消除核对后外部直写的窗口；OSS 没有目标旧 ETag 条件替换/删除。不能宣称跨入口原子替换、删除或无竞态补偿，本地适配器结果不代替真实 OSS 验收。

## 页面交互

保留系统 Token、侧栏、卡片/列表偏好和现有组件。单击选择、双击打开、Ctrl/Shift 多选、Ctrl+A 跨页全选、F2 改名、Enter 保存、Escape 取消。新建待命名项取消不写入文件。网页内复制、剪切、粘贴与多选拖拽；冲突默认取消，可保留两份、替换或逐项选择。删除展示实际递归影响，变化后重新确认。

地址栏默认可点击面包屑，双击或 Enter/F2 原位编辑，也可直接输入字符。Enter 跳转、Escape 取消，Backspace 或 Alt+↑ 返回上级；支持 `?path=` 定位。工作区及 Skill 编辑都有未保存离开保护，viewer 只读。手机提供更多操作和长按入口，触控目标至少 44px。

## 本地验证

使用隔离 Docker、临时 PostgreSQL、临时账号/工作区及独立本地对象目录。`tests/run_workspace_acceptance.py` 创建空库，完整升级、重复升级和并发检查后清理自己的临时库。`tests/workspace_local_preview.py` 仅在显式测试启动时注入本地 S3 或 filesystem 适配器，不构成线上存储失败回退。截图与临时配置放在忽略的 output/。不使用生产数据库、线上 OSS、真实模型或付费云电脑。
