async (page) => {
  const models = await (await page.request.get("http://127.0.0.1:8125/site/admin/llm-providers")).json();
  const id = models[0].id;
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.goto(`http://127.0.0.1:3110/admin/models/${id}`);
    const capacity = page.getByRole("spinbutton", { name: "上下文容量（Token）" });
    const output = page.getByRole("spinbutton", { name: "最大输出（Token）" });
    await capacity.waitFor();
    await capacity.fill("");
    await page.getByText("请同时填写上下文容量和最大输出，或同时留空。", { exact: true }).waitFor();
    if (await page.getByRole("button", { name: "保存连接配置", exact: true }).isEnabled()) throw new Error("Invalid budget can save");
    await capacity.fill("16000");
    await output.fill("2000");
    await capacity.scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/playwright/issue45-admin-budget-${width}.png` });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error("Admin overflow");
    results.push(`${width}: reused form grid, budget labels, paired validation, no overflow`);
  }
  return results;
}
