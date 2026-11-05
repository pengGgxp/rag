// Uses the isolated image_preview.py server; no paid upstream requests.
async page => {
  const base = "http://127.0.0.1:3113", api = "http://127.0.0.1:8128";
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const { cookie, model_id } = await (await page.request.get(`${api}/__fixture/session`)).json();
  await page.context().addCookies([{ name: "rag_session", value: cookie, url: base }]);
  await page.goto(`${base}/admin/models/${model_id}`);
  await page.getByRole("heading", { name: "图片能力与价格" }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`model overflow ${width}`);
    await page.screenshot({ path: `output/playwright/images/model-${width}.png`, fullPage: true });
  }
  await page.goto(`${base}/admin/models`);
  await page.getByRole("heading", { name: "默认图片模型" }).waitFor();
  await page.getByText("图片付费测试", { exact: true }).click();
  await page.getByRole("textbox", { name: "提示词", exact: true }).fill("合成图片测试");
  await page.getByRole("button", { name: "测试当前默认模型" }).click();
  await page.getByRole("button", { name: "确认付费测试" }).click();
  await page.getByRole("button", { name: "预览", exact: true }).waitFor({ timeout: 20000 });
  const original = await page.evaluate(() => JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(key => key.startsWith("rag-image-test:")))).key);
  await page.reload();
  await page.getByText("图片付费测试", { exact: true }).click();
  await page.getByRole("button", { name: "预览", exact: true }).waitFor();
  const restored = await page.evaluate(() => JSON.parse(sessionStorage.getItem(Object.keys(sessionStorage).find(key => key.startsWith("rag-image-test:")))).key);
  if (original !== restored) throw new Error("refresh lost original image request");
  await page.getByRole("button", { name: "预览", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  const image = page.getByRole("dialog").locator("img");
  await image.evaluate(img => img.decode());
  await page.waitForFunction(() => {
    const modal = document.querySelector('[role="dialog"]');
    return modal && getComputedStyle(modal).opacity === "1";
  });
  await page.waitForTimeout(250);
  await page.screenshot({ path: "output/playwright/images/preview-390.png" });
  await page.keyboard.press("Escape");
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`settings overflow ${width}`);
    await page.screenshot({ path: `output/playwright/images/settings-${width}.png`, fullPage: true });
  }
  const identity = await (await page.request.get(`${api}/auth/me`)).json();
  const headers = { "X-RAG-Request": "1", "X-CSRF-Token": identity.csrf_token };
  async function createSynthetic(prompt) {
    const response = await page.request.post(`${api}/image-tasks`, { headers, data: { prompt, idempotency_key: crypto.randomUUID() } });
    if (response.status() !== 202) throw new Error(`synthetic task admission ${response.status()}`);
    const task = await response.json();
    for (let i = 0; i < 40; i++) {
      const state = await (await page.request.get(`${api}/image-tasks/${task.task_id}`)).json();
      if (state.terminal) return state;
      await page.waitForTimeout(100);
    }
    throw new Error("synthetic task did not finish");
  }
  const uncertain = await createSynthetic("synthetic-uncertain");
  if (uncertain.status !== "uncertain") throw new Error("uncertain result was incorrectly settled");
  await page.getByText("待核对图片任务", { exact: true }).click();
  await page.getByText(uncertain.task_id, { exact: true }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`review overflow ${width}`);
    await page.screenshot({ path: `output/playwright/images/review-${width}.png`, fullPage: true });
  }
  await page.getByRole("textbox", { name: "核对依据" }).fill("合成上游确认未生成图片");
  await page.getByRole("button", { name: "核对费用", exact: true }).click();
  await page.getByRole("button", { name: "确认结算", exact: true }).click();
  await page.getByText("暂无待核对任务", { exact: true }).waitFor();
  await page.getByRole("textbox", { name: "提示词", exact: true }).fill("synthetic-save-failure");
  await page.getByRole("button", { name: "测试当前默认模型" }).click();
  await page.getByRole("button", { name: "确认付费测试" }).click();
  await page.getByRole("button", { name: "重试保存", exact: true }).waitFor({ timeout: 20000 });
  await page.getByRole("button", { name: "重试保存", exact: true }).click();
  await page.getByRole("button", { name: "预览", exact: true }).waitFor({ timeout: 20000 });
  await page.goto(`${base}/?chat=1`);
  await page.getByRole("textbox", { name: "向知识库提问" }).fill("请生成一个蓝色方块图片");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await page.getByRole("button", { name: "预览", exact: true }).waitFor({ timeout: 20000 });
  await page.reload();
  await page.getByRole("button", { name: "预览", exact: true }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error(`chat overflow ${width}`);
    await page.screenshot({ path: `output/playwright/images/chat-${width}.png`, fullPage: true });
  }
  if (errors.length) throw new Error(errors.join("; "));
  console.log("Model configuration, synthetic task, authenticated preview and widths passed");
}
