// Additional fault/clock scenarios against the same local build. All cloud endpoints are mocked.
async (page) => {
  const base = "http://localhost:3127", api = "http://localhost:8127";
  const assert = (ok, why) => { if (!ok) throw new Error(why); };
  const conversations = await (await page.request.get(`${api}/conversations`)).json();
  const conversationId = conversations.items[0].id;
  const original = await (await page.request.get(`${api}/conversations/${conversationId}/preview`)).json();
  const preview = { ...original.preview, status: "ready", terminal: false, summary: "预览已就绪" };
  const current = `/conversations/${conversationId}/cloud-computer`;
  const initial = { id: "mock-env", status: "ready", terminal: false, summary: "云电脑运行中", credits: "4.9765",
    credits_per_minute: "2.1234", seconds: 149, idle_ends_at: new Date(Date.now() + 600000).toISOString(),
    max_ends_at: new Date(Date.now() + 3600000).toISOString() };
  let computer = null, previewValue = null, commands = [], denied = false, holdRead = null;
  let reads = 0, leases = 0, cancels = 0;
  const headers = { "access-control-allow-origin": base, "access-control-allow-credentials": "true",
    "access-control-allow-headers": "*", "access-control-allow-methods": "GET,POST,OPTIONS" };
  const handler = async route => {
    const path = new URL(route.request().url()).pathname;
    const send = body => route.fulfill({ json: body, headers });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (path === current) {
      reads++;
      if (holdRead) { const held = holdRead; holdRead = null; return held(route); }
      if (denied) return route.fulfill({ status: 403, headers, json: { detail: "mock revoked" } });
      return send({ computer });
    }
    if (path === `${current}/commands`) return send({ items: commands });
    if (path === `/conversations/${conversationId}/preview`) return send({ preview: previewValue });
    if (path === `/previews/${preview.id}`) return send({ preview: previewValue, computer, conversation_id: conversationId });
    if (path === `/previews/${preview.id}/heartbeat`) { leases++; return send({ computer }); }
    if (path === `/previews/${preview.id}/access`) return send({ url: "https://mock.preview.cloud107.test/" });
    if (path === `/cloud-computer/commands/mock-command/cancel`) {
      cancels++; commands = [{ ...commands[0], terminal: true, status: "cancelled", cancel_requested: true }];
      return send(commands[0]);
    }
    if (path === `${current}/end`) return route.fulfill({ status: 503, headers, json: { detail: "mock lost response" } });
    return route.fallback();
  };
  const context = page.context();
  await context.route(`${api}/**`, handler);
  await context.route("https://*.preview.cloud107.test/**", route => route.fulfill({
    path: "backend/tests/browser/cloud-preview.html", contentType: "text/html",
  }));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.goto(`${base}/?conversation=${conversationId}`);
  const rail = page.getByRole("region", { name: "云电脑", exact: true });
  await rail.getByText("云电脑待命", { exact: true }).waitFor();
  assert(await rail.getByRole("button", { name: "结束云电脑", exact: true }).count() === 0, "未启动不能结束");
  const before = leases;
  await rail.getByRole("button", { name: "费用详情", exact: true }).click();
  await page.getByText("尚未启动，不产生云电脑费用。", { exact: false }).waitFor();
  await page.keyboard.press("Escape");
  assert(leases === before, "费用展开不续时");
  computer = { ...initial, status: "starting", summary: "正在启动云电脑", seconds: 0, credits: "0" };
  previewValue = { ...preview, status: "starting", summary: "正在启动预览" };
  await rail.getByText("正在启动云电脑", { exact: true }).waitFor({ timeout: 8000 });
  assert(await page.getByRole("button", { name: `打开预览 · ${preview.name}`, exact: true }).count() === 0, "启动中不能虚报预览就绪");
  await page.screenshot({ path: "output/playwright/cloud107-390-starting.png", fullPage: true });
  computer = { ...initial, status: "failed", terminal: true, summary: "云电脑未能启动", credits: "0", seconds: 0 };
  previewValue = { ...preview, status: "failed", terminal: true, summary: "预览未能启动" };
  await rail.getByText("云电脑未能启动", { exact: true }).waitFor({ timeout: 8000 });
  await page.screenshot({ path: "output/playwright/cloud107-390-start-failed.png", fullPage: true });
  // A new environment can follow a terminal one; the previous terminal ID cannot be revived.
  computer = { ...initial, id: "mock-env-2", status: "stopping", summary: "正在结束云电脑",
    stop_reason: "insufficient_balance", stop_reason_text: "可用积分不足以继续运行", renewal_required_credits: "20" };
  await rail.getByText("可用积分不足以继续运行", { exact: true }).waitFor({ timeout: 8000 });
  assert(await rail.getByRole("button", { name: "正在结束", exact: true }).isDisabled(), "余额不足仍等待结束确认");
  await page.screenshot({ path: "output/playwright/cloud107-390-balance.png", fullPage: true });
  computer = { ...initial, id: "mock-env-3" }; previewValue = preview;
  commands = [{ id: "mock-command", status: "running", terminal: false, cancel_requested: false, result: {} }];
  const open = page.getByRole("button", { name: `打开预览 · ${preview.name}`, exact: true });
  await open.waitFor({ timeout: 8000 });
  await rail.getByRole("button", { name: "停止当前命令", exact: true }).click();
  await rail.getByRole("button", { name: "停止当前命令", exact: true }).waitFor({ state: "detached" });
  assert(cancels === 1, "命令取消只提交一次");
  await open.click();
  const surface = page.getByRole("region", { name: "成果预览", exact: true });
  await surface.frameLocator("iframe").getByRole("button", { name: "单步", exact: true }).waitFor();
  assert(leases > before, "可见预览才续时");
  const popupPromise = page.waitForEvent("popup");
  await surface.getByRole("link", { name: "新窗口打开预览", exact: true }).click();
  const popup = await popupPromise;
  await popup.getByRole("region", { name: "成果预览", exact: true }).frameLocator("iframe").getByRole("button", { name: "单步", exact: true }).waitFor();
  assert(await popup.getByRole("button", { name: "费用详情", exact: true }).isVisible(), "独立窗口有费用");
  assert(await popup.getByRole("button", { name: "结束云电脑", exact: true }).isVisible(), "独立窗口有结束");
  await popup.screenshot({ path: "output/playwright/cloud107-standalone.png", fullPage: true });
  await popup.close();
  // Synthetic visibility events and clock only affect this page, never server time or billing.
  await page.clock.install();
  await page.evaluate(() => {
    window.__cloud107Hidden = true;
    Object.defineProperty(document, "hidden", { configurable: true, get: () => window.__cloud107Hidden });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.runFor(100);
  const hiddenReads = reads, hiddenLeases = leases;
  await page.clock.runFor(40000);
  assert(leases === hiddenLeases && reads === hiddenReads, "隐藏页不轮询、不续时");
  await page.evaluate(() => { window.__cloud107Hidden = false; document.dispatchEvent(new Event("visibilitychange")); });
  await page.clock.runFor(500);
  assert(reads > hiddenReads && leases > hiddenLeases, "恢复可见立即核对并续时");
  await surface.getByRole("button", { name: "关闭预览", exact: true }).click();
  await page.clock.runFor(500);
  const closedLeases = leases;
  await page.clock.runFor(40000);
  assert(leases === closedLeases, "关闭预览不再续时");
  await page.clock.resume();
  await rail.getByRole("button", { name: "结束云电脑", exact: true }).click();
  await rail.getByText("结束结果尚未确认，请刷新状态后重试", { exact: true }).waitFor();
  assert(await rail.getByRole("button", { name: "结束云电脑", exact: true }).isEnabled(), "响应失败允许明确重试");
  assert(!await rail.innerText().then(text => text.includes("本次消费")), "失败不显示最终消费");
  // A delayed old-conversation read must not return into a blank draft.
  let release;
  const waiting = new Promise(resolve => { release = resolve; });
  holdRead = async route => { await waiting; await route.fulfill({ json: { computer: initial }, headers }).catch(() => {}); };
  await rail.getByRole("button", { name: "刷新云电脑状态", exact: true }).click();
  await page.getByRole("button", { name: "打开导航", exact: true }).click();
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  release();
  await page.getByRole("textbox", { name: "发送消息", exact: true }).waitFor();
  assert(await page.getByText("云电脑运行中", { exact: true }).count() === 0, "旧响应不能串入新对话");
  await page.goto(`${base}/?conversation=${conversationId}`);
  await open.waitFor();
  denied = true;
  await rail.getByText("云电脑暂不可访问，请重新登录或切换对话", { exact: true }).waitFor({ timeout: 8000 });
  assert(await rail.getByRole("button", { name: "费用详情", exact: true }).count() === 0, "撤权清除费用和操作");
  await page.screenshot({ path: "output/playwright/cloud107-390-revoked.png", fullPage: true });
  await context.unroute(`${api}/**`, handler);
  await context.unroute("https://*.preview.cloud107.test/**");
  return "PASS: mocked startup/failure/insufficient balance, cancellation independence, standalone controls, hidden/closed lease behavior, lost end response, stale switch and revocation.";
}
