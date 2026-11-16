// playwright-cli -s=issue106 run-code --filename frontend/tests/browser/message-files.acceptance.js
// 需要 message_files_preview.py :8136 与正式构建前端 :3136。无真实付费调用。
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const api = "http://127.0.0.1:8136", base = "http://127.0.0.1:3136";
  const session = await (await page.request.get(`${api}/__fixture/session`)).json();
  await page.context().addCookies([{ name: "rag_session", value: session.cookie,
    domain: "127.0.0.1", path: "/", httpOnly: true, sameSite: "Lax" }]);
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1100 });
  await page.goto(`${base}/?chat=1`);
  await page.getByRole("textbox", { name: "发送消息", exact: true }).fill("生成报告、图片及过程文件并提供文件链接");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  const answer = page.getByRole("article", { name: "知库回答", exact: true });
  const inline = answer.getByRole("link", { name: "用户需要的报告.pptx", exact: true });
  await inline.waitFor({ timeout: 60000 });
  const chatUrl = page.url();
  const files = answer.locator(".message-files");
  assert(await files.locator(".message-file").count() === 21, "21 个明确关联文件必须全部保留且去重");
  assert(await files.locator(".unavailable").count() === 1, "删除项保留文件名和不可用状态");
  assert(await files.getByRole("link", { name: "查看文件：目录甲/同名.txt", exact: true }).count() === 1
    && await files.getByRole("link", { name: "查看文件：目录乙/同名.txt", exact: true }).count() === 1,
    "不同目录同名文件必须独立");
  assert(await answer.locator(".activity-file-downloads a[href*='/resources']").count() === 0,
    "处理步骤不能重复展示文件附件");
  assert(await answer.locator(".image-task-result img").count() === 0, "聊天不能重复展示生图附件");
  const order = await files.locator(".message-file").allTextContents();
  for (const [width, columns] of [[1440, 3], [700, 2], [390, 1]]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1100 });
    assert(await files.evaluate(element => getComputedStyle(element).gridTemplateColumns.split(" ").length) === columns,
      `${width}px 列数错误`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px 横向溢出`);
    assert(await files.locator("a").evaluateAll(elements => elements.every(element => element.getBoundingClientRect().height >= 44)),
      "点击区域至少 44px");
    await page.screenshot({ path: `output/playwright/issue106/files-${width}.png`, fullPage: true });
  }
  await page.setViewportSize({ width: 1440, height: 1100 });
  // 正文和底部共用同一预览；键盘打开，Escape 后回原链接。
  const href = await inline.getAttribute("href");
  await inline.focus();
  await page.keyboard.press("Enter");
  await page.getByText("该格式暂不支持预览", { exact: true }).waitFor();
  const downloadWait = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载原文件", exact: true }).click();
  const download = await downloadWait;
  await download.saveAs("output/playwright/issue106/report.pptx");
  assert((await download.failure()) === null, "PPT 原文件下载失败");
  await page.screenshot({ path: "output/playwright/issue106/ppt-preview.png" });
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.textContent === "用户需要的报告.pptx");
  const image = files.getByRole("link", { name: "查看文件：当前图片.png", exact: true });
  const imageColor = async () => {
    const img = page.locator(".resource-image-stage img");
    await img.waitFor();
    await img.evaluate(element => element.decode());
    return img.evaluate(element => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 1;
      const context = canvas.getContext("2d");
      context.drawImage(element, 0, 0, 1, 1);
      return [...context.getImageData(0, 0, 1, 1).data];
    });
  };
  await image.click();
  const oldColor = await imageColor();
  assert(oldColor[2] > oldColor[0], "初始图片应为蓝色");
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => document.activeElement?.getAttribute("title") === "当前图片.png");
  const identity = await (await page.request.get(`${api}/auth/me`)).json();
  const overwritten = await page.request.post(`${api}/__fixture/overwrite-image`, {
    headers: { "X-RAG-Request": "1", "X-CSRF-Token": identity.csrf_token },
  });
  assert(overwritten.ok(), "验收环境图片覆盖操作失败");
  await image.click();
  const newColor = await imageColor();
  assert(newColor[0] > newColor[2], "覆盖后必须打开当前红色图片，而非旧缓存");
  assert(await page.getByText("检查通过", { exact: true }).count() === 0, "不能暗示当前画面已被此前检查");
  await page.keyboard.press("Escape");
  for (const [path, selector] of [["检查.pdf", ".resource-pdf-reader canvas"], ["说明.txt", ".resource-plain-text"],
    ["生成脚本.py", ".resource-plain-text"]]) {
    await files.getByRole("link", { name: `查看文件：${path}`, exact: true }).click();
    await page.locator(selector).waitFor();
    if (path.endsWith(".pdf")) await page.waitForFunction(() => document.querySelector(".resource-pdf-reader canvas")?.width > 0);
    await page.keyboard.press("Escape");
    await page.waitForFunction(path => document.activeElement?.getAttribute("title") === path, path);
  }
  // 单个对象的真实错误不会破坏整个消息；重试可恢复。
  const note = files.getByRole("link", { name: "查看文件：说明.txt", exact: true });
  const noteId = new URL(await note.getAttribute("href"), base).searchParams.get("file");
  for (const status of [403, 503]) {
    await page.route(`${api}/resources/${noteId}`, route => route.fulfill({
      status, json: { detail: status === 403 ? "此文件无权访问" : "文件暂时无法核验，请稍后重试" },
      headers: { "access-control-allow-origin": base, "access-control-allow-credentials": "true" },
    }));
    await note.click();
    await answer.getByRole("alert").waitFor();
    assert(await page.getByRole("dialog").count() === 0, "错误时不得显示假预览");
    await page.unroute(`${api}/resources/${noteId}`);
  }
  await note.click();
  await page.locator(".resource-plain-text").waitFor();
  await page.keyboard.press("Escape");
  await page.reload();
  await page.locator(".message-files").waitFor();
  assert(JSON.stringify(await page.locator(".message-file").allTextContents()) === JSON.stringify(order), "刷新后顺序或去重状态变化");
  // 修改键新标签页/复制链接/刷新都使用服务端真实鉴权，不需要会话内状态。
  const fresh = await page.context().newPage();
  await fresh.goto(`${base}${href}`);
  await fresh.getByText("该格式暂不支持预览", { exact: true }).waitFor();
  await fresh.reload();
  await fresh.getByRole("button", { name: "下载原文件", exact: true }).waitFor();
  await fresh.close();
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.locator(".message-file").filter({ hasText: "当前图片.png" }).click();
  await imageColor();
  await page.keyboard.press("Escape");
  assert(page.url() === chatUrl, "聊天内打开不能离开当前对话");
  assert(errors.length === 0, `浏览器运行错误：${errors.join("; ")}`);
  console.log("验收通过：真实命令文件/链接/正文/下载；21文件三档布局；图片当前内容；PDF/文本/脚本；删除/403/503/恢复；键盘焦点；资源直达与刷新；reduced motion。");
}
