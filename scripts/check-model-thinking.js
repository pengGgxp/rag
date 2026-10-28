// Run with playwright-cli run-code --filename=scripts/check-model-thinking.js.
async (page) => {
  const origin = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (value, message) => { if (!value) throw new Error(message); };
  const empty = { kind: "unconfigured", adapter: "none", options: [], default_option_id: null,
    budget_min: 1, budget_max: 32768, supports_tools: true, stream_only: false, always_thinking: false };
  const models = [
    { id: "deepseek", name: "DeepSeek", model: "deepseek-flash", thinking: { ...empty, kind: "effort", adapter: "deepseek",
      options: [{ id: "high", label: "高", value: "high" }, { id: "low", label: "低", value: "low" }], default_option_id: "high" } },
    { id: "bailian", name: "百炼", model: "qwen-long-name-for-responsive-check", thinking: { ...empty, kind: "budget", adapter: "bailian",
      options: [{ id: "small", label: "精简", value: 2048 }, { id: "large", label: "深入", value: 8192 }], default_option_id: "small" } },
    { id: "fixed", name: "固定模型", model: "fixed-thinking", thinking: { ...empty, kind: "fixed", adapter: "bailian" } },
    { id: "unsupported", name: "普通模型", model: "plain", thinking: { ...empty, kind: "unsupported" } },
    { id: "unknown", name: "未配置模型", model: "unknown", thinking: { ...empty } },
  ].map(model => ({ key: model.id, enabled: true, available: true, unavailable_reason: null, has_api_key: true,
    base_url: "https://model.example/v1", timeout_seconds: 120, workspace_count: 0, version: 2, thinking_version: 1,
    is_default: false, prices: ["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"].map(capability => ({
      capability, unit: "tokens", unit_size: 1000, credits_per_unit: 4, enabled: true, version: 2 })), ...model }));
  const publicModel = model => ({ id: model.id, name: model.name, model: model.model, available: true,
    thinking: { kind: model.thinking.kind, version: model.thinking_version, default_option_id: model.thinking.default_option_id,
      options: model.thinking.options.map(option => ({ id: option.id, label: option.label,
        detail: `${option.value}${model.thinking.kind === "budget" ? " Token" : ""}` })) } });
  let conversation = null, releaseSend = null, rejectSend = false, failAnswer = false, conflict = false;
  let requests = [], saved = [], failModels = false;
  const session = { user: { id: "u1", name: "思考配置验收", email: "test@example.com", has_password: true,
    email_verified: true, wechat_linked: false, is_site_admin: true }, workspace_id: "w1",
    workspaces: [{ id: "w1", name: "验收工作区", role: "admin" }, { id: "w2", name: "另一工作区", role: "admin" }], csrf_token: "fixture" };
  const fixtures = { "/auth/me": session, "/auth/options": { first_run: false, setup_available: false },
    "/knowledge-bases": [], "/agent/skills": [], "/agent/mcp": [], "/health": { status: "ok" },
    "/ingestion/options": { max_upload_bytes: 209715200 }, "/usage": { balance: 1000, pending_credits: 0, entries: [] },
    "/payments/catalog": { enabled: false, packages: [] }, "/payments/orders": { items: [], total: 0, page: 1, page_size: 10 } };
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === origin && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    const method = route.request().method();
    const reply = (json, status = 200) => route.fulfill({ json, status });
    if (path === "/site/admin/llm-providers") return reply(models);
    const admin = path.match(/^\/site\/admin\/llm-providers\/([^/]+)$/);
    if (admin) {
      const model = models.find(item => item.id === admin[1]);
      if (method === "PUT") {
        const body = route.request().postDataJSON();
        saved.push(body);
        if (conflict) { conflict = false; return reply({ detail: "供应商已更新，请重新加载后保存" }, 409); }
        Object.assign(model, body, { thinking_version: model.thinking_version + 1, version: model.version + 1 });
      }
      return reply(model);
    }
    if (path === "/models/available") return failModels ? reply({ detail: "模型列表暂不可用" }, 503)
      : reply({ default_provider_id: "deepseek", providers: models.map(publicModel) });
    if (path === "/conversations" && method === "POST") {
      conversation = { id: "c1", title: "新对话", knowledge_bases: [], skill_ids: [], mcp_server_ids: [],
        active_request_id: null, messages: [], updated_at: new Date().toISOString() };
      return reply(conversation);
    }
    if (path === "/conversations") return reply(conversation ? [conversation] : []);
    if (path === "/conversations/c1") return reply(conversation);
    if (path === "/conversations/c1/messages") {
      const body = route.request().postDataJSON();
      requests.push(body);
      if (rejectSend) {
        rejectSend = false;
        models[0].thinking_version++;
        return reply({ detail: "模型思考配置已更新，请重新加载模型并确认思考设置" }, 409);
      }
      await new Promise(resolve => { releaseSend = resolve; });
      const model = models.find(item => item.id === body.provider_id);
      const option = model.thinking.options.find(item => item.id === body.thinking_option_id);
      const next = conversation.messages.length;
      const answer = { id: body.retry_message_id ?? `a${next}`, sequence: body.retry_message_id ? next - 1 : next + 1,
        role: "assistant", status: failAnswer ? "failed" : "completed", content: failAnswer ? "" : "公开回答，未包含思考过程。",
        request_id: body.request_id, citations: [], traces: [], tool_events: [], refused: false,
        provider_name: model.name, model: model.model,
        thinking: option ? { label: option.label, option_id: option.id, config_version: body.thinking_config_version, kind: model.thinking.kind } : null,
        error_detail: failAnswer ? "受控失败" : null };
      if (body.retry_message_id) conversation.messages = conversation.messages.filter(item => item.id !== body.retry_message_id);
      else conversation.messages.push({ id: `u${next}`, sequence: next, role: "user", status: "completed", content: body.query,
        citations: [], traces: [], refused: false });
      conversation.messages.push(answer);
      return route.fulfill({ contentType: "text/event-stream", body: `event: done\ndata: ${JSON.stringify(answer)}\n\n` });
    }
    if (path.endsWith("/stop")) { if (releaseSend) { releaseSend(); releaseSend = null; } return reply({}); }
    if (path === "/auth/workspace") {
      session.workspace_id = route.request().postDataJSON().workspace_id; return reply(session);
    }
    if (fixtures[path]) return reply(fixtures[path]);
    return reply({ detail: `Missing fixture: ${path}` }, 404);
  });
  const noOverflow = async () => check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/admin/models/deepseek`);
    const kind = page.getByRole("combobox", { name: "思考方式", exact: true });
    await kind.waitFor();
    await kind.selectOption("effort");
    await page.getByRole("checkbox", { name: /^低 low$/ }).check();
    await page.getByRole("checkbox", { name: /更高 xhigh/ }).waitFor({ state: "detached" });
    await page.getByRole("checkbox", { name: /最高 max/ }).check();
    await page.getByRole("textbox", { name: "max 显示名称", exact: true }).fill("深入分析");
    await page.getByRole("combobox", { name: "默认思考设置" }).selectOption("max");
    await page.getByRole("tab", { name: "模型基准价", exact: true }).click();
    await page.getByRole("button", { name: "继续编辑", exact: true }).click();
    check(await page.getByRole("textbox", { name: "max 显示名称", exact: true }).inputValue() === "深入分析", "Unsaved guard lost labels");
    conflict = true;
    await page.getByRole("button", { name: "保存连接配置", exact: true }).click();
    await page.getByRole("alert").filter({ hasText: "供应商已更新" }).waitFor();
    check(await page.getByRole("combobox", { name: "默认思考设置" }).inputValue() === "max", "Conflict lost selection");
    await page.getByRole("button", { name: "保存连接配置", exact: true }).click();
    await page.getByText("连接配置已保存", { exact: true }).waitFor();
    check(saved.at(-1).thinking.default_option_id === "max", "Saved wrong default");
    check(!("api_key" in saved.at(-1)), "Blank credential overwritten");
    await noOverflow();
    await page.screenshot({ path: `output/playwright/issue44-admin-effort-${width}.png`, fullPage: true });
    // Restore the fixture's default before checking user page initial state.
    models[0].thinking.default_option_id = "high";
    await page.goto(`${origin}/admin/models/bailian`);
    await kind.waitFor();
    await page.getByRole("checkbox", { name: "仅支持流式响应", exact: true }).check();
    await page.getByRole("checkbox", { name: "模型始终思考", exact: true }).check();
    await page.getByRole("button", { name: "添加预算预设", exact: true }).click();
    await page.getByRole("spinbutton", { name: "预算（Token）", exact: true }).last().fill("2048");
    await page.getByRole("alert").filter({ hasText: "不同预设不能使用相同参数值" }).waitFor();
    check(!await page.getByRole("button", { name: "保存连接配置", exact: true }).isEnabled(), "Duplicate budget could be saved");
    await page.getByRole("spinbutton", { name: "预算（Token）", exact: true }).last().fill("16384");
    await noOverflow();
    await page.screenshot({ path: `output/playwright/issue44-admin-budget-${width}.png`, fullPage: true });
    await page.getByRole("tab", { name: "模型基准价", exact: true }).click();
    await page.getByRole("button", { name: "放弃修改", exact: true }).click();
    await page.getByRole("table", { name: "模型基准价", exact: true }).waitFor();
    conversation = null; requests = [];
    await page.goto(`${origin}/?chat=1`);
    const model = page.getByRole("combobox", { name: "本次请求模型" });
    const thinking = page.getByRole("combobox", { name: "本次请求思考设置" });
    const input = page.getByRole("textbox", { name: "向知识库提问" });
    const send = page.getByRole("button", { name: "发送问题", exact: true });
    await thinking.waitFor();
    check(await thinking.inputValue() === "high", "Wrong initial thinking");
    await thinking.focus();
    await thinking.press("Shift+Tab");
    check(await model.evaluate(el => document.activeElement === el), "Thinking keyboard order");
    await thinking.selectOption("low");
    await model.selectOption("bailian");
    await page.waitForFunction(() => document.querySelector('select[aria-label="本次请求思考设置"]')?.value === "small");
    check((await thinking.locator("option").allTextContents()).some(text => text.includes("8192 Token")), "Budget units absent");
    await thinking.selectOption("large");
    await input.fill("验证请求级思考");
    failAnswer = true;
    await send.click();
    await page.getByRole("button", { name: "停止回答" }).waitFor();
    for (let count = 0; count < 100 && !releaseSend; count++) await page.waitForTimeout(10);
    check(!await thinking.isEnabled() && !await model.isEnabled(), "Controls active during generation");
    check(requests.at(-1).thinking_option_id === "large", "Selected thinking not sent");
    releaseSend(); releaseSend = null;
    await page.getByText("受控失败", { exact: true }).waitFor();
    failAnswer = false;
    await thinking.selectOption("small");
    await page.getByRole("button", { name: "重试", exact: true }).click();
    for (let count = 0; count < 100 && !releaseSend; count++) await page.waitForTimeout(10);
    check(requests.at(-1).retry_message_id && requests.at(-1).thinking_option_id === "small", "Retry did not use current thinking");
    releaseSend(); releaseSend = null;
    await page.getByText("请求思考设置：精简", { exact: true }).waitFor();
    check(await thinking.inputValue() === "small", "Completion reset choice");
    await noOverflow();
    if (width === 390) {
      const box = await thinking.boundingBox();
      check(box.height >= 44 && box.x + box.width <= width, "Mobile thinking target or overflow");
    }
    await page.screenshot({ path: `output/playwright/issue44-chat-${width}.png` });
    await model.selectOption("fixed");
    await thinking.waitFor({ state: "detached" });
    await page.getByText("固定思考", { exact: true }).waitFor();
    await model.selectOption("unsupported");
    check(await thinking.count() === 0 && await page.getByText("固定思考", { exact: true }).count() === 0, "Unsupported thinking shown");
    await model.selectOption("unknown");
    check(await thinking.count() === 0, "Unconfigured thinking shown");
    await model.selectOption("deepseek");
    await thinking.waitFor();
    await thinking.selectOption("low");
    await page.reload();
    await thinking.waitFor();
    check(await model.inputValue() === "deepseek" && await thinking.inputValue() === "high", "Refresh persisted selection");
    await thinking.selectOption("low");
    await input.fill("配置冲突保留问题");
    rejectSend = true;
    await send.click();
    await page.getByRole("alert").filter({ hasText: "思考配置已更新，请确认当前设置" }).waitFor();
    check(!await send.isEnabled(), "Stale settings auto-confirmed");
    check(await input.inputValue() === "配置冲突保留问题", "Conflict cleared question");
    await page.getByRole("button", { name: "确认设置", exact: true }).click();
    check(await send.isEnabled(), "Confirmation did not unlock send");
    await noOverflow();
    await page.screenshot({ path: `output/playwright/issue44-confirmed-${width}.png` });
    results.push(`${width}: admin effort/budget, save/conflict/guards, current request/retry, generation lockout, privacy, reset, stale confirmation, keyboard, responsive`);
  }
  failModels = true;
  await page.reload();
  await page.getByRole("alert").filter({ hasText: "模型列表暂不可用" }).waitFor();
  await page.getByRole("textbox", { name: "向知识库提问" }).fill("错误重试保留问题");
  failModels = false;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await page.getByRole("combobox", { name: "本次请求思考设置" }).waitFor();
  check(await page.getByRole("textbox", { name: "向知识库提问" }).inputValue() === "错误重试保留问题", "Retry cleared input");
  check(errors.length === 0, errors.join("; "));
  return { passed: results, errors };
}
