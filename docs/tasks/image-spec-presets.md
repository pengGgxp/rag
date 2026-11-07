# 生图规格选择

- 目标：GPT Image 与 Gemini 通过后端能力目录选择规格，批量配置价格，保留已有配置与账务。
- 范围：管理员模型表单、能力校验、默认规格、请求参数、文档与模拟测试。
- 不包含：线上部署、真实付费生图、价格调整、增加图片模型选择入口。
- 验收：预设与自定义均可保存；切换预设明确确认差异；生成/编辑独立默认；非法规格在请求前拒绝；旧配置无隐式修改。
- 验证：后端回归、构建、1440/700/390px 浏览器交互和截图、git diff --check。

官方依据及核对结果见 image-spec-presets.md（docs 根目录）。

## 实施状态

- 后端能力目录、参数校验、独立默认值与任务快照已实现；已有 JSON 配置无需迁移。
- 后台预设、规格选择、批量价格、切换差异及自定义高级区已实现。
- 无真实模型调用；未部署线上，真实供应商能力保持未验收。

## 本地验收（2026-11-07）

- `pytest tests/test_image_presets.py tests/test_image_tasks.py tests/test_image_adapters.py tests/test_model_connections.py tests/test_shared_providers.py -q`：57 passed。
- `NEXT_DIST_DIR=.next-image-specs npm run build`：通过。
- Playwright：GPT/Gemini 各 1440/700/390px 截图已逐张检查；修正原生价格输入框样式与确认框焦点恢复。配置保存与刷新、批量价格、复制价格、预设切换取消、409 草稿保留、页签未保存保护、删除确认、自定义高级区及未定价阻止提交通过。
- 测试使用独立本地账号、SQLite 和模拟 HTTP 上游；供应商真实可用性未验证。Next CSS 预加载警告存在，无页面运行错误（409 为主动模拟）。
- 未执行线上迁移或部署；本次没有数据库结构变更。
