// Load the temporary browser state before running. This check makes no model calls.
async (page) => {
  await page.unrouteAll({ behavior: "wait" });
  const origin = "https://zk.str1ct.top";
  const conversations = [
    { model: "qwen3.8-max-0902", id: "5fb2f907-7413-4f5f-b7e2-5c186d375d9d", thoughts: 2 },
    { model: "deepseek-flash", id: "45117a5d-1272-4c01-9742-e0e9c86fb2d1", thoughts: 1 },
  ];
  const check = (value, message) => { if (!value) throw new Error(message); };
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const results = [];
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: width === 700 ? "reduce" : "no-preference" });
    for (const item of conversations) {
      await page.goto(`${origin}/?conversation=${item.id}`);
      const answer = page.locator(".chat-message.assistant").last();
      const thoughts = answer.locator(".conversation-thinking");
      await thoughts.first().waitFor();
      check(await thoughts.count() === item.thoughts, "Unexpected real thinking round count");
      check(await thoughts.first().getByRole("button").getAttribute("aria-expanded") === "false",
        "Completed thinking was not collapsed");
      const fetched = page.waitForResponse(response => response.url().endsWith("/thinking"));
      await thoughts.first().getByRole("button").click();
      const response = await fetched;
      check(response.status() === 200, "Protected thinking retrieval failed");
      await thoughts.first().locator(".thinking-content p").waitFor();
      check((await thoughts.first().locator(".thinking-content p").innerText()).length > 0,
        "Real thinking text was empty");
      await page.locator(".chat-messages").evaluate(node => { node.scrollTop = 0; });
      await page.screenshot({ path: `output/playwright/issue49-online-${item.model}-${width}.png` });
      await thoughts.first().getByRole("button").focus();
      await page.keyboard.press("Enter");
      check(await thoughts.first().getByRole("button").getAttribute("aria-expanded") === "false",
        "Online keyboard toggle failed");
      await page.getByText("回答详情", { exact: true }).last().click();
      await page.getByText("已收到思考内容", { exact: false }).first().waitFor();
      check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        "Online conversation overflow");
      results.push({ width, model: item.model, protectedRead: true, diagnostics: true });
    }
    await page.goto(`${origin}/admin/models`);
    await page.locator('[role="row"]').filter({ hasText: "qwen3.8-max-0902" })
      .getByRole("link").first().click();
    await page.getByRole("heading", { name: "参数验证", exact: true }).waitFor();
    await page.getByRole("button", { name: "预览发送参数", exact: true }).click();
    await page.getByRole("definition").filter({ hasText: '"high"' }).waitFor();
    await page.getByRole("heading", { name: "参数验证", exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: `output/playwright/issue49-online-admin-${width}.png` });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      "Online admin overflow");
  }
  check(errors.length === 0, errors.join("\n"));
  return { results, errors, modelCalls: 0 };
}
