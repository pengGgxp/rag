# 历史 fsl1 独立测试环境

> 2026-11-18 更新：当前测试服务器已由用户明确为 `103.97.177.61`，
> 沿用 `https://zk.str1ct.top`。本文记录此前 fsl1 的独立测试站，
> 其中目录、域名、数据卷和功能停用项不作为当前部署依据。
> 当前生产入口为 `https://syfc.moonlitstarway.top/`；测试入口为
> `https://zk.str1ct.top/`。
> 当前目标见 [测试发布文档](../deploy/testing/README.md)。

本环境用于 PR #119 的用户验收，与生产发布流程分开。目标服务器为 fsl1，
根目录 `/opt/zhiku-test`。Nginx 主站和预览使用 HTTPS 443，80 保留证书验证与 HTTPS 跳转，应用、数据库和 Redis
不直接暴露公网。

## 访问

目标入口为 `https://test.zk.moonlitstarway.top`，不需要 SSH 隧道。云安全组需要放行
TCP 80 和 443。HTTP 请求以 308 跳转至同路径 HTTPS，证书验证路径除外。
使用独立测试账号，不使用生产账号密码。
`FRONTEND_URL=https://test.zk.moonlitstarway.top`、
`API_PUBLIC_URL=https://test.zk.moonlitstarway.top/api`，
测试环境的 `AUTH_COOKIE_SECURE=true`。生产配置不变。

原来的本机隧道入口已停用。测试账号和配置保存在忽略的
`output/fsl1-test/`，不要提交其中的密钥或凭据。
Nginx 拒绝公开访问 `/api/internal/`。

成果预览已恢复 `PREVIEW_ENABLED=true`，
`PREVIEW_DOMAIN=test-zk-preview.moonlitstarway.top`。这仅表示功能开关与生产一致：
2026-11-17 已签发并安装匹配证书，真实域名 HTTPS 和未授权拒绝验证通过；
真实任务的票据兑换、Cookie、刷新和结束运行仍待验收。
网关仍执行票据、会话、工作区及所有权校验，不降低鉴权要求。
`deploy/testing/nginx-preview.conf` 对应的独立 HTTPS 网关配置已在服务器启用，
证书与私钥路径链接到独立的 Certbot 证书目录。

## 构建与启动

从已提交源码在本机构建 `deploy/testing/Dockerfile.*`，记录完整提交号；
这些测试 Dockerfile 使用本机已有依赖层，不能代替生产构建契约。
Web 使用同源 `/api`，并在侧栏显示 PR 与构建提交。服务器只加载/拉取镜像。

将 `deploy/testing/compose.yml` 与 `nginx.conf` 放在
`releases/<提交号>/`，机密放在 `config/app.env`（权限 600）。
Compose 插值变量由同目录 `.env` 提供。

启动顺序为数据库和 Redis、一次性 `migrate`、配置导入与测试账号初始化，
最后启动 API、两个 Worker、预览网关和 Web。Nginx 配置通过 `nginx -t`
后加载。只为该测试项目创建 Docker volumes，不采用生产 volumes。

## 隔离与验收

从生产只读导出实际生效的环境配置及站点、搜索、认证、支付、充值、模型、
图片、云电脑和对象存储配置；不导出用户、聊天、文档、订单和余额。
凭据在测试环境使用新密钥重新加密，支付与充值配置的创建者映射到测试管理员。
测试用户与工作区使用全新 UUID，OSS 文件使用新工作区前缀。
数据库、Redis、队列及缓存卷独立。完整对照见
[配置与验收记录](private-test-alignment.md)。

邮件和支付配置按生产保留，不主动发信或创建真实订单。主站切换 HTTPS 后，
充值目录接口 200、启用状态为 true，返回 3 个套餐；尚未验证真实订单与支付回调。
题库由用户在 2026-11-17 明确要求不启用：连接参数为空、测试账号入口关闭，
不部署测试题库，不连接生产题库。

启动和健康检查不调用收费模型或创建沙箱。真实模型和沙箱仅由用户在测试
页面明确发起任务时调用，会产生供应商费用；测试积分不是实际充值。

验收分别记录配置一致性、源站实际行为、公网浏览器流程和收费任务。
登录接口通过不等同于浏览器登录完成；预览开关开启不等同于 HTTPS 入口可用。
停止测试使用本项目的 `docker compose stop`；不要执行全局 prune。

## 2026-11-17 原域名修正与验收记录

主站改为公网 8080，旧隧道进程及其服务端专用授权已移除。
切换前的 env 和 Nginx 配置保存在服务器
`/opt/zhiku-test/backups/before-public-8080/`，目录权限 700。
本轮修改前额外备份 env、Nginx 和完整测试数据库至
`/opt/zhiku-test/backups/before-alignment-20261117/`，目录权限 700。
Xterminal 已实际调用成功，域名 env 和 Nginx 已远程应用，读取 env 的
API、Worker、预览 Worker 和网关已重建。应用镜像提交仍为
`233a4524c8ce30144c5e7201d0b198df842ff884`，本轮未重新构建应用。

已验证：七个服务运行、health/ready 和两个 Worker 心跳正常、`nginx -t` 通过；
测试域名 Origin 的源站登录返回 200，错误 Origin 返回 403；4 个对话模型可用，
资源目录、会话及积分接口正常，测试余额 1000、支出 0。通过真实 OSS 上传、
幂等重试、下载、删除验证，测试文件已删除；匿名访问 401，删除后下载 404。
预览网关对缺失或无效授权返回 401。

公网验收未通过：Playwright 访问 `/auth/login` 及部分静态资源返回
`403 Non-compliance ICP Filing`，响应正文指向阿里云备案拦截页；
源站相同页面和资源返回 200。首页、version、health 偶有 200 不能替代登录验收。
最新外部复查时，health 和 version 同样返回备案拦截 403。
未改用隧道、IP 登录或其他入口规避此阻塞。

预览泛域名已解析，但 HTTPS 握手失败，fsl1 现有证书只覆盖旧 `.localhost`
域名；生产预览证书只覆盖 `*.preview.str1ct.top`，不覆盖测试预览域名。
本轮未复制证书私钥，未部署不匹配的证书，未创建收费沙箱验证预览。
浏览器登录、真实问答、邮件发送、微信回调、支付订单及真实成果预览均未完成验收。
PR #119 和 Issue #118 保持打开，不合并 PR、不部署生产。

## 2026-11-17 新域名与 80 端口

用户确认改用已备案域名：主站 `test.zk.moonlitstarway.top`，
预览 `*.test-zk-preview.moonlitstarway.top`，主站使用 80 端口。
本次范围为更新测试环境域名、签发独立预览证书和恢复公网验收；
不修改生产、不启用题库、不放宽预览鉴权和支付 HTTPS 校验。
验收包括真实域名浏览器登录、跨来源拒绝、预览 TLS 与未授权拒绝；
证书签发不等同于真实收费沙箱任务验收。

切换前配置备份在
`/opt/zhiku-test/backups/before-moonlitstarway-20261117/`。
已更新 Nginx、三个域名环境变量并重建 API、两个 Worker 和预览网关。
新主站登录页和 API health 通过域名 HTTP 返回 200，公开 internal 路径返回 404，
Nginx 当前监听 80，已移除 8080 监听。
Playwright 已通过正常页面输入测试账号并登录，登录请求 200、知识库首页正常显示；
刷新后 `/auth/me` 与知识库列表均为 200，会话保持。页面和静态资源返回 200，
错误 Origin 的登录请求仍返回 403。截图保存在忽略的
`output/playwright/fsl1-moonlitstarway-login.png`。

用户添加 TXT 后，Certbot 已成功签发
`*.test-zk-preview.moonlitstarway.top` 的独立 Let's Encrypt 证书。
证书目录为 `/etc/letsencrypt/live/zhiku-test-preview/`，
有效期至 2027-02-15 09:57:05（Asia/Shanghai）。私钥在测试服务器独立生成，
权限 600，由 root 持有，没有复制生产私钥。

Nginx 配置 `/etc/nginx/conf.d/zhiku-private-test-preview.conf` 已启用，
监听 443 并代理到本机 18181。配置中的
`/opt/zhiku-test/config/test-preview.fullchain.pem` 和 `test-preview.key`
分别链接到 Certbot 的 `fullchain.pem` 和 `privkey.pem`。
`nginx -t` 通过后已重载；续期部署钩子会在该证书成功更新后校验并重载 Nginx。
此证书使用人工 DNS 验证，后续续期仍需执行 DNS 验证并发布新的 TXT；
现有 Certbot 定时任务和重载钩子不代表无人值守续期已经接通。

源站及外部 curl 均通过真实域名证书校验，未授权访问和无效票据返回 401，
响应包含 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`。
Playwright 对真实预览泛域名的 HTTPS 访问同样返回预期 401，没有证书错误；
截图为忽略的 `output/playwright/fsl1-preview-https-unauthorized.png`。
主站重载后仍返回 200。尚未创建收费沙箱，不能将 TLS 与拒绝鉴权验证
描述为真实预览的票据兑换、Cookie、刷新与结束运行全部验收通过。

## 2026-11-17 主站 HTTPS

用户要求为主站签发证书。范围为主站独立证书、HTTPS 入口、来源与登录配置，
以及实际浏览器和续期验证；不修改生产、题库和支付价格，不创建收费订单或沙箱。
切换前 env 和主站 Nginx 配置备份在
`/opt/zhiku-test/backups/before-main-https-20261117/`。

主站证书位于 `/etc/letsencrypt/live/zhiku-test-main/`，
覆盖 `test.zk.moonlitstarway.top`，有效期至 2027-02-15 10:17:11（Asia/Shanghai）。
使用现有 80 端口的 HTTP-01 webroot 验证，不需要新增 DNS TXT。
验证目录为 `/opt/zhiku-test/acme-webroot`，Nginx 保留
`/.well-known/acme-challenge/` 静态路径，其余 HTTP 路径跳转到主站 HTTPS。
主站和 API 公共地址均切为 HTTPS，登录 Cookie 启用 Secure；
读取 env 的 API、两个 Worker 和预览网关已重建，应用镜像未变。

外部 curl 证书校验通过，登录页及 health 200；HTTP 308 保留路径和查询参数。
Playwright 已通过表单重新登录并刷新，登录、会话、知识库接口均为 200，
Cookie 的 Secure、HttpOnly 为 true，SameSite 为 Lax；刷新后无控制台错误。
错误 Origin 登录 403，公开 internal 路径 404，预览未授权访问仍为 401。
HTTPS 充值目录 200、enabled=true、3 个套餐，未创建订单。
截图为忽略的 `output/playwright/fsl1-main-https-login.png`。

Certbot 定时任务 active，主站 renewal authenticator 为 webroot，
`certbot renew --cert-name zhiku-test-main --dry-run --run-deploy-hooks`
已成功完成模拟续期和 Nginx 重载钩子验证，线上仍使用正式证书。
主站可自动续期；预览泛域名继续使用人工 DNS 验证，续期方式不变。
