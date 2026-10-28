async (page) => {
  const check = (value, message) => { if (!value) throw new Error(message); };
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/models/available", async route => {
    const response = await route.fetch();
    const data = await response.json();
    data.providers = data.providers.map(item => ({ ...item,
      name: "超长名称的企业知识问答与复杂推理模型",
      thinking: { kind: "effort", version: 1, default_option_id: "high",
        options: [{ id: "high", label: "复杂任务深度推理", detail: "high" }] },
    }));
    await route.fulfill({ response, json: data });
  });
  await page.goto("http://127.0.0.1:3110/?chat=1");
  const model = page.getByRole("button", { name: "本次请求模型", exact: true });
  const thinking = page.getByRole("button", { name: "本次请求思考设置", exact: true });
  await thinking.waitFor();
  const boxes = await Promise.all([model, thinking, page.getByRole("button", { name: "上下文", exact: true }),
    page.getByRole("button", { name: "发送问题", exact: true })].map(item => item.boundingBox()));
  check(boxes.every(box => box && box.width >= 44 && box.height >= 44), "Small mobile target");
  for (let index = 0; index < boxes.length - 1; index++)
    check(boxes[index].x + boxes[index].width <= boxes[index + 1].x + 1, "Composer controls overlap");
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Long labels overflow");
  await model.click();
  await page.getByRole("option").filter({ hasText: "超长名称" }).first().waitFor();
  await page.keyboard.press("Escape");
  await page.screenshot({ path: "output/playwright/issue45-long-controls-390.png" });
  await page.unroute("**/models/available");
  await page.goto("http://127.0.0.1:3110/?chat=1");
  await model.filter({ hasText: "本地流式测试" }).waitFor();
  await page.getByRole("textbox", { name: "向知识库提问" }).fill("检查滚动和取消。");
  await page.getByRole("button", { name: "发送问题", exact: true }).click();
  await page.waitForFunction(() => {
    const area = document.querySelector(".chat-messages");
    return area && area.scrollHeight > area.clientHeight + 150;
  });
  await page.locator(".chat-messages").evaluate(node => { node.scrollTop = 0; });
  await page.getByRole("button", { name: "回到最新", exact: true }).waitFor();
  await page.waitForTimeout(300);
  check(await page.locator(".chat-messages").evaluate(node => node.scrollTop < 10), "Forced follow while reading");
  await page.getByRole("button", { name: "回到最新", exact: true }).click();
  check(await page.locator(".chat-messages").evaluate(node => node.scrollHeight - node.scrollTop - node.clientHeight < 100),
    "Latest button did not scroll");
  const stop = page.getByRole("button", { name: "停止回答", exact: true });
  if (await stop.count()) await stop.click();
  await stop.waitFor({ state: "detached" });
  await page.reload();
  await page.locator(".chat-message.assistant").last().waitFor();
  check(await page.locator(".chat-message.assistant").last().innerText() !== "", "Stop lost partial content");
  return "390px long labels and 44px targets; full picker labels; reading pauses follow; latest resumes; stop and refresh preserve text";
}
