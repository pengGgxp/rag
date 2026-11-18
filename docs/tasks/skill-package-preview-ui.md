# Skill 包预览界面修正

## 目标

修复工作区“添加 Skill”页上传多个 Skill 后的预览问题：YAML 清单不应重复显示为正文；长列表应在预览区域内滚动；批量导入应避免逐个刷新和提交，并为 ZIP 解析及导入使用符合大包任务的请求等待时间。

## 范围

- 工作区 Skill 包预览和导入。
- 站点管理端复用相同的包预览正文字段。
- Skill 上传预览/导入请求时限。
- 后端个人 Skill 批量导入及相应用例。

## 不包含

- 修改 Skill 包格式、现有 200 MiB 容量上限或 ZIP 条目数量策略。
- 修改文件权限、路径安全和存储迁移行为。
- 使用 Figma 修改原型。

## 验收

- 有效 YAML frontmatter 的 `name` / `description` / `metadata` 不出现在正文预览中，正文内容完整保留。
- 多 Skill 个人导入通过一次批量工作区写入完成，保留唯一目录名和资源文件。
- 预览/导入长耗时请求不在 120 秒时被前端提前中断。
- 长 Skill 列表和 Skill 正文在预览窗格内滚动，窄屏仍可读。
- 后端相关测试、前端测试、类型检查和构建通过；验证限制如实记录。

## 验证

- Edge 页面已复现：识别出 24 个 Skill，20 个默认勾选；预览正文包含 `name`、`description`、`metadata` YAML，并保留一条连接超时错误。页面当时的上传按钮已恢复为空闲态。该页面是线上现有版本，未执行导入或其他写操作。
- 后端 `test_skill_uploads.py` 与 `test_skill_resources.py`：20 项通过；排除 1 项调用 Bash 的 Windows 不兼容用例。
- 前端 `npm test`：52 项通过；`npx tsc --noEmit` 通过。
- 设置 `NEXT_PUBLIC_API_URL=http://localhost:8000` 后 `npm run build` 通过。
- `git diff --check` 通过。新版本尚未部署，因此没有声称线上 UI 已更新。
