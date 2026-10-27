// Run with playwright-cli run-code --filename=scripts/check-request-models.js.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (value, detail) => { if (!value) throw new Error(detail); };
  const models = { default_provider_id: "one", providers: [
    { id: "one", name: "DeepSeek", model: "deepseek-chat", available: true },
    { id: "two", name: "Qwen", model: "qwen-plus", available: true },
    { id: "long", name: "Long provider name", model: "a-very-long-model-name-for-responsive-verification", available: true },
    { id: "disabled", name: "Disabled", model: "disabled", available: false, unavailable_reason: "model_disabled" },
    { id: "missing", name: "Missing", model: "missing", available: false, unavailable_reason: "pricing_unconfigured" },
    { id: "pending", name: "Pending", model: "pending", available: false, unavailable_reason: "service_not_ready" },
  ] };
  let conversation = null;
  let releaseSend;
  let modelFailure = false;
  let modelEmpty = false;
  let sendFailure = false;
  let requestFailure = false;
  let requests = [];
  const identity = { user: { id: "u1", name: "验收用户", email: "test@example.com", has_password: true,
    email_verified: true, wechat_linked: false, is_site_admin: true },
    workspaces: [{ id: "w1", name: "验收工作区", role: "viewer" },
      { id: "w2", name: "另一工作区", role: "viewer" }], workspace_id: "w1", csrf_token: "test" };
  const fixtures = {
    "/auth/options": { setup_available: false, first_run: false },
    "/auth/me": identity, "/knowledge-bases": [], "/agent/skills": [], "/agent/mcp": [],
    "/health": { status: "ok" }, "/ingestion/options": { max_upload_bytes: 209715200 },
    "/usage": { balance: 1000, pending_credits: 0, entries: [], lifetime_spent: 0, lifetime_granted: 1000 },
    "/payments/catalog": { enabled: false, packages: [] },
    "/payments/orders": { items: [], total: 0, page: 1, page_size: 10 },
  };
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    const method = route.request().method();
    if (path === "/auth/workspace") {
      identity.workspace_id = route.request().postDataJSON().workspace_id;
      return route.fulfill({ json: identity });
    }
    if (path === "/models/available") return route.fulfill(modelFailure
      ? { status: 503, json: { detail: "模型列表暂不可用" } }
      : { json: modelEmpty ? { default_provider_id: null, providers: [] } : models });
    if (path === "/conversations" && method === "POST") {
      const body = route.request().postDataJSON();
      check(!("provider_id" in body), "Model persisted on conversation creation");
      conversation = { id: "c1", title: "新对话", knowledge_bases: [], active_request_id: null,
        messages: [], skill_ids: [], mcp_server_ids: [], updated_at: new Date().toISOString() };
      return route.fulfill({ json: conversation });
    }
    if (path === "/conversations") return route.fulfill({ json: conversation ? [conversation] : [] });
    if (path === "/conversations/c1") return route.fulfill({ json: conversation });
    if (path === "/conversations/c1/messages") {
      const body = route.request().postDataJSON();
      check(Boolean(body.provider_id), "Request omitted explicit model");
      requests.push(body);
      if (requestFailure) return route.fulfill({ status: 409, json: { detail: "模型已停用，请选择其他模型" } });
      await new Promise(resolve => { releaseSend = resolve; });
      const model = models.providers.find(item => item.id === body.provider_id);
      const sequence = conversation.messages.length;
      const message = { id: body.retry_message_id ?? `m${sequence + 1}`, sequence: body.retry_message_id ? sequence - 1 : sequence + 1,
        role: "assistant", status: sendFailure ? "failed" : "completed", content: sendFailure ? "" : "回答已完成。",
        request_id: body.request_id, provider_name: model.name, model: model.model, citations: [], traces: [],
        tool_events: [], refused: false, error_detail: sendFailure ? "受控失败，请换模型重试" : null };
      if (body.retry_message_id) conversation.messages = conversation.messages.filter(item => item.id !== body.retry_message_id);
      else conversation.messages.push({ id: `u${sequence}`, sequence, role: "user", content: body.query,
        status: "completed", citations: [], traces: [], refused: false });
      conversation.messages.push(message);
      return route.fulfill({ contentType: "text/event-stream",
        body: `event: message\ndata: ${JSON.stringify({ ...message, content: "", status: "streaming" })}\n\n` +
          `event: done\ndata: ${JSON.stringify(message)}\n\n` });
    }
    if (path.endsWith("/stop")) return route.fulfill({ json: {} });
    if (!(path in fixtures)) throw new Error(`Unexpected API request: ${path}`);
    return route.fulfill({ json: fixtures[path] });
  });
  const selector = page.getByRole("combobox", { name: "本次请求模型" });
  const input = page.getByRole("textbox", { name: "向知识库提问" });
  const send = page.getByRole("button", { name: "发送问题", exact: true });
  const waitSend = async () => {
    await page.waitForFunction(() => Boolean(document.querySelector('button[aria-label="停止回答"]')));
    for (let count = 0; count < 50 && !releaseSend; count++) await page.waitForTimeout(20);
    check(Boolean(releaseSend), "No pending request");
    check(!await selector.isEnabled(), "Model selectable while generating");
    const release = releaseSend;
    releaseSend = null;
    release();
    await send.waitFor();
    await page.waitForFunction(() => !document.querySelector('button[aria-label="停止回答"]'));
  };
  const results = [];
  for (const width of [1440, 390]) {
    conversation = null; requests = []; sendFailure = false;
    identity.workspace_id = "w1";
    await page.setViewportSize({ width, height: width === 1440 ? 900 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${base}/?chat=1`);
    await selector.waitFor();
    await page.waitForFunction(() => document.querySelector('select[aria-label="本次请求模型"]')?.value === "one");
    check(await selector.locator('option[value="disabled"]').evaluate(el => el.disabled), "Disabled model selectable");
    check((await selector.locator('option[value="missing"]').textContent()).includes("价格待配置"), "Missing unavailable reason");
    await selector.selectOption("two");
    await input.fill("第一次使用 Qwen");
    sendFailure = true;
    await send.click();
    await waitSend();
    check(requests[0].provider_id === "two", "Selected model not sent");
    await page.getByText("受控失败，请换模型重试", { exact: true }).waitFor();
    sendFailure = false;
    await selector.selectOption("one");
    await page.getByRole("button", { name: "重试", exact: true }).click();
    await waitSend();
    check(requests[1].provider_id === "one" && requests[1].retry_message_id, "Retry ignored current model");
    await page.locator(".answer-model").filter({ hasText: "DeepSeek" }).waitFor();
    await selector.selectOption("two");
    await input.fill("第二次切换模型");
    await send.click();
    await waitSend();
    check(requests[2].provider_id === "two", "Next request ignored changed model");
    check(await selector.inputValue() === "two", "Choice not retained on current page");
    await page.locator(".answer-model").filter({ hasText: "Qwen" }).waitFor();
    await page.locator(".chat-message").last().evaluate(el => el.getAnimations({ subtree: true }).forEach(animation => animation.finish()));
    await page.screenshot({ path: `output/playwright/issue41-chat-${width}.png` });
    if (width === 390) await page.locator("[data-workspace-menu]").click();
    await page.getByRole("button", { name: "新建对话", exact: true }).click();
    check(await selector.inputValue() === "two", "New conversation reset current-page model");
    if (width === 390) await page.locator("[data-workspace-menu]").click();
    await page.getByRole("navigation", { name: "历史对话", exact: true })
      .getByRole("button", { name: "新对话", exact: true }).click();
    await page.waitForURL(url => url.searchParams.get("conversation") === "c1");
    check(await selector.inputValue() === "two", "History selection reset current-page model");
    await page.reload();
    await page.waitForFunction(() => document.querySelector('select[aria-label="本次请求模型"]')?.value === "one");
    await selector.selectOption("long");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
    const bounds = await selector.boundingBox();
    check(bounds.width > 44 && bounds.x + bounds.width <= width, "Long model control overflow");
    await selector.focus();
    await selector.press("Shift+Tab");
    check(await input.evaluate(el => el === document.activeElement), "Keyboard did not return to question");
    await page.screenshot({ path: `output/playwright/issue41-long-${width}.png` });
    if (width === 390) await page.locator("[data-workspace-menu]").click();
    await page.locator("[data-account-trigger]").click();
    await page.getByRole("menuitem", { name: "工作区设置", exact: true }).click();
    await page.getByRole("dialog").waitFor();
    check(await page.getByRole("dialog").count() === 1, "Duplicate settings window");
    check(await page.getByRole("link", { name: "模型选择", exact: true }).count() === 0, "Obsolete settings model entry remains");
    if (width === 390) check(!(await page.getByRole("combobox", { name: "选择设置页面" }).locator("option").allTextContents()).includes("模型选择"), "Mobile model entry remains");
    await page.getByRole("button", { name: "关闭", exact: true }).click();
    await page.getByRole("dialog").waitFor({ state: "detached" });
    check(await selector.inputValue() === "long", "Settings reset current-page model");
    if (width === 390) await page.locator("[data-workspace-menu]").click();
    await page.locator("[data-account-trigger]").click();
    await page.getByRole("menuitem", { name: "另一工作区", exact: true }).click();
    await page.waitForFunction(() => document.querySelector('select[aria-label="本次请求模型"]')?.value === "one");
    results.push(`${width}px: explicit selection, generation lockout, retry, multiple models, default reset, long label, settings and keyboard`);
  }
  modelFailure = true;
  await page.reload();
  await page.getByRole("alert").filter({ hasText: "模型列表暂不可用" }).waitFor();
  await input.fill("错误后保留输入");
  check(!await send.isEnabled(), "Sending allowed after model list failure");
  modelFailure = false;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('select[aria-label="本次请求模型"]')?.value === "one");
  check(await input.inputValue() === "错误后保留输入", "Model retry cleared question");
  requestFailure = true;
  await send.click();
  await page.getByRole("alert").filter({ hasText: "模型已停用" }).waitFor();
  check(await input.inputValue() === "错误后保留输入", "Rejected request cleared question");
  modelEmpty = true;
  await page.reload();
  await page.getByRole("status").filter({ hasText: "暂无可用模型" }).waitFor();
  await input.fill("无可用模型");
  check(!await send.isEnabled(), "Sending allowed without a model");
  await page.screenshot({ path: "output/playwright/issue41-empty-390.png" });
  check(errors.length === 0, errors.join("; "));
  return { passed: results, pageErrors: errors };
}
