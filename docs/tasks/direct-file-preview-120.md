# Issue #120：全站文件直连与前端预览

## 交付范围

OSS 保持私有。应用 API 只做身份、工作区、附件或知识库权限检查，读取对象元数据并签发五分钟短期链接；文件正文由浏览器直接从 OSS 读取。预览不调用下载预留、不扣用户积分或下载额度；用户主动下载仍走原有鉴权和实际发送字节计量。

资源库、聊天成果和附件、Skill 工作区文件、知识库原文件共用同一查看器和缓存。聊天图片在消息中直接显示，点击后打开完整查看器。知识库原文件和已有解析文本是两个独立入口。

## 接口契约

- `POST /resources/{id}/preview-access`
- `POST /chat/attachments/{id}/preview-access`
- `POST /documents/{id}/preview-access`
- `GET|HEAD /documents/{id}/download` 沿用现有下载计量链路

预览请求只接受可选 `revision`，拒绝对象键、上游地址和其他字段。响应包含文件名、内容类型、渲染类型、大小、修订标识、ETag、条件读取头、短期链接和过期时间。知识库链接绑定具体文档版本。签名 URL 不写入消息、普通日志或持久缓存。

生产 OSS CORS 必须允许实际 Web Origin 的 `GET`、`HEAD`、`Range`、`If-Match`，并暴露 `ETag`、`Content-Range`、`Accept-Ranges`、`Content-Length`。代码和本地验收不能替代线上真实 Origin 验证。

## 格式支持矩阵

| 类型 | 浏览器处理 | 限制与失败行为 |
| --- | --- | --- |
| PNG/JPEG/WebP/GIF/AVIF/BMP | 直接读取、原比例、缩放和拖动 | 无法读取时提供重试和下载 |
| PDF | PDF.js 64 KiB Range 分段读取、分页、缩放 | ETag/Range 不一致时停止并要求重新打开 |
| DOCX | `docx-preview`，浏览器内分页 | 原文件不超过 50 MiB，ZIP 解压总量不超过 150 MiB、最多 4000 项 |
| PPTX | `@jvmr/pptx-to-html`，浏览器内分页 | 同上；复杂排版允许差异 |
| Markdown、文本、常见代码 | 纯文本、长度限制 | Markdown 不执行 HTML；超长内容提示下载 |
| `.doc`、`.ppt`、加密、损坏或超限文件 | 不在浏览器转换 | 明确说明原因并保留下载入口 |
| HTML、SVG 等主动内容 | 作为惰性文本 | 不作为知库同源页面执行 |

Office 页面放入无脚本、无外部连接的受限 iframe；解析依赖按需加载，关闭文件时取消读取并释放对象 URL、PDF 任务和 Office 页面。

## 缓存与权限

缓存键由账号、工作区、文件身份、修订和字节范围组成，不使用签名 URL。内存与 IndexedDB 共用 200 MiB 上限和七天淘汰；每次打开、命中和阅读期间均先核验权限及当前修订。退出、切换账号/工作区、撤权或删除会清理相关缓存，链接过期最多续签一次。

## 验收记录

隔离本地容器使用临时账号、独立工作区和真实私有 OSS 对象完成浏览器验收；未调用真实模型或云电脑，也未改动生产数据库。覆盖 1440、700、390px、中文文件名、图片、三页 PDF、DOCX、PPTX、Markdown、旧格式、损坏文件、Skill 文件和知识库原文件。检查了 OSS 直连、Range、凭据不随请求发送、预览不改积分/下载额度，以及显式下载仍进入计量接口。

前端测试 49 项通过；新增后端预览授权与 PostgreSQL 测试 5 项通过；前端类型检查、生产构建和 `git diff --check` 通过。综合后端回归为 54 通过、1 个失败；失败来自基线中的 `test_chat_attachments.py::test_compaction_treats_images_as_images_and_retains_original`，在未修改的基线提交上可复现，与 Issue #120 无关。

浏览器截图位于本地验收目录 `output/playwright/issue120/`，覆盖四种主要文件类型和三种视口；截图不作为产品运行时依赖。显式下载使用带认证上下文的浏览器请求验证响应正文和实际计量接口；本地跨端口环境的直接 `fetch` 受 CORS 流式响应限制，因此生产发布前必须用真实 Web Origin 完成下载按钮的浏览器验收。

## 未验收与发布前检查

1. 生产 OSS CORS 尚需使用 `https://zk.str1ct.top` 的真实浏览器 Origin 做 GET、HEAD、Range、If-Match 和暴露响应头验收。
2. 目前只对小型 Office 样例做内容可读验证，未把大于限制的压缩炸弹、加密文件和复杂桌面排版宣称为完全兼容。
3. `next@16.4.2` 构建存在既有安全公告；本次未做无关依赖升级。
4. 合并前需重新运行生产构建和相关后端回归，并在 PR 中附上格式矩阵、截图与上述未验收项目。
