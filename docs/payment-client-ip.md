# 支付真实来源 IP 与发布校验（Issue #124、#133）

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

应用解析器的通用能力不变。受管部署采用更窄的拓扑契约：一个本地 NAT bridge、一个精确网关 host 地址、API 仅发布一个 IPv4 loopback 端口。多层代理由受控 Nginx 覆盖或追加真实来源，必要的 CDN realip 信任保留在 Nginx，不扩大支付白名单。

## 发布流程中的一致性约束

`deploy/payment_proxy.py` 不修改配置、不学习请求来源。生产 release 包和独立测试 release 包均携带同一校验器，并纳入包校验和。生产 shadow 的运行检查、正式切换、启动后复核、自动及手动回滚均执行校验；测试发布在停止应用/备份/迁移前和启动后执行校验。

校验依次核对：

1. Compose 渲染后的 API 必须直接使用 Uvicorn 和 `--no-proxy-headers`，不可用自定义 entrypoint 改写启动行为；只绑定 `127.0.0.1:<port>:8000/tcp`，只加入一个明确命名的网络。
2. 所选网络必须是本机 NAT bridge；`PAYMENT_TRUSTED_PROXY_IPS` 只能包含其一个精确网关（裸 IP 或 host CIDR），不能留空、使用旧网络地址、增加其他代理或信任整个子网。
3. 有效 `nginx -T` 以及生产候选 Nginx 配置中的目标 HTTPS server、`/api/` upstream 和 XFF 覆盖/追加规则必须匹配。隐藏 include、改写 API 路由、改变连接来源的 `proxy_bind`、覆盖 API 的正则路由或全信任 realip 均不能作为已验证拓扑通过。首次接管维护期间检查候选，安装完成后再复核有效入口。
4. 使用同一 release 镜像启动短暂探针，不挂载文件、数据库或业务环境，只传入可信地址，随机绑定一个 loopback 端口。宿主机真实 TCP 对端必须等于网络中的预期网关，镜像内原始来源解析必须正确；观察到其他来源只报错，不加入白名单。
5. 另一临时容器从同一 bridge 直接访问探针并伪造 XFF、X-Real-IP、Forwarded，其解析结果必须仍是原始连接来源。API 启动后还核对实际镜像、命令、可信配置、端口绑定和网络 ID，避免只验证候选配置或普通健康检查。

探针结束后删除自身容器，普通成功输出不含 IP、凭据、签名请求或支付链接。它证明受测连接的行为，不把 Docker 网关解释成 Nginx 的独有身份：宿主机及能访问 loopback 的本机进程仍属于现有信任边界。Docker/宿主机管理权限也可以改变这条边界，不能向不可信任务授予。

首次部署先由操作者明确创建或选择目标 bridge；校验器不会因名字拼错自动创建网络。网络重建、换宿主机或恢复备份后，即使 `/health` 为 200，旧配置也会阻止发布。读取所选网络元数据：

```sh
docker network inspect <compose-resolved-network-name>
```

核对目标网络、Nginx upstream 和 loopback 绑定后，备份受保护的 `config/app.env`，仅按目标网络的精确网关修正该变量并重试。不要复制任何历史网关，不要把探针报出的陌生来源加入白名单。若连接来自公网、不受控入口或其他网络，应修复拓扑，不能扩大信任。

需要单独诊断受管 release 时，在其宿主机运行：

```sh
python3 <verified-release>/payment_proxy.py \
  --compose <release>/compose.yml \
  --env-file <release>/.env.release \
  --candidate-nginx <release>/nginx/syfc.moonlitstarway.top \
  --running
```

独立测试包使用其 `.env`，省略 `--candidate-nginx`（测试发布不安装 Nginx）。此命令运行无业务凭据探针，不下单、不收款，也不更改 Nginx。它要求本机 Linux Docker 和有效 Nginx；Docker Desktop、rootless 或其他连接来源不符合契约时明确失败，不自动降级为宽松模式。

### 备份与失败恢复

- 发布前保留受保护的 `config/app.env`、当前及 previous release 配置、Nginx 配置和网络 inspect 记录，目录权限 700、文件 600；不要将含环境变量的 Compose 渲染结果上传到公开记录。既有发布流程继续负责数据库备份、迁移及恢复策略。
- 切换前失败不会启动候选服务或改变 current；按错误指出的配置/拓扑修复后重新发布，不跳过校验。
- 切换后失败沿用现有回滚流程；回滚先检查 previous 的保存配置与当前网络，失配时阻止切换。网络已经变化时，先备份 previous 的 `app.env`，只修正其中的可信网关并同步受保护配置，再用当前已校验的发布工具重试回滚。不要改旧 manifest、镜像 digest 或数据库回滚策略来让检查通过。
- 不要通过旧版、不含此校验的发布脚本绕过检查。无法恢复原拓扑或正确配置时保持维护状态，依据既有恢复说明处理；独立测试发布失败保留备份和失败记录，不自动降级数据库。

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

## Issue #133 验证记录（2026-11-18）

开发基线为 `3ec6f1c5a4f7003cbb3674cdbe1653267ffa9da8`，未部署本次改动。

- 只读核对了生产 `syfc.moonlitstarway.top` 和测试 `zk.str1ct.top` 的有效 Nginx、Docker 端口、API 启动命令及各自配置。两套当前 API 均只绑定宿主机 loopback，保留原始 ASGI 来源；测试站另有受限 CDN realip 配置。通过保持一个受控的未完成健康请求并比较容器 TCP 连接，分别确认原始 TCP 对端匹配各自的精确信任值。未读取业务文件或输出用户 IP。
- 从本任务工作树构建 API 镜像，在本地独立 Docker-in-Docker 中执行真实 Nginx/TCP 验证：首次部署、单层 Nginx 的真实来源、同桥伪造转发头、API loopback 端口不可经网关访问、网络重建及网关变化、空值/旧值阻断、普通 HTTP 成功不能掩盖旧值、重新发布、旧回滚快照阻断、明确修正隔离快照后的回滚目标启动及复核。
- 可重复的容器验收入口为 `deploy/tests/test_payment_proxy_containers.py`。默认跳过，只有在独立 Linux Docker/Nginx 环境设置 `RUN_PAYMENT_PROXY_CONTAINER_TESTS=1`、`PAYMENT_PROXY_TEST_IMAGE=<本工作树构建的镜像>` 后才执行。该验证使用来源解析探针，不运行数据库迁移、业务订单或真实发布的整套维护事务；事务顺序及阻断行为另有单元测试。
- 支付来源/路由/配置原有 103 项回归通过，保留 IPv4/IPv6、重复头及多层代理测试。部署全集原有两个失败已在精确基线的独立副本复现：配置目录缺少两个 `PREVIEW_CACHE_*`、测试引用已不存在的 `preview-worker`；不在本 Issue 扩大修复范围。
- 新增代理与发布阻断测试 14 项通过，原有发布/回滚测试 42 项及生产打包测试 3 项通过。Windows 部署全集共 106 项：102 通过、2 跳过（Unix 权限测试及默认关闭的隔离容器测试）、2 个上述基线失败；隔离容器场景另行启用并通过。Python 编译与 `git diff --check` 通过。
- 当前生产及测试域名的真实受控下单、适配器与平台来源匹配、收银台跳转和平台 IPv6 接受情况仍未验收。本任务未部署、未改线上配置、未重启服务、未实付；不把探针或单元测试称为支付全链路通过，Issue 保持开放。
