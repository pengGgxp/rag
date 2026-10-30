# Request-Level Model Selection

## Goal and Boundary

Users choose a site-managed model for each question. There is no workspace or
conversation current-model setting. Reopening a conversation restores its last
recorded request model, including after refresh. A new conversation retains the
current available choice; an initial page uses the available site default, or
the first available model when the default cannot be used. An unavailable
historical choice falls back with a short notice.
Generation disables selection; a genuine retry uses the current choice.

Reuse the existing chat/Agent runtime, shared pricing, workspace multiplier and
UI controls. Do not enable Issue #36, change commercial rates, copy prices,
persist browser preferences or add another Agent conversation surface.

## Implementation

- Authenticated GET /models/available returns sanitized model choices and an
  available site default. Centrally configured models are visible to all
  workspaces; disabled models or connections are omitted from the user list,
  while enabled but incomplete models remain listed with a safe reason.
- Conversation messages and single-answer requests require a site provider ID.
  Resolve it against the workspace, validate availability, and create/reuse the
  unique accounting association without changing any active workspace model.
- Persist immutable provider name/model on assistant messages and request
  accounting context. Historical messages remain unknown, never backfilled.
  Return the associated site provider ID separately from the workspace billing
  provider ID, so restoring a choice never guesses by mutable name or model.
- Each request pins its connection, model and base prices; existing multiplier,
  usage, idempotency and settlement semantics remain intact. A stopped provider
  prevents subsequent upstream rounds without rewriting confirmed usage.
- Remove settings/admin current-model editors and retire their write endpoints.
  Keep historical associations and ledger foreign keys, not a second workflow.
- The input toolbar uses a native accessible select with existing form tokens,
  local loading/retry and unavailable reasons. Display actual model on answers.

## Acceptance

Backend tests cover universal model visibility, redaction, missing/zero
prices, disabled providers, explicit selection, retries, idempotency, request
snapshots, workspace isolation and concurrent association creation. Verify fresh
and upgrade PostgreSQL migrations, existing balances and historical records.

Build the frontend, run diff checks, and use Playwright at 1440px/390px for model
selection, consecutive requests, streaming lockout, retry with another model,
default reset, loading/errors, keyboard access and removed settings entry.
Deploy API/Web after backup and run controlled real requests without changing
prices, billing flags or an existing workspace's current model.

## Rollback

Use small Conventional Commits. Preserve added message metadata, accounting
associations and usage snapshots; never reverse financial records. This is a
development release: ship the request-level path directly, without activation
gates, dual current-model flows or migration product pages.

## Local Verification, 2026-10-27

- Linux backend regression: 342 passed, 15 conditional skips. Object-storage
  integration uses the Compose network endpoint, not container-local localhost.
- Disposable PostgreSQL: 10 passed across provider, immutable billing snapshot,
  request settlement, registration and site-admin concurrency/transaction tests.
  Includes matching configuration lock order for request admission and admin
  edits. Fresh migrations and 0025-to-0026 upgrade succeeded.
- Frontend production build and `git diff --check` passed.
- Playwright at 1440px/390px passed explicit selection, multiple models, retry
  with a changed model, generation lockout, refresh defaults, long names, empty
  lists, failed list retry, rejected-request input preservation, keyboard and
  reduced-motion checks. Screenshots: `output/playwright/issue41-*`.
- Existing settings regression passed cold/slow routing, immediate/persistent
  single dialog, loading cancellation, history, direct refresh, unsaved password
  guards, local billing retry, mobile focus trap and focus restoration.
- Backend tests use controlled model responses; they are not evidence of real
  upstream connectivity. Online deployment and controlled calls are recorded
  separately after release.

## Online Verification, 2026-10-27

- Deployed runtime commits `bb1e096` and `ed1a5e5` to API/Web after database,
  protected configuration, source and previous-image backups under
  `/data1/rag-releases/issue41-20261027`.
- Revision 0026 applied successfully. Checksums of existing balances, usage,
  prices, provider associations and pre-existing message fields are unchanged
  across migration; `.env` and DB/Redis/storage/Worker container IDs unchanged.
- Shared prices remain enabled, Issue #36 remains disabled. No commercial
  prices, workspace multipliers or site default were changed.
- An isolated acceptance account made two real HTTP/SSE requests in one
  conversation: `qwen3.8-max-0902` (435 input / 32 output, 24 credits) and
  `deepseek-flash` (443 input / 16 output, 48 credits). Each answer and usage
  record points to its actual provider; duplicate submissions replayed the
  answer without another charge. No workspace price rows were created.
- The acceptance account's sessions were revoked and the account suspended
  afterwards; consumption and audit records remain traceable.
- An already-authenticated live browser verified the composer choices, settings
  navigation removal, a single settings dialog, selection retention when closing
  settings and clearing on refresh. The site has no configured default model;
  refresh therefore shows the selection placeholder, not an invented fallback.

## 思考设置

思考设置仅保留当前页面状态，不保存为会话偏好；模型按上文规则从历史回答恢复。
切换模型恢复目标模型默认值；生成期间禁用，重试使用当前设置。
服务端返回过期版本时保留问题，重新加载并要求确认，不静默换档。
回答的“请求思考设置”是请求快照，不表示实测思考深度。

## 2026-10-30 模型选择体验验证（#60）

- 停用模型及停用连接不出现在用户列表，后台管理和价格预览继续保留。
- 通过已有回答关联恢复站点模型 ID，不新增数据迁移、浏览器偏好或工作区当前模型。
- 后端相关回归 38 项通过；前端构建和 `git diff --check` 通过。
- 隔离浏览器验证默认选择、新会话沿用、历史恢复、刷新、真实发送请求的模型 ID、历史模型失效回退及慢响应；人工检查 1440px、390px 截图。
- 浏览器调用使用本地测试服务，未触发付费上游，不修改 OCR、商业价格或计费开关。
