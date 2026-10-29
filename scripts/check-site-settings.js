// Disposable local API/database only. Never changes deployed grants or announcements.
async page => {
  await page.unrouteAll({ behavior: "wait" });
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const root = "http://127.0.0.1:8127";
  const fixture = await (await page.request.get(`${root}/__fixture/session`)).json();
  await page.context().addCookies([{ name: "rag_session", value: fixture.cookie, domain: "127.0.0.1", path: "/" }]);
  const me = await (await page.request.get(`${root}/auth/me`, { headers: { cookie: `rag_session=${fixture.cookie}` } })).json();
  const headers = { cookie: `rag_session=${fixture.cookie}`, "X-RAG-Request": "1", "X-CSRF-Token": me.csrf_token };
  let fail = false;
  let conflict = false;
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route(url => url.origin === "http://localhost:8000", async route => {
    const req = route.request();
    if (req.url().endsWith("/site/admin/settings") && req.method() === "PUT" && (fail || conflict)) {
      return route.fulfill({ status: conflict ? 409 : 503, contentType: "application/json",
        body: JSON.stringify({ detail: conflict ? "站点设置已更新，请重新加载后再保存" : "保存失败，请稍后重试" }) });
    }
    await route.continue({ url: req.url().replace("http://localhost:8000", root),
      headers: { ...req.headers(), cookie: `rag_session=${fixture.cookie}` } });
  });
  await page.goto("http://127.0.0.1:3110/admin/settings");
  await page.getByLabel("赠送积分", { exact: true }).waitFor();
  assert(await page.getByLabel("赠送积分", { exact: true }).inputValue() === "1000", "Default grant changed");
  await page.getByLabel("赠送积分", { exact: true }).fill("0");
  await page.getByLabel("公告标题", { exact: true }).fill("服务更新与使用说明 · " + "知识工作台".repeat(10));
  const body = "欢迎使用知库。\n\n支持逐步整理资料，并通过引用核对原文。\n<script>window.__announcementExecuted = true</script>";
  await page.getByLabel("公告正文", { exact: true }).fill(body);
  await page.getByLabel("发布到工作台", { exact: true }).check();
  await page.getByRole("link", { name: "用户", exact: true }).click();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  fail = true;
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "保存失败，请稍后重试" }).waitFor();
  assert(await page.getByLabel("公告正文", { exact: true }).inputValue() === body, "Save error lost draft");
  fail = false;
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "站点设置已保存" }).waitFor();
  await page.reload();
  await page.getByLabel("赠送积分", { exact: true }).waitFor();
  assert(await page.getByLabel("赠送积分", { exact: true }).inputValue() === "0", "Grant not saved");
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.screenshot({ path: `output/playwright/issue48-admin-${width}.png` });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Admin overflow");
  }
  await page.getByLabel("赠送积分", { exact: true }).fill("7");
  conflict = true;
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await page.getByRole("button", { name: "放弃修改并重新加载", exact: true }).waitFor();
  assert(await page.getByLabel("赠送积分", { exact: true }).inputValue() === "7", "Conflict lost input");
  conflict = false;
  await page.getByRole("button", { name: "放弃修改并重新加载", exact: true }).click();
  await page.waitForFunction(() => document.querySelector('input[type="number"]')?.value === "0");
  await page.setViewportSize({ width: 1440, height: 1000 });
  for (const path of ["/", "/?chat=1", "/?extensions=1"]) {
    await page.goto(`http://127.0.0.1:3110${path}`);
    const banner = page.getByRole("complementary", { name: "站点公告", exact: true });
    await banner.waitFor();
    await page.screenshot({ path: `output/playwright/issue48-workspace-${path === "/" ? "catalog" : path.includes("chat") ? "chat" : "extensions"}.png` });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Workspace overflow");
  }
  await page.emulateMedia({ reducedMotion: "reduce" });
  const banner = page.getByRole("complementary", { name: "站点公告", exact: true });
  await banner.getByRole("button").first().click();
  await page.getByRole("dialog").waitFor();
  await page.waitForFunction(() => {
    const panel = document.querySelector('[role="dialog"] > div');
    return panel && Number(getComputedStyle(panel).opacity) === 1;
  });
  assert(await page.locator(".site-announcement-content").innerText() === body, "Announcement body mismatch");
  assert(!await page.evaluate(() => window.__announcementExecuted), "Announcement executed HTML");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.classList.contains("announcement-title"));
  await page.getByRole("button", { name: "关闭公告", exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: "站点公告", exact: true }).waitFor();
  assert(await banner.count() === 0, "Dismissal not remembered");
  await page.getByRole("button", { name: "站点公告", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('[role="dialog"] > div')).opacity) === 1);
  await page.screenshot({ path: "output/playwright/issue48-announcement-desktop.png" });
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "打开导航", exact: true }).click();
  await page.getByRole("button", { name: "站点公告", exact: true }).click();
  await page.locator(".site-announcement-content").waitFor();
  await page.waitForFunction(() => Number(getComputedStyle(document.querySelector('[role="dialog"] > div')).opacity) === 1);
  assert(await page.getByRole("dialog").count() === 1, "Nested mobile dialogs");
  const closeBounds = await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).boundingBox();
  assert(closeBounds.width >= 44 && closeBounds.height >= 44, "Mobile close target too small");
  await page.screenshot({ path: "output/playwright/issue48-announcement-mobile.png" });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Announcement overflow");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "detached" });
  const latest = await (await page.request.get(`${root}/site/admin/settings`, { headers })).json();
  const update = async changes => {
    const current = await (await page.request.get(`${root}/site/admin/settings`, { headers })).json();
    const { announcement_revision, announcement_updated_at, ...payload } = current;
    const result = await page.request.put(`${root}/site/admin/settings`, { headers, data: { ...payload, ...changes } });
    assert(result.ok(), await result.text());
  };
  await update({ announcement_body: body + "\n新版本" });
  await page.reload();
  await banner.waitFor();
  await page.screenshot({ path: "output/playwright/issue48-banner-mobile.png" });
  await update({ announcement_published: false });
  await page.reload();
  await page.getByRole("heading", { level: 1 }).waitFor();
  assert(await page.getByRole("button", { name: "站点公告", exact: true }).count() === 0, "Unpublished announcement visible");
  assert(errors.length === 0, errors.join("\n"));
  const result = { passed: true, widths: [1440, 390], pageErrors: errors, publicationRevision: latest.announcement_revision };
  console.log(JSON.stringify(result));
  return result;
}
