# 云电脑后台体验隔离验收

仅用于本地，固定监听 `127.0.0.1` 的 3138 / 8138 / 8938 端口。
所有密码、密钥、模型与供应商均为合成测试值，不加载线上 env。
数据库名称以 `rag_auth_test_` 开头，存储和子进程仅使用本 Compose 的 `data` 卷。
不要把该 Compose 当作生产发布包。

从仓库根目录执行：

```powershell
docker compose -f backend/tests/browser/cloud-background/compose.yml build
docker compose -f backend/tests/browser/cloud-background/compose.yml up -d
```

等待 `http://localhost:8138/health` 就绪后初始化测试用户：

```powershell
docker compose -f backend/tests/browser/cloud-background/compose.yml exec -T api python /acceptance/seed.py
```

在浏览器打开 `http://localhost:3138/?chat=1`，
以 `issue118-local@example.com` / `Pr117-local-only-2026!` 登录。
发送“制作一个算法交互页面，临时任务完成后结束，保留网页供我交互”。
模拟模型按固定工具序列执行真实 Agent / Worker / PostgreSQL 代码，
供应商适配器在独立测试容器运行真实本地命令和 HTTP 子进程。

回答完成后准备大量历史和独立任务：

```powershell
docker compose -f backend/tests/browser/cloud-background/compose.yml exec -T api python /acceptance/seed_history.py
docker compose -f backend/tests/browser/cloud-background/compose.yml exec -T api python /acceptance/keep_background.py
```

在同一 Playwright CLI 登录会话、已完成的对话页面执行：

```powershell
npx --yes --package @playwright/cli playwright-cli --session issue118 run-code --filename frontend/tests/browser/cloud-background.acceptance.js
```

脚本只替换隔离预览域名的传输（转到本地实际 HTTP 服务），并通过临时请求中断覆盖失败状态；
平台登录、鉴权、Agent 工具执行、计费、查询、下载和销毁均使用本地真实应用代码。
生成的 1440 / 700 / 390px 截图保存在 `output/playwright/issue118-*.png`。
每次完整重跑需发送一条新任务，因为验收会真正结束该预览对应的本地环境。
已有历史夹具不重复初始化；首次新建 DB 才运行 seed 和 seed_history。

PostgreSQL 套件迁移与验证：

```powershell
docker compose -f backend/tests/browser/cloud-background/compose.yml exec -T -e MIGRATION_DATABASE_URL=postgresql+psycopg://issue118:local-only@db:5432/rag_auth_test_issue118_suite api alembic upgrade head
docker compose -f backend/tests/browser/cloud-background/compose.yml exec -T api pytest -q tests/test_cloud_background_postgres.py tests/test_cloud_computer_postgres.py tests/test_cloud_ending_postgres.py tests/test_cloud_preview_postgres.py tests/test_explicit_cloud_postgres.py
```

完成后先通过本地应用结束剩余测试环境，再执行该 Compose 的 `down`。
不要停止其他项目容器，也不要清理其他卷。`down` 不删除本夹具的命名数据卷。
