# 阿里云 DocMind PDF 解析验收

对应 Issue #27。目标是让扫描 PDF 和混合 PDF 进入现有的资料处理、检索和引用闭环。

## 接入边界

- 只有后端 Worker 读取 `ALIYUN_ACCESS_KEY_ID` 和 `ALIYUN_ACCESS_KEY_SECRET`。
- 新 PDF 在 `PDF_PARSER=aliyun` 时提交到阿里云 DocMind；其他格式继续使用本地解析器。
- 默认不开启 `LlmEnhancement`、VLM 和公式增强，避免未经验证的额外费用与延迟。
- 单份 PDF 默认最多 50 页，可通过 `DOCMIND_MAX_PAGES` 调整（上限 500）。
- 云端结果只在任务完成后写入本地，页面、块文本、Markdown 表格、页码和坐标保存在 chunk 来源元数据中。

## 任务语义

```text
本地上传 → 提交 DocMind → 保存远程任务 ID → 轮询状态 → 分批拉取 layouts
          → 规范化页/块 → 切分 → Embedding → 可检索
```

- `Document.parsing_metadata` 保存 provider、任务 ID、配置、状态、页数和结果页信息。
- 提交成功但响应不确定时标记为 `submitting`，重试禁止再次提交，避免重复计费。
- 已有任务 ID 的重试只继续查询原任务。
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

脚本只写入被 `.gitignore` 忽略的 `.env`，不会打印密钥。上传界面会提示用户 PDF 将发送到阿里云，并显示当前页数上限。
