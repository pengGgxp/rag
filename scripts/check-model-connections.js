// Playwright CLI run-code fixture acceptance; live upstream acceptance is separate.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const check = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  const thinking = { kind: "unconfigured", adapter: "none", options: [], default_option_id: null,
    budget_min: null, budget_max: null, supports_tools: true, stream_only: false };
  const prices = ["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"].map(
    capability => ({ capability, unit: "tokens", unit_size: 1000, credits_per_unit: 2, enabled: true, version: 1 }));
  const connections = [{ id: "c1", name: "研发共享连接", platform: "custom", base_url: "https://example.test/v1",
    api_protocol: "chat_completions", timeout_seconds: 60, enabled: true, has_api_key: true, version: 1 }];
  const models = [{ id: "m1", key: "generated", connection_id: "c1", connection_name: "研发共享连接",
    connection_version: 1, platform: "custom", name: "现有模型", model: "existing-chat",
    enabled: true, available: true, unavailable_reason: null, is_default: true, version: 1,
    thinking, thinking_version: 0, context_capacity: 128000, max_output_tokens: 8192,
    prices, configured: true, workspace_count: 2 }];
  const platforms = [
    { id: "deepseek", name: "DeepSeek", base_url: "https://api.deepseek.com", adapter: "deepseek" },
    { id: "bailian", name: "阿里云百炼", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1", adapter: "bailian" },
    { id: "openai", name: "OpenAI", base_url: "https://api.openai.com/v1", adapter: "openai" },
    { id: "custom", name: "自定义兼容接口", base_url: "", adapter: "none" },
  ];
  let discoverFails = false, discoverEmpty = false, saveStatus = 200, discovered = 0, created = 0;
  let captured = null;
  let discoveryWait = null, releaseDiscovery;
  const read = c => ({ ...c, model_count: models.filter(m => m.connection_id === c.id).length,
    available_model_count: models.filter(m => m.connection_id === c.id && m.available).length });
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    const method = route.request().method(), body = () => route.request().postDataJSON();
    const reply = (json, status = 200) => route.fulfill({ json, status });
    if (path === "/auth/options") return reply({ first_run: false });
    if (path === "/auth/me") return reply({ user: { id: "admin", name: "验收管理员",
      email: "admin@example.test", is_site_admin: true, email_verified: true, has_password: true },
      csrf_token: "fixture", workspace_id: "w1", workspaces: [{ id: "w1", name: "验收", role: "admin" }] });
    if (path === "/site/admin/pricing-template") return reply({ version: 1, policies: prices });
    if (path === "/site/admin/model-connections/platforms") return reply(platforms);
    if (path === "/site/admin/model-connections/model-presets") return reply([]);
    if (path === "/site/admin/model-connections") {
      if (method === "POST") {
        const { api_key, ...payload } = body();
        captured = payload;
        const c = { ...payload, id: "c-new", version: 1, has_api_key: !!api_key };
        connections.push(c); return reply(read(c), 201);
      }
      return reply(connections.map(read));
    }
    const conn = path.match(/^\/site\/admin\/model-connections\/([^/]+)(\/models\/discover|\/models)?$/);
    if (conn) {
      const c = connections.find(v => v.id === conn[1]);
      if (conn[2] === "/models/discover") {
        discovered++;
        if (discoveryWait) await discoveryWait;
        if (discoverFails) return reply({ detail: "未能获取模型列表，请检查连接或手动添加模型" }, 502);
        return reply({ models: discoverEmpty ? [] : ["existing-chat", "new-fast", "new-pro"], connection_version: c.version });
      }
      if (conn[2] === "/models") {
        if (saveStatus !== 200) return reply({ detail: saveStatus === 409 ?
          "供应商连接已更新，请重新加载后保存" : "保存暂时失败，请重试" }, saveStatus);
        const list = body().models.map((m, i) => ({ ...models[0], ...m, id: `new-${++created}`,
          prices: m.policies, connection_name: c.name, platform: c.platform, available: m.enabled }));
        models.push(...list); return reply(list, 201);
      }
      if (method === "PUT") {
        if (saveStatus !== 200) return reply({ detail: "保存失败，请重试" }, saveStatus);
        captured = body();
        const { api_key, ...payload } = body();
        Object.assign(c, payload, { version: c.version + 1 });
      }
      return reply(read(c));
    }
    if (path === "/site/admin/llm-providers") return reply(models);
    const model = path.match(/^\/site\/admin\/llm-providers\/([^/]+)$/);
    if (model) {
      const m = models.find(v => v.id === model[1]);
      if (method === "PUT") {
        if (saveStatus !== 200) return reply({ detail: saveStatus === 409 ? "模型已更新，请重新加载后保存" : "保存失败，请重试" }, saveStatus);
        captured = body(); Object.assign(m, captured, { prices: captured.policies, version: m.version + 1 });
      }
      return reply(m);
    }
    return reply({ detail: `Unhandled fixture ${path}` }, 404);
  });
  const heading = text => page.getByRole("heading", { level: 1, name: text, exact: true });
  const shot = async name => {
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Overflow ${name}`);
    await page.screenshot({ path: `output/playwright/issue52-${name}.png`, fullPage: true });
  };
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${base}/admin/models`);
  await heading("模型管理").waitFor();
  await page.getByRole("link", { name: "研发共享连接", exact: true }).waitFor();
  await shot("suppliers-1440");
  await page.getByRole("textbox", { name: "搜索供应商" }).fill("不存在");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.getByText("没有符合条件的供应商", { exact: true }).waitFor();
  await page.getByRole("textbox", { name: "搜索供应商" }).fill("");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.getByRole("link", { name: "添加供应商" }).click();
  check(await page.getByRole("textbox", { name: "接口地址" }).count() === 0, "Connection fields shown before choosing protocol");
  check(await page.getByRole("button", { name: "保存并选择模型" }).isDisabled(), "Protocol selection not required");
  await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).selectOption("responses");
  await page.getByText("/responses", { exact: true }).waitFor();
  await page.getByRole("button", { name: "DeepSeek", exact: true }).click();
  check(await page.getByRole("textbox", { name: "接口地址" }).inputValue() === "https://api.deepseek.com", "Platform URL");
  await page.getByRole("textbox", { name: "接口地址" }).fill("https://custom.example/v1");
  await page.locator('input[type="password"]').fill("test-secret-only");
  await page.getByRole("button", { name: "OpenAI", exact: true }).click();
  check(await page.getByRole("textbox", { name: "接口地址" }).inputValue() === "https://custom.example/v1", "Platform overwrote custom URL");
  check(await page.locator('input[type="password"]').inputValue() === "test-secret-only", "Platform lost key");
  await page.getByRole("textbox", { name: "供应商名称" }).fill("新共享供应商");
  await shot("new-connection-1440");
  await page.getByRole("button", { name: "保存并选择模型" }).click();
  await page.waitForURL(/connections\/c-new/);
  check(captured.api_protocol === "responses", "Protocol not saved");
  discoveryWait = new Promise(resolve => { releaseDiscovery = resolve; });
  await page.getByRole("button", { name: "获取模型列表" }).click();
  check(await page.getByRole("button", { name: "获取模型列表" }).isDisabled(), "Duplicate discovery allowed");
  await page.waitForFunction(() => Array.from(document.querySelectorAll('[role="tab"]')).some(
    el => el.textContent.includes("连接设置") && el.hasAttribute("disabled")));
  check(await page.getByRole("tab", { name: "连接设置", exact: true }).isDisabled(), "Busy connection tab navigation");
  releaseDiscovery(); discoveryWait = null;
  await page.getByRole("checkbox", { name: "new-fast", exact: true }).check();
  await page.getByRole("checkbox", { name: "new-pro", exact: true }).check();
  await page.getByRole("button", { name: "配置模型", exact: true }).click();
  await page.getByRole("button", { name: "保存并启用", exact: true }).waitFor();
  await page.getByRole("textbox", { name: "显示名称", exact: true }).first().fill("");
  await page.getByRole("button", { name: "保存并启用", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "请填写显示名称和模型名" }).waitFor();
  check(await page.locator('.model-draft[data-invalid="true"]').evaluate(el => el.open), "Invalid model not located");
  await page.getByRole("textbox", { name: "显示名称", exact: true }).first().fill("new-fast");
  await shot("configure-1440");
  saveStatus = 503;
  await page.getByRole("button", { name: "保存并启用", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "保存暂时失败" }).waitFor();
  check(await page.getByRole("button", { name: "重新加载并放弃修改" }).count() === 0, "Nonconflict lost draft");
  check(await page.getByRole("textbox", { name: "显示名称", exact: true }).first().inputValue() === "new-fast", "Lost model draft");
  saveStatus = 200;
  await page.getByRole("button", { name: "保存并启用", exact: true }).click();
  await page.getByRole("link", { name: "new-fast", exact: true }).waitFor();
  check(created === 2 && discovered === 1, "Unexpected bulk/discover calls");
  await page.getByRole("link", { name: "new-fast", exact: true }).click();
  await heading("new-fast").waitFor();
  check(await page.locator('input[type="password"]').count() === 0, "Model contains credential field");
  await page.getByRole("textbox", { name: "显示名称", exact: true }).fill("快速模型");
  await page.getByRole("tab", { name: "价格", exact: true }).click();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  check(await page.getByRole("textbox", { name: "显示名称", exact: true }).inputValue() === "快速模型", "Unsaved cancel");
  await page.getByRole("button", { name: "保存模型", exact: true }).click();
  await page.getByText("模型已保存", { exact: true }).waitFor();
  check(captured.policies.length === 3 && !("api_key" in captured), "Atomic model pricing/credentials");
  await page.getByRole("tab", { name: "价格", exact: true }).click();
  await shot("model-prices-1440");
  await page.reload();
  await page.getByRole("tab", { name: "价格", exact: true }).waitFor();
  await page.getByRole("tab", { name: "价格", exact: true }).focus();
  await page.keyboard.press("ArrowLeft");
  await page.getByRole("tab", { name: "模型设置", exact: true }).waitFor({ state: "attached" });
  check(await page.getByRole("tab", { name: "模型设置", exact: true }).evaluate(el => el === document.activeElement), "Tab keyboard focus");
  await page.keyboard.press("Enter");
  await page.waitForURL(/\/new-1$/);
  await page.getByRole("textbox", { name: "显示名称", exact: true }).waitFor();
  await page.getByRole("textbox", { name: "显示名称", exact: true }).fill("冲突草稿");
  saveStatus = 409;
  await page.getByRole("button", { name: "保存模型", exact: true }).click();
  await page.getByRole("button", { name: "重新加载并放弃修改", exact: true }).click();
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByRole("textbox", { name: "显示名称", exact: true }).waitFor();
  check(await page.getByRole("textbox", { name: "显示名称", exact: true }).inputValue() === "快速模型", "Reload conflict");
  saveStatus = 200;
  await page.goto(`${base}/admin/models/connections/c1?add=1`);
  discoverFails = true;
  await page.getByRole("button", { name: "获取模型列表" }).click();
  await page.getByRole("alert").filter({ hasText: "未能获取" }).waitFor();
  discoverFails = false; discoverEmpty = true;
  await page.getByRole("button", { name: "获取模型列表" }).click();
  await page.getByText("未获取到模型，可以手动添加。", { exact: true }).waitFor();
  await page.getByRole("textbox", { name: "手动添加模型", exact: true }).fill("manual-chat");
  await page.getByRole("button", { name: "添加", exact: true }).click();
  await page.getByRole("button", { name: "配置模型", exact: true }).click();
  await page.getByRole("button", { name: "保存草稿", exact: true }).click();
  await page.getByRole("link", { name: "manual-chat", exact: true }).waitFor();
  for (const width of [700, 390]) {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${base}/admin/models`);
    await page.getByRole("link", { name: "研发共享连接", exact: true }).waitFor();
    await shot(`suppliers-${width}`);
    await page.getByRole("link", { name: "研发共享连接", exact: true }).click();
    await page.getByRole("tab", { name: "连接设置", exact: true }).click();
    await page.getByRole("textbox", { name: "供应商名称", exact: true }).waitFor();
    await shot(`connection-${width}`);
    await page.goto(`${base}/admin/models/new`);
    await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).waitFor();
    check(await page.getByRole("textbox", { name: "供应商名称", exact: true }).count() === 0, "Protocol must precede details");
    await shot(`choose-protocol-${width}`);
    await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).focus();
    await page.keyboard.press("r");
    await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).selectOption("responses");
    await page.getByRole("textbox", { name: "供应商名称", exact: true }).waitFor();
    await shot(`new-${width}`);
    await page.locator(".admin-back").click();
    await page.getByRole("button", { name: "继续编辑", exact: true }).click();
    check(await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).inputValue() === "responses", "Unsaved dialog lost protocol");
    await page.locator(".admin-back").click();
    await page.getByRole("button", { name: "放弃修改", exact: true }).click();
    await page.waitForURL(`${base}/admin/models`);
    await page.goto(`${base}/admin/models/new-1?tab=pricing`);
    await page.getByRole("tab", { name: "价格", exact: true }).waitFor();
    await shot(`model-price-${width}`);
  }
  await page.goto(`${base}/admin/models/connections/c-new?tab=connection`);
  await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).waitFor();
  check(await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).inputValue() === "responses", "Saved protocol lost");
  await page.reload();
  await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).waitFor();
  check(await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).inputValue() === "responses", "Refresh lost protocol");
  await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).selectOption("chat_completions");
  saveStatus = 503;
  await page.getByRole("button", { name: "保存连接", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "保存失败" }).waitFor();
  check(await page.getByRole("combobox", { name: "OpenAI 接口", exact: true }).inputValue() === "chat_completions", "Failed save lost protocol");
  saveStatus = 200;
  await page.getByRole("button", { name: "保存连接", exact: true }).click();
  await page.getByText("供应商连接已保存", { exact: true }).waitFor();
  check(captured.api_protocol === "chat_completions" && !captured.api_key, "Protocol switch changed credential");
  check(!errors.length, errors.join("\n"));
  return { passed: true, created, discovered, viewportWidths: [1440, 700, 390] };
}
