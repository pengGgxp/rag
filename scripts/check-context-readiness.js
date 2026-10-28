// Uses the disposable real API in backend/tests/conversation_preview.py.
async (page) => {
  await page.unrouteAll({ behavior: "wait" });
  const check = (value, message) => { if (!value) throw new Error(message); };
  const session = await (await page.request.get("http://127.0.0.1:8125/__fixture/session")).json();
  await page.context().addCookies([{ name: "rag_session", value: session.cookie,
    domain: "127.0.0.1", path: "/", sameSite: "Lax" }]);
  let mode = "none";
  let contextMode = "normal";
  let reads = 0;
  let created = 0;
  const models = await (await page.request.get("http://127.0.0.1:8125/models/available")).json();
  const configured = models.providers[0];
  const missing = { ...configured, id: "11111111-1111-4111-8111-111111111111",
    name: "未配置容量的模型", context_capacity: null, max_output_tokens: null };
  await page.route(url => url.origin === "http://localhost:8000", async route => {
    const url = new URL(route.request().url());
    if (url.pathname === "/models/available") {
      if (mode === "model-error") return route.fulfill({ status: 503,
        json: { detail: "模型列表读取失败" } });
      return route.fulfill({ json: { ...models, default_provider_id: mode === "none" ? null : configured.id,
        providers: [configured, missing] } });
    }
    if (/\/conversations\/[^/]+\/context$/.test(url.pathname)) {
      reads++;
      if (contextMode === "error") return route.fulfill({ status: 503,
        json: { detail: "上下文服务暂时不可用" } });
      if (contextMode === "slow") await new Promise(resolve => setTimeout(resolve, 1800));
    }
    if (url.pathname === "/conversations" && route.request().method() === "POST") created++;
    return route.continue({ url: route.request().url().replace("http://localhost:8000", "http://127.0.0.1:8125"),
      headers: { ...route.request().headers(), cookie: `rag_session=${session.cookie}` } });
  });
  const context = page.getByRole("button", { name: "上下文", exact: true });
  const panel = page.getByRole("dialog", { name: "上下文", exact: true });
  const model = page.getByRole("button", { name: "本次请求模型", exact: true });
  const select = async name => {
    await model.click();
    await page.getByRole("option").filter({ hasText: name }).click();
  };
  const noWarning = async () => check(!await panel.getByText(/请联系管理员配置模型容量/).count(), "False missing-capacity warning");
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    mode = "none";
    await page.goto("http://127.0.0.1:3110/?chat=1");
    await model.filter({ hasText: "选择模型" }).waitFor();
    await context.click();
    await panel.getByText("请先选择模型，再查看上下文状态。").waitFor();
    await noWarning();
    await page.keyboard.press("Escape");
    await select(configured.name);
    await context.click();
    await panel.getByText("上下文保护已就绪，发送问题后显示输入估算。").waitFor();
    await panel.getByText("16,000 Token", { exact: true }).waitFor();
    check(await panel.getByRole("progressbar").count() === 0, "New chat has a fabricated estimate");
    await noWarning();
    await page.screenshot({ path: `output/playwright/issue45-context-ready-${width}.png` });
    await page.keyboard.press("Escape");
    await context.focus();
    await page.keyboard.press("Enter");
    await panel.waitFor();
    await page.keyboard.press("Escape");
    check(await context.evaluate(node => node === document.activeElement), "Focus did not return");
    await select(missing.name);
    await context.click();
    await panel.getByText("上下文保护未就绪，请联系管理员配置模型容量。").waitFor();
    await page.keyboard.press("Escape");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
    results.push(`${width}: no-selection, configured new chat, real missing config, keyboard, no fake estimate`);
  }
  check(reads === 0 && created === 0, "New chat created or queried a conversation");
  mode = "model-error";
  await page.reload();
  await context.click();
  await panel.getByText("模型列表读取失败", { exact: true }).waitFor();
  await noWarning();
  mode = "default";
  await panel.getByRole("button", { name: "重新加载" }).click();
  await panel.getByText("上下文保护已就绪，发送问题后显示输入估算。").waitFor();
  await page.keyboard.press("Escape");
  await page.getByRole("textbox", { name: "向知识库提问" }).fill("核对上下文读取状态。");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await page.getByRole("button", { name: "停止回答", exact: true }).waitFor();
  await page.getByRole("button", { name: "停止回答", exact: true }).waitFor({ state: "detached", timeout: 30000 });
  const existing = page.url();
  await page.reload();
  await context.click();
  await panel.getByRole("progressbar").waitFor();
  await noWarning();
  await page.screenshot({ path: "output/playwright/issue45-context-existing-390.png" });
  contextMode = "error";
  await page.reload();
  await context.click();
  await panel.getByText("上下文服务暂时不可用", { exact: true }).waitFor();
  await noWarning();
  contextMode = "normal";
  await panel.getByRole("button", { name: "重新加载" }).click();
  await panel.getByRole("progressbar").waitFor();
  contextMode = "slow";
  await page.reload();
  await context.click();
  await panel.getByText("正在读取上下文状态…").waitFor();
  await noWarning();
  await page.keyboard.press("Escape");
  await page.goto("http://127.0.0.1:3110/?chat=1");
  await model.filter({ hasText: configured.name }).waitFor();
  await context.click();
  await panel.getByText("上下文保护已就绪，发送问题后显示输入估算。").waitFor();
  await page.waitForTimeout(2200);
  check(await panel.getByRole("progressbar").count() === 0, "Late response overwrote new-chat state");
  await page.keyboard.press("Escape");
  contextMode = "normal";
  await page.goto(existing);
  await context.click();
  await panel.getByRole("progressbar").waitFor();
  results.push("model-list retry; existing estimate; context error/retry; slow loading; stale response ignored");
  return results;
}
