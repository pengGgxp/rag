# 模型调用协议

## 多供应商升级（Issue #75）

连接的 `api_protocol` 仅作为新模型默认值；每个模型以 `chat_protocol` 明确配置
Chat Completions、Responses、Anthropic Messages 或 Gemini 原生协议。
修改连接默认值不重写已有模型。0054 迁移逐个复制原连接协议，保留模型 ID、价格及默认选择。

原生协议分别编码消息、图片、工具结果和思考参数；签名与原生内容继续走服务端加密续接，
不向浏览器暴露。流式完成标识缺失时不执行未完成的工具调用；Token 用量进入原计费链路。
Anthropic 使用 Messages 预算思考，Gemini 使用 thinkingConfig；不透传其他厂商的思考参数。
原生模型发现使用各自认证头，失败仍允许手工添加；发现成功不代表能力验证通过。

`supports_chat` 与图片理解、生成、编辑分别设置，不以模型名推断。图片专用模型不进入对话选择。
图片协议和任务计费见 [图片任务](image-tasks.md)。以下为升级前的历史实现和验收记录。

Issue #52，关联 #51、#44、#45。

## 目标与边界

新增供应商先明确选择 OpenAI Chat Completions 或 OpenAI Responses，之后才填写平台、
名称、地址和凭据。调用类型属于共享连接，同连接下的模型共用；模型思考能力和价格仍独立。
本次不改价格、倍率、默认模型、计费开关、工作区模型选择或 OCR/Embedding。

## 配置与运行

- `api_protocol` 必填，只接受 `chat_completions`、`responses`；界面无隐含初始选择。
- 地址可填写基础地址或与选择一致的完整端点；不同类型的完整端点返回 422，
  不根据地址推断类型，不自动换协议或重试生成。模型发现从基础地址读取 `/models`。
- 已有连接经 `0034_model_api_protocol` 明确记为 Chat Completions，保持原调用方式。
- 新请求固定连接版本和调用类型；管理员中途修改不改变已开始请求。
- Responses 使用 `input`、扁平函数工具、`function_call_output`、`reasoning.effort`
  及 `max_output_tokens`；不沿用 Chat Completions 的字段，也不默认添加采样参数。
- Responses 当前只允许未配置、不支持或 OpenAI 思考适配器；连接切换、模型保存、
  请求解析和参数预览均校验，不静默丢弃 DeepSeek/百炼参数。
- 请求 `store: false`，由服务端续传上下文；不会依赖上游保存会话或 `previous_response_id`。
  需要的加密推理条目与工具调用组只存入现有加密续传字段，按模型、协议和连接隔离。
- 不将 Responses 私有推理条目转为展示思考，不写入公开消息、日志、审计或计费元数据；
  压缩输入排除这些条目。普通回答仍由正文、工具活动及现有引用组成。
- 真实流式正文立即透传，工具参数以完成响应校验为准，未完整生成不执行工具。
  `response.completed` 中的累计用量进入统一解析，缓存和推理用量不重复相加；
  incomplete/failed 保存其可靠用量。一次性 JSON 返回记录原有诊断，不伪造逐字流。

## 验收

- 通过真实分时本地 HTTP 流检查早到正文、工具续传、断流及最终用量。
- 检查普通/工具/异步流使用同一协议，非法参数、端点不符、权限和版本冲突。
- 通过完整会话检查多轮工具、加密历史恢复和两种现有结算分支。
- PostgreSQL 检查原有数据迁移、重复升级、并发修改及配置保留。
- Playwright 在 1440/700/390px 检查先选类型、保存、刷新、错误保留、键盘、
  未保存确认及 reduced motion；截图在 `output/playwright/issue52-*`，不提交产物。
- 真实 Responses 上游验收须使用管理员明确配置的连接，不擅自切换现有模型协议。
  未完成真实验收不关闭 Issue。

本次网络访问 OpenAI 官方文档被访问挑战阻止，未将未读取的页面作为已核实依据。
真实上游兼容性仍须与实际连接的官方规格及调用结果核对，尤其是网关支持的字段子集。

2026-10-29 本地验证：

- Windows 后端回归 494 passed、27 skipped，排除一个已知 Windows ZIP 路径归一化用例；
  后续增加的 Responses 真实 HTTP API 流式/断线用例及相关聚焦回归 57 passed。
- 专用 PostgreSQL 迁移、并发编辑、用量与快照回归 11 passed。
- 前端生产构建通过；Playwright 三种宽度、键盘、刷新、错误恢复、放弃确认
  和 reduced motion 通过，已人工检查桌面及手机截图。
- 本地模拟上游覆盖真实 HTTP 分时传输，不代表真实 OpenAI 或网关账号验收。

2026-10-29 部署验证：

- 候选 Linux 镜像完整回归 497 passed、27 skipped；API/Web 已更新，
  数据库为 `0034_model_api_protocol`。已有三个连接均保留 Chat Completions。
- 已备份数据库、配置与旧镜像；`.env`、Compose 校验一致，
  Worker、数据库、Redis 和原存储容器未重建。
- 隔离工作区实测 `deepseek-flash`、`qwen3.8-max-0902` 的模型发现、受控参数测试、
  流式正文、思考、工具调用、加密恢复与用量记录通过；配置不变量一致，
  临时验收账号已撤权。未调用新增的第三个模型。
- Responses 尚无真实连接验收，Issue #52 保持打开。
