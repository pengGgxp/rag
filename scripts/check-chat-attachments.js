// Local fixture exercises real APIs; the upstream model and storage are explicitly test doubles.
async page => {
  const base = "http://127.0.0.1:3110";
  const api = "http://127.0.0.1:8127";
  const { cookie } = await (await page.request.get(`${api}/__fixture/session`)).json();
  await page.context().addCookies([{ name: "rag_session", value: cookie, url: base }]);
  const check = (ok, message) => { if (!ok) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto(`${base}/?chat=1`);
  await page.getByRole("button", { name: "本次请求模型" }).click();
  await page.getByRole("option", { name: "视觉模型 test-model" }).click();
  const files = () => page.locator('input[type="file"].chat-attachment-input');
  const image = {
    name: "设计草图.png", mimeType: "image/png",
    buffer: Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC", "base64"),
  };
  const document = { name: "产品说明.txt", mimeType: "text/plain", buffer: Buffer.from("这份附件用于核对文档问答与续问。") };
  // Read one actual valid PNG from fixture-free browser canvas, not a mock image URL.
  image.buffer = Buffer.from(await page.evaluate(() => {
    const canvas = document.createElement("canvas"); canvas.width = 160; canvas.height = 100;
    const context = canvas.getContext("2d"); context.fillStyle = "#2563eb"; context.fillRect(0, 0, 160, 100);
    context.fillStyle = "#fff"; context.font = "18px sans-serif"; context.fillText("Knowledge", 18, 54);
    return canvas.toDataURL("image/png").split(",")[1];
  }), "base64");
  let failUpload = true;
  await page.route(`${api}/chat/attachments`, async route => {
    if (route.request().method() !== "POST") return route.continue();
    if (failUpload) { failUpload = false; return route.fulfill({ status: 503, json: { detail: "上传失败，请重试" } }); }
    await new Promise(resolve => setTimeout(resolve, 500));
    return route.continue();
  });
  await files().setInputFiles(document);
  await page.getByRole("button", { name: "重试上传 产品说明.txt" }).click();
  await page.getByRole("button", { name: "重试上传 产品说明.txt" }).waitFor({ state: "hidden" });
  await page.getByRole("button", { name: "发送问题" }).waitFor({ state: "visible" });
  await page.waitForFunction(() => !document.querySelector('[aria-label="发送问题"]').disabled);
  await files().setInputFiles(image);
  await page.getByText("正在上传", { exact: true }).waitFor({ state: "hidden" });
  check(await page.locator(".composer .chat-attachments li").count() === 2, "Both drafts must remain");
  await page.getByRole("button", { name: "本次请求模型" }).click();
  await page.getByRole("option", { name: "文字模型 text-only" }).click();
  check(await page.getByRole("button", { name: "发送问题" }).isDisabled(), "Image must not send with a text model");
  check(await page.locator(".composer .chat-attachments li").count() === 2, "Switching model lost attachments");
  await page.getByRole("button", { name: "本次请求模型" }).click();
  await page.getByRole("option", { name: "视觉模型 test-model" }).click();
  const transferImage = async type => {
    const transfer = await page.evaluateHandle(({ name, mime, data }) => {
      const bytes = Uint8Array.from(atob(data), value => value.charCodeAt(0));
      const dt = new DataTransfer(); dt.items.add(new File([bytes], name, { type: mime })); return dt;
    }, { name: image.name, mime: image.mimeType, data: image.buffer.toString("base64") });
    if (type === "drop") await page.locator(".composer").dispatchEvent("drop", { dataTransfer: transfer });
    else await page.getByRole("textbox", { name: "向知识库提问" }).evaluate((element, clipboardData) =>
      element.dispatchEvent(new ClipboardEvent("paste", { clipboardData, bubbles: true, cancelable: true })), transfer);
    await transfer.dispose();
    await page.getByText("正在上传", { exact: true }).waitFor({ state: "hidden" });
    check(await page.locator(".composer .chat-attachments li").count() === 2, `${type} did not add image`);
  };
  await page.getByRole("button", { name: "移除 设计草图.png" }).click();
  await transferImage("drop");
  await page.getByRole("button", { name: "移除 设计草图.png" }).click();
  await transferImage("paste");
  let admissionFailed = false;
  await page.route(/\/conversations\/[^/]+\/messages$/, async route => {
    if (!admissionFailed) { admissionFailed = true; return route.fulfill({ status: 422, json: { detail: "请检查附件后重试" } }); }
    await new Promise(resolve => setTimeout(resolve, 500));
    return route.continue();
  });
  await page.getByRole("textbox", { name: "向知识库提问" }).fill("保留这段问题");
  await page.getByRole("button", { name: "发送问题" }).click();
  await page.getByText("请检查附件后重试", { exact: true }).waitFor();
  check(await page.getByRole("textbox", { name: "向知识库提问" }).inputValue() === "保留这段问题", "Failed send lost question");
  check(await page.locator(".composer .chat-attachments li").count() === 2, "Failed send lost drafts");
  await page.getByRole("button", { name: "关闭通知" }).click();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 950 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.getByRole("textbox", { name: "向知识库提问" }).fill("请帮我分析这张草图，并结合文件给出建议。");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Overflow at ${width}`);
    await page.screenshot({ path: `output/playwright/issue56-draft-${width}.png` });
  }
  // Image-only is a valid turn. Actual server admission binds the two attachments.
  await page.getByRole("textbox", { name: "向知识库提问" }).fill("");
  await page.getByRole("button", { name: "发送问题" }).click();
  await page.getByRole("button", { name: "停止回答" }).waitFor();
  check(await page.getByRole("button", { name: "添加图片或文件" }).isDisabled(), "Upload disabled during generation");
  await page.getByRole("button", { name: "停止回答" }).waitFor({ state: "hidden" });
  check(await page.locator(".chat-question").count() === 0, "Do not invent an image-only prompt");
  check(await page.locator(".chat-sent-attachments li").count() === 2, "Sent attachments missing");
  await page.getByRole("button", { name: "查看 设计草图.png", exact: true }).click();
  await page.getByRole("dialog", { name: "设计草图.png" }).waitFor();
  check(await page.locator(".chat-image-preview").evaluate(img => img.naturalWidth === 160), "Protected preview did not load");
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "设计草图.png" }).waitFor({ state: "hidden" });
  await page.reload();
  await page.getByRole("button", { name: "查看 设计草图.png", exact: true }).waitFor();
  check(await page.locator(".chat-sent-attachments li").count() === 2, "Refresh lost original attachments");
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 950 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Sent overflow at ${width}`);
    await page.screenshot({ path: `output/playwright/issue56-sent-${width}.png` });
  }
  check(errors.length === 0, errors.join("\n"));
  return { passed: true, viewports: [1440, 700, 390], tests: ["upload retry", "draft preservation", "attachment-only",
    "model compatibility", "paste", "drag drop", "admission failure preservation",
    "generation disabled", "private image preview", "escape", "refresh", "no overflow"] };
}
