# 阿里云 DocMind PDF 解析验收

对应 Issue #27。目标是让扫描 PDF 和混合 PDF 进入现有的资料处理、检索和引用闭环。

## 接入边界

- 只有后端 Worker 读取 `ALIYUN_ACCESS_KEY_ID` 和 `ALIYUN_ACCESS_KEY_SECRET`。
- 新 PDF 在 `PDF_PARSER=aliyun` 时提交到阿里云 DocMind；其他格式继续使用本地解析器。
- 默认不开启 `LlmEnhancement`、VLM 和公式增强，避免未经验证的额外费用与延迟。
- 单文件上传上限为 200 MB（209715200 字节），不再限制 PDF 总页数；旧的 `DOCMIND_MAX_PAGES` 已移除，环境中的遗留值不再生效。
- PDF 以 100 MiB、500 页为内部提交预算，必要时自动按页拆分；这是保守的分批策略，不是整份文档限制，也不是对供应商容量的承诺。重写后再次检查批次大小，共享资源导致体积增大时继续二分。单页无法继续拆分时提交该页，由供应商返回实际处理结果。
- 云服务自身仍可能因文档损坏、单页容量、额度、超时等原因拒绝处理；项目不能取消供应商限制，失败保留可重试状态，不静默丢页。
- 云端结果只在任务完成后写入本地，页面、块文本、Markdown 表格、页码和坐标保存在 chunk 来源元数据中。

## 任务语义

```text
本地上传 → 提交 DocMind → 保存远程任务 ID → 轮询状态 → 分批拉取 layouts
          → 规范化页/块 → 切分 → Embedding → 可检索
```

- `Document.parsing_metadata` 保存 provider、任务 ID、配置、状态、页数和结果页信息。
- 提交成功但响应不确定时标记为 `submitting`，重试禁止再次提交，避免重复计费。
- 已有任务 ID 的重试只继续查询原任务。
- 分批任务逐批保存任务 ID 和已完成结果，重试复用成功批次；显式拒绝允许重新提交对应批次，不确定提交阻止再次付费。
- 分批结果重新组装为原 PDF 页码和全文偏移，保留块坐标；OCR 仍按整份 PDF 页数结算，解析缓存复用不重复结算。
- 删除文档或取消任务时，Worker 不得写入解析结果或创建 Embedding 任务。
- DocMind 错误会被清洗，不把 AccessKey、签名 URL 或原文回显到 API、日志和任务错误。

## 验证

- 后端：82 项测试通过。
- 前端：`npm run build` 通过。
- Alembic：`0014_add_parser_locations` 已执行。
- Docker：API、Worker、数据库、Redis、对象存储健康。
- 真实云端冒烟：两页 PDF 成功返回两页文本和两个版面块，页码与坐标进入规范化结果。

## 使用

将阿里云凭证文件交给本地脚本：

```powershell
.\backend\.venv\Scripts\python.exe tools/configure_docmind.py C:\path\to\aliyun-credentials.txt
docker compose up -d --build api worker
```

脚本只写入被 `.gitignore` 忽略的 `.env`，不会打印密钥。上传界面提示用户 PDF 将发送到阿里云，不再显示页数上限。

## 200 MB 上传变更（Issue #35）

- 前端和 API 均允许恰好 200 MiB；API 逐 MiB 扫描上传临时文件并计算摘要，再将原文件流上传对象存储，避免再次复制整个文件。
- 浏览器文档上传等待上限 30 分钟，其他请求保持原有超时设置。
- Nginx 请求体上限 201 MiB，为 multipart 留出空间；API 始终执行精确的单文件 200 MiB 检查。接收请求体与转发发送的空闲超时为 600 秒。
- 原始文档上传和版本上传使用同一套大小校验、权限和去重逻辑；不改变 Skill 包限制。
- 自动测试覆盖真实临时文件的 0/200 MiB/200 MiB+1 字节、501 页、50000 以上版面块、分批重试与原页码引用。浏览器脚本为 `scripts/check-document-upload.js`，只针对本地专用验收知识库执行。
- 供应商官方容量页面本次未能正常读取；不把模拟解析或小样本验收等同于真实 200 MiB 云端处理通过。
- 2026-10-26 验证：本地浏览器实际上传 209715200 字节文件成功存入对象存储，延迟响应超过旧 15 秒阈值仍成功；测试任务已取消。200 MiB+1 字节在前端拒绝，1440/390px 无横向溢出。阿里云两个独立一页样本实际提交与采集成功，合并页码和字符偏移通过断言；真实 200 MiB PDF 云端全链路仍待验收。
- 同日已部署 API、Worker、Web。线上 HTTPS 209715200 字节 multipart 请求穿过 Nginx 并得到正常的未登录 401 响应；未伪造线上管理员会话。服务器 API 上限确认 209715200 字节，旧页数字段已不存在，Worker ping 成功，`.env` 校验及保存的认证配置摘要一致。最初部署脚本的首次 Worker ping 早于就绪而失败，随后手工复查成功；脚本已补充就绪重试。

## 部署与回滚

`scripts/deploy-document-upload.sh` 更新 API、Worker、Web 与知库 Nginx 请求体设置；保留现有 Compose、`.env`、认证配置和数据服务。旧 Compose 中残留的 `DOCMIND_MAX_PAGES` 被新代码忽略，无需修改服务器业务密钥。

部署前备份保存在 `/data1/rag-releases/document-upload-20261026`，包括数据库、环境、源代码、Nginx 和三个服务镜像。回滚时恢复 `source.before.tar.gz`、`nginx.before.conf`，将 `images.before` 中镜像重新标记为当前服务标签，再以 `--no-build --no-deps` 重建三个应用容器并检查健康。此次无数据库迁移，不恢复数据库来回退此功能。
