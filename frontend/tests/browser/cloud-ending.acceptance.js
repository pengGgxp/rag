// Run with playwright-cli run-code --filename against compose.cloud-ending.yml.
// Real local auth/status/end/billing APIs; only provider IO and iframe transport are synthetic.
async (page) => {
  const base = "http://localhost:3129", api = "http://localhost:8129";
  const assert = (ok, why) => { if (!ok) throw new Error(why); };
  const errors = [];
  await page.unrouteAll({ behavior: "ignoreErrors" });
  await page.context().unrouteAll({ behavior: "ignoreErrors" });
  page.on("pageerror", error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.context().route("https://*.preview.cloud107.test/**", route => route.fulfill({
    path: "backend/tests/browser/cloud-preview.html", contentType: "text/html",
  }));
  await page.context().clearCookies();
  await page.goto(`${base}/auth/login`);
  await page.getByLabel("邮箱", { exact: true }).fill("cloud107-local@example.com");
  await page.getByLabel("密码", { exact: true }).fill("cloud107-local-only-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("link", { name: "知识问答", exact: true }).waitFor();
  const identity = await (await page.request.get(`${api}/auth/me`)).json();
  const control = async path => {
    const response = await page.request.post(`${api}/__test__/${path}`, {
      headers: { "X-RAG-Request": "1", "X-CSRF-Token": identity.csrf_token },
      maxRetries: 1, // Only synthetic reset/confirmation controls; never retry user commands.
    });
    assert(response.ok(), `fixture ${path}: ${response.status()} ${await response.text()}`);
    return response.json();
  };
  const rail = page.getByRole("region", { name: "云电脑", exact: true });
  const fees = page.getByRole("dialog", { name: "云电脑费用", exact: true });
  const checkLayout = async width => {
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px horizontal overflow`);
    for (const button of await rail.getByRole("button").all()) {
      if (await button.isVisible()) assert((await button.boundingBox()).height >= 44, "44px targets");
    }
  };
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    let receipt = await control("scenario/ready");
    const url = `${base}/?conversation=${receipt.conversation_id}`;
    await page.goto(url);
    await rail.getByText("云电脑运行中", { exact: true }).waitFor();
    await rail.getByRole("button", { name: "费用详情", exact: true }).click();
    await fees.waitFor();
    assert(await fees.getByText("续期所需预留", { exact: true }).count() === 0, "No routine reservation reminder");
    assert(await fees.locator('[role="status"],[aria-live]').count() === 0, "No continuous fee announcements");
    await page.keyboard.press("Escape");
    await fees.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.classList.contains("cloud-fees-trigger"));
    const open = page.getByRole("button", { name: /^打开预览 ·/ });
    await open.click();
    const surface = page.getByRole("region", { name: "成果预览", exact: true });
    await surface.frameLocator("iframe").getByRole("button", { name: "单步", exact: true }).click();
    await surface.frameLocator("iframe").getByText("已比较 1 次", { exact: true }).waitFor();
    await page.screenshot({ path: `output/playwright/cloud114-${width}-preview.png`, fullPage: true });
    await surface.getByRole("button", { name: "关闭预览", exact: true }).click();
    await surface.waitFor({ state: "detached" });
    // Manual stop uses the real end API; provider confirmation is intentionally delayed.
    const endResponse = page.waitForResponse(response => response.url().endsWith(`/conversations/${receipt.conversation_id}/cloud-computer/end`)
      && response.request().method() === "POST" && response.status() === 200);
    await rail.getByRole("button", { name: "结束云电脑", exact: true }).click();
    await endResponse;
    await rail.getByText("正在结束云电脑", { exact: true }).waitFor();
    assert(!await rail.innerText().then(text => text.includes("本次消费")), "Manual request is not confirmation");
    await control("confirm-end");
    await rail.getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 9000 });
    await rail.getByText("你已请求结束云电脑", { exact: true }).waitFor();

    receipt = await control("scenario/delayed");
    await page.goto(url);
    await rail.getByText("正在结束云电脑", { exact: true }).waitFor();
    await rail.getByText("运行状态核对中断", { exact: true }).waitFor();
    assert(!await rail.innerText().then(text => /积分不足|本次消费/.test(text)), "Ample balance must not be labeled insufficient or settled");
    await checkLayout(width);
    await page.screenshot({ path: `output/playwright/cloud114-${width}-delayed-stopping.png`, fullPage: true });
    await page.getByRole("button", { name: /^查看预览状态 ·/ }).click();
    await surface.getByText("正在结束云电脑", { exact: true }).waitFor();
    await surface.getByText("运行状态核对中断", { exact: true }).waitFor();
    let popup = null;
    if (width === 1440) {
      const promise = page.waitForEvent("popup");
      await surface.getByRole("link", { name: "新窗口打开预览", exact: true }).click();
      popup = await promise;
      await popup.getByText("运行状态核对中断", { exact: true }).waitFor();
    }
    await control("confirm-end");
    await surface.getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 9000 });
    await surface.getByText("运行状态核对中断，云电脑已结束", { exact: true }).waitFor();
    if (popup) {
      await popup.getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 9000 });
      await popup.getByText("运行状态核对中断，云电脑已结束", { exact: true }).waitFor();
      await popup.screenshot({ path: "output/playwright/cloud114-standalone-ended.png", fullPage: true });
      await popup.close();
    }
    await page.screenshot({ path: `output/playwright/cloud114-${width}-preview-ended.png`, fullPage: true });
    await surface.getByRole("button", { name: "关闭预览", exact: true }).click();
    await surface.waitFor({ state: "detached" });
    await rail.getByText("云电脑已结束", { exact: true }).waitFor();
    assert(!await rail.innerText().then(text => /正在结束|积分不足/.test(text)), "Terminal summary is authoritative");
    assert(await rail.getByRole("link", { name: "查看文件", exact: true }).isVisible(), "Files remain accessible");
    await page.screenshot({ path: `output/playwright/cloud114-${width}-delayed-ended.png`, fullPage: true });

    await control("scenario/insufficient");
    await page.goto(url);
    await rail.getByText("可用积分不足以继续运行", { exact: true }).waitFor();
    await rail.getByRole("button", { name: "费用详情", exact: true }).click();
    await fees.getByText("续期所需预留", { exact: true }).waitFor();
    await fees.getByText("20 积分", { exact: true }).waitFor();
    await page.screenshot({ path: `output/playwright/cloud114-${width}-insufficient-fees.png`, fullPage: true });
    await page.keyboard.press("Escape");
    await fees.waitFor({ state: "detached" });
    await control("confirm-end");
    await rail.getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 9000 });
    await rail.getByText("可用积分不足以继续运行", { exact: true }).waitFor();
    await checkLayout(width);
    await page.screenshot({ path: `output/playwright/cloud114-${width}-insufficient-ended.png`, fullPage: true });
  }
  const failureCase = await control("scenario/ready");
  await page.goto(`${base}/?conversation=${failureCase.conversation_id}`);
  await rail.getByText("云电脑运行中", { exact: true }).waitFor();
  const current = `${api}/conversations/${failureCase.conversation_id}/cloud-computer`;
  const outage = route => route.fulfill({ status: 503, json: { detail: "synthetic status outage" } });
  await page.route(current, outage);
  await rail.getByText("状态更新失败，当前显示上次结果", { exact: true }).waitFor({ timeout: 9000 });
  await rail.getByText("云电脑运行中", { exact: true }).waitFor();
  assert(!await rail.innerText().then(text => text.includes("本次消费")), "Status outage cannot imply final settlement");
  await page.screenshot({ path: "output/playwright/cloud114-status-outage.png", fullPage: true });
  await page.unroute(current, outage);
  await rail.getByRole("button", { name: "刷新云电脑状态", exact: true }).click();
  await rail.getByText("状态更新失败，当前显示上次结果", { exact: true }).waitFor({ state: "detached" });
  const accessPath = `${api}/previews/${failureCase.preview_id}/access`;
  const accessFailure = route => route.fulfill({ status: 503, json: { detail: "synthetic grant failure" } });
  await page.route(accessPath, accessFailure);
  await page.getByRole("button", { name: /^打开预览 ·/ }).click();
  const failedSurface = page.getByRole("region", { name: "成果预览", exact: true });
  await failedSurface.getByText("预览暂时无法打开，请刷新重试", { exact: true }).first().waitFor();
  const failedEndResponse = page.waitForResponse(response => response.url().endsWith(`/conversations/${failureCase.conversation_id}/cloud-computer/end`)
    && response.request().method() === "POST" && response.status() === 200);
  await failedSurface.getByRole("button", { name: "结束云电脑", exact: true }).click();
  await failedEndResponse;
  await control("confirm-end");
  await failedSurface.getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 9000 });
  assert(await failedSurface.getByText("预览暂时无法打开，请刷新重试", { exact: true }).count() === 0, "Old access error cannot mask final state");
  await page.screenshot({ path: "output/playwright/cloud114-grant-failure-ended.png", fullPage: true });
  await failedSurface.getByRole("button", { name: "关闭预览", exact: true }).click();
  await page.unroute(accessPath, accessFailure);
  const receipt = await control("scenario/legacy");
  await page.goto(`${base}/?conversation=${receipt.conversation_id}`);
  await rail.getByText("云电脑已结束", { exact: true }).waitFor();
  assert(!await rail.innerText().then(text => /正在结束|积分不足/.test(text)), "Historical error must not override ended or invent a cause");
  await rail.getByRole("link", { name: "查看文件", exact: true }).click();
  await page.getByRole("heading", { name: "工作区", exact: true }).waitFor();
  await page.getByText("排序可视化.html", { exact: true }).first().waitFor();
  await page.screenshot({ path: "output/playwright/cloud114-files-retained.png", fullPage: true });
  assert(errors.length === 0, `Browser errors: ${errors.join("; ")}`);
  return "PASS: 1440/700/390px; real local end/confirmation, ample-balance delay, true reservation rejection, preview and standalone agreement, legacy summaries, fee focus, file retention. No paid provider/model calls.";
}
