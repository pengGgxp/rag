# Model Selection and Settings Loading

Issue: #40. Related: #38, #39, #36.

## Behavior

- Model listing and saving share one availability check. Responses retain
  `available` and include a safe `unavailable_reason`: `model_disabled`,
  `pricing_unconfigured`, `service_not_ready`, or null.
- Shared pricing activation uses the existing pause, drain, configuration
  fingerprint and completeness checks. It does not enable request-level
  settlement, change commercial rates or switch existing workspaces' models.
- Workspace layout owns the only intercepted settings Dialog. Clicking an
  account-menu settings link mounts it before route completion; intercepted
  layouts supply only content. Direct links retain the same window component.
- Pending navigation and data requests show local skeletons. The pending route
  has a 15-second retry state, without hiding navigation or the close button.
- Local loading/error styles are shared outside the admin layout so settings
  loaders have real dimensions. Form dirtiness is synchronized before paint.
- New settings host and loading/error adapters compose existing Dialog, sidebar,
  tokens and form context. They own route-pending state only, not API business
  logic or a new modal system. Radix retains focus trapping and Escape behavior.

## Local Verification

- Model selection and workspace billing tests: 39 passed.
- Full Linux backend regression: 332 passed, 14 environment-dependent skips.
- Disposable PostgreSQL migrated to head: concurrent model selection and
  immutable billing snapshots both passed.
- Windows full suite: 331 passed, 14 skipped, one existing ZIP-path expectation
  differs because Python normalizes archive backslashes on Windows. The same
  test passes on Linux; no unrelated Skill code changed.
- Frontend production build and `git diff --check` passed.
- `scripts/check-model-settings.js` exercises cold/slow route opening, pending
  section navigation, retained window DOM, close-before-response, preserved
  knowledge search/chat draft, history, direct refresh, save conflicts,
  unavailable reasons, unsaved guards, local loading/error/retry, keyboard
  focus, mobile drawer handoff and reduced motion.
- Screenshots live under untracked `output/playwright/issue40-*`.

## Release Checks

Back up database, configuration and prior API/Web images. Build first, pause
paid admissions, stop and drain API/Worker, and revalidate the reviewed pricing
fingerprint before activation. Keep Issue #36 disabled. Resume API/Worker after
activation; leave database, Redis, storage containers and `.env` unchanged.

Check production settings navigation and model availability, and use an isolated
test workspace for selection and a controlled real model request. Do not close
the issue until real selection, accounting and production browser checks pass.
After shared-source consumption exists, never roll back to settlement code that
ignores its immutable snapshots.
