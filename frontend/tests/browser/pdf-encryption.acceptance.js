// Run with playwright-cli run-code --filename after signing into the disposable
// localhost:3123 / localhost:8123 fixture, seeded with backend/tests/pdf_fixtures.py.
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  assert(page.url().startsWith("http://localhost:3123/"), "Only the local Issue #123 fixture is allowed");
  const api = "http://localhost:8123";
  const session = await (await page.request.get(`${api}/auth/me`)).json();
  assert(session.user.email === "issue123-local@example.com", "Only the disposable PDF account is allowed");

  // This workstation's download interceptor changes PDF fetch responses. Keep
  // the real API bytes, authorization, Range and metering; change only the
  // browser-test response type so the interceptor does not claim these fetches.
  await page.route(`${api}/**`, async route => {
    if (!/\/issue123-objects\/|\/download(?:\?|$)/.test(route.request().url())) return route.continue();
    const response = await route.fetch();
    const headers = { ...response.headers(), "content-type": "application/octet-stream" };
    delete headers["content-disposition"];
    await route.fulfill({ response, headers });
  });
  const files = await (await page.request.get(`${api}/resources`)).json();
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on("pageerror", onError);
  const results = [];
  try {
    await page.goto("http://localhost:3123/resources");
    const open = async name => {
      const option = page.getByRole("option", { name, exact: true });
      await option.dblclick();
      const dialog = page.getByRole("dialog", { name, exact: true });
      await dialog.waitFor();
      return dialog;
    };
    const close = async () => {
      await page.keyboard.press("Escape");
      await page.getByRole("dialog").waitFor({ state: "hidden" });
    };
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 960 });
      await page.emulateMedia({ reducedMotion: width === 390 ? "reduce" : "no-preference" });
      for (const name of ["ordinary.pdf", "permissions.pdf"]) {
        const dialog = await open(name);
        await dialog.getByText("1 / 2", { exact: true }).waitFor();
        await page.waitForFunction(() => !document.querySelector(".file-page-loading") && document.querySelector(".file-page-stage canvas")?.width > 0);
        const pixels = await dialog.locator(".file-page-stage canvas").evaluate(canvas => {
          const data = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data;
          let ink = 0;
          for (let i = 0; i < data.length; i += 4) if (data[i + 3] && data[i] < 200) ink++;
          return ink;
        });
        assert(pixels > 50, `${name} must render real page text`);
        const firstPage = await dialog.locator(".file-page-stage canvas").evaluate(canvas => canvas.toDataURL());
        await dialog.getByRole("button", { name: "下一页", exact: true }).click();
        await dialog.getByText("2 / 2", { exact: true }).waitFor();
        await page.waitForFunction(first => !document.querySelector(".file-page-loading")
          && document.querySelector(".file-page-stage canvas")?.toDataURL() !== first, firstPage);
        await page.screenshot({ path: `output/playwright/pdf123-${width}-${name}.png` });
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "No page overflow");
        results.push({ width, name, pixels, pages: 2 });
        await close();
      }
      for (const [name, expected] of [["locked.pdf", "需要打开密码"], ["corrupt.pdf", "已损坏或格式无效"],
        ["empty.pdf", "为空或没有页面"], ["zero-pages.pdf", "为空或没有页面"]]) {
        const dialog = await open(name);
        const alert = dialog.getByRole("alert");
        await alert.filter({ hasText: expected }).waitFor();
        if (name === "locked.pdf") {
          assert(!(await alert.textContent()).includes("损坏"), "Password protection must not be called corruption");
          await page.screenshot({ path: `output/playwright/pdf123-${width}-password.png` });
          const saved = page.waitForEvent("download");
          await dialog.getByRole("button", { name: "下载原文件", exact: true }).click();
          const download = await saved;
          assert(download.suggestedFilename() === name, "Original download remains available");
          await download.saveAs(`output/playwright/pdf123-${width}-download.pdf`);
          assert(await download.failure() === null, "Original download succeeds");
        }
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "No error-state overflow");
        results.push({ width, name, message: await alert.textContent() });
        await close();
      }
    }
    assert(errors.length === 0, `No unhandled page errors: ${errors.join("; ")}`);
    return { results, resources: files.items.length, unhandledErrors: errors };
  } finally {
    page.off("pageerror", onError);
    await page.unroute(`${api}/**`);
  }
}
