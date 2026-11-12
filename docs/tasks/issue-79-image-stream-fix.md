# Issue #79 图片文件流读取修复

## 目标与边界

修复 2026-11-12 Issue 最新故障评论中确认的 `FileStreamReader.read` 不存在问题。
使用锁定的 `e2b==2.31.0` 迭代接口有界读取，让预期的传输失败作为图片工具结果返回，
使 Agent 可以修正路径、再次调用或继续回答。

不修改文件挂载、网络策略、图片路径权限、云电脑生命周期、价格、账单或前端；
不重试此前失败的线上回答，不部署、合并或关闭 Issue。
PR #82 已合并，本次从 `origin/main@d153154` 开始单独提交后续修复。

## 实现与验收

- 按块累计前检查字节上限；正常结束、超限、中断和取消均关闭文件流，不先读取完整文件再校验。
- HTTP 传输中断、SDK 文件消失和超时转换为安全的 `image_transfer_failed` 工具错误；
  取消、鉴权、权限异常和未知程序错误继续传播，不返回成功或伪造图片输入。
- 维持图片实际格式、完整性、10 MiB、4000 万像素和 SHA-256 校验。
- 使用 SDK 真实 `FileStreamReader`，以 HTTP 分块响应测试正常多块、空文件、恰好达到上限、
  跨块超限、传输中断和关闭；不使用带 `.read()` 的普通文件替身。
- 四类协议回归覆盖读取后图片进入实际模型请求、失败后工具事件及 Agent 后续回答，
  同时保留压缩与续传净化回归；不调用商业模型。

## 真实沙箱验收

`backend/tests/test_image_reading_live.py` 只连接显式指定的既有沙箱，不创建或停止实例，
只在 `/tmp` 创建并清理随机命名的合成图片。它验证沙箱内生成、SDK 读取、格式与摘要校验，
输出 SDK 实际版本及安全元数据，不输出图片字节、凭据或 Base64。

需设置 `RAG_IMAGE_READ_LIVE_ACCEPT=1`、`RAG_IMAGE_READ_SANDBOX_ID`、
`RAG_IMAGE_READ_REGION` 和 `RAG_CLOUD_CHECK_KEY` 后，运行
`python -m pytest -q -s tests/test_image_reading_live.py`。
默认跳过，不因单元测试成功声称真实沙箱链路通过。

本轮没有提供指定测试实例或对应凭据，真实链路尚未验收；没有新建收费实例。

## 本轮验证结果

- 后端定向：`91 passed, 2 skipped`，覆盖真实 SDK 流、图片工具、四类协议、附件及净化。
  跳过项为 Windows 不适用的 Linux 启动器测试和未启用的真实沙箱验收。
- 后端全量：`980 passed, 68 skipped, 1 failed`，未将其标记为全量通过。
  唯一失败为资源预览的 `test_preview_generation_dedup_range_billing_and_no_storage_charge`，
  SQLite 从 UUID 列返回浮点数，SQLAlchemy UUID 反序列化报错；该测试文件独立复跑 `9 passed`。
  使用未修改的 `ResourcePreview` 表定义和全数字 UUID
  `62706604849447748888888888888888` 可确定性复现：SQLite 存储类型为 `real`，
  读取时报相同的 `AttributeError: 'float' object has no attribute 'replace'`。
  本次不扩大范围修改数据库模型或资源预览。
- 前端生产构建通过，构建时设置 `NEXT_PUBLIC_API_URL=http://localhost:8000`；未改前端源码。
- Python 编译检查与 `git diff --check` 通过。
- 未执行真实付费模型调用、线上任务重试、部署、合并或 Issue 关闭。
