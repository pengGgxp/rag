# Model Management Review

Issue: #43. Related: #42, #38, #41.

## Findings and Scope

- Publication scope had no assignment workflow. Unpublished models could not
  be assigned to a new workspace, yet the UI implied configurable grants.
- Detail tabs lacked a visible control surface and consistent selected,
  hover and keyboard-focus states.
- Tab width did not align with the editor. API-key status created uneven
  input heights, and the enabled control missed existing form conventions.

All centrally configured models are now visible to every workspace. Disabled,
unpriced and disconnected models remain unavailable, with the same diagnostics
used by request admission and billing previews. Remove publication inputs,
responses, list columns and filters. The unused database column remains without
a schema migration; no data, commercial price, multiplier, default model,
balance or billing-engine flag is modified.

Reuse Radix Tabs with Lucide icons, visible active/hover/focus states and manual
keyboard activation. Connection and price editors keep independent saves and
unsaved-change guards. Align editor widths and credential fields; mobile targets
remain at least 44px. No dependencies, grant system or rollout UI are added.

## Verification

- Backend: 61 focused shared-provider, request-model, catalog, workspace-billing
  and pricing tests passed on 2026-10-28. Legacy unpublished records are visible,
  selectable and eligible as defaults when otherwise available.
- Frontend production build and `git diff --check` pass.
- Playwright fixture checks pass at 1440/700/390px: removed scope controls,
  filters, tabs and keyboard activation, separate saves, conflicts, secret
  redaction, new-model flow, loading/error retry, unsaved changes, refresh,
  history, reduced motion, focus restoration and no horizontal overflow.
- Screenshots inspected under `output/playwright/issue43-*`; output stays
  untracked. Fixture checks are not evidence of real upstream model calls.
- API/Web deployment and read-only online checks are recorded on the issue
  after release. No upstream call or live configuration edit is needed.
