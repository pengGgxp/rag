# 测试环境发布

路径：功能 PR → 合并到 `test` → 本地手动构建并推送镜像 →
上传发布包 → 更新 `103.97.177.61` 测试服务 → 检查健康和提交版本。

## 当前部署目标（2026-11-18 用户确认）

| 项目 | 当前约定 |
| --- | --- |
| 测试服务器 | `103.97.177.61` |
| 访问域名 | 沿用 `https://zk.str1ct.top`，使用测试镜像创建容器，不切换域名 |
| 构建来源 | 已合并并推送的干净 `test` 分支 |
| 远程操作 | 使用 Xterminal，按主机 IP 确认目标，不凭服务器昵称判断角色 |
| 目录、Compose 项目和数据卷 | 更新前从该服务器当前运行容器及 Compose 标签读取，保留现有配置和数据 |

`fsl1`（`39.106.81.248`）不是当前测试部署目标。在 fsl1 找不到
`/opt/zhiku-test`，不能据此判断当前测试环境缺失，也不需要新建空环境。
下文第 3 节的 `/opt/zhiku-test`、`zhiku-private-test`、旧测试域名与
数据卷名称属于此前 fsl1 独立测试站的部署契约，不代表当前服务器布局。
现有 `release.py deploy` 仍包含这些旧目标及检查，尚未适配本次目标；
不能仅修改本文就认定该脚本已能直接部署当前测试服务。

当前不使用 GitHub Actions 构建、推送或部署测试环境。
目标为 `test` 的 PR 跳过现有 CI，向 `test` 推送也不会触发云电脑镜像构建，
因此合并前必须完成本地检查。
`main` 和正式版 tag 发布保留原有流程，见 `docs/ci-cd.md`。
沿用目标服务器实际生效的功能配置；不得将历史 fsl1 的“题库停用”等
配置直接套用到当前测试服务。此前测试站使用的
`https://test.zk.moonlitstarway.top` 和 `*.test-zk-preview.moonlitstarway.top`
仅作为历史记录保留。

## 1. 合并与本地检查

功能 PR 的目标分支选 `test`。合并前在本地执行对应的后端、前端或
部署测试，检查合并差异。脚本只接受干净且已推送的 `test`；
不从未提交修改、功能分支或 PR head 直接发布。

```sh
git fetch origin
git switch test
git merge --ff-only origin/test
git status --short
python -m unittest discover -s deploy/tests -p 'test_*.py' -v
```

首次创建 `test` 分支以 `main` 为基线；当前在测试服务器试用的功能
需要通过面向 `test` 的 PR 合入后，才应发布该分支。不要把“建立分支”
当作“当前测试站代码已经进入分支”，也不要直接用基线覆盖现有测试站。

## 2. 在本地构建与推送

本机需要 Python 3.11.8+（或 3.12+）、Git、Docker Buildx，并已使用获准的推送凭据
登录 SWR。服务器使用自己的只读拉取凭据；不要复制个人 Docker 配置、
应用密钥或数据库备份进 Git 或发布包。

在仓库根目录手动执行：

```sh
python deploy/testing/release.py publish
```

默认 SWR 为 `swr.cn-south-1.myhuaweicloud.com/str1ct`。可通过
`--registry`、`--organization` 覆盖。依次构建并推送 `linux/amd64`：

- `zhiku-api`：API、普通任务 Worker、预览网关共用。
- `zhiku-web`：干净安装前端依赖，输出 standalone，API 地址为 `/api`。

每次执行分配带提交号与 UTC 时间的独立 `test-...` 镜像标签，
不移动正式版标签。最终 `release.json` 记录完整提交号、两个 digest
和 Alembic head。Compose 使用 digest，服务器不依赖可变标签。
构建从该提交的 Git archive 快照进行，后续工作区修改和忽略的本地文件
不会混入镜像。

产物在忽略的 `output/test-releases/<version>/` 中，包括受限权限的
构建日志。只有两个镜像全部推送成功才生成 `.tar.gz` 及 `.sha256`。
发布包只包含部署脚本、Compose、manifest 和校验清单，不包含环境变量
或应用数据。构建失败查看本地 `<service>.build.log`，修复后重新发布。

## 3. 历史 fsl1 上传与部署步骤

以下步骤记录旧 `/opt/zhiku-test` 环境，不能原样用于当前
`103.97.177.61`。当前发布应先读取目标服务器实际运行配置，备份环境、
数据库和上一版本，再按实际项目更新；保留现用域名、数据卷与回滚记录。

先通过 Xterminal 确认 fsl1 身份和 SSH 可用，再将刚生成的 `.tar.gz`
和对应 `.sha256` 上传到 `/opt/zhiku-test/incoming/`。也可使用已配置的
专用 SSH 别名执行 `scp`。不要关闭 SSH 主机密钥校验。

服务器需要：

- Python 3.11+、Docker Compose v2、可拉取 API 和 Web 两个仓库的 Docker 登录。
- 原有 `/opt/zhiku-test/config/app.env`，权限 `600`。
- 原有 `zhiku-private-test` 项目中的数据库和 Redis 正在运行。
- 原有 `zhiku-private-test_postgres`、`zhiku-private-test_redis`、
  `zhiku-private-test_cache` 数据卷。
- 原有 Nginx 和证书仍有效；本路径不修改域名和证书配置。

下面的 `<version>` 必须使用本次命令实际输出的版本：

```sh
umask 077
cd /opt/zhiku-test/incoming
sha256sum -c <version>.tar.gz.sha256
mkdir -p /opt/zhiku-test/releases
tar -xzf <version>.tar.gz -C /opt/zhiku-test/releases
python3 /opt/zhiku-test/releases/<version>/release.py deploy \
  --release /opt/zhiku-test/releases/<version>
```

首次接入时还没有 `current` 链接，须在上述部署命令追加
`--adopt-release /opt/zhiku-test/releases/233a452`，并先确认它确实是
当前运行的旧版本。部署入口从旧版本受保护的 `.env` 保留服务器配置，
只更新镜像与提交身份。不会创建空数据库来替代缺失的已有数据卷。

部署加独占锁，先拉镜像并核对镜像中的迁移 head，再停止应用服务、
生成 PostgreSQL custom-format 备份并验证归档目录，执行一次性迁移，
然后启动应用。数据库和 Redis 不会被重建或重启；服务更新有短暂停机。
备份目录与 SHA-256 记录在版本目录的 `evidence.json`。

成功必须满足：

- `/ready` 中数据库、Redis、普通 Worker 均正常。
- 普通 Worker 响应其节点的 Celery ping；旧文件预览 Worker 已从当前发布契约移除。
- 实际运行镜像 digest、API 版本和数据库 revision 与发布包一致。
- 预览网关、Web 页面、公开 HTTPS API 和版本均可访问。

全部通过才更新 `/opt/zhiku-test/current` 和 `previous`。
成功消息包含发布版本和完整提交号；容器启动不代表验收通过。
部署日志保存在受限权限的 `<release>/deploy.log`，不要直接公开日志内容。

## 4. 失败与恢复

拉镜像和预检失败不会停止原应用。停止应用后的失败会保留原发布目录、
备份、镜像和失败阶段；不会自动降级数据库或覆盖数据恢复备份。
迁移失败不会启动新应用；健康检查失败不会标记发布成功。

先查看 `evidence.json` 的 `failed_step` 和 `backup`，结合受控日志决定恢复。
若数据库 revision 未改变，可按 `previous_release` 中的旧 Compose 和 `.env`
启动原应用；若已改变，先检查与旧应用兼容性，必要时明确确认后恢复备份。
不要直接运行 `alembic downgrade`，也不要在未确认数据影响时恢复数据库。
同一个发布目录不重复执行，修复后生成新版本。

GitHub Actions 余额恢复后，可以另行将这里的手动构建、上传和部署入口
接入自动化；当前没有后台触发器。
