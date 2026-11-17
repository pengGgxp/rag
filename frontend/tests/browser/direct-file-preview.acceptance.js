// Run with playwright-cli against the isolated Issue120 stack and its private OSS fixtures.
async page => {
  const api = "http://localhost:8140", app = "http://localhost:3140";
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const me = await (await page.request.get(`${api}/auth/me`)).json();
  assert(me.user.email === "issue120-local@example.com", "Use the temporary acceptance account only");
  const requestHeaders = { Origin: app, "X-RAG-Request": "1", "X-CSRF-Token": me.csrf_token };
  const before = await (await page.request.get(`${api}/storage/allowances`)).json();
  const balance = await (await page.request.get(`${api}/usage`)).json();
  await page.evaluate(() => new Promise((resolve, reject) => {
    const request = indexedDB.open("zhiku-resource-previews", 2);
    request.onsuccess = () => {
      const db = request.result, transaction = db.transaction(["content", "metadata"], "readwrite");
      transaction.objectStore("content").clear(); transaction.objectStore("metadata").clear();
      transaction.oncomplete = () => { db.close(); resolve(); };
      transaction.onerror = () => reject(transaction.error);
    };
    request.onerror = () => reject(request.error);
  }));
  const errors = [], requests = [], metrics = [];
  page.on("pageerror", error => errors.push(error.message));
  const pendingRequests = [];
  const record = request => {
    const url = new URL(request.url());
    pendingRequests.push(request.allHeaders().then(headers => requests.push({
      host: url.hostname, path: url.pathname, method: request.method(),
      range: headers["range"], cookie: !!headers["cookie"],
      authorization: !!headers["authorization"],
    })));
  };
  page.on("request", record);
  const close = async () => {
    const dialog = page.getByRole("dialog");
    if (await dialog.count()) {
      await dialog.getByRole("button", { name: "关闭", exact: true }).click();
      await dialog.waitFor({ state: "detached" });
    }
  };
  const open = async name => {
    const start = Date.now(), count = requests.length;
    await page.getByRole("option", { name, exact: true }).dblclick();
    const dialog = page.getByRole("dialog", { name, exact: true });
    await dialog.waitFor();
    if (name.endsWith(".png")) await dialog.locator("img").waitFor();
    else if (name.endsWith(".pdf")) {
      await dialog.locator("canvas").waitFor();
      await dialog.locator(".file-page-loading").waitFor({ state: "detached" });
      assert(await dialog.locator("canvas").evaluate(canvas => {
        const context = canvas.getContext("2d"), bytes = context.getImageData(0, 0, canvas.width, canvas.height).data;
        let ink = 0;
        for (let index = 0; index < bytes.length; index += 64) if (bytes[index] < 220 && bytes[index+3]) ink++;
        return ink > 100;
      }), "PDF canvas is blank");
    } else if (name === "图文报告.docx" || name === "演示文稿.pptx") {
      const frame = dialog.frameLocator("iframe");
      await frame.getByText(name.endsWith("docx") ? "私有文件直连预览" : "直连演示文稿 · 第 1 页", { exact: true }).waitFor();
      assert(await frame.locator("img").evaluateAll(images => images.length && images.every(image => image.complete && image.naturalWidth > 0)), "Office image is missing");
      assert(await dialog.locator("iframe").getAttribute("sandbox") === "", "Office iframe must remain opaque and scriptless");
    }
    metrics.push({ name, milliseconds: Date.now()-start, requests: requests.length-count });
    return dialog;
  };
  await close();
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${app}/resources`);
  await page.getByRole("option", { name: "图文报告.docx", exact: true }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 1000 });
    for (const name of ["验收图片.png", "三页报告.pdf", "图文报告.docx", "演示文稿.pptx"]) {
      const dialog = await open(name);
      await page.waitForTimeout(250);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px page overflow`);
      if (width === 390) {
        const sizes = await dialog.locator("button").evaluateAll(nodes => nodes.filter(node => node.getClientRects().length)
          .map(node => ({ label: node.getAttribute("aria-label"), width: node.getBoundingClientRect().width, height: node.getBoundingClientRect().height })));
        assert(sizes.every(size => size.width >= 43.5 && size.height >= 43.5), `Small touch target: ${JSON.stringify(sizes)}`);
      }
      if (!name.endsWith(".png")) {
        await dialog.getByRole("button", { name: "下一页", exact: true }).click();
        if (name.endsWith(".pdf")) await dialog.locator(".file-page-loading").waitFor({ state: "detached" });
        else await dialog.frameLocator("iframe").getByText(name.endsWith("docx") ? "第二页" : "直连演示文稿 · 第 2 页", { exact: true }).waitFor();
        await dialog.getByRole("button", { name: "上一页", exact: true }).click();
      }
      await dialog.getByRole("button", { name: "放大", exact: true }).click();
      await dialog.getByRole("button", { name: "适应窗口", exact: true }).click();
      await page.mouse.move(0, 0);
      await page.screenshot({ path: `output/playwright/issue120/${width}-${name.split(".").pop()}.png` });
      await close();
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("option", { name: "说明.md", exact: true }).dblclick();
  assert(await page.locator(".resource-plain-text").textContent() === "# 直连预览\n<script>alert('NO')</script>\n这是一份测试文件。", "HTML must stay inert text");
  await close();
  await page.getByRole("option", { name: "旧版文件.doc", exact: true }).dblclick();
  await page.getByText("此格式请下载后查看", { exact: true }).waitFor();
  await close();
  await page.getByRole("option", { name: "损坏文档.docx", exact: true }).dblclick();
  await page.getByRole("alert").filter({ hasText: "文档未能打开" }).waitFor();
  await page.screenshot({ path: "output/playwright/issue120/damaged.png" });
  await close();
  const namespace = await (await page.request.get(`${api}/auth/me`)).json();
  assert(namespace.user.email === "issue120-local@example.com", "Acceptance account changed unexpectedly");
  await page.goto(`${app}/?conversation=e1d7f0e7-7b29-4782-8537-95341a49e94b`);
  await page.getByText("聊天文件入口验证。", { exact: true }).waitFor();
  await page.locator(".inline-file-image img").first().waitFor();
  const chatImage = page.getByRole("button", { name: "放大图片 验收图片.png" }).first();
  const chatFileLink = page.getByRole("link", { name: "查看文件：验收图片.png" });
  await chatImage.waitFor();
  await chatImage.click();
  await page.getByRole("dialog", { name: "验收图片.png" }).waitFor();
  await page.keyboard.press("Escape");
  await page.getByRole("dialog", { name: "验收图片.png" }).waitFor({ state: "detached" });
  await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "查看文件：验收图片.png");
  assert(await chatFileLink.evaluate(node => node === document.activeElement), "Chat image close did not restore focus to the file entry");
  const attachment = page.getByRole("button", { name: "查看 验收图片.png" });
  await attachment.click();
  await page.getByRole("dialog", { name: "验收图片.png" }).waitFor();
  await close();
  const allowanceBeforeDownload = await (await page.request.get(`${api}/storage/allowances`)).json();
  const usageBeforeDownload = await (await page.request.get(`${api}/usage`)).json();
  const pdfGrant = await (await page.request.post(`${api}/resources/024edd5c-bdf5-4cf2-a220-7cd60852eaaa/preview-access`, { headers: requestHeaders, data: {} })).json();
  await page.goto(`${app}/resources`);
  const pdfDialog = await open("三页报告.pdf");
  assert(await pdfDialog.getByRole("button", { name: "下载原文件", exact: true }).isVisible(), "Download action is missing");
  const download = await page.request.get(`${api}/resources/024edd5c-bdf5-4cf2-a220-7cd60852eaaa/download`);
  assert(download.ok() && Number(download.headers()["content-length"]) === pdfGrant.size_bytes,
    `Explicit download body was not delivered by the authenticated API (${download.status()} ${download.headers()["content-length"]} vs ${pdfGrant.size_bytes})`);
  await close();
  await close();
  const allowanceAfterDownload = await (await page.request.get(`${api}/storage/allowances`)).json();
  const usageAfterDownload = await (await page.request.get(`${api}/usage`)).json();
  assert(before.download_remaining_bytes === allowanceBeforeDownload.download_remaining_bytes
    && JSON.stringify(before.download_usage) === JSON.stringify(allowanceBeforeDownload.download_usage), "Preview changed allowance before explicit download");
  assert(allowanceBeforeDownload.download_remaining_bytes - allowanceAfterDownload.download_remaining_bytes === pdfGrant.size_bytes,
    "Explicit full download was not metered by actual bytes");
  assert(allowanceAfterDownload.download_usage.user_download - allowanceBeforeDownload.download_usage.user_download === pdfGrant.size_bytes
    && allowanceAfterDownload.download_usage.model_read === allowanceBeforeDownload.download_usage.model_read,
    "Download usage category or byte count is incorrect");
  assert(JSON.stringify(usageBeforeDownload) === JSON.stringify(usageAfterDownload), "Explicit file download charged user credits");
  const namespaceData = JSON.parse(await (await page.request.get(`${api}/resources/browse?path=.skill%2Fpreview`)).text());
  const skill = namespaceData.items.find(item => item.path === ".skill/preview/SKILL.md");
  assert(skill, "Skill workspace file fixture is missing");
  await page.goto(`${app}/resources?path=.skill%2Fpreview`);
  const skillOption = page.getByRole("option", { name: "SKILL.md" });
  await skillOption.dblclick();
  await page.getByRole("dialog", { name: "SKILL.md" }).waitFor();
  assert((await page.locator(".resource-plain-text").textContent()).includes("文件预览验收"), "Skill file did not open as text");
  await page.screenshot({ path: "output/playwright/issue120/skill-file.png" });
  await close();
  await page.goto(`${app}/?library=50953bcd-fd16-425c-84a5-41ee8235a5ea&view=documents`);
  await page.getByRole("button", { name: "查看 知识库原文件.pdf", description: "查看原文" }).click();
  const kbViewer = page.getByRole("dialog", { name: "知识库原文件.pdf" });
  await kbViewer.waitFor();
  await kbViewer.locator("canvas").waitFor();
  await kbViewer.locator(".file-page-loading").waitFor({ state: "detached" });
  assert(await kbViewer.locator("canvas").count() > 0, "Knowledge-base original did not open as PDF");
  await page.screenshot({ path: "output/playwright/issue120/knowledge-base-original.png" });
  await close();
  const after = await (await page.request.get(`${api}/storage/allowances`)).json();
  assert(after.download_remaining_bytes === allowanceAfterDownload.download_remaining_bytes
    && JSON.stringify(after.download_usage) === JSON.stringify(allowanceAfterDownload.download_usage), "Viewer changed download allowance after explicit download");
  const finalBalance = await (await page.request.get(`${api}/usage`)).json();
  assert(balance.balance === finalBalance.balance && balance.lifetime_spent === finalBalance.lifetime_spent
    && JSON.stringify(balance.entries) === JSON.stringify(finalBalance.entries), "Preview changed credits/ledger");
  assert(!requests.some(request => request.host === "localhost" && /\/(preview|image-preview-link|content)$/.test(request.path)), "Preview transferred/converted through API");
  await Promise.all(pendingRequests);
  const bodies = requests.filter(request => request.host.endsWith("aliyuncs.com"));
  assert(bodies.length && bodies.some(request => request.range), "No real OSS range reads observed");
  assert(bodies.every(request => !request.cookie && !request.authorization), "Application credentials leaked to OSS");
  assert(errors.length === 0, `Browser errors: ${errors.join(", ")}`);
  page.off("request", record);
  return { status: "passed", metrics, ossReads: bodies.length, rangeReads: bodies.filter(request => request.range).length,
    credentialsOmitted: true, browserErrors: errors };
}
