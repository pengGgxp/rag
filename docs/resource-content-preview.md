# 资源库内容预览

本页描述当前用户文件预览行为。Issue #120 的实现及验收记录见 [`tasks/direct-file-preview-120.md`](tasks/direct-file-preview-120.md)。

## 用户流程

工作区资源、聊天附件和成果、Skill 工作区文件、知识库原文件共用浏览器文件查看器。图片可在聊天中直接显示；文件入口默认打开预览，下载是独立操作。知识库保留“查看解析文本”作为原文件阅读的另一种方式。HTML、SVG、Markdown 等文字内容均以惰性文本显示，不执行文件携带的脚本或活动内容。

支持 PNG、JPEG、WebP、GIF、AVIF、BMP 图片，PDF、DOCX、PPTX，以及 Markdown、文本和常见代码格式。图片按原比例显示，支持缩放；PDF 使用 PDF.js 分段读取，支持分页和缩放；DOCX/PPTX 在浏览器内解析为可读页面。Office 以内容可读为目标，不承诺和桌面 Office 像素一致。`.doc`、`.ppt`、加密、损坏或超限文件提供明确错误和下载入口。Office 原文件上限 50 MiB，解压资源总量上限 150 MiB、最多 4000 个 ZIP 条目；Office 依赖按需加载。

## 访问与计量

OSS 保持私有。`POST /resources/{id}/preview-access`、`POST /chat/attachments/{id}/preview-access` 和 `POST /documents/{id}/preview-access` 在服务端执行资源、工作区、会话附件或知识库权限检查，HEAD 校验当前对象元数据，然后签发五分钟有效的短期读取链接。知识库链接指向确切文档版本。客户端不能提交对象键或存储地址。

预览正文由浏览器直接从 OSS 读取，不经过应用 API、不调用转换 Worker；预览链接不写入聊天或普通日志。OSS CORS 应允许实际站点 Origin 的 GET/HEAD、Range 和 If-Match，并暴露 ETag、Content-Range、Accept-Ranges 和 Content-Length。浏览器请求不携带应用 Cookie、Authorization 或 Referer。生产桶的 CORS 配置须在部署前按该契约确认；代码测试不会代替线上 CORS 验收。

预览签发、OSS 读取和本地缓存命中都不预留或扣减用户下载额度及积分，相关存储流量成本由平台承担。主动下载继续通过现有鉴权下载服务，按实际发送字节计量；HEAD 不收费，中断不对未发送字节结算。知识库原文件的下载也使用同一下载计量链路。

签名链接临近过期或 OSS 返回 401/403 时最多重新鉴权签发一次。对象 ETag 或版本变化、删除、撤权时拒绝旧内容，并清除本地预览缓存。PDF Range 读取绑定同一 ETag，读取失败会终止查看而不继续展示混合版本。

## 缓存与资源释放

内存和 IndexedDB 缓存按账号、工作区、文件类型/身份、修订标识和字节范围隔离。200 MiB 上限，七天未访问淘汰。每次打开、缓存读取和阅读期间均重新核验权限与对象修订；签名 URL 不作为缓存键。退出或切换身份/工作区清理缓存。关闭、切换文件或组件卸载时取消读取，释放图片对象 URL、PDF 任务和 Office 页面资源。

## 后台职责

文件预览不启动、轮询或等待资源转换任务。旧资源转换接口和 Preview Worker 已移除；历史 `resource_previews` 表及迁移记录仅作为历史数据结构保留，不再创建新的转换缓存或清理任务。它与云电脑启动的用户 HTTP 服务预览是不同功能，后者继续使用独立的 `preview` 网关。
