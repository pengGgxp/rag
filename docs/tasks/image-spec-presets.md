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

## 线上部署（2026-11-07，用户追加授权）

- 发布代码：`28d1dbd`、`e551190`。更新 API、Worker、预览网关、预览 Worker 和 Web；无新增数据库结构迁移，不调整商业售价。
- 发布目录：`/root/rag-releases/image-specs-e551190`。数据库、环境配置、Compose、源文件与旧镜像标识保存在其 `backup/` 下，原镜像保留。
- 服务器 Web 构建因内存压力终止，改用本机按 `NEXT_PUBLIC_API_URL=https://zk.str1ct.top/api` 构建的产物打包；不在服务器继续运行编译。服务器内存恢复，服务切换后 API 健康检查通过。
- 新代码校验全部 11 个现存模型配置成功；切换前后 `site_model_providers.image_config` 的逐模型摘要一致。
- 公网检查：`/admin/models/new` 及其 15 个脚本资源返回正常，脚本包含新规格表单；匿名访问能力目录返回 401，短期管理员会话读取目录返回 6 个预设，现存模型列表可读取。检查后已删除临时会话。
- 当前发布使用原有 Compose 文件组合，最后叠加发布目录的 `release.yml`；相同配置已保存为项目目录的 `docker-compose.image-specs.yml`。
- 回滚：用相同 Compose 文件组合去掉最后的 image-specs 覆盖，重新创建 api/worker/preview/preview-worker/web 即恢复上一组保留镜像；如需恢复服务器源文件，使用 `backup/source.tar.gz`。本次无结构变更，不为应用回滚覆盖用户的新数据。
- 未调用真实生图模型，未进行付费接口测试；真实供应商能力仍未验收。
