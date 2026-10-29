// Real local APIs with a disposable catalog/database, never production prices.
async page => {
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  page.setDefaultTimeout(15000);
  await page.unrouteAll({ behavior: "wait" });
  const fixture = await (await page.request.get("http://127.0.0.1:8127/__fixture/session")).json();
  await page.context().addCookies([{ name: "rag_session", value: fixture.cookie, domain: "127.0.0.1", path: "/" }]);
  const errors = [];
  let writes = 0;
  let failQuote = false;
  let slowQuote = false;
  let contextMode = "ready";
  page.on("pageerror", error => errors.push(error.message));
  await page.route(url => url.origin === "http://localhost:8000", async route => {
    const url = route.request().url();
    if (route.request().method() === "PUT") writes++;
    if (url.endsWith("/pricing-calculator") && contextMode !== "ready") return route.fulfill({
      status: contextMode === "error" ? 503 : 200, contentType: "application/json",
      body: JSON.stringify(contextMode === "error" ? { detail: "套餐暂时无法读取" }
        : { ready: false, reference_package: null, reason: "请先配置可用的充值套餐，再按人民币成本计算价格" }),
    });
    if (url.endsWith("/pricing-calculator/quote")) {
      if (failQuote) return route.fulfill({ status: 503, contentType: "application/json", body: JSON.stringify({ detail: "计算暂时失败，请重试" }) });
      if (slowQuote) await new Promise(resolve => setTimeout(resolve, 1200));
    }
    await route.continue({ url: url.replace("http://localhost:8000", "http://127.0.0.1:8127"),
      headers: { ...route.request().headers(), cookie: `rag_session=${fixture.cookie}` } });
  });
  await page.goto("http://127.0.0.1:3110/admin/pricing/template");
  const entry = page.getByRole("button", { name: "按成本计算平台文档基准价OCR 页数", exact: true });
  await entry.waitFor();
  await entry.click();
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("支付费率", { exact: true }).waitFor();
  assert(await dialog.getByLabel("支付费率", { exact: true }).inputValue() === "0.1", "Default payment fee wrong");
  await dialog.getByLabel("人民币成本", { exact: true }).fill("2");
  await dialog.getByLabel("目标利润", { exact: true }).fill("3");
  await dialog.locator(".calculator-total").getByText("5,006", { exact: false }).waitFor();
  assert(await dialog.getByText("¥3.000994", { exact: true }).count() === 1, "Incorrect profit");
  assert(await dialog.getByText(/优惠套餐/).count() === 1, "Not using most generous package");
  await dialog.getByLabel("支付费率", { exact: true }).fill("10");
  await dialog.locator(".calculator-total").getByText("5,556", { exact: false }).waitFor();
  await dialog.getByLabel("人民币成本", { exact: true }).press("Enter");
  assert(writes === 0, "Calculator submitted parent form");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  assert(await page.getByLabel("平台文档基准价OCR 页数积分", { exact: true }).inputValue() === "4", "Cancel changed price");
  await entry.focus();
  await entry.press("Enter");
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("人民币成本", { exact: true }).fill("2");
  await dialog.getByLabel("目标利润", { exact: true }).fill("3");
  await dialog.locator(".calculator-total").getByText("5,006", { exact: false }).waitFor();
  await dialog.getByLabel("工作区倍率试算", { exact: true }).fill("0.01");
  await dialog.getByText("该倍率下预计亏损，请检查基准价或工作区倍率。", { exact: true }).waitFor();
  await dialog.getByLabel("工作区倍率试算", { exact: true }).fill("1");
  await dialog.locator(".calculator-profit").getByText("¥3.000994", { exact: true }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
    const bounds = await dialog.boundingBox();
    assert(bounds.x >= 0 && bounds.x + bounds.width <= width, "Dialog offscreen");
    await page.screenshot({ path: `output/playwright/issue47-calculator-${width}.png` });
  }
  await dialog.getByLabel("支付费率", { exact: true }).fill("100");
  assert(!await dialog.getByRole("button", { name: "应用到价格", exact: true }).isEnabled(), "Invalid fee allowed");
  await dialog.getByLabel("支付费率", { exact: true }).fill("0.1");
  await dialog.locator(".calculator-total").getByText("5,006", { exact: false }).waitFor();
  slowQuote = true;
  await dialog.getByLabel("目标利润", { exact: true }).fill("4");
  assert(!await dialog.getByRole("button", { name: "应用到价格", exact: true }).isEnabled(), "Applied stale quote");
  await dialog.getByLabel("目标利润", { exact: true }).fill("3");
  await dialog.locator(".calculator-total").getByText("5,006", { exact: false }).waitFor();
  slowQuote = false;
  failQuote = true;
  await dialog.getByLabel("其他费用分摊", { exact: true }).fill("1");
  await dialog.getByRole("alert").filter({ hasText: "计算暂时失败，请重试" }).waitFor();
  assert(await dialog.getByLabel("其他费用分摊", { exact: true }).inputValue() === "1", "Failure lost input");
  assert(!await dialog.getByRole("button", { name: "应用到价格", exact: true }).isEnabled(), "Failed quote applied");
  failQuote = false;
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await dialog.locator(".calculator-total").getByText("6,007", { exact: false }).waitFor();
  await dialog.getByRole("button", { name: "应用到价格", exact: true }).click();
  assert(await page.getByLabel("平台文档基准价OCR 页数积分", { exact: true }).inputValue() === "6007", "Apply did not update parent");
  assert(writes === 0, "Application saved automatically");
  await page.getByRole("button", { name: "保存平台价格与初始价", exact: true }).click();
  await page.getByRole("status").filter({ hasText: /平台基准价已保存/ }).waitFor();
  await page.reload();
  await page.getByLabel("平台文档基准价OCR 页数积分", { exact: true }).waitFor();
  assert(await page.getByLabel("平台文档基准价OCR 页数积分", { exact: true }).inputValue() === "6007", "Saved result lost on refresh");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`http://127.0.0.1:3110/admin/models/${fixture.provider_id}?tab=pricing`);
  const modelEntry = page.getByRole("button", { name: "按成本计算模型基准价模型输入 Token", exact: true });
  await modelEntry.waitFor();
  await modelEntry.click();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel("计价用量", { exact: true }).fill("1000000");
  await dialog.getByLabel("人民币成本", { exact: true }).fill("2");
  await dialog.getByLabel("目标利润", { exact: true }).fill("3");
  await dialog.locator(".calculator-total").getByText("5,006", { exact: false }).waitFor();
  await dialog.getByText(/应用将同时把计价用量从/).waitFor();
  await page.screenshot({ path: "output/playwright/issue47-model-calculator.png" });
  await dialog.getByRole("button", { name: "应用到价格", exact: true }).click();
  assert(await page.getByLabel("模型基准价模型输入 Token单位大小", { exact: true }).inputValue() === "1000000", "Unit not applied");
  await modelEntry.click();
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "detached" });
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "按成本计算模型基准价模型输入 Token");
  assert(await modelEntry.evaluate(element => element === document.activeElement), "Focus did not return");
  await page.getByRole("button", { name: "保存模型基准价", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "模型基准价已保存" }).waitFor();
  contextMode = "missing";
  await modelEntry.click();
  dialog = page.getByRole("dialog");
  await dialog.getByRole("alert").filter({ hasText: "请先配置可用的充值套餐" }).waitFor();
  assert(!await dialog.getByRole("button", { name: "应用到价格", exact: true }).isEnabled(), "Missing rate applied");
  contextMode = "error";
  await dialog.getByRole("button", { name: "重新读取", exact: true }).click();
  await dialog.getByRole("alert").filter({ hasText: "套餐暂时无法读取" }).waitFor();
  contextMode = "ready";
  await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await dialog.getByText(/优惠套餐/).waitFor();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  assert(writes === 2, "Unexpected writes during calculation");
  assert(!await page.evaluate(() => Object.values(localStorage).some(value => value.includes("payment_fee_percent"))), "Calculator persisted inputs");
  assert(errors.length === 0, errors.join("\n"));
  console.log("PASS: real local quote/save APIs, adjustable fee, conservative catalog, cancel/apply/save, discount loss, unit change, stale/error recovery, keyboard/focus, 1440/700/390 and reduced motion.");
  return { passed: true, widths: [1440, 700, 390], explicitSaves: writes, pageErrors: errors };
}
