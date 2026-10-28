# Model Management and Billing UX

Issue: #42. Related: #36, #38, #39, #41.

## Goal and Boundaries

Separate model discovery/editing, workspace targeting and multiplier previews.
Use the existing UI primitives and admin patterns without new dependencies.
Do not change prices, balances, multipliers, model defaults, billing engines or
request routing. No schema migration or activation step is required.

## Delivery

1. Read-only billing previews use the composer's model visibility rules, not
   accounting associations. Add permission-protected model detail reads and
   shared availability diagnostics without exposing secrets.
2. Billing management has workspace/platform secondary navigation. Target
   selection is separate from editing. Compare all applicable model prices
   with explicit units and draft multipliers; label inactive cache settlement.
3. Model management has a searchable/filterable list and route-based details.
   Connection and price tabs save independently with unsaved-change guards.

## Verification

- API unit/regression checks: 42 passed on 2026-10-28, including unused and
  universally visible models, zero/missing prices, availability, permissions, read-only queries,
  version conflicts and unchanged accounting.
- Frontend build, responsive browser checks and online verification: pending.
- Preserve untracked output artifacts; never commit credentials or screenshots.

## Release

Deploy API/Web after tests and backup. Do not rebuild data services or alter
environment configuration. Verify online by reading pages and APIs only.
Keep the issue open until required acceptance checks are complete.
