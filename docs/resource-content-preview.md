# 资源库内容预览

任务范围与验收记录见 `tasks/resource-content-preview.md`。本次完成本地开发，尚未部署线上。

## 用户流程

`/resources` 默认瀑布流，支持列表偏好、搜索、来源筛选、上传、预览与删除。聊天附件选择保持紧凑列表，不改变资源权限或附件引用。

图片使用真实 WebP 缩略图，打开后显示原图并支持缩放拖动。PDF 在浏览器内按页阅读，采用 64 KiB Range 读取，不先接收完整文件。Word（`.doc`、`.docx`）仅在首次打开时由 LibreOffice 转为 PDF，后续使用相同阅读器；列表不触发 Word 转换。保留原文件下载。HTML/SVG/脚本不以同源活动内容执行，文字按纯文本展示。

## 接口与数据

所有接口沿用当前登录用户和工作区资源权限；viewer 也可以请求生成预览。模型不参与预览生成。

| 方法 | 路径 | 行为 |
| --- | --- | --- |
| GET | `/resources/{id}/preview` | 重新鉴权、HEAD 原对象，返回当前指纹、类型、大小与预览状态 |
| POST | `/resources/{id}/preview` | `{kind: "thumbnail"或"document", retry: boolean}`；202 返回持久化任务，重复调用复用同一记录 |
| GET/HEAD | `/resources/{id}/preview/original?revision=…` | 当前原文件；支持单段 Range，指纹变化返回 409 |
| GET/HEAD | `/resources/{id}/preview/{preview_id}/content` | 当前缩略图或转换 PDF；校验资源、工作区、指纹与缓存状态 |

迁移 `0061_resource_previews` 只增加可重建的预览任务，不更新原资源、知识库或账单。唯一约束为 `(resource_id, revision, kind)`。指纹包含资源 ID、对象 ETag、Last-Modified、大小和渲染器版本；它只用于缓存失效，不是资源历史版本。修改原文件后无法通过旧指纹获取内容；删除后所有入口立即拒绝访问。

任务记录 queued/running/ready/failed/expired。API 每分钟向 `previews` 队列投递维护任务，重新投递超过五分钟的中断任务；转换进程最长 125 秒。对象上传前记录目标键，中断恢复时清理未提交产物，失败不重复生成收费任务。原资源更新或删除期间生成的结果丢弃。

## 缓存与费用

- 产物保存在当前主存储的 `preview-cache/` 独立前缀，不进入资源目录，不生成资源条目，不占用户存储额度。临时转换目录结束即回收，服务器不永久存放缩略图。
- `PREVIEW_CACHE_MAX_BYTES` 默认 5 GiB，`PREVIEW_CACHE_IDLE_SECONDS` 默认七天，维护任务按最近访问时间淘汰。资源删除后异步回收相关缓存；对象在存储侧消失时可重新准备。
- 转换读取原文件由平台承担，不记 model_read，不运行云电脑、不收工具费、不触发 OCR 或 Embedding。平台承担预览缓存存储费用。
- 缩略图、阅读器分段和原图发送给浏览器时，沿用鉴权下载服务，预留额度并按 ASGI 实际发送字节结算；HEAD 免费，中断释放剩余额度。
- 浏览器使用内存和 IndexedDB，按账号、工作区、指纹、URL、范围隔离；每账号最多 200 MiB、闲置七天淘汰。同一块并发请求合并，接收完整后才缓存。命中本地缓存不发文件请求，不重复扣下载额度；原文件下载按钮仍走正常下载计量。
- 打开、切回前台及阅读期间每 30 秒重新校验状态。撤权后无法再从服务端取得内容，活动阅读器在下一次校验时隐藏预览。浏览器已接收的文件无法被远程撤回。登出/登录失效清理本地预览缓存；禁用 IndexedDB 时退回受限内存缓存。

## Worker 与隔离

`docker compose build preview-worker` 构建专用镜像，`docker compose up -d preview-worker` 启动 `previews` 队列。部署前执行 Alembic 迁移，并协调更新 API、Web 和 Worker；普通解析 Worker 不消费该队列。不需要真实模型或云电脑凭据。

镜像包含 LibreOffice Writer、Noto 中文字体、Pillow 和固定版本 PDFium。每次转换使用独立目录和清空的环境变量，Linux 下以非特权 UID 运行，Landlock 只允许读取系统库、字体及必要的 Office 配置，只有本次目录可写；seccomp 拒绝 IPv4/IPv6 socket。禁用宏和链接更新，并限制 CPU、地址空间、文件尺寸、打开文件数及进程时间。Office IPC 仅使用本次临时目录，镜像 `/tmp`、`/var/tmp` 不向转换 UID 开放写入。

预览 Worker 所在内核须支持 Landlock；不可用时拒绝转换，不降低隔离运行。Word 单文件上限 50 MiB，其他源文件 200 MiB，图片解码上限 4000 万像素。加密、损坏、超限或不支持的文件返回下载/重试提示。Word 的精确字体和分页仍取决于模板字体与 LibreOffice 的格式兼容能力，不修改原文档。
