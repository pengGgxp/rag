// Visual fixture only: run with playwright-cli run-code --filename=... against next dev.
async (page) => {
  const origin = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const session = { user: { id: "prototype", name: "设计预览", email: "preview@example.test",
    has_password: true, email_verified: true, is_site_admin: true },
    workspace_id: "preview", csrf_token: "preview",
    workspaces: [{ id: "preview", name: "知库工作区", role: "admin" }] };
  await page.route(url => url.origin === "http://localhost:8000" || url.pathname.startsWith("/api/"), route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    if (path === "/auth/me") return route.fulfill({ json: session });
    if (path === "/auth/options") return route.fulfill({ json: { first_run: false, setup_available: false } });
    return route.fulfill({ json: {} });
  });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (value, message) => { if (!value) throw new Error(message); };
  const results = [];
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${origin}/dev/conversation`);
    await page.getByRole("button", { name: "展开执行活动", exact: true }).first().waitFor();
    check(await page.locator(".answer-actions .answer-model").count() === 0, "Persistent model label remains");
    await page.screenshot({ path: `output/playwright/issue45-prototype-completed-${width}.png` });
    const blocks = await page.locator(".chat-message.assistant").evaluate(node =>
      Array.from(node.children).filter(child => child.matches(".answer-text, .conversation-activity")).map(child => child.className));
    check(blocks.join(",") === "answer-text,conversation-activity,answer-text,conversation-activity,answer-text", "Tools are not interleaved");
    await page.getByRole("button", { name: "展开执行活动", exact: true }).first().click();
    await page.getByText("找到 6 条相关内容", { exact: true }).waitFor();
    await page.getByRole("button", { name: "上下文", exact: true }).click();
    const panel = page.getByRole("dialog", { name: "上下文", exact: true });
    await panel.waitFor();
    await page.waitForFunction(() => Number(getComputedStyle(document.querySelector(".selection-surface > div")).opacity) === 1);
    await page.screenshot({ path: `output/playwright/issue45-prototype-context-${width}.png` });
    await page.getByRole("button", { name: "立即整理", exact: true }).click();
    await page.getByRole("button", { name: "取消", exact: true }).click();
    await page.getByRole("button", { name: "立即整理", exact: true }).click();
    await page.getByRole("button", { name: "确认整理", exact: true }).click();
    await page.getByText("上下文已整理", { exact: true }).waitFor();
    check(!await page.getByRole("button", { name: "立即整理", exact: true }).isEnabled(), "Redundant compaction enabled");
    await page.keyboard.press("Escape");
    await panel.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "上下文");
    await page.getByRole("combobox", { name: "样稿状态" }).selectOption("running");
    await page.getByRole("button", { name: "停止回答", exact: true }).waitFor();
    check(!await page.getByRole("button", { name: "本次请求模型", exact: true }).isEnabled(), "Generating model selection enabled");
    await page.getByRole("heading", { level: 1 }).click();
    await page.screenshot({ path: `output/playwright/issue45-prototype-running-${width}.png` });
    await page.getByRole("combobox", { name: "样稿状态" }).selectOption("failed");
    await page.getByRole("alert").filter({ hasText: "检查清单暂时无法读取" }).waitFor();
    await page.getByRole("combobox", { name: "样稿状态" }).selectOption("unconfigured");
    await page.getByRole("button", { name: "上下文", exact: true }).click();
    await page.getByText("上下文保护未就绪，请联系管理员配置模型容量。", { exact: true }).waitFor();
    check(await page.locator("progress").count() === 0, "Unconfigured capacity invents percentage");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Page horizontal overflow");
    const box = await panel.boundingBox();
    check(box.x >= 0 && box.x + box.width <= width, "Context panel overflows");
    await page.keyboard.press("Escape");
    if (width <= 700) {
      const target = await page.getByRole("button", { name: "上下文", exact: true }).boundingBox();
      check(target.height >= 44 && target.width >= 44, "Context touch target too small");
    }
    results.push(`${width}: activity, context, confirmation, empty budget, generation lockout, keyboard/focus, overflow`);
  }
  check(!errors.length, errors.join("\n"));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("combobox", { name: "样稿状态" }).selectOption("running");
  return { results, errors };
}
