# 图片能力预设与价格配置

## 管理员流程

在模型管理的图片能力区域选择预设、调用协议，分别在“图片生成”和“图片编辑”中勾选开放规格、填写价格、选择默认规格。预设不选择供应商、不替换实际模型名，也不自动设定售价。Responses 仍需填写图片工具模型。

GPT 预设提供像素尺寸与质量；Gemini 提供宽高比与分辨率。规格选项带比例轮廓。价格支持当前操作批量设置与生成价格复制到编辑；复制会补齐缺少的编辑规格并明确确认价格替换。新规格价格为空，免费必须填写 0。默认规格必须存在且已定价。

切换预设先展示保留项数量、不兼容项及其价格、协议、数量限制和默认值变更。确认只修改草稿，最后仍需保存模型。移除已定价规格也需要确认。保存冲突沿用原有草稿保护。

不匹配预设的已有配置保持自定义，原参数、默认值及价格不变；完全匹配时提示可应用的预设，不在读取时写回或修改数据库。自定义尺寸和质量/分辨率在折叠的高级区域编辑。

## 后端接口与持久化

- `GET /site/admin/model-connections/image-presets`：仅站点管理员可读，无凭据、无价格。返回预设 ID、修订号、官方来源、核对日期、协议、操作、尺寸、质量/分辨率及平台数量上限。
- 模型 `image_config` 增加可空的 `preset_id`、`preset_revision` 和按 `generate` / `edit` 划分的 `defaults: {size, quality}`。沿用既有 JSON 字段，无数据库结构迁移。
- 未设置独立默认值时仍使用原 `default_size` / `default_quality`。旧 API 数据和历史任务无需改写。采用预设后服务端严格校验修订号、协议、规格和数量；自定义配置继续校验格式、重复项与价格。
- 外部任务与工具继续使用 `size` / `quality` 参数，Gemini 分别解释为比例 / 分辨率。可用工具只列当前开放规格；省略参数使用对应操作默认值。任务创建时冻结价格与适配参数，不受后续修改影响。
- Gemini 2.5 原生分辨率以 `native` 作为内部规格标记，任务快照记录省略分辨率；上游不发送不存在的 `imageSize: native`。其他 Gemini 分辨率按原生 `imageSize` 传递。历史任务仍按原快照执行。

## 官方依据（2026-11-07）

- OpenAI：<https://developers.openai.com/api/docs/guides/image-generation>
- Google：<https://ai.google.dev/gemini-api/docs/image-generation>
- Google GenerateContent 参数：<https://ai.google.dev/api/generate-content>

预设目录为 `backend/app/images/presets.py`，是前后端唯一参数来源。当前提供 GPT Image 1/1.5/Mini、GPT Image 2、GPT Image 2.5 Sunburst/Flare、Gemini 2.5 Flash Image、Gemini 3 Pro Image、Gemini 3.1 Flash Image。

GPT 2/2.5 仅预选常用及非实验性高清尺寸，自定义尺寸可切换高级配置。2.5 提供 low/medium/high/xhigh/max/auto，较早系列不提供 xhigh/max。Gemini 2.5 不发送分辨率参数；3 Pro 提供 1K/2K/4K；3.1 Flash 另提供 512 和扩展比例。平台仍限制一次最多 4 张（原生协议 1 张）、最多 8 张参考图、最多 100 个定价组合；这是本平台支持范围，并非供应商全部上限。

官方目录不证明中转供应商兼容；后台保留测试入口，真实测试会收费。本次不调用真实模型。未在当前适配器中核实的其他型号可使用自定义兼容配置，不提供未经核实的预设。

## 验收

本地隔离数据及模拟上游覆盖预设合法组合、默认值、权限、配置冲突、旧配置、Gemini 请求参数及价格快照。浏览器使用独立 SQLite 与合成账号，不接入业务数据库、真实模型或生产服务。验收截图位于 `output/playwright/image-spec-*.png`。线上部署与真实供应商验证另行执行。
