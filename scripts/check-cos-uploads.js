// Disposable local fixture only. This does not count as real Tencent COS acceptance.
async (page) => {
  const assert = (ok, text) => { if (!ok) throw new Error(text); };
  const session = await (await page.request.get("http://127.0.0.1:8126/__fixture/session")).json();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.context().addCookies([{ name: "rag_session", value: session.cookie, domain: "127.0.0.1", path: "/" }]);
  await page.route(url => url.origin === "http://localhost:8000", route => route.continue({
    url: route.request().url().replace("http://localhost:8000", "http://127.0.0.1:8126"),
    headers: { ...route.request().headers(), cookie: `rag_session=${session.cookie}` },
  }));
  await page.evaluate(() => Object.keys(localStorage).filter(key => key.startsWith("rag-uploads-v1:")).forEach(key => localStorage.removeItem(key)));
  const url = `http://127.0.0.1:3110/?library=${session.library}`;
  await page.goto(url);
  await page.setViewportSize({ width: 1440, height: 1000 });
  const openTasks = async () => {
    if (await page.getByRole("button", { name: "打开导航" }).count())
      await page.getByRole("button", { name: "打开导航" }).click();
    await page.getByRole("button", { name: /上传任务/ }).click();
  };
  await page.getByRole("button", { name: "上传文档", exact: true }).first().click();
  let dialog = page.getByRole("dialog");
  const content = Buffer.alloc(9 * 1024 * 1024, "a");
  content.write(String(Date.now()));
  const file = { name: "断点续传验收.txt", mimeType: "text/plain", buffer: content };
  await dialog.getByLabel("选择文件", { exact: true }).setInputFiles(file);
  await dialog.getByRole("button", { name: "开始上传" }).click();
  await dialog.getByText("正在传输", { exact: true }).waitFor();
  await dialog.getByRole("progressbar").waitFor();
  await page.screenshot({ path: "output/playwright/issue46-upload-1440.png" });
  for (const width of [700, 390]) {
    await page.setViewportSize({ width, height: 844 });
    await page.screenshot({ path: `output/playwright/issue46-upload-active-${width}.png` });
    const buttons = await dialog.locator(".icon-button").evaluateAll(nodes => nodes.map(node => ({ w: node.getBoundingClientRect().width, h: node.getBoundingClientRect().height })));
    assert(buttons.every(button => button.w >= 44 && button.h >= 44), "Mobile touch target below 44px");
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await dialog.getByRole("button", { name: `暂停${file.name}`, exact: true }).click();
  await dialog.getByText("已暂停", { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "继续", exact: true }).click();
  await dialog.getByText("正在传输", { exact: true }).waitFor();
  await page.keyboard.press("Escape");
  await dialog.waitFor({ state: "detached" });
  await page.goto(`http://127.0.0.1:3110/?library=${session.other}`);
  // Reload requires file re-selection and does not persist the original File.
  await openTasks();
  dialog = page.getByRole("dialog");
  await dialog.getByLabel(`重新选择${file.name}`, { exact: true }).setInputFiles(file);
  await dialog.getByText("正在传输", { exact: true }).waitFor();
  await page.context().setOffline(true);
  await page.waitForTimeout(400);
  await page.context().setOffline(false);
  await page.waitForTimeout(2000);
  if (await dialog.getByRole("button", { name: "重试", exact: true }).count())
    await dialog.getByRole("button", { name: "重试", exact: true }).click();
  await dialog.getByText("可检索", { exact: true }).waitFor({ timeout: 60000 });
  assert(await dialog.getByRole("link", { name: "查看文档" }).count() === 1, "Missing admitted document");
  const persisted = await page.evaluate(() => Object.entries(localStorage).filter(([key]) => key.startsWith("rag-uploads-v1:")).map(([,value]) => value).join(""));
  assert(!/https?:|secret|signature|url"/i.test(persisted), "Sensitive upload metadata persisted");
  assert(!persisted.includes("aaaaaa"), "File body persisted");
  for (const width of [700, 390]) {
    await page.setViewportSize({ width, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Upload overflow");
    await page.screenshot({ path: `output/playwright/issue46-upload-${width}.png` });
  }
  await page.keyboard.press("Escape");
  await page.goto("http://127.0.0.1:3110/admin/storage");
  await page.getByRole("heading", { name: "文件存储", exact: true }).waitFor();
  await page.getByLabel("SecretId").fill("local-test-identifier");
  await page.getByLabel("SecretKey").fill("local-test-secret");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "配置已保存" }).waitFor();
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "验证通过" }).waitFor();
  assert(await page.getByLabel("SecretKey").inputValue() === "", "Secret remained in form");
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Storage admin overflow");
    await page.screenshot({ path: `output/playwright/issue46-storage-${width}.png` });
  }
  await page.getByLabel("SecretId").fill("unsaved-test");
  await page.getByRole("link", { name: "模型管理", exact: true }).click();
  await page.getByRole("alert").filter({ hasText: "有尚未保存的修改" }).waitFor();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  assert(page.url().endsWith("/admin/storage"), "Lost dirty form");
  assert(errors.length === 0, errors.join("\n"));
  console.log("PASS: local HTTP multipart, pause/reselect/retry, worker admission, stages, private metadata, admin save/test/dirty guard, 1440/700/390 and reduced motion.");
}
