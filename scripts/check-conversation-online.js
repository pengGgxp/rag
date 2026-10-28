async (page) => {
  // Load the temporary session with playwright-cli state-load before running.
  const state = { frontend_url: "https://zk.str1ct.top", conversation_id: "80820313-4b9b-490e-bde1-1a7cf152686c" };
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (value, message) => { if (!value) throw new Error(message); };
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 1440 ? 1000 : 844 });
    await page.goto(`${state.frontend_url}/?conversation=${state.conversation_id}`);
    await page.locator(".conversation-activity").nth(1).waitFor();
    const order = await page.locator(".chat-message.assistant").last().evaluate(node =>
      Array.from(node.children).filter(child => child.matches(".answer-text, .conversation-activity"))
        .map(child => child.className));
    check(order.join(",") === "answer-text,conversation-activity,answer-text,conversation-activity,answer-text",
      "Online tool blocks lost their positions");
    await page.locator(".chat-messages").evaluate(node => { node.scrollTop = 0; });
    await page.screenshot({ path: `output/playwright/issue45-online-tools-${width}.png` });
    await page.getByRole("button", { name: "展开执行活动", exact: true }).first().click();
    await page.getByRole("list", { name: "执行活动", exact: true }).waitFor();
    await page.getByRole("button", { name: "收起执行活动", exact: true }).first().click();
    await page.reload();
    await page.locator(".conversation-activity").nth(1).waitFor();
    check(await page.locator(".answer-actions .answer-model").count() === 0, "Persistent model label");
    await page.getByRole("button", { name: "本次请求模型", exact: true }).click();
    await page.getByRole("option").filter({ hasText: "deepseek-flash" }).click();
    await page.getByRole("button", { name: "上下文", exact: true }).click();
    await page.getByText("模型容量", { exact: true }).waitFor();
    await page.getByText("1,000,000 Token", { exact: true }).waitFor();
    await page.screenshot({ path: `output/playwright/issue45-online-context-${width}.png` });
    await page.keyboard.press("Escape");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Online overflow");
    results.push(`${width}: real model/tool blocks, expand, refresh, context capacity, no persistent model badge or overflow`);
  }
  const prototype = await page.request.get(`${state.frontend_url}/dev/conversation`);
  check(prototype.status() === 404, `Prototype HTTP ${prototype.status()}`);
  check(!errors.length, errors.join("\n"));
  return { results, prototype: 404, errors };
}
