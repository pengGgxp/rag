async page => {
  const api = "http://localhost:8137";
  const output = "output/playwright";
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const conversation = new URL(page.url()).searchParams.get("conversation");
  assert(conversation, "Open a conversation with multiple cloud computers first");
  const payload = await (await page.request.get(`${api}/conversations/${conversation}`)).json();
  const answer = payload.messages.filter(item => item.role === "assistant").at(-1);
  const ids = answer.tool_events.filter(item => item.name === "create_cloud_computer").map(item => item.computer_id);
  assert(ids.length >= 2, "Expected two instances created by the local acceptance model");

  const panel = page.getByRole("region", { name: "云电脑", exact: true });
  const list = panel.locator("details.cloud-computer-list");
  if (!await list.evaluate(element => element.open)) await list.locator("summary").click();
  const assertLayout = async width => {
    const metrics = await list.locator(".cloud-computer-rows").evaluate(container => {
      const left = container.getBoundingClientRect().left + container.clientLeft;
      const right = left + container.clientWidth;
      const controls = [...container.querySelectorAll(".cloud-computer-controls > *")].map(control => {
        const rect = control.getBoundingClientRect();
        return { left: rect.left, right: rect.right, height: rect.height };
      });
      return {
        clientWidth: container.clientWidth,
        scrollWidth: container.scrollWidth,
        overflowY: getComputedStyle(container).overflowY,
        controls: controls.map(control => ({ ...control, inside: control.left >= left && control.right <= right })),
      };
    });
    assert(metrics.scrollWidth <= metrics.clientWidth, `${width}px instance overflow: ${JSON.stringify(metrics)}`);
    assert(metrics.overflowY === "auto", `${width}px vertical scrolling was removed`);
    assert(metrics.controls.length && metrics.controls.every(control => control.inside && control.height >= 44),
      `${width}px controls are clipped or below 44px: ${JSON.stringify(metrics)}`);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px page overflow`);
    return metrics;
  };
  const rows = ids.map(id => panel.locator(".cloud-computer-instance").filter({ hasText: id.slice(0, 8) }));
  const names = await Promise.all(rows.map(async row =>
    (await row.locator(".cloud-instance-name").innerText()).split(" · ")[0]));
  const screenshots = {};

  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await assertLayout(width);
    screenshots[`${width}-running`] = `${output}/pr117-fix-${width}-running.png`;
    await page.screenshot({ path: screenshots[`${width}-running`], fullPage: true });
    const fee = rows[0].getByRole("button", { name: `费用详情 · ${names[0]}`, exact: true });
    await page.keyboard.press("Tab");
    await fee.focus();
    assert(await fee.evaluate(element => Number.parseFloat(getComputedStyle(element).outlineWidth) >= 2),
      `${width}px keyboard focus ring missing`);
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog", { name: `${names[0]} · 费用`, exact: true });
    await dialog.waitFor();
    screenshots[`${width}-running-fees`] = `${output}/pr117-fix-${width}-running-fees.png`;
    await page.screenshot({ path: screenshots[`${width}-running-fees`], fullPage: true });
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    assert(await fee.evaluate(element => element === document.activeElement), `${width}px fee focus was not restored`);
    await assertLayout(width);
  }

  await page.setViewportSize({ width: 1440, height: 950 });
  const endResponse = page.waitForResponse(response =>
    response.url().endsWith(`/cloud-computers/${ids[0]}/end`) && response.request().method() === "POST");
  await rows[0].getByRole("button", { name: `结束云电脑 · ${names[0]}`, exact: true }).click();
  assert((await endResponse).ok(), "Ending the first instance failed");
  await rows[0].getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 45000 });
  await rows[0].getByRole("link", { name: "查看文件", exact: true }).waitFor();
  await rows[1].getByText("云电脑运行中", { exact: true }).waitFor();

  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 950 });
    await assertLayout(width);
    screenshots[`${width}-ended`] = `${output}/pr117-fix-${width}-ended.png`;
    await page.screenshot({ path: screenshots[`${width}-ended`], fullPage: true });
    await rows[0].getByRole("link", { name: "查看文件", exact: true }).waitFor();
    assert(await rows[0].getByRole("button", { name: `结束云电脑 · ${names[0]}`, exact: true }).count() === 0,
      "Ended instance still offers an end action");
    await rows[0].getByRole("button", { name: `费用详情 · ${names[0]}`, exact: true }).click();
    const dialog = page.getByRole("dialog", { name: `${names[0]} · 费用`, exact: true });
    await dialog.getByText("最终消费", { exact: true }).waitFor();
    screenshots[`${width}-ended-fees`] = `${output}/pr117-fix-${width}-ended-fees.png`;
    await page.screenshot({ path: screenshots[`${width}-ended-fees`], fullPage: true });
    await page.keyboard.press("Escape");
    await dialog.waitFor({ state: "detached" });
    await assertLayout(width);
  }

  await rows[1].getByRole("button", { name: `结束云电脑 · ${names[1]}`, exact: true }).click();
  await rows[1].getByText("云电脑已结束", { exact: true }).waitFor({ timeout: 45000 });
  return { ids, names, screenshots, result: "running/ended states, fees, end/files actions and inner overflow verified at 1440/700/390px" };
}
