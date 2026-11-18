# 支付真实来源 IP（Issue #124）

## 目标与边界

支付下单从可信代理链取得来源 IP，交给既有适配器签名并发送。只调整来源解析、API 启动约定及配置说明，不修改金额、配置版本、订单幂等、响应验签和回调入账。本次不部署、不修改现网配置、不实际付款。

## 仓库部署拓扑与信任边界

- `deploy/nginx/zk.str1ct.top.conf`：宿主机 Nginx 将 `/api/` 转到 `127.0.0.1:8000`，追加 `$remote_addr` 到 X-Forwarded-For。客户端预置的左侧内容不能作为来源依据。
- `deploy/testing/nginx.conf`：宿主机 Nginx 将 `/api/` 转到 `127.0.0.1:18180`，用 `$remote_addr` 覆盖 X-Forwarded-For。
- `deploy/compose.production.yml`、`deploy/testing/compose.yml` 和旧部署 overlay 仅将 API 端口绑定宿主机 loopback。API 通常看到 Docker 网络网关作为连接对端，不能把这个地址当成浏览器来源。
- API 镜像和上述两套 Compose 的启动命令显式使用 `--no-proxy-headers`。Uvicorn 不先行改写 ASGI `client`，支付解析和部署控制的 loopback 检查仍有真实连接依据。不得改成 `--forwarded-allow-ips='*'`，也不得在外层增加另一套先行改写 client 的中间件。

新生产域名不在已跟踪 Nginx 文件中；上述是仓库可确认的拓扑，不能替代线上有效配置验收。若实际入口增加 CDN/LB，须先核对每一跳的连接来源和头部覆盖/追加行为。

## 配置与解析规则

`PAYMENT_TRUSTED_PROXY_IPS` 在受保护的 `app.env` 中配置，使用逗号分隔的精确 IP 或必要的最小 CIDR，默认空（不信任转发头）。禁止 `*`、IPv4/IPv6 `/0`，错误配置在 Settings 加载时失败。不能为方便而信任整个 Docker/私网地址范围。开发 Compose 从同名环境变量透传；生产及测试 Compose 从原有 app.env 读取，不增加第二份运行配置。

部署时从目标 API 容器网络核对网关，不能复制历史环境的地址：

```sh
docker inspect --format '{{range $name, $network := .NetworkSettings.Networks}}{{$name}} gateway={{$network.Gateway}} ipv6gateway={{$network.IPv6Gateway}}{{println}}{{end}}' <api-container>
```

检查实际 Nginx upstream 与 API 的 loopback 端口绑定，并通过受控请求确认 API 的连接对端。只有确认该网关代表宿主机 Nginx，才将该精确地址加入变量；网络被重建后需重新核对。宿主机及其能访问 loopback API 的进程属于这条信任边界。若容器通过公网或不受控网络直连，应先恢复既有 loopback 绑定，不能把外部请求对端配置成代理。

多层代理仅添加真实受控的代理地址，最外层入口须覆盖来自客户端的头，或追加真实连接来源；不能原样透传客户端输入。若 Nginx realip 模块改写 `$remote_addr`，其 `set_real_ip_from` 也必须限定真实上游，不能信任所有来源。

支付解析行为：

1. 验证连接对端为 IP，未命中可信范围时完全忽略转发头。
2. 合并有序的 X-Forwarded-For 字段，从右向左移除可信代理，停在首个不可信地址；该地址左侧的内容不再读取。
3. 不通过 X-Real-IP 或 Forwarded 开辟其他信任路径，不接受请求 JSON 中的 clientip。
4. 缺失转发头，或尚需读取的一跳畸形时，返回原始连接对端；无有效对端时返回空。不能根据畸形头猜测真实来源。这些场景可能仍被平台拒单，须修复代理配置。
5. 支持裸 IPv4/IPv6，IPv4-mapped IPv6 规范化为 IPv4；拒绝主机名、端口、方括号形式、zone ID。不会把 IPv6 替换为网关或虚构 IPv4。

解析不记录完整用户 IP、不新增响应字段；普通界面、Issue 及日志不应附带原始签名请求、密钥或付款链接。

## 验证与上线验收

自动回归覆盖可信/不可信来源、客户端伪造左侧地址、多层链、重复头、缺失/畸形地址及 IPv4/IPv6；真实本地 TCP/Uvicorn 测试确认原始对端不会被头改写。下单路由测试核对传入适配器的来源，重复请求不会再次调用平台；适配器测试核对 clientip 包含在 RSA 签名内容中，现有金额、响应验签及回调回归继续执行。

2026-11-18 本地验证结果：

- 来源解析、支付和支付配置测试：103 passed。
- 独立 PostgreSQL 测试库从空库迁移到 `0076_explicit_cloud_computers` 后，支付并发、认证及部署控制测试：22 passed（其中支付 PostgreSQL 4 项，无跳过）。
- 部署配置测试 11 passed；另有两个原有失败：Settings 中两个 `PREVIEW_CACHE_*` 未列入 APP_KEYS，及测试引用不存在的 `preview-worker` 服务。已用 `git archive` 提取未修改的 `ed037b3` 相关文件复现相同失败，不扩大本 Issue 修复范围。
- `git diff --check` 通过。

仍需在授权部署时完成：

- 核对新生产及当前测试站的实际有效 Nginx/Docker/API 配置，填写并确认精确可信对端；代码发布本身不等于来源修复已启用。
- 两个站点真实浏览器页面下单并跳转收银台；在受控诊断中确认平台记录的 clientip 等于浏览器公网来源，不在公开 Issue 输出完整地址。未付款只报告下单与跳转。
- 核对平台 IPv6 接受情况。2026-11-18 访问仓库引用的 `https://www.ezfp.cn/doc/pay_create.html` 返回 404，尚无可靠依据声明平台已支持或不支持 IPv6；目前透传真实 IPv6，由平台真实验收确认，不伪造来源，也不擅自关闭 IPv6 访问。
- 验收结果和未覆盖限制更新 Issue；真实页面/代理链未验收时保持 Issue 开放。
