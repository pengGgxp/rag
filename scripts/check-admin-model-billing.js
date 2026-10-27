// Run with playwright-cli run-code --filename=scripts/check-admin-model-billing.js.
async (page) => {
  page.on("dialog", dialog => dialog.accept());
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const results = [];
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const caps = ["llm_input_tokens", "llm_cached_input_tokens", "llm_output_tokens"];
  const prices = (rate = 4) => caps.map((capability, index) => ({
    capability, unit: "tokens", unit_size: index === 1 ? 10000 : 1000,
    credits_per_unit: rate * (index + 1), enabled: true, version: 2, configured: true, source: "model_provider",
  }));
  const providers = [
    { id: "p1", key: "deepseek", name: "DeepSeek", model: "deepseek-flash", prices: prices(), available: true, unavailable_reason: null },
    { id: "p2", key: "qwen", name: "通义千问长名称模型配置用于验证窄屏排版", model: "qwen3.8-max-0902", prices: prices(8), available: true, unavailable_reason: null },
    { id: "p3", key: "disabled", name: "停用模型", model: "disabled-model", prices: prices(0), available: false, unavailable_reason: "model_disabled", enabled: false },
    { id: "p4", key: "missing", name: "缺价模型", model: "missing-cache-price", prices: prices().filter(p => p.capability !== caps[1]), available: false, unavailable_reason: "pricing_unconfigured" },
  ].map(p => ({ base_url: "https://model.example/v1", timeout_seconds: 60, has_api_key: true,
    enabled: true, published: true, is_default: false, configured: true, version: 2, workspace_count: 0, ...p }));
  const workspaces = Array.from({ length: 12 }, (_, i) => ({ id: `w${i + 1}`,
    name: i === 1 ? "第二工作区" : i === 2 ? "慢请求工作区" : `知识运营工作区 ${i + 1}`, role: "admin" }));
  const documents = [
    { capability: "ocr_pages", unit: "pages", unit_size: 1, credits_per_unit: 10, enabled: true, configured: true, version: 3, source: "platform_documents" },
    { capability: "embedding_tokens", unit: "tokens", unit_size: 1000, credits_per_unit: 2, enabled: true, configured: true, version: 3, source: "platform_documents" },
  ];
  const billing = id => ({ version: 2, multiplier: id === "w2" ? "2.00" : "1.00",
    shared_pricing_enabled: true, billing_enabled: false, document_policies: documents,
    models: providers.map(p => ({ provider_id: p.id, name: p.name, model: p.model, available: p.available,
      unavailable_reason: p.unavailable_reason, policies: caps.map(capability => p.prices.find(item => item.capability === capability) ??
        { capability, unit: "tokens", unit_size: 1000, credits_per_unit: null, configured: false, enabled: false, version: 2 }) })) });
  const balances = Object.fromEntries(workspaces.map(w => [w.id, billing(w.id)]));
  let conflict = false, failure = false, delay = null, release;
  let template = { version: 3, configured: true, shared_pricing_enabled: true, billing_enabled: false, policies: [...documents, ...prices()] };
  await page.route(url => url.origin === "http://localhost:8000" || (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const url = new URL(route.request().url());
    const path = url.pathname.replace(/^\/api/, "");
    const method = route.request().method();
    const reply = (json, status = 200) => route.fulfill({ status, json });
    if (path === "/auth/options") return reply({ first_run: false, setup_available: false, password_reset: false });
    if (path === "/auth/me") return reply({ user: { id: "u1", name: "验收管理员", email: "admin@example.com", is_site_admin: true,
      email_verified: true, has_password: true }, workspace_id: "w1", csrf_token: "test", workspaces });
    if (path === "/site/admin/workspaces") {
      const list = workspaces.filter(w => `${w.name} ${w.id}`.includes(url.searchParams.get("q") ?? ""));
      const current = Number(url.searchParams.get("page") ?? 1);
      return reply({ items: list.slice((current - 1) * 10, current * 10), total: list.length, page: current, page_size: 10 });
    }
    const target = path.match(/^\/site\/admin\/workspaces\/([^/]+)(\/billing-settings)?$/);
    if (target) {
      const id = target[1];
      if (id === "w3" && delay) await delay;
      if (failure && target[2]) return reply({ detail: "计费数据加载失败" }, 503);
      if (!target[2]) return reply(workspaces.find(w => w.id === id));
      if (method === "PUT") {
        if (conflict) { conflict = false; return reply({ detail: "工作区倍率已更新，请重新加载后保存" }, 409); }
        const payload = route.request().postDataJSON();
        balances[id] = { ...balances[id], multiplier: payload.multiplier, version: balances[id].version + 1 };
      }
      return reply(balances[id]);
    }
    if (path === "/site/admin/pricing-template") {
      if (method === "PUT") template = { ...template, ...route.request().postDataJSON(), version: template.version + 1 };
      return reply(template);
    }
    if (path === "/site/admin/llm-providers") {
      if (method === "POST") {
        const { api_key, ...form } = route.request().postDataJSON();
        const created = { ...providers[0], ...form, id: "p-new", version: 1, enabled: false, prices: prices(), has_api_key: Boolean(api_key) };
        providers.push(created);
        return reply(created, 201);
      }
      return reply(providers);
    }
    const model = path.match(/^\/site\/admin\/llm-providers\/([^/]+)(\/pricing|\/default)?$/);
    if (model) {
      const p = providers.find(p => p.id === model[1]);
      if (!p) return reply({ detail: "模型配置不存在" }, 404);
      if (method !== "GET") {
        if (conflict) { conflict = false; return reply({ detail: "供应商已更新，请重新加载后保存" }, 409); }
        const payload = route.request().postDataJSON();
        if (model[2] === "/pricing") p.prices = payload.policies;
        else if (model[2] === "/default") p.is_default = true;
        else { const { api_key, ...form } = payload; Object.assign(p, form); }
        p.version++;
      }
      return reply(p);
    }
    return reply({ detail: `Missing fixture ${path}` }, 404);
  });
  const title = text => page.getByRole("heading", { level: 1, name: text, exact: true });
  const noOverflow = async () => check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Page overflow");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${base}/admin/pricing`);
  await title("工作区倍率").waitFor();
  await page.getByRole("button", { name: "管理计费" }).first().waitFor();
  check(await page.locator("#workspace-multiplier").count() === 0, "Unselected workspace shows editor");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await page.getByText("知识运营工作区 11", { exact: true }).waitFor();
  await page.getByRole("button", { name: "上一页", exact: true }).click();
  await page.getByRole("textbox", { name: "搜索工作区" }).fill("第二");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.getByRole("button", { name: "管理计费", exact: true }).click();
  await page.waitForURL(/workspace=w2/);
  const multiplier = page.getByRole("spinbutton", { name: "消费倍率" });
  await multiplier.waitFor();
  check(await multiplier.inputValue() === "2.00", "Wrong target multiplier");
  check(await page.locator(".model-price-row").count() === 2, "Unused available models absent");
  await page.getByRole("checkbox", { name: "显示不可用模型" }).check();
  check(await page.locator(".model-price-row").count() === 4, "Unavailable toggle");
  await page.getByText("价格待配置", { exact: true }).waitFor();
  await page.getByRole("checkbox", { name: "显示不可用模型" }).uncheck();
  await multiplier.fill("1.25");
  check((await page.locator(".model-price-row").first().innerText()).includes("5.00"), "Draft model effective price");
  await page.screenshot({ path: "output/playwright/issue42-billing-1440.png", fullPage: true });
  await page.getByRole("button", { name: "更换", exact: true }).click();
  await page.getByRole("dialog", { name: "更换管理工作区" }).waitFor();
  await page.getByRole("radio", { name: /知识运营工作区 1 w1$/ }).check();
  await page.getByRole("button", { name: "确认更换", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "当前倍率尚未保存" }).waitFor();
  await page.getByRole("button", { name: "取消", exact: true }).click();
  check(await multiplier.inputValue() === "1.25", "Cancel discarded multiplier");
  await page.getByRole("button", { name: "更换", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.getByRole("dialog").press("Escape");
  await page.waitForTimeout(150);
  console.log("escape-state", await page.locator("[role=dialog]").count(), await page.locator("[role=dialog]").first().getAttribute("data-state").catch(() => "none"));
  await page.getByRole("dialog").waitFor({ state: "detached" });
  await page.waitForFunction(() => document.activeElement?.matches("[data-workspace-target]"));
  check(await page.getByRole("button", { name: "更换", exact: true }).evaluate(el => el === document.activeElement), "Picker focus not restored");
  conflict = true;
  await page.getByRole("button", { name: "保存倍率", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "工作区倍率已更新" }).waitFor();
  check(await multiplier.inputValue() === "1.25", "Conflict lost draft");
  await page.getByRole("button", { name: "保存倍率", exact: true }).click();
  await page.getByText("消费倍率已保存，后续新请求生效", { exact: true }).waitFor();
  await page.getByRole("button", { name: "更换", exact: true }).click();
  await page.getByRole("radio", { name: /知识运营工作区 1 w1$/ }).check();
  await page.getByRole("button", { name: "确认更换", exact: true }).click();
  await page.waitForURL(/workspace=w1/);
  check(await multiplier.inputValue() === "1.00", "Workspace data mismatch");
  await page.goBack(); await page.waitForURL(/workspace=w2/);
  await page.goForward(); await page.waitForURL(/workspace=w1/);
  await page.reload(); await multiplier.waitFor();
  await noOverflow();
  results.push("target search/pagination, all-model previews, draft/units, unsaved switch, Escape/focus, conflict/save, history/refresh");

  delay = new Promise(resolve => { release = resolve; });
  await page.goto(`${base}/admin/pricing?workspace=w3`);
  await page.getByRole("button", { name: "更换", exact: true }).waitFor();
  await page.getByRole("button", { name: "更换", exact: true }).click();
  await page.getByRole("radio", { name: /第二工作区/ }).check();
  await page.getByRole("button", { name: "确认更换", exact: true }).click();
  await page.waitForURL(/workspace=w2/);
  release(); delay = null;
  await page.locator(".admin-target-context strong").filter({ hasText: "第二工作区" }).waitFor();
  check(await multiplier.inputValue() === "1.25", "Late target response overwrote editor");
  failure = true;
  await page.reload();
  await page.getByRole("alert").filter({ hasText: "计费数据加载失败" }).waitFor();
  check(await page.getByRole("button", { name: "更换", exact: true }).isVisible(), "Load error hid target");
  failure = false;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await multiplier.waitFor();
  results.push("slow target cancellation and local data error/retry keep target context");

  await page.getByRole("link", { name: "平台价格", exact: true }).click();
  await title("平台价格").waitFor();
  check(!new URL(page.url()).searchParams.has("workspace"), "Platform price retains workspace scope");
  check(await page.locator(".admin-target-context").count() === 0, "Global price shows workspace");
  await page.getByRole("spinbutton", { name: "平台文档基准价OCR 页数积分", exact: true }).fill("12");
  await page.getByRole("button", { name: "保存平台价格与初始价", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "平台基准价已保存" }).waitFor();
  results.push("global price navigation and explicit full-page save");

  await page.getByRole("link", { name: "模型管理", exact: true }).click();
  await title("模型管理").waitFor();
  await page.getByRole("link", { name: "DeepSeek", exact: true }).waitFor();
  check(await page.getByText("https://model.example/v1", { exact: true }).count() === 0, "List leaks connection clutter");
  await page.getByRole("combobox", { name: "筛选", exact: true }).selectOption("unavailable");
  check(await page.getByText("停用模型", { exact: true }).count() === 1
    && await page.getByText("缺价模型", { exact: true }).count() === 1, "Status filter failed");
  await page.getByRole("combobox", { name: "筛选", exact: true }).selectOption("all");
  await page.getByRole("textbox", { name: "搜索模型", exact: true }).fill("deep");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  check(await page.locator(".model-catalog-row").count() === 1, "Search failed");
  await page.getByRole("link", { name: "DeepSeek", exact: true }).click();
  await title("DeepSeek").waitFor();
  await page.getByRole("textbox", { name: "显示名称", exact: true }).fill("DeepSeek draft");
  await page.getByRole("tab", { name: "模型基准价", exact: true }).click();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  check(await page.getByRole("textbox", { name: "显示名称", exact: true }).inputValue() === "DeepSeek draft", "Tab guard lost input");
  await page.getByRole("tab", { name: "模型基准价", exact: true }).click();
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByRole("table", { name: "模型基准价", exact: true }).waitFor();
  await page.getByRole("spinbutton", { name: "模型基准价模型输入 Token积分", exact: true }).fill("9");
  conflict = true;
  await page.getByRole("button", { name: "保存模型基准价", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "供应商已更新" }).waitFor();
  check(await page.getByRole("spinbutton", { name: "模型基准价模型输入 Token积分", exact: true }).inputValue() === "9", "Model conflict lost price");
  await page.getByRole("button", { name: "重新加载并放弃修改", exact: true }).click();
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await page.getByRole("spinbutton", { name: "模型基准价模型输入 Token积分", exact: true }).fill("9");
  await page.getByRole("button", { name: "保存模型基准价", exact: true }).click();
  await page.getByText("模型基准价已保存，后续新调用使用新价格", { exact: true }).waitFor();
  await page.getByRole("tab", { name: "连接配置", exact: true }).click();
  check(await page.getByLabel("API Key", { exact: false }).inputValue() === "", "Secret was returned to form");
  await page.getByRole("textbox", { name: "显示名称", exact: true }).fill("DeepSeek renamed");
  await page.getByRole("button", { name: "保存连接配置", exact: true }).click();
  await title("DeepSeek renamed").waitFor();
  await page.getByRole("button", { name: "设为问答默认模型", exact: true }).click();
  await page.getByText("问答默认模型", { exact: true }).waitFor();
  await page.screenshot({ path: "output/playwright/issue42-model-connection-1440.png", fullPage: true });
  await page.getByRole("tab", { name: "模型基准价", exact: true }).click();
  await page.waitForURL(/tab=pricing/);
  await page.reload();
  await page.getByRole("table", { name: "模型基准价", exact: true }).waitFor();
  await page.screenshot({ path: "output/playwright/issue42-model-pricing-1440.png", fullPage: true });
  results.push("model list/search/status, route details, tab guards, conflict/reload, independent saves/default, redaction/refresh");

  await page.goto(`${base}/admin/models/new`);
  await title("添加模型").waitFor();
  check(await page.getByRole("tab", { name: "模型基准价", exact: true }).isDisabled(), "New model pricing before connection");
  await page.getByRole("textbox", { name: "显示名称", exact: true }).fill("新模型");
  await page.getByRole("textbox", { name: "模型名", exact: true }).fill("new-model");
  await page.getByRole("textbox", { name: "配置标识", exact: true }).fill("new-model");
  await page.getByRole("textbox", { name: "接口地址", exact: true }).fill("https://new.example/v1");
  await page.getByLabel("API Key", { exact: false }).fill("fixture-only-secret");
  await page.getByRole("button", { name: "保存并配置基准价", exact: true }).click();
  await page.waitForURL(/p-new\?tab=pricing/);
  await page.getByRole("table", { name: "模型基准价", exact: true }).waitFor();
  results.push("create connection then route to model pricing");

  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    for (const [path, name] of [
      ["/admin/pricing?workspace=w1", "billing"],
      ["/admin/pricing/template", "platform"],
      ["/admin/models", "models"],
      ["/admin/models/p2", "connection"],
      ["/admin/models/p2?tab=pricing", "pricing"],
    ]) {
      await page.goto(`${base}${path}`);
      await page.getByRole("heading", { level: 1 }).waitFor();
      await page.locator(".admin-skeleton").waitFor({ state: "detached" });
      await noOverflow();
      await page.screenshot({ path: `output/playwright/issue42-${name}-${width}.png`, fullPage: true });
    }
    await page.goto(`${base}/admin/pricing?workspace=w1`);
    await page.getByRole("button", { name: "更换", exact: true }).click();
    await page.getByRole("dialog").waitFor();
    await page.getByRole("radio").first().waitFor();
    await page.screenshot({ path: `output/playwright/issue42-picker-${width}.png` });
    await page.keyboard.press("Shift+Tab");
    check(await page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]'))), "Dialog focus escaped");
    await page.getByRole("dialog").press("Escape");
    console.log("responsive-escape", width, await page.locator("[role=dialog]").count(), await page.locator("[role=dialog]").first().getAttribute("data-state").catch(() => "none"));
    await page.getByRole("dialog").waitFor({ state: "detached" });
  }
  await page.goto(`${base}/admin/models/unknown`);
  await page.getByRole("alert").filter({ hasText: "模型配置不存在" }).waitFor();
  check(await page.getByRole("link", { name: "模型管理", exact: true }).count() > 0, "Missing model lost navigation");
  check(errors.length === 0, errors.join("\n"));
  results.push("1440/700/390 screenshots, no page overflow, long names, reduced motion, focus trap, Escape, 404");
  return results;
}
