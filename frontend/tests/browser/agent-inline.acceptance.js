// playwright-cli -s=issue105 run-code --filename frontend/tests/browser/agent-inline.acceptance.js
// Requires image_preview.py (synthetic providers) at :8135 and production frontend at :3135.
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const api = "http://127.0.0.1:8135", base = "http://127.0.0.1:3135";
  const session = await (await page.request.get(`${api}/__fixture/session`)).json();
  await page.context().addCookies([{ name: "rag_session", value: session.cookie,
    domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  page.on("console", message => { if (message.type() === "error") errors.push(message.text()); });
  page.on("response", response => {
    if (response.url().startsWith(api) && response.status() >= 500) errors.push(`HTTP ${response.status()}`);
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${base}/?chat=1`);
  await page.getByRole("textbox", { name: "发送消息", exact: true }).fill("请生成一张蓝色方块图片");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await page.getByText("正在生成图片", { exact: true }).first().waitFor();
  assert(await page.locator(".image-task-result").count() === 1, "运行中应展示一张进度卡片");
  assert(await page.getByRole("button", { name: "停止", exact: true }).isVisible(), "应能停止图片任务");
  await page.screenshot({ path: "output/playwright/issue105/running-desktop.png", fullPage: true });
  const conversationUrl = page.url();
  // A refresh disconnects the original event stream, but the outer worker keeps running.
  await page.reload();
  await page.getByText("图片已生成并保存，共 1 张。", { exact: true }).waitFor();
  const file = page.locator(".message-file").first();
  await file.waitFor();
  assert(await page.locator(".image-task-result img").count() === 0, "处理步骤不重复展示图片附件");
  await file.click();
  const image = page.locator(".resource-image-stage img");
  await image.waitFor();
  await image.evaluate(img => img.decode());
  assert(await image.evaluate(img => img.naturalWidth > 0), "成果缩略图应可解码");
  assert(await page.locator(".image-task-result").count() === 1, "刷新后仍只有同一张成果卡片");
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载原文件", exact: true }).click();
  const download = await downloadEvent;
  await download.saveAs("output/playwright/issue105/generated.png");
  assert((await download.failure()) === null, "原图应能下载");
  await page.keyboard.press("Escape");
  await page.screenshot({ path: "output/playwright/issue105/completed-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "移动端不能横向溢出");
  await page.screenshot({ path: "output/playwright/issue105/completed-mobile.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("textbox", { name: "发送消息", exact: true }).fill("请再生成一张蓝色方块图片");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await page.getByText("正在生成图片", { exact: true }).first().waitFor();
  await page.getByRole("button", { name: "停止回答", exact: true }).click();
  // The submitted synthetic provider returns late; it must update only this answer.
  await page.waitForFunction(() => document.querySelectorAll(".message-file").length === 2);
  await page.reload();
  await page.locator(".message-file").nth(1).waitFor();
  assert(await page.getByText("图片已生成并保存，共 1 张。", { exact: true }).count() === 1,
    "停止后的迟到结果不能续写模型回答");
  assert(await page.locator(".image-task-result").count() === 2, "两次调用应各自保留原成果");
  await page.screenshot({ path: "output/playwright/issue105/stopped-late-result.png", fullPage: true });
  await page.goto(`${base}/?chat=1`);
  assert(await page.locator(".image-task-result").count() === 0, "迟到结果不能附到新回答");
  await page.goto(conversationUrl);
  await page.locator(".message-file").nth(1).waitFor();
  assert(errors.length === 0, `浏览器错误：${errors.join("; ")}`);
  console.log("验收通过：单槽执行、进度、刷新恢复、原图下载、移动布局、停止及迟到结果归属");
}
