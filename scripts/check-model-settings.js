// Run with playwright-cli run-code --filename=scripts/check-model-settings.js.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const results = [];
  let modelFailure = false;
  let saveConflict = false;
  let modelDelay = null;
  const models = { version: 0, provider_id: "old", providers: [
    { id: "old", name: "现有模型", model: "existing", available: true, enabled: true, unavailable_reason: null },
    { id: "new", name: "新供应商", model: "new-model", available: true, enabled: true, unavailable_reason: null },
    { id: "disabled", name: "已停用模型", model: "disabled", available: false, enabled: false, unavailable_reason: "model_disabled" },
    { id: "missing", name: "待配置模型", model: "missing", available: false, enabled: true, unavailable_reason: "pricing_unconfigured" },
    { id: "pending", name: "未就绪模型", model: "pending", available: false, enabled: true, unavailable_reason: "service_not_ready" },
  ] };
  const fixtures = {
    "/auth/options": { oauth_name: null, password_reset: false, setup_available: false, first_run: false },
    "/auth/me": { user: { id: "u1", name: "验收管理员", email: "admin@example.com", has_password: true,
      oauth_linked: false, email_verified: true, wechat_linked: false, is_site_admin: true },
      workspaces: [{ id: "w1", name: "验收工作区", role: "admin" }], workspace_id: "w1", csrf_token: "test" },
    "/health": { status: "ok", database: "ok", redis: "ok" },
    "/knowledge-bases": [], "/conversations": [], "/agent/skills": [], "/agent/mcp": [],
    "/ingestion/options": { third_party_processor: null, max_pdf_pages: null, max_upload_bytes: 209715200 },
    "/usage": { balance: 1200, lifetime_spent: 80, lifetime_granted: 1280, pending_credits: 0, entries: [] },
    "/auth/members": [], "/audit-logs": { items: [], total: 0, page: 1, page_size: 10 },
    "/payments/catalog": { enabled: false, packages: [] },
    "/payments/orders": { items: [], total: 0, page: 1, page_size: 10 },
  };
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    if (path === "/settings/models") {
      if (modelDelay) await modelDelay;
      if (modelFailure) return route.fulfill({ status: 503, json: { detail: "模型列表加载失败" } });
      if (route.request().method() === "PUT") {
        if (saveConflict) return route.fulfill({ status: 409, json: { detail: "工作区模型已更新，请重新加载" } });
        const body = route.request().postDataJSON();
        models.provider_id = body.provider_id;
        models.version++;
      }
      return route.fulfill({ json: models });
    }
    if (!(path in fixtures)) throw new Error(`Missing API fixture: ${path}`);
    await route.fulfill({ json: fixtures[path] });
  });
  const frame = page.locator(".modal-settings");
  const title = name => page.getByRole("heading", { level: 1, name, exact: true });
  const close = page.getByRole("button", { name: "关闭", exact: true });
  const open = async (name = "工作区设置") => {
    await page.locator("[data-account-trigger]:visible, [data-workspace-menu]:visible").first().waitFor();
    if (!await page.locator("[data-account-trigger]").isVisible())
      await page.locator('[data-workspace-menu]').click();
    await page.locator("[data-account-trigger]").click();
    await page.getByRole("menuitem", { name, exact: true }).click();
  };
  const navigate = async (path, name) => {
    if ((await page.viewportSize()).width <= 700)
      await page.getByRole("combobox", { name: "选择设置页面" }).selectOption(path);
    else await page.locator(".settings-sidebar").getByRole("link", { name, exact: true }).click();
    await title(name).waitFor();
    await page.waitForURL(url => url.pathname === `/settings${path === "account" ? "" : `/${path}`}`);
  };
  const closed = async route => {
    await frame.waitFor({ state: "detached" });
    await page.waitForURL(url => url.pathname + url.search === route);
  };
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(base);
  await page.getByRole("textbox", { name: "搜索知识库", exact: true }).fill("保留搜索");
  let releaseRoutes;
  const routeDelay = new Promise(resolve => { releaseRoutes = resolve; });
  const slowRoutes = async route => { await routeDelay; await route.continue(); };
  await page.route(url => url.origin === base && url.pathname.startsWith("/settings"), slowRoutes);
  await open();
  await frame.waitFor({ timeout: 750 });
  await title("基本信息").waitFor({ timeout: 750 });
  check(new URL(page.url()).pathname === "/", "Window waited for cold settings route");
  check(await page.getByRole("dialog").count() === 1, "Opening duplicated dialogs");
  await page.getByRole("status", { name: "加载中", exact: true }).waitFor();
  await page.screenshot({ path: "output/playwright/issue40-settings-route-loading.png" });
  await page.getByRole("button", { name: "重试", exact: true }).waitFor({ timeout: 18_000 });
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await page.getByRole("status", { name: "加载中", exact: true }).waitFor();
  // Internal navigation must also work while the initial route is outstanding.
  await page.locator(".settings-sidebar").getByRole("link", { name: "模型选择", exact: true }).click();
  await title("模型选择").waitFor({ timeout: 750 });
  releaseRoutes();
  await page.waitForURL(url => url.pathname === "/settings/models");
  await page.getByRole("combobox", { name: "当前模型" }).waitFor();
  const dialogHandle = await frame.elementHandle();
  await navigate("workspace", "基本信息");
  check(await dialogHandle.evaluate(el => el.isConnected), "Route replacement remounted the settings dialog");
  await close.click();
  await closed("/");
  check(await page.getByRole("textbox", { name: "搜索知识库", exact: true }).inputValue() === "保留搜索", "Workspace state lost");
  await page.goForward();
  await title("基本信息").waitFor();
  await page.goBack();
  await closed("/");
  results.push("cold/slow route: immediate single frame, pending section switch, persistent dialog, history and preserved search");
  await page.unroute(url => url.origin === base && url.pathname.startsWith("/settings"), slowRoutes);

  // Cancel an outstanding, previously unvisited route and release its late response.
  await page.goto(`${base}/?chat=1`);
  await page.getByRole("textbox", { name: "向知识库提问", exact: true }).fill("保留问答草稿");
  let releaseCancelled;
  const cancelledDelay = new Promise(resolve => { releaseCancelled = resolve; });
  const cancelledRoute = async route => { await cancelledDelay; await route.continue(); };
  await page.route(url => url.origin === base && url.pathname === "/settings", cancelledRoute);
  await open("我的账号");
  await title("个人资料").waitFor({ timeout: 750 });
  await close.click();
  releaseCancelled();
  await closed("/?chat=1");
  await page.waitForLoadState("networkidle");
  check(await frame.count() === 0, "Late response reopened a closed window");
  check(await page.getByRole("textbox", { name: "向知识库提问", exact: true }).inputValue() === "保留问答草稿", "Chat draft lost");
  await page.unroute(url => url.origin === base && url.pathname === "/settings", cancelledRoute);
  results.push("close during cold navigation: no late reopening; chat draft preserved");

  await open();
  await navigate("models", "模型选择");
  const select = page.getByRole("combobox", { name: "当前模型" });
  await select.selectOption("new");
  await close.click();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  check(await select.inputValue() === "new", "Unsaved selection was cleared");
  saveConflict = true;
  await page.getByRole("button", { name: "保存选择", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "工作区模型已更新" }).waitFor();
  saveConflict = false;
  await page.getByRole("button", { name: "保存选择", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "模型已更新" }).waitFor();
  check(await select.inputValue() === "new", "Model selection did not survive save");
  check(await select.locator('option[value="disabled"]').evaluate(el => el.disabled), "Disabled provider selectable");
  check(await select.locator('option[value="missing"]').textContent().then(text => text.includes("价格待配置")), "Missing safe unavailable reason");
  await page.screenshot({ path: "output/playwright/issue40-settings-desktop.png" });
  await navigate("security", "登录与安全");
  await page.getByLabel("新密码", { exact: true }).fill("not-persisted-test-password");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await closed("/?chat=1");
  results.push("model save/conflict/reasons and unsaved selection/password guards");

  await page.goto(`${base}/?extensions=1`);
  await open();
  await navigate("models", "模型选择");
  await page.keyboard.press("Escape");
  await closed("/?extensions=1");
  await page.waitForFunction(() => document.activeElement?.matches("[data-account-trigger]"));
  for (const [path, name] of [["", "个人资料"], ["/workspace", "基本信息"], ["/models", "模型选择"], ["/billing", "积分与充值"]]) {
    await page.goto(`${base}/settings${path}?returnTo=%2F%3Fchat%3D1`);
    await title(name).waitFor();
    await page.reload();
    await title(name).waitFor();
    check(await frame.count() === 1, "Direct settings route duplicated frame");
    await close.click();
    await closed("/?chat=1");
  }
  results.push("Agent, Escape/focus return, direct routes and refresh");

  await page.goto(base);
  await open();
  let releaseModels;
  modelDelay = new Promise(resolve => { releaseModels = resolve; });
  modelFailure = true;
  await navigate("models", "模型选择");
  await page.getByRole("status", { name: "加载中", exact: true }).waitFor();
  check(await close.isVisible(), "Data load hid close button");
  releaseModels();
  modelDelay = null;
  await page.getByRole("alert").filter({ hasText: "模型列表加载失败" }).waitFor();
  modelFailure = false;
  await page.getByRole("button", { name: "重试", exact: true }).click();
  await select.waitFor();
  for (let index = 0; index < 12; index++) {
    await page.keyboard.press("Tab");
    check(await page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]'))), "Keyboard focus escaped settings");
  }
  await close.click();
  await closed("/");
  results.push("data loading/failure stays inside frame; local retry");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto(base);
  await open();
  await title("基本信息").waitFor();
  check(await page.getByRole("dialog").count() === 1, "Mobile settings stacked on navigation drawer");
  await navigate("models", "模型选择");
  await select.waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile horizontal overflow");
  await page.screenshot({ path: "output/playwright/issue40-settings-mobile.png" });
  await page.keyboard.press("Escape");
  await closed("/");
  await page.waitForFunction(() => document.activeElement?.matches("[data-workspace-menu]"));
  results.push("390px: single dialog, selector, no overflow, Escape/focus, reduced motion");
  check(!errors.length, `Browser errors: ${errors.join("; ")}`);
  return { passed: results, pageErrors: errors };
}
