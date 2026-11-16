// Run from the repository root against the isolated cloud-107 containers.
// Real app/auth/resources/status/end APIs; only the cloud page transport is simulated.
async (page) => {
  const base = "http://localhost:3127", api = "http://localhost:8127";
  const assert = (condition, text) => { if (!condition) throw new Error(text); };
  const errors = [], calls = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => { if (request.url().startsWith(api)) calls.push(new URL(request.url()).pathname); });
  await page.route("https://*.preview.cloud107.test/**", route => route.fulfill({
    path: "backend/tests/browser/cloud-preview.html", contentType: "text/html",
  }));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.context().clearCookies();
  await page.goto(`${base}/auth/login`);
  await page.getByLabel("邮箱", { exact: true }).fill("cloud107-local@example.com");
  await page.getByLabel("密码", { exact: true }).fill("cloud107-local-only-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("link", { name: "知识问答", exact: true }).waitFor();
  const conversations = await (await page.request.get(`${api}/conversations`)).json();
  const conversation = conversations.items[0];
  assert(!!conversation, "临时用户应有隔离测试对话");
  const currentPath = `/conversations/${conversation.id}/cloud-computer`;
  const previewData = await (await page.request.get(`${api}/conversations/${conversation.id}/preview`)).json();
  const preview = previewData.preview;
  await page.goto(`${base}/?conversation=${conversation.id}`);
  const rail = page.getByRole("region", { name: "云电脑", exact: true });
  const open = page.getByRole("button", { name: `打开预览 · ${preview.name}`, exact: true });
  await open.waitFor();
  assert(!await rail.innerText().then(text => /累计|积分\/分钟/.test(text)), "控制栏不能常驻费用数字和单价");
  assert(await page.getByText("下载 排序可视化.html", { exact: true }).isVisible(), "保留实际文件下载");
  assert(await page.locator(".cloud-result-actions").getByRole("link", { name: "查看文件", exact: true }).isVisible(), "成果区应有资源入口");
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: width === 390 ? "reduce" : "no-preference" });
    await open.scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px 无横向溢出`);
    await page.screenshot({ path: `output/playwright/cloud107-${width}-chat.png`, fullPage: true });
    const fees = rail.getByRole("button", { name: "费用详情", exact: true });
    await fees.focus(); await page.keyboard.press("Enter");
    const feeDialog = page.getByRole("dialog", { name: "云电脑费用", exact: true });
    await feeDialog.waitFor();
    await page.waitForFunction(() => Array.from(document.querySelectorAll('[role="dialog"][data-state="open"] > div'))
      .every(el => getComputedStyle(el).opacity === "1"));
    assert(await feeDialog.getByText("2.1234 积分/分钟", { exact: true }).isVisible(), "应使用环境冻结单价与四位精度");
    assert(await feeDialog.getByText("闲置结束时间", { exact: true }).isVisible(), "显示实际闲置结束时间");
    assert(await feeDialog.locator('[aria-live], [role="status"]').count() === 0, "费用刷新不能持续读屏播报");
    assert(width > 700 ? await feeDialog.evaluate(el => el.classList.contains("selection-surface"))
      : await feeDialog.evaluate(el => el.classList.contains("modal")), "桌面浮层/手机 Dialog");
    await page.screenshot({ path: `output/playwright/cloud107-${width}-fees.png`, fullPage: true });
    await page.keyboard.press("Escape");
    await feeDialog.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.classList.contains("cloud-fees-trigger"));
    await open.click();
    const surface = page.getByRole("region", { name: "成果预览", exact: true });
    await surface.waitFor();
    const frame = surface.frameLocator("iframe");
    await frame.getByRole("button", { name: "单步", exact: true }).click();
    assert(await frame.getByText("已比较 1 次", { exact: true }).isVisible(), "真实 iframe 可交互");
    if (width === 1440) {
      assert(await surface.getByRole("button", { name: "费用详情", exact: true }).count() === 0, "桌面预览不重复控制栏");
      // A normal five-second state refresh must preserve iframe state.
      const reads = calls.filter(path => path === currentPath).length;
      await page.waitForFunction(() => true);
      await page.waitForResponse(response => new URL(response.url()).pathname === currentPath, { timeout: 8000 });
      assert(calls.filter(path => path === currentPath).length > reads, "共享轮询刷新");
      assert(await frame.getByText("已比较 1 次", { exact: true }).isVisible(), "状态轮询不能重载网页");
      await rail.getByRole("button", { name: "费用详情", exact: true }).click();
      await page.getByRole("dialog", { name: "云电脑费用", exact: true }).waitFor();
      await page.keyboard.press("Escape");
      await page.getByRole("dialog", { name: "云电脑费用", exact: true }).waitFor({ state: "detached" });
      await surface.waitFor();
      assert(await surface.isVisible(), "关闭费用浮层不能同时关闭预览");
    } else {
      const previewFees = surface.getByRole("button", { name: "费用详情", exact: true });
      assert((await previewFees.boundingBox()).height >= 44, "手机点击目标44px");
      await previewFees.click();
      await page.getByRole("dialog", { name: "云电脑费用", exact: true }).waitFor();
      await page.keyboard.press("Escape");
      await page.getByRole("dialog", { name: "云电脑费用", exact: true }).waitFor({ state: "detached" });
      await surface.waitFor();
      assert(await surface.isVisible(), "手机费用关闭后预览仍在");
      await page.waitForFunction(() => document.activeElement?.classList.contains("cloud-fees-trigger"));
    }
    await page.screenshot({ path: `output/playwright/cloud107-${width}-preview.png`, fullPage: true });
    await surface.getByRole("button", { name: "刷新预览", exact: true }).click();
    await frame.getByText("准备就绪", { exact: true }).waitFor();
    await surface.getByRole("button", { name: "关闭预览", exact: true }).click();
    await surface.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.classList.contains("cloud-result-action"));
    const stillActive = await (await page.request.get(`${api}${currentPath}`)).json();
    assert(!stillActive.computer.terminal, "关闭预览不销毁云电脑");
    if (width === 390) {
      for (const selector of [".cloud-result-action", ".cloud-fees-trigger", ".cloud-end-button"]) {
        const boxes = await page.locator(selector).evaluateAll(elements => elements.map(el => el.getBoundingClientRect().height));
        assert(boxes.every(height => height >= 44), `${selector} 手机目标44px`);
      }
    }
  }
  // Transient status failure preserves the last known environment and exposes recovery.
  await page.route(`${api}${currentPath}`, route => route.fulfill({ status: 503, json: { detail: "synthetic outage" },
    headers: { "access-control-allow-origin": base, "access-control-allow-credentials": "true" } }));
  await rail.getByText("状态更新失败，当前显示上次结果", { exact: true }).waitFor({ timeout: 10000 });
  assert(await rail.getByRole("button", { name: "结束云电脑", exact: true }).isVisible(), "失败仍保留环境，不能误报结束");
  await page.screenshot({ path: "output/playwright/cloud107-390-stale.png", fullPage: true });
  await page.unroute(`${api}${currentPath}`);
  await rail.getByRole("button", { name: "刷新云电脑状态", exact: true }).click();
  await page.getByText("状态更新失败，当前显示上次结果", { exact: true }).waitFor({ state: "detached" });
  await page.reload(); await open.waitFor();
  assert(await rail.getByRole("button", { name: "结束云电脑", exact: true }).isVisible(), "刷新恢复同一环境");
  const download = page.waitForEvent("download");
  await page.getByText("下载 排序可视化.html", { exact: true }).click();
  assert((await download).suggestedFilename() === "排序可视化.html", "实际 API 下载保留名称");
  await rail.getByRole("button", { name: "结束云电脑", exact: true }).click();
  await rail.getByText("正在结束云电脑", { exact: true }).waitFor();
  assert(await rail.getByRole("button", { name: "正在结束", exact: true }).isDisabled(), "结束中防止重复提交");
  assert(!await rail.innerText().then(text => text.includes("本次消费")), "服务端确认前不展示最终消费");
  await page.screenshot({ path: "output/playwright/cloud107-390-stopping.png", fullPage: true });
  await rail.getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 25000 });
  const ended = await (await page.request.get(`${api}${currentPath}`)).json();
  assert(ended.computer.terminal, "服务端确认已结束");
  assert(await rail.innerText().then(text => text.includes("本次消费")), "确认后展示最终消费");
  await page.screenshot({ path: "output/playwright/cloud107-390-ended.png", fullPage: true });
  const me = await (await page.request.get(`${api}/auth/me`)).json();
  const denied = await page.request.post(`${api}/previews/${preview.id}/access`, {
    headers: { "X-RAG-Request": "1", "X-CSRF-Token": me.csrf_token }, data: {},
  });
  assert(denied.status() === 410, "环境结束后服务端拒绝预览授权");
  const downloadAfter = page.waitForEvent("download");
  await page.getByText("下载 排序可视化.html", { exact: true }).click();
  assert((await downloadAfter).suggestedFilename() === "排序可视化.html", "结束后文件仍可下载");
  await rail.getByRole("link", { name: "查看文件", exact: true }).click();
  await page.getByText("排序可视化.html", { exact: true }).first().waitFor();
  await page.screenshot({ path: "output/playwright/cloud107-390-files.png", fullPage: true });
  assert(errors.length === 0, `页面异常 ${errors.join("; ")}`);
  assert(calls.filter(path => path.endsWith("/cloud-computer/end")).length === 1, "只提交一次结束");
  return "PASS: isolated real auth/API/resource loop, frozen fees, desktop/mobile, iframe interaction/refresh, focus/Escape, stale recovery, confirmed end and retained download. Cloud page transport is mocked; no paid calls.";
}
