async page => {
  // Run with playwright-cli against the isolated Issue #118 stack after its synthetic task completes.
  const api = "http://localhost:8138", app = "http://localhost:3138";
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const conversation = new URL(page.url()).searchParams.get("conversation");
  assert(conversation, "Open the completed synthetic conversation first");
  const errors = [], calls = [], screenshots = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("request", request => calls.push({ url: request.url(), method: request.method() }));
  await page.context().route("https://*.preview.issue118.test/**", async route => {
    const response = await page.request.get("http://localhost:8938/frame/8398/");
    await route.fulfill({ response });
  });
  const payload = await (await page.request.get(`${api}/conversations/${conversation}`)).json();
  const answer = payload.messages.filter(item => item.role === "assistant").at(-1);
  assert(answer.status === "completed", "Synthetic Agent task did not finish");
  const created = answer.tool_events.filter(item => item.name === "create_cloud_computer");
  const previewEvent = answer.tool_events.find(item => item.name === "start_preview");
  const previewId = previewEvent.preview.id, primary = previewEvent.computer_id;
  const temporary = created.find(item => item.computer_id !== primary).computer_id;
  assert(answer.tool_events.some(item => item.name === "end_cloud_computer" && item.computer_id === temporary),
    "Temporary task did not explicitly end its environment");
  const records = await (await page.request.get(`${api}/cloud-computers?limit=200`)).json();
  assert(records.items.find(item => item.id === temporary).terminal, "Temporary environment still running");
  assert(!records.items.find(item => item.id === primary).terminal, "Interactive preview was ended");
  const otherActive = records.items.filter(item => !item.terminal && item.id !== primary).map(item => item.id);
  assert(otherActive.length > 0 && records.items.length >= 25, "Missing independent task/history fixtures");
  const metrics = await (await page.request.get("http://localhost:8938/metrics")).json();
  assert(metrics.requests.filter(item => item.offered.length).every(item => item.cleanup_prompt && item.keep_preview_prompt),
    "Updated lifecycle prompt did not reach the model");
  const capture = async name => {
    const path = `output/playwright/issue118-${name}.png`;
    await page.screenshot({ path, fullPage: true }); screenshots.push(path);
  };
  const noPanel = async () => {
    assert(await page.getByRole("region", { name: "云电脑", exact: true }).count() === 0, "Instance list remains in chat");
    assert(await page.getByRole("button", { name: /费用详情/ }).count() === 0, "Fees remain in chat");
    assert(await page.getByText("云电脑待命", { exact: true }).count() === 0, "Standby copy remains");
  };
  const noOverflow = async () => assert(await page.evaluate(() =>
    document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
  const action = page.getByRole("button", { name: /^打开预览 · Issue118/ });
  await page.reload();
  await action.waitFor();
  const count = calls.length;
  await page.waitForTimeout(5500);
  assert(!calls.slice(count).some(item => /\/previews\//.test(item.url)), "Closed preview keeps polling");
  assert(!calls.some(item => /\/cloud-computers(?:\?|$)|\/commands(?:\?|$)|\/cloud-computer\/catalog/.test(item.url)),
    "Chat traverses workspace instances or commands");
  await page.emulateMedia({ reducedMotion: "reduce" });
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await noPanel(); await noOverflow(); await capture(`${width}-chat`);
    await action.click();
    const preview = page.getByRole("region", { name: "成果预览", exact: true });
    await preview.frameLocator("iframe").getByRole("button", { name: "测试交互", exact: true }).waitFor();
    await preview.locator(".cloud-preview-loading").waitFor({ state: "detached" });
    // Capture a painted cross-origin frame before dispatching a real pointer event.
    await capture(`${width}-preview`);
    await preview.frameLocator("iframe").getByRole("button", { name: "测试交互", exact: true }).click();
    await preview.frameLocator("iframe").getByRole("button", { name: "交互成功", exact: true }).waitFor();
    await preview.getByRole("button", { name: "刷新预览", exact: true }).click();
    await preview.frameLocator("iframe").getByRole("button", { name: "测试交互", exact: true }).waitFor();
    assert(await preview.getByRole("link", { name: "预览运行期间计费", exact: true }).getAttribute("href") === "/settings/billing",
      "Billing link is missing");
    assert(await preview.getByRole("button", { name: /费用详情/ }).count() === 0, "Preview fee popup remains");
    await preview.getByRole("button", { name: "结束运行", exact: true }).click();
    const confirmation = page.getByRole("dialog", { name: "结束运行？", exact: true });
    await confirmation.getByText("此预览将停止，已保存的文件会保留。", { exact: true }).waitFor();
    if (width === 390) await capture("390-confirm");
    await page.keyboard.press("Escape");
    await confirmation.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.matches(".cloud-preview-surface .cloud-end-button"));
    assert(await preview.getByRole("button", { name: "结束运行", exact: true })
      .evaluate(element => element === document.activeElement), "End confirmation lost focus");
    await preview.waitFor(); await capture(`${width}-preview`); await noOverflow();
    if (width === 390) {
      const sizes = await preview.locator("button, .icon-button, .cloud-preview-notice a")
        .evaluateAll(elements => elements.map(element => element.getBoundingClientRect().height));
      assert(sizes.every(height => height >= 44), `Undersized touch target: ${sizes}`);
    }
    await preview.getByRole("button", { name: "关闭预览", exact: true }).click();
    await preview.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.matches(".cloud-result-action"));
    assert(await action.evaluate(element => element === document.activeElement), "Preview close lost focus");
  }
  await page.setViewportSize({ width: 1440, height: 950 });
  await action.click();
  const surface = page.getByRole("region", { name: "成果预览", exact: true });
  const popupPromise = page.waitForEvent("popup");
  await surface.getByRole("link", { name: "新窗口打开预览", exact: true }).click();
  const popup = await popupPromise;
  await popup.getByRole("button", { name: "结束运行", exact: true }).waitFor();
  await popup.frameLocator("iframe").getByRole("button", { name: "测试交互", exact: true }).waitFor();
  await popup.screenshot({ path: "output/playwright/issue118-standalone.png", fullPage: true });
  screenshots.push("output/playwright/issue118-standalone.png");
  await Promise.all([popup.waitForEvent("close"),
    popup.getByRole("button", { name: "关闭预览", exact: true }).click()]);
  // Transient query and end failures must retain a truthful, retryable state.
  await page.route(`${api}/previews/${previewId}`, route => route.abort("failed"), { times: 1 });
  await surface.getByRole("button", { name: "刷新预览", exact: true }).click();
  await surface.getByText("状态更新失败，当前显示上次结果，请刷新重试", { exact: true }).waitFor();
  await surface.getByRole("button", { name: "刷新预览状态", exact: true }).click();
  await surface.frameLocator("iframe").getByRole("button", { name: "测试交互", exact: true }).waitFor();
  await page.route(`${api}/cloud-computers/${primary}/end`, route => route.abort("failed"), { times: 1 });
  const confirmEnd = async () => {
    await surface.getByRole("button", { name: "结束运行", exact: true }).click();
    await page.getByRole("dialog", { name: "结束运行？", exact: true })
      .getByRole("button", { name: "结束运行", exact: true }).click();
  };
  await confirmEnd();
  await surface.getByText("结束结果尚未确认，请刷新状态后重试", { exact: true }).waitFor();
  assert(await surface.getByRole("button", { name: "结束运行", exact: true }).isEnabled(), "Failed end cannot retry");
  await confirmEnd();
  await surface.getByRole("button", { name: "正在结束", exact: true }).waitFor();
  await surface.getByText("预览已结束", { exact: true }).waitFor({ timeout: 45000 });
  await surface.getByRole("link", { name: "查看文件", exact: true }).waitFor();
  assert(await surface.locator("iframe").count() === 0, "Ended preview frame remains");
  await capture("1440-ended");
  await surface.getByRole("button", { name: "关闭预览", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: /^查看预览状态 · Issue118/ }).click();
  await surface.getByText("预览已结束", { exact: true }).waitFor();
  await page.setViewportSize({ width: 390, height: 950 });
  await capture("390-ended");
  await surface.getByRole("button", { name: "关闭预览", exact: true }).click();
  const afterEnd = await (await page.request.get(`${api}/cloud-computers?limit=200`)).json();
  assert(otherActive.every(id => !afterEnd.items.find(item => item.id === id).terminal), "Ending preview affected another task");
  const resources = await (await page.request.get(`${api}/resources`)).json();
  const file = resources.items.find(item => item.path === "issue118-retest/shared.txt");
  const download = await page.request.get(`${api}/resources/${file.id}/download`);
  assert(download.ok() && await download.text() === "shared-from-alpha\n", "File/download lost after end");
  const usageResponse = page.waitForResponse(response =>
    response.url() === `${api}/cloud-computers?offset=0&limit=10`);
  await page.goto(`${app}/settings/billing`);
  const usagePayload = await (await usageResponse).json();
  const usage = page.getByRole("region", { name: "我的云电脑用量", exact: true });
  await usage.locator("details").first().waitFor();
  const rendered = usage.locator("details");
  for (let index = 0; index < usagePayload.items.length; index++) {
    const record = usagePayload.items[index], text = await rendered.nth(index).textContent();
    assert(text.includes(record.name), "Usage name does not match authenticated API");
    const credits = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 4 }).format(Number(record.credits));
    assert(text.includes(`${credits} 积分`), "Usage charge does not match server amount");
    assert(!text.includes(record.id), "Instance identifier is displayed");
  }
  await page.getByRole("region", { name: "最近使用量", exact: true })
    .getByText("云电脑运行时长", { exact: false }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await usage.scrollIntoViewIfNeeded();
    assert(await usage.locator("details").count() === 10, "Usage page size incorrect");
    assert(await usage.getByRole("button", { name: /结束/ }).count() === 0, "Billing contains management actions");
    assert(!(await usage.textContent()).includes("不应出现的其他用户记录"), "Foreign record exposed");
    assert((await usage.textContent()).includes("未结算") && (await usage.textContent()).includes("已结算"),
      "Unsettled/final status is unclear");
    await usage.locator("summary").first().focus();
    await page.keyboard.press("Enter");
    await usage.getByText("本次单价", { exact: true }).first().waitFor();
    await capture(`${width}-usage`); await noOverflow();
    await usage.locator("summary").first().press("Enter");
  }
  const pageCount = Math.ceil(afterEnd.items.length / 10);
  for (let current = 2; current <= pageCount; current++) {
    await usage.getByRole("button", { name: "下一页云电脑用量", exact: true }).click();
    await usage.getByText(`第 ${current} 页`, { exact: true }).waitFor();
    await usage.locator("details").first().waitFor();
  }
  assert(await usage.locator("details").count() === (afterEnd.items.length - 1) % 10 + 1, "Last usage page incorrect");
  assert(await usage.getByRole("button", { name: "下一页云电脑用量", exact: true }).isDisabled(), "Extra empty page");
  const latest = await (await page.request.get(`${api}/cloud-computers?limit=200`)).json();
  assert(otherActive.every(id => latest.items.find(item => item.id === id).idle_ends_at
    === afterEnd.items.find(item => item.id === id).idle_ends_at), "Viewing billing renewed an environment");
  const finalMetrics = await (await page.request.get("http://localhost:8938/metrics")).json();
  assert(finalMetrics.creates === metrics.creates, "Viewing expired preview restarted a paid environment");
  assert(errors.length === 0, `Page errors: ${errors.join("; ")}`);
  return { conversation, primary, temporary, independent: otherActive, screenshots: [...new Set(screenshots)], errors,
    checks: "synthetic model lifecycle, no workspace polling, 3 widths, iframe interaction/refresh, focus, failures, independent end, history, pagination, file download" };
}
