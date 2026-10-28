async (page) => {
  const assert = (ok, text) => { if (!ok) throw new Error(text); };
  const session = await (await page.request.get("http://127.0.0.1:8126/__fixture/session")).json();
  await page.context().addCookies([{ name: "rag_session", value: session.cookie, domain: "127.0.0.1", path: "/" }]);
  await page.route(url => url.origin === "http://localhost:8000", route => route.continue({
    url: route.request().url().replace("http://localhost:8000", "http://127.0.0.1:8126"),
    headers: { ...route.request().headers(), cookie: `rag_session=${session.cookie}` },
  }));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`http://127.0.0.1:3110/?library=${session.library}`);
  await page.getByRole("button", { name: "上传文档", exact: true }).first().click();
  const body = Buffer.alloc(25 * 1024 * 1024, "b");
  body.write(String(Date.now()));
  const file = { name: `跨工作区暂停验收-${Date.now()}.txt`, mimeType: "text/plain", buffer: body };
  let dialog = page.getByRole("dialog");
  await dialog.getByLabel("选择文件", { exact: true }).setInputFiles(file);
  await dialog.getByRole("button", { name: "开始上传" }).click();
  const task = () => dialog.getByRole("listitem").filter({ hasText: file.name });
  await task().getByText("正在传输", { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await dialog.waitFor({ state: "detached" });
  await page.getByRole("button", { name: /另一知识库/ }).click();
  await page.getByRole("button", { name: /上传任务/ }).click();
  await task().getByText("正在传输", { exact: true }).waitFor();
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await dialog.waitFor({ state: "detached" });
  await page.getByRole("button", { name: "Owner test workspace", exact: true }).click();
  await page.getByRole("menuitem", { name: "另一测试工作区", exact: true }).click();
  await page.getByRole("button", { name: "Owner 另一测试工作区", exact: true }).waitFor();
  assert(await page.getByRole("button", { name: /上传任务/ }).count() === 0, "Cross-workspace tasks visible");
  await page.getByRole("button", { name: "Owner 另一测试工作区", exact: true }).click();
  await page.getByRole("menuitem", { name: "test workspace", exact: true }).click();
  await page.getByRole("button", { name: /上传任务/ }).click();
  await task().getByText("已暂停", { exact: true }).waitFor();
  await task().getByRole("button", { name: "继续", exact: true }).click();
  let identifier;
  for (let attempt = 0; attempt < 40; attempt++) {
    const metadata = await page.evaluate(() => JSON.parse(localStorage.getItem(Object.keys(localStorage).find(key => key.startsWith("rag-uploads-v1:"))) || "[]"));
    identifier = metadata.find(item => item.name === file.name)?.uploadId;
    if (identifier) {
      const result = await (await page.request.get(`http://127.0.0.1:8126/uploads/${identifier}`)).json();
      if (result.parts?.length) break;
    }
    await page.waitForTimeout(200);
  }
  await task().getByRole("button", { name: `暂停${file.name}`, exact: true }).click();
  await page.reload();
  await page.getByRole("button", { name: /上传任务/ }).click();
  const corrupt = { ...file, buffer: Buffer.alloc(file.buffer.length, "c") };
  await task().getByLabel(`重新选择${file.name}`, { exact: true }).setInputFiles(corrupt);
  await task().getByText("原文件与已上传分片不一致，请取消此任务后重新上传", { exact: true }).waitFor();
  await task().getByRole("button", { name: `取消${file.name}`, exact: true }).click();
  await task().getByText("已取消", { exact: true }).waitFor();
  const state = await (await page.request.get(`http://127.0.0.1:8126/uploads/${identifier}`)).json();
  assert(state.state === "cancelled" && !state.document_id, "Cancelled transfer was admitted");
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await dialog.waitFor({ state: "detached" });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "打开导航", exact: true }).click();
  await page.getByRole("button", { name: /上传任务/ }).click();
  assert(await page.getByRole("dialog").count() === 1, "Mobile task windows stacked");
  await page.getByRole("dialog").getByRole("button", { name: "关闭", exact: true }).focus();
  await page.screenshot({ path: "output/playwright/issue46-upload-edge-390.png" });
  await page.keyboard.press("Escape");
  await page.getByRole("dialog").waitFor({ state: "detached" });
  assert(await page.getByRole("dialog").count() === 0, "Escape did not close tasks");
  console.log("PASS: soft navigation keeps transfer, workspace change pauses, return preserves File, refresh/corrupt-part guard, cancellation, mobile single dialog and Escape.");
}
