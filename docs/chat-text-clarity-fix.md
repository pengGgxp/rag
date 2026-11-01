# Conversation text clarity (Issue #71)

## Scope

Keep streamed and saved replies crisp. Remove message-wide entrance opacity and
vertical translation, and the persistent `will-change: opacity, transform` hint.
These styles can keep text in a composited layer and affect text rasterization.
Keep independent tool/status feedback. No API, model, billing or parsing changes.

## Verification

- Frontend production build and TypeScript checks passed.
- Playwright checked the actual conversation component with isolated API fixtures
  at 1440, 700 and 390px in normal and reduced-motion modes. Reply text and all
  ancestors have full opacity, no transform/filter and no persistent compositing
  hint. Screenshots are in `output/playwright/chat71-*.png`; desktop and phone
  screenshots were manually reviewed.
- Incremental conversation refreshes grew the reply and completed it without
  introducing a message transform, opacity animation or persistent `will-change`.
  This is a rendering check, not an upstream SSE or paid model test.
- The user's intermittent blur was not reproduced on their original device;
  browser/GPU-specific behavior still needs confirmation there.

## Deployment

Web was updated on 2026-11-01 after backing up the deployed frontend source and
image to `chat71-20261101` and `rag-platform-web:before-chat71`. The deployed Web
image is `8b2e273b23c4`; HTTP returned 200 and the API health check passed.
API, ongoing generation, Worker and data services were not restarted.
