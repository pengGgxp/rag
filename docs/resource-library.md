# 工作区接口与执行边界

资源库内容浏览、缩略图与 Word/PDF 阅读器见 `resource-content-preview.md`；当前共享目录执行规则以 `tasks/shared-resource-directory.md` 为准，下文早期版本与输入准备记录不适用于新预览功能。

任务与验收记录见 `resource-library-task.md`。本次只进行本地开发，不部署线上。

## 数据与权限

沿用 `workspace_files`、`workspace_file_versions`，增加来源和对象可用性。`chat_message_resources` 将消息与固定版本附件关联，同一附件可被多条消息引用；删除会话不删除资源。资源对当前工作区成员共享，admin/editor 可上传、写入与删除，viewer 可查看、下载与选用已有资源。

上传来源为 `chat_upload`、`resource_upload` 或 `model_output`。同名上传的路径含独立 UUID。修改文件必须携带当前版本；删除路径不能恢复原资源，重新生成需使用新路径。入库不创建知识库文档，不调用 OCR、Embedding 或索引任务。

## API

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/resources?q=&source=&offset=0&limit=100` | 元信息分页，最多 500 项，返回 has_more |
| POST | `/resources` | multipart file，与聊天上传共用校验和创建服务 |
| GET | `/resources/{id}` | 当前资源元信息 |
| GET/HEAD | `/resources/{id}/download?version=N&preview=true` | 固定版本鉴权下载、单段 Range、用户出口计量 |
| DELETE | `/resources/{id}` | 立即隐藏，异步清理无引用对象 |
| POST | `/resources/{id}/attachment?version=N` | 取得固定版本聊天引用，不复制对象 |
| GET | `/resources/browse?path=&q=&sort=&direction=` | 按当前工作区目录浏览直接子项，返回面包屑和完整路径 |
| POST/PUT/DELETE | `/resources/directories` | 创建、重命名和递归删除工作区文件夹 |
| PUT | `/resources/files/{id}` | 在工作区内重命名文件 |
| POST | `/resources/operations/move` 或 `/resources/operations/copy` | 在同一工作区移动或复制文件/文件夹 |

旧聊天附件内容链接保留鉴权和计量。页面 `/workspace-files` 重定向到 `/resources`；用户可见名称为“工作区”，采用 Windows 文件资源管理器式的路径、选择、双击打开、复制/剪切/粘贴和删除交互。工作区、Skill 管理页、云电脑和模型文件工具读取同一份文件。

## Agent 工具

`list_files` 只列元信息；`read_file` 接收 id 或 path、可选 version、byte offset/limit（最多 12000 byte）。文本返回片段和 next_offset；图片及二进制返回元信息和 `{id, version}` 输入引用。写入与补丁保留版本冲突检查。

`run_command` 接收 command 与可选 `inputs: [{id, version}]`。默认为空，只准备指定资源，不要求先列文件或加载 Skill。请求附件向模型提供服务端确认的资源 ID、版本和执行相对路径。单次执行输入最多 512 MiB，单文件最多 200 MiB。Skill 包仍独立只读；执行后输出保存成资源版本，返回固定版本预览与下载链接。

`run_command` 使用按存续时间收费的云电脑，同一用户和会话在环境结束前保留输入、产物和依赖；新环境只准备本次指定输入。详情见 `cloud-computer.md`。图片嵌入 HTML 时同时指定图片与文档，在脚本内编码并保存，不向模型输出大段 Base64。长脚本可先保存为资源，再与输入文件一同指定。`apply_patch` 接收带行号的 unified diff，不接收任意补丁语法。

执行容器看不到缓存根目录、宿主目录或存储凭据。文件缓存位于 API/Worker 共享持久卷，默认 5 GiB、闲置 24 小时清理，可通过 RESOURCE_CACHE_DIR、RESOURCE_CACHE_MAX_BYTES、RESOURCE_CACHE_IDLE_SECONDS 调整。按工作区、资源版本、摘要和范围定位；文件锁合并同版本并发读取，校验失败重取，LRU 淘汰不删除云端资源。

## 计量与清理

模型读取先预留下载额度，读取后逐块结算实际字节，失败释放剩余预留。缓存命中不预留不扣量；用户预览与下载按实际向用户发送的字节另行结算，不叠加内部读取费。账目分别汇总 model_read 与 user_download。不增加工具费或重收生成费用。

共享容量按对象键去重，保留版本计入实际占用。连续超额 30 天后，Worker 锁定工作区并重新核对容量与超额时间，按创建时间和 ID 清理资源。知识库引用的对象保留，知识库独占超额时继续阻止新增。删除与自动清理均写审计；有效资源不因年龄、未发送或删除会话被回收。

## 迁移与部署注意

0053 保留旧对象键、附件 ID、原创建时间与消息关联；未删除附件进入资源记录，路径冲突消歧。旧资源版本标记待核验，Worker 通过对象 HEAD 确认可用性；缺失对象不可下载，不恢复已删除记录。已有超额工作区重新获得 30 天宽限。共享消息引用出现后禁止有损降级，故障恢复应使用兼容代码或完整备份。

将来部署须备份数据库、对象存储配置与镜像，停止旧清理 Worker，再迁移并协调启动 API、Web、Worker、执行器。不得让旧年龄清理逻辑在迁移期间运行。本次没有执行这些线上操作。
