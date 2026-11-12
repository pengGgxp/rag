# Model configuration page: empty image settings

## Goal and scope

Restore model detail rendering when a chat-only model has the supported legacy
image_config value {}. Normalize read responses and frontend drafts without
writing database configuration, changing prices or enabling image capabilities.
No model calls, provider configuration changes or production deployment.

## Cause

The reported deepseek-flash model has image_config = {}. Its detail API returned
200 with that value. The frontend used nullish fallback, which does not handle
an empty object; ImageConfigFields then dereferenced specs.length.

## Acceptance

- Empty/partial settings render with disabled image capabilities and empty specs.
- Existing specs, defaults and prices are preserved; opening is not an edit.
- Admin detail/list GETs do not persist defaults or change revisions.
- Verify regression tests, TypeScript, production build and git diff --check.
- Reproduce with production assets and a synthetic admin response; verify the
  fixed local page at 1440/700/390px, including tab changes and image toggles.
  All browser API fixtures are isolated and reject writes.

## Verification (2026-11-12)

- Production assets with the synthetic empty configuration reproduce the error:
  Cannot read properties of undefined (reading 'length').
- Backend shared-provider, connection and image-preset regression: 38 passed.
- Frontend tests: 14 passed. TypeScript and production build passed.
- Local production build with the same empty response: 1440/700/390px passed,
  including both tabs and enabling/disabling image generation. No page errors,
  no API writes and no false unsaved changes. All three screenshots inspected.
- Screenshots: output/playwright/empty-image-config-{1440,700,390}.png.
- No production data was changed; production deployment is not included.
