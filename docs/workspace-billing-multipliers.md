# Shared base prices and workspace multipliers

Issue #39; replaces the copied-price rules of #38. Request-level settlement remains independently controlled by #36.

## Ownership and calculation

- Providers own three live model base prices. The platform owns live OCR and Embedding base prices.
- Each workspace has one site-admin-controlled multiplier, stored as integer hundredths (1..10000, default 100). No workspace price copies are created.
- The model section of the old template route only initializes newly created providers. It is never a runtime fallback.
- Exact model settlement sums per-call rational costs times the request multiplier and rounds once. Legacy settlement retains unit-block rounding, then multiplies the request total once. Document events retain their existing unit-block rounding, then multiply and round once per event.
- Explicit zero base prices are valid; missing or disabled prices fail before upstream work. Workspace multipliers cannot be zero.

## Snapshots and compatibility

Model prices are fixed before each call; the multiplier and price source are fixed at admission for the entire request. OCR and Embedding persist an operation snapshot before paid submission and reuse it on recovery. Snapshots and historical usage remain immutable. Provider associations and historical policy IDs are retained.

Independent workspace price write endpoints return 410 after site-admin authorization. Existing policies remain readable for history and migration comparisons. While the shared-source gate is off, existing workspaces retain their old calculation; staged multipliers have no effect. New provider selections wait for activation rather than creating new legacy prices.

## Safe activation

1. Back up database, configuration and API/Web/Worker images. Deploy the additive migration and compatible application code without changing either billing gate.
2. Configure provider base prices and platform document base prices explicitly, then set workspace multipliers in the console. Do not infer multipliers from old prices.
3. Run `python -m app.manage pricing preview` or inspect the site-only migration preview. Review every old/new price, fixed sample and missing item. The SHA-256 fingerprint covers all relevant prices, versions, workspace rates and associations.
4. Pause incoming paid work and Worker consumption, drain running operations, and run `python -m app.manage pricing pause`. A pause blocks new billing admissions but preserves already captured task snapshots.
5. Run `python -m app.manage pricing activate <reviewed-fingerprint>`. Activation requires no running model/ingestion work, complete in-use prices and the exact reviewed fingerprint. It never toggles #36. Repeated activation is idempotent.
6. Run `python -m app.manage pricing resume`, resume Workers, and verify controlled consumption. Configuration gaps or unreviewed commercial changes mean activation must remain pending.

After shared consumption exists, do not roll back to an API that ignores snapshots or shared providers. Keep all added structures and pending amounts; use compatible fixes or a UI-only rollback. Never recalculate historical consumption or pending charges from current prices.

## Verification and release status

- Backend full regression: 323 passed and 14 environment-dependent skips; the additional pending-price and provider-disable regressions pass in the 27-test multiplier suite.
- Disposable PostgreSQL: fresh upgrade through revision `0025_workspace_billing`; five management, settlement and snapshot concurrency tests pass. Snapshots remain unique and immutable, and concurrent multiplier updates return one success and two version conflicts without creating price copies.
- Frontend production build and `git diff --check` pass. Playwright fixtures cover 1440, 700 and 390px layouts, decimal previews, keyboard save, refresh persistence, invalid values, 409 conflicts, unsaved-input guards and local failure/retry. Screenshots are retained under untracked `output/playwright/issue39-*`.
- Fixture browser checks are not proof of real provider settlement. Production deployment must retain the old price-source and #36 gates until the operator reviews the migration preview. Controlled live calls and post-activation shared billing remain separate acceptance items; Issue #39 stays open until these are confirmed.
