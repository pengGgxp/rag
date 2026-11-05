# 资源库与附件选择视觉修复

- 目标：按设计系统修复资源列表、附件入口和资源选择弹窗的布局与视觉层次。
- 范围：复用现有 Dialog、Tooltip、文件接口；不改权限、计费和存储行为。
- 验收：1440、700、390px 实际截图审查；搜索、选择、关闭及键盘操作可用，无横向溢出。
- 原因：弹窗缺少内容内边距，误用未在本路由加载的 storage-dialog-actions；完整页面列表直接复用在选择弹窗，未区分信息密度。

## 验证记录

- 已查看真实浏览器截图：资源页与选择弹窗 1440/700/390px，附件入口 1440/390px，文件类型、间距、换行及工具栏。
- 截图保存在 output/playwright/resources/、picker-final-*.png、attachment-final-*.png。
- scripts/check-resources.js 通过：搜索、上传、预览、删除、对话选用与移除、viewer/editor 权限、超额禁用及横向溢出。
- 修复选择弹窗 Escape 关闭后焦点未恢复的问题，已验证返回附件按钮。
- 前端生产构建及 TypeScript 通过；git diff --check 通过。本次尚未部署线上。

## 线上发布

- 2026-11-05 通过 XTerminal 发布 939d08c，仅重建并更新 Web，无数据库迁移。
- 备份：/data1/rag-releases/resource-ui-939d08c，保留源码及原 Web 镜像。
- 服务器构建成功，公网 /resources 返回 200、/api/health 正常；公网浏览器验证新样式已加载。受保护界面的视觉验收沿用本地隔离账号截图，未借用线上用户会话。
- API、Worker、执行器和数据服务容器 ID 保持不变。
