# Brand Rollout

Date: 2026-11-02

## Scope

Apply the approved folded-page symbol and Chinese tagline to the existing
sign-in, workspace, admin, loading and browser-brand surfaces. Keep product
navigation, authentication and billing unchanged.

## Verification

- `npm run build` and `git diff --check` passed.
- Inspected live sign-in screenshots at 1440, 700 and 390 pixels.
- Live sign-in returned HTTP 200 with the new title and SVG icon.
- Updated only Web using Compose project `rag-platform`; previous image and
  affected source files were backed up before deployment.
- Authenticated workspace and admin visual checks remain unverified in this
  browser session; no login credentials were used for this branding task.
- Screenshots remain in untracked `output/zhiku-brand/`.
