async (page) => {
  const base = "http://localhost:3132", api = "http://localhost:8132";
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.context().route("https://*.preview.cloud107.test/**", route => route.fulfill({
    path: "backend/tests/browser/cloud-preview.html", contentType: "text/html",
  }));
  await page.context().clearCookies();
  await page.goto(`${base}/auth/login`);
  await page.getByLabel("邮箱", { exact: true }).fill("cloud107-local@example.com");
  await page.getByLabel("密码", { exact: true }).fill("cloud107-local-only-123");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("heading", { name: "知识库", exact: true }).waitFor();
  const authenticated = await page.request.get(`${api}/auth/me`);
  assert(authenticated.ok(), `login did not establish a session: ${authenticated.status()}`);
  const identity = await (await page.request.get(`${api}/auth/me`)).json();
  const control = async () => {
    const response = await page.request.post(`${api}/__test__/scenario/multiple`, {
      headers: { "X-RAG-Request": "1", "X-CSRF-Token": identity.csrf_token },
    });
    assert(response.ok(), `fixture: ${response.status()} ${await response.text()}`);
    return response.json();
  };
  const panel = page.getByRole("region", { name: "云电脑", exact: true });
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    const receipt = await control();
    await page.goto(`${base}/?conversation=${receipt.conversation_id}`);
    const list = panel.locator("details.cloud-computer-list");
    await list.locator("summary").click();
    for (let index = 0; index < 2; index++) {
      const instance = panel.getByRole("region", { name: receipt.names[index], exact: true });
      await instance.getByText("云电脑运行中", { exact: true }).waitFor();
      await instance.getByText(receipt.computer_ids[index].slice(0, 8), { exact: false }).waitFor();
    }
    const first = panel.getByRole("region", { name: receipt.names[0], exact: true });
    const second = panel.getByRole("region", { name: receipt.names[1], exact: true });
    await first.getByText(/命令 .* 执行中/).waitFor();
    await first.getByRole("button", { name: /^停止命令/ }).waitFor();
    await first.getByRole("button", { name: /^费用详情/ }).click();
    const feeDialog = page.getByRole("dialog", { name: `${receipt.names[0]} · 费用`, exact: true });
    await feeDialog.waitFor();
    await page.keyboard.press("Escape");
    await feeDialog.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.classList.contains("cloud-fees-trigger"));
    const previewButton = page.getByRole("button", { name: /^打开预览 ·/ });
    await previewButton.click();
    const preview = page.getByRole("region", { name: "成果预览", exact: true });
    await preview.frameLocator("iframe").getByRole("button", { name: "单步", exact: true }).click();
    await preview.frameLocator("iframe").getByText("已比较 1 次", { exact: true }).waitFor();
    const previewData = await (await page.request.get(`${api}/previews/${receipt.preview_id}`)).json();
    let feeSurface = preview;
    let popup;
    if (width >= 1000) {
      const opened = page.waitForEvent("popup");
      await preview.getByRole("link", { name: "新窗口打开预览", exact: true }).click();
      popup = await opened;
      feeSurface = popup.getByRole("region", { name: "成果预览", exact: true });
      await feeSurface.getByRole("button", { name: /^费用详情/ }).waitFor();
    }
    await feeSurface.getByRole("button", { name: /^费用详情/ }).click();
    const previewFees = (popup || page).getByRole("dialog", { name: `${previewData.computer.name} · 费用`, exact: true });
    await previewFees.waitFor();
    await (popup || page).keyboard.press("Escape");
    await previewFees.waitFor({ state: "detached" });
    if (popup) await popup.close();
    await preview.getByRole("button", { name: "关闭预览", exact: true }).click();
    await preview.waitFor({ state: "detached" });
    await page.screenshot({ path: `output/playwright/issue112-${width}-multiple.png`, fullPage: true });
    const ending = page.waitForResponse(response => response.url().endsWith(`/cloud-computers/${receipt.computer_ids[0]}/end`)
      && response.request().method() === "POST");
    await first.getByRole("button", { name: `结束云电脑 · ${receipt.names[0]}`, exact: true }).click();
    assert((await ending).ok(), "Ending selected instance should succeed");
    await first.getByText("正在结束云电脑", { exact: true }).waitFor();
    await second.getByText("云电脑运行中", { exact: true }).waitFor();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px horizontal overflow`);
    assert(errors.length === 0, `Browser errors: ${errors.join("; ")}`);
  }
  return "PASS: explicit IDs, per-instance status/fees/end, command ownership, 1440/700/390px, Escape focus, reduced motion.";
}
