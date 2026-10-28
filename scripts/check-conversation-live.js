// Requires backend/tests/conversation_preview.py and next dev on 3110.
async (page) => {
  await page.unrouteAll({ behavior: "wait" });
  const session = await (await page.request.get("http://127.0.0.1:8125/__fixture/session")).json();
  await page.context().addCookies(["localhost", "127.0.0.1"].map(domain =>
    ({ name: "rag_session", value: session.cookie, domain, path: "/", sameSite: "Lax" })));
  await page.route(url => url.origin === "http://localhost:8000", route => route.continue({
    url: route.request().url().replace("http://localhost:8000", "http://127.0.0.1:8125"),
    headers: { ...route.request().headers(), cookie: `rag_session=${session.cookie}` },
  }));
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (value, message) => { if (!value) throw new Error(message); };
  const results = [];
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto("http://127.0.0.1:3110/?chat=1");
    await page.getByRole("textbox", { name: "向知识库提问" }).waitFor();
    await page.getByRole("button", { name: "本次请求模型" }).filter({ hasText: "本地流式测试" }).waitFor();
    await page.getByRole("textbox", { name: "向知识库提问" }).fill("请核对记录并说明结果。");
    await page.getByRole("button", { name: "发送问题", exact: true }).click();
    await page.getByText("我先回查", { exact: false }).first().waitFor();
    check(await page.getByRole("button", { name: "停止回答", exact: true }).count() === 1, "Text only arrived after completion");
    check(!await page.getByRole("button", { name: "本次请求模型" }).isEnabled(), "Model can change mid-request");
    await page.locator(".conversation-activity").nth(1).waitFor();
    await page.screenshot({ path: `output/playwright/issue45-live-running-${width}.png` });
    await page.getByRole("button", { name: "停止回答", exact: true }).waitFor({ state: "detached", timeout: 30000 });
    const readBlocks = () => page.locator(".chat-message.assistant").last().evaluate(node =>
      Array.from(node.children).filter(child => child.matches(".answer-text, .conversation-activity")).map(child => child.className));
    check((await readBlocks()).join(",") === "answer-text,conversation-activity,answer-text,conversation-activity,answer-text",
      "Live activity order is wrong");
    check(await page.locator(".answer-actions .answer-model").count() === 0, "Persistent metadata remains");
    await page.locator(".chat-messages").evaluate(node => { node.scrollTop = 0; });
    await page.screenshot({ path: `output/playwright/issue45-live-completed-${width}.png` });
    await page.reload();
    await page.locator(".conversation-activity").nth(1).waitFor();
    check((await readBlocks()).join(",") === "answer-text,conversation-activity,answer-text,conversation-activity,answer-text",
      "Refresh lost tool positions");
    await page.locator(".chat-messages").evaluate(node => { node.scrollTop = 0; });
    await page.getByRole("button", { name: "上下文", exact: true }).click();
    const panel = page.getByRole("dialog", { name: "上下文", exact: true });
    await panel.waitFor();
    await page.getByText("模型容量", { exact: true }).waitFor();
    check(await panel.getByRole("progressbar").count() === 1, "Missing budget");
    await page.screenshot({ path: `output/playwright/issue45-live-context-${width}.png` });
    await page.keyboard.press("Escape");
    await panel.waitFor({ state: "detached" });
    await page.waitForFunction(() => document.activeElement?.getAttribute("aria-label") === "上下文");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
    results.push(`${width}: actual streamed text, two actual tools, ordered persistence, refresh, metadata, context, keyboard`);
  }
  const conversationId = new URL(page.url()).searchParams.get("conversation");
  const seeded = await page.request.post(`http://127.0.0.1:8125/__fixture/history?conversation_id=${conversationId}`,
    { headers: { "X-RAG-Request": "1" } });
  check(seeded.ok(), `History fixture failed: ${seeded.status()}`);
  await page.reload();
  await page.getByRole("button", { name: "上下文", exact: true }).click();
  await page.getByRole("button", { name: "立即整理", exact: true }).waitFor();
  await page.getByRole("button", { name: "立即整理", exact: true }).click();
  await page.getByRole("button", { name: "确认整理", exact: true }).click();
  await page.getByText("上下文已整理", { exact: true }).waitFor({ timeout: 15000 });
  await page.screenshot({ path: "output/playwright/issue45-live-compacted-390.png" });
  check(!await page.getByRole("button", { name: "立即整理", exact: true }).isEnabled(), "Compaction no-op still enabled");
  await page.keyboard.press("Escape");
  check(!errors.length, errors.join("\n"));
  return { results, compaction: "real API checkpoint and settlement, no duplicate compaction", errors };
}
