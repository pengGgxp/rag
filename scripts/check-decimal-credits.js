// Real local quote/save APIs backed by pricing_calculator_preview.py, not deployed data.
async page => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const base = new URL(page.url()).origin;
  const fixture = await (await page.request.get("http://127.0.0.1:8127/__fixture/session")).json();
  await page.unrouteAll({ behavior: "wait" });
  let saves = 0;
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    if (route.request().method() === "PUT") saves++;
    const response = await route.fetch({ url: `http://127.0.0.1:8127${path}`,
      headers: { ...route.request().headers(), origin: "http://127.0.0.1:3110",
        cookie: `rag_session=${fixture.cookie}` } });
    await route.fulfill({ response, headers: { ...response.headers(),
      "access-control-allow-origin": base, "access-control-allow-credentials": "true" } });
  });
  await page.goto(`${base}/admin/pricing/template`);
  const input = page.getByLabel("平台文档基准价OCR 页数积分", { exact: true });
  await input.waitFor();
  const persistedPrice = await input.inputValue() === "0.1234" ? "0.4321" : "0.1234";
  await page.getByRole("button", { name: "按成本计算平台文档基准价OCR 页数", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "按成本计算", exact: true });
  await dialog.getByLabel("人民币成本", { exact: true }).fill("2");
  await dialog.getByLabel("目标利润", { exact: true }).fill("3");
  await dialog.locator(".calculator-total").getByText("5,005.005", { exact: false }).waitFor();
  check(await dialog.getByText("¥3", { exact: true }).count() === 1, "Quote profit used integer ceiling");
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Overflow at ${width}`);
    await page.screenshot({ path: `output/playwright/issue57-calculator-${width}.png` });
  }
  await dialog.getByRole("button", { name: "应用到价格", exact: true }).click();
  check(await input.inputValue() === "5005.005", "Applied fractional price was truncated");
  await input.fill(persistedPrice);
  await page.getByRole("button", { name: "保存平台价格与初始价", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "平台基准价已保存" }).waitFor();
  await page.reload();
  await input.waitFor();
  check(await input.inputValue() === persistedPrice, "Fourth decimal lost after save/refresh");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: "output/playwright/issue57-saved-price.png" });
  check(saves === 1, "Quote unexpectedly changed saved prices");
  await page.route("**/usage", route => route.fulfill({ json: {
    balance: 998.5789, pending_credits: 0.1234, lifetime_granted: 1000, lifetime_spent: 1.4211,
    usage: { events: 1, units: { llm_input_tokens: { units: 886, credits: 1.2422 } } },
    entries: [{ id: "decimal-fixture", amount: -1.4211, balance_after: 998.5789,
      kind: "usage_settlement", metadata: {}, created_at: "2026-10-30T00:00:00Z" }],
  } }));
  await page.goto(`${base}/settings/billing`);
  await page.locator(".billing-heading").getByText("998.5789", { exact: true }).waitFor();
  check(await page.getByText("0.1234 积分待结算", { exact: true }).isVisible(), "Pending fourth decimal lost");
  check(await page.getByText("-1.4211", { exact: true }).isVisible(), "Ledger fourth decimal lost");
  await page.setViewportSize({ width: 390, height: 844 });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Billing overflow");
  await page.screenshot({ path: "output/playwright/issue57-balance-390.png" });
  return { passed: true, widths: [1440, 700, 390], actualApiSaves: saves, persistedPrice };
}
