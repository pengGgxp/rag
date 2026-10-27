// Run with playwright-cli run-code --filename=scripts/check-model-settings.js.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const results = [];
  let billingFailure = false;
  let billingDelay = null;
  const fixtures = {
    "/auth/options": { oauth_name: null, password_reset: false, setup_available: false, first_run: false },
    "/auth/me": { user: { id: "u1", name: "验收管理员", email: "admin@example.com", has_password: true,
      oauth_linked: false, email_verified: true, wechat_linked: false, is_site_admin: true },
      workspaces: [{ id: "w1", name: "验收工作区", role: "admin" }], workspace_id: "w1", csrf_token: "test" },
    "/health": { status: "ok", database: "ok", redis: "ok" },
    "/models/available": { default_provider_id: null, providers: [] },
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
    if (path === "/usage") {
      if (billingDelay) await billingDelay;
      if (billingFailure) return route.fulfill({ status: 503, json: { detail: "积分信息加载失败" } });
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
  await page.locator(".settings-sidebar").getByRole("link", { name: "积分与充值", exact: true }).click();
  await title("积分与充值").waitFor({ timeout: 750 });
  releaseRoutes();
  await page.waitForURL(url => url.pathname === "/settings/billing");
  await page.locator(".billing-heading strong").filter({ hasText: /[0-9]/ }).waitFor();
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
  await navigate("security", "登录与安全");
  const password = page.getByLabel("新密码", { exact: true });
  await password.fill("not-persisted-test-password");
  await close.click();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  check(await password.inputValue() === "not-persisted-test-password", "Unsaved password was cleared");
  await page.screenshot({ path: "output/playwright/issue41-settings-desktop.png" });
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "放弃修改", exact: true }).click();
  await closed("/?chat=1");
  results.push("unsaved password close/continue/Escape guards");

  await page.goto(`${base}/?extensions=1`);
  await open();
  await navigate("security", "登录与安全");
  await page.keyboard.press("Escape");
  await closed("/?extensions=1");
  await page.waitForFunction(() => document.activeElement?.matches("[data-account-trigger]"));
  for (const [path, name] of [["", "个人资料"], ["/workspace", "基本信息"], ["/security", "登录与安全"], ["/billing", "积分与充值"]]) {
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
  let releaseBilling;
  billingDelay = new Promise(resolve => { releaseBilling = resolve; });
  billingFailure = true;
  await navigate("billing", "积分与充值");
  await page.getByRole("status", { name: "加载中", exact: true }).waitFor();
  check(await close.isVisible(), "Data load hid close button");
  releaseBilling();
  billingDelay = null;
  await page.getByRole("alert").filter({ hasText: "积分信息加载失败" }).waitFor();
  billingFailure = false;
  await page.getByRole("button", { name: "刷新充值记录", exact: true }).click();
  await page.locator(".billing-heading strong").filter({ hasText: /[0-9]/ }).waitFor();
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
  await navigate("security", "登录与安全");
  await password.waitFor();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Mobile horizontal overflow");
  await page.screenshot({ path: "output/playwright/issue41-settings-mobile.png" });
  await page.keyboard.press("Escape");
  await closed("/");
  await page.waitForFunction(() => document.activeElement?.matches("[data-workspace-menu]"));
  results.push("390px: single dialog, selector, no overflow, Escape/focus, reduced motion");
  check(!errors.length, `Browser errors: ${errors.join("; ")}`);
  return { passed: results, pageErrors: errors };
}
