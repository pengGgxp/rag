// Run with playwright-cli run-code; uses the disposable resource_preview.py API only.
async page => {
  const base = "http://127.0.0.1:3110";
  const api = "http://127.0.0.1:8127";
  const check = (ok, message) => { if (!ok) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const { cookie, viewer, editor } = await (await page.request.get(`${api}/__fixture/session`)).json();
  await page.context().addCookies([{ name: "rag_session", value: cookie, url: base }]);
  await page.goto(`${base}/resources`);
  await page.getByRole("button", { name: "产品说明与团队协作使用记录.txt", exact: true }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Overflow at ${width}`);
    await page.screenshot({ path: `output/playwright/resources/list-${width}.png` });
  }
  await page.getByRole("button", { name: "产品说明与团队协作使用记录.txt", exact: true }).click();
  await page.getByRole("dialog", { name: "产品说明与团队协作使用记录.txt" }).waitFor();
  check((await page.locator(".resource-text-preview").textContent()).includes("中文内容"), "Text preview missing");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "hidden" });
  await page.getByRole("textbox", { name: "搜索资源" }).fill("无匹配资源");
  await page.getByText("暂无资源", { exact: true }).waitFor();
  await page.screenshot({ path: "output/playwright/resources/empty-390.png" });
  await page.getByRole("textbox", { name: "搜索资源" }).fill("");
  await page.locator('input[type="file"]').setInputFiles({ name: "浏览器验收.txt", mimeType: "text/plain", buffer: Buffer.from("资源上传与跨会话选用") });
  await page.getByRole("button", { name: "浏览器验收.txt", exact: true }).waitFor();
  await page.getByRole("button", { name: /删除.*浏览器验收.txt/ }).click();
  await page.getByRole("dialog", { name: "删除资源" }).waitFor();
  await page.screenshot({ path: "output/playwright/resources/delete-390.png" });
  await page.getByRole("dialog").getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "浏览器验收.txt", exact: true }).waitFor({ state: "hidden" });
  await page.goto(`${base}/?chat=1`);
  await page.getByRole("button", { name: "添加图片或文件" }).click();
  await page.getByRole("button", { name: "从资源库选择" }).click();
  await page.getByRole("button", { name: "产品说明与团队协作使用记录.txt", exact: true }).click();
  await page.getByRole("button", { name: "移除 产品说明与团队协作使用记录.txt" }).waitFor();
  await page.screenshot({ path: "output/playwright/resources/chat-selected-390.png" });
  await page.getByRole("button", { name: "移除 产品说明与团队协作使用记录.txt" }).click();
  await page.goto(`${base}/resources`);
  await page.getByRole("button", { name: "产品说明与团队协作使用记录.txt", exact: true }).waitFor();
  for (const [role, token] of [["viewer", viewer], ["editor", editor]]) {
    await page.context().addCookies([{ name: "rag_session", value: token, url: base }]);
    await page.reload();
    await page.getByRole("button", { name: "产品说明与团队协作使用记录.txt", exact: true }).waitFor();
    check(await page.getByRole("button", { name: "上传", exact: true }).count() === (role === "viewer" ? 0 : 1), `Wrong upload permission: ${role}`);
    check(await page.getByRole("button", { name: /删除.*产品说明/ }).count() === (role === "viewer" ? 0 : 1), `Wrong deletion permission: ${role}`);
    await page.screenshot({ path: `output/playwright/resources/${role}-390.png` });
  }
  await page.route(`${api}/storage/allowances`, async route => {
    const response = await route.fetch();
    const data = await response.json();
    await route.fulfill({ response, json: { ...data, over_quota_since: new Date().toISOString(),
      cleanup_due_at: new Date(Date.now()+86400000).toISOString() } });
  });
  await page.reload();
  await page.getByText(/宽限期即将结束/).waitFor();
  check(await page.getByRole("button", { name: "上传", exact: true }).isDisabled(), "Overage upload must be disabled");
  await page.screenshot({ path: "output/playwright/resources/overage-390.png" });
  await page.unroute(`${api}/storage/allowances`);
  await page.context().addCookies([{ name: "rag_session", value: cookie, url: base }]);
  check(errors.length === 0, errors.join("\n"));
  console.log("Resource list, upload, preview, search, deletion, picker and draft removal passed at 1440/700/390px");
}
