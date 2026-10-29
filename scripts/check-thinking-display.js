// Run with playwright-cli run-code --filename=scripts/check-thinking-display.js.
// Uses conversation_preview.py with PREVIEW_THINKING=1, port 8129 and frontend 3109.
async (page) => {
  await page.unrouteAll({ behavior: "wait" });
  const fixture = "http://127.0.0.1:8129";
  const session = await (await page.request.get(`${fixture}/__fixture/session`)).json();
  await page.context().addCookies(["localhost", "127.0.0.1"].map(domain => ({
    name: "rag_session", value: session.cookie, domain, path: "/", sameSite: "Lax" })));
  await page.route(url => url.origin === "http://localhost:8000", route => route.continue({
    url: route.request().url().replace("http://localhost:8000", fixture),
    headers: { ...route.request().headers(), cookie: `rag_session=${session.cookie}` },
  }));
  const check = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const results = [];
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: width === 700 ? "reduce" : "no-preference" });
    await page.goto("http://127.0.0.1:3109/?chat=1");
    const input = page.getByRole("textbox", { name: "向知识库提问" });
    await input.waitFor();
    await page.getByRole("button", { name: "本次请求模型" }).filter({ hasText: "本地流式测试" }).waitFor();
    await input.fill("请核对已有记录，然后说明结论。");
    await page.getByRole("button", { name: "发送问题", exact: true }).click();
    const thoughts = page.locator(".chat-message.assistant").last().locator(".conversation-thinking");
    await thoughts.first().locator(".thinking-content").waitFor();
    check(await page.getByRole("button", { name: "停止回答", exact: true }).count() === 1, "Thinking was not streamed");
    await page.screenshot({ path: `output/playwright/issue49-thinking-running-${width}.png` });
    // Explicitly close and reopen: automatic completion must preserve this choice.
    const toggle = thoughts.first().getByRole("button");
    await toggle.click();
    await toggle.click();
    await page.getByRole("button", { name: "停止回答", exact: true }).waitFor({ state: "detached", timeout: 30000 });
    check(await toggle.getAttribute("aria-expanded") === "true", "Manual expansion was overwritten");
    check(await thoughts.count() === 3, "Not all model rounds have thinking blocks");
    check(await thoughts.nth(1).getByRole("button").getAttribute("aria-expanded") === "false", "Finished round did not collapse");
    const order = await page.locator(".chat-message.assistant").last().evaluate(node =>
      Array.from(node.children).filter(child => child.matches(".answer-text,.conversation-thinking,.conversation-activity"))
        .map(child => child.className));
    check(order.join(",") === "conversation-thinking,answer-text,conversation-activity,conversation-thinking,answer-text,conversation-activity,conversation-thinking,answer-text", "Wrong thinking/tool/text order");
    await page.locator(".chat-messages").evaluate(node => { node.scrollTop = 0; });
    await page.screenshot({ path: `output/playwright/issue49-thinking-completed-${width}.png` });
    await page.reload();
    await thoughts.first().waitFor();
    const protectedRead = page.waitForResponse(response => response.url().endsWith("/thinking"));
    await thoughts.first().getByRole("button").click();
    await protectedRead;
    await thoughts.first().getByText("先核对已有记录，再按顺序验证关键细节，最后整理结论。", { exact: true }).waitFor();
    await thoughts.first().getByRole("button").focus();
    await page.keyboard.press("Enter");
    check(await thoughts.first().getByRole("button").getAttribute("aria-expanded") === "false", "Keyboard toggle failed");
    if (width <= 700) {
      const bounds = await thoughts.first().getByRole("button").boundingBox();
      check(bounds.height >= 44, "Thinking touch target too small");
    }
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Conversation overflow");
    await page.getByText("回答详情", { exact: true }).last().click();
    check(!/推理用量|已收到思考内容|未返回思考内容|第\s*\d+\s*轮/.test(
      await page.locator(".answer-details").last().innerText()), "Internal thinking diagnostics are visible");
    await page.goto("http://127.0.0.1:3109/admin/models");
    await page.getByRole("link", { name: "本地流式测试", exact: true }).click();
    await page.getByRole("heading", { name: "参数验证", exact: true }).waitFor();
    await page.getByRole("button", { name: "预览发送参数", exact: true }).click();
    await page.getByRole("definition").filter({ hasText: "true" }).first().waitFor();
    await page.getByRole("button", { name: "测试已保存配置", exact: true }).click();
    await page.getByRole("button", { name: "确认测试", exact: true }).click();
    await page.getByText("模型已完整返回", { exact: true }).waitFor({ timeout: 30000 });
    check(await page.getByText("已收到", { exact: true }).count() === 1, "Probe did not observe thinking");
    await page.getByRole("heading", { name: "参数验证", exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/playwright/issue49-admin-validation-${width}.png` });
    await page.getByLabel("显示名称", { exact: true }).first().fill("未保存名称");
    check(!await page.getByRole("button", { name: "测试已保存配置", exact: true }).isEnabled(), "Unsaved configuration can be tested");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Admin overflow");
    // Restore input so the ordinary unsaved-navigation guard does not interfere with next viewport.
    await page.getByLabel("显示名称", { exact: true }).first().fill("本地流式测试");
    results.push({ width, orderedRounds: 3, refresh: true, parameterPreview: true, meteredTest: true });
  }
  check(errors.length === 0, errors.join("\n"));
  return { results, errors };
}
