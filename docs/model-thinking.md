# 模型思考配置与请求选择

关联 Issue #44、#41、#43。

## 配置归属

站点管理员在模型连接配置内维护思考能力，使用独立能力版本。
未配置与不支持均不发送新增参数；现有模型不自动推测或开启思考。
固定思考不可切换；开关、等级、预算使用该模型实际支持的选项。
显示名称只服务于该模型，不代表跨模型等效深度。

DeepSeek 仅使用真实等级 low/high/max，不把映射别名展示为不同等级。
该适配器的 disabled 选项表示关闭思考，不发送 reasoning_effort。
OpenAI 使用管理员确认的模型支持子集，不假设 Chat Completions 支持所有工具组合。
百炼预算范围须按具体模型填写；固定开启的模型不发送 enable_thinking。
仅流式百炼模型使用同一 Chat Completions SSE 协议聚合模型响应和工具参数，
保留最终 usage，思考内容不作为回答输出。

## 请求边界

普通模型列表只返回类型、选项显示信息、默认选项、能力版本。
客户端发送 option ID 和版本，服务端校验并映射参数，禁止传任意上游参数。
省略选项使用管理员默认值；非法选项返回 422，过期版本返回 409。
请求开始后固定能力配置；后台改配置不改变已开始的 Agent 轮次。

参考官方文档：

- https://api-docs.deepseek.com/guides/thinking_mode/
- https://www.alibabacloud.com/help/en/model-studio/deep-thinking
- https://developers.openai.com/api/docs/guides/reasoning

## 验收记录

- 能力和参数适配：57 项相关后端测试通过。
- Agent、界面、数据库迁移、真实上游与部署验收待后续提交记录。
- 不调整商业价格、工作区倍率、默认模型或 Issue #36 开关。
