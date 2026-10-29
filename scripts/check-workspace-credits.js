// UI fixture checks via Playwright CLI; real PostgreSQL accounting is verified separately.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const check = (ok, message) => { if (!ok) throw new Error(message); };
  const credits = { w1: { balance: 1000, lifetime_granted: 1000, lifetime_spent: 0 },
    w2: { balance: 2000, lifetime_granted: 2000, lifetime_spent: 0 } };
  const records = { w1: [], w2: [] }, saved = new Map(), calls = [];
  let mode = "", readFails = false;
  const member = { id: "u1", name: "王晓明", email: "member@example.test", email_verified: true,
    is_suspended: false, is_site_admin: false, created_at: "2026-10-20T10:00:00Z",
    active_sessions: 1, login_methods: { password: true } };
  const workspaces = () => Object.entries(credits).map(([id, value]) => ({ id,
    name: id === "w1" ? "产品知识库" : "客户支持", role: "admin", credits: { ...value } }));
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    const reply = (json, status = 200) => route.fulfill({ json, status });
    if (path === "/auth/me") return reply({ user: { id: "admin", name: "站点管理员", email: "admin@example.test",
      is_site_admin: true, email_verified: true }, csrf_token: "fixture", workspace_id: "own",
      workspaces: [{ id: "own", name: "管理员工作区", role: "admin" }] });
    if (path === "/auth/options") return reply({ first_run: false });
    if (path === "/site/admin/users/u1") return reply({ ...member, workspaces: workspaces() });
    if (path === "/site/admin/workspaces/w1") return reply({ ...workspaces()[0],
      created_at: member.created_at, member_count: 1, members: [{ ...member, role: "admin" }],
      configuration: { enabled_model_providers: 2, provider_names: ["模型一", "模型二"] }, billing_multiplier: "1.00" });
    const match = path.match(/^\/site\/admin\/workspaces\/(w1|w2)\/credits(\/adjustments)?$/);
    if (match) {
      const wid = match[1];
      if (!match[2]) return readFails ? reply({ detail: "暂时无法读取余额，请重试" }, 503) :
        reply({ credits: { ...credits[wid] }, pending_credits: 0, adjustments: records[wid] });
      const body = route.request().postDataJSON(); calls.push({ wid, ...body });
      if (mode === "conflict") { mode = ""; credits[wid].balance += 10;
        return reply({ detail: "积分余额已变化，请刷新余额后重新确认" }, 409); }
      if (!saved.has(body.request_id)) {
        const signed = body.amount * (body.direction === "add" ? 1 : -1);
        credits[wid].balance += signed;
        credits[wid][signed > 0 ? "lifetime_granted" : "lifetime_spent"] += Math.abs(signed);
        const adjustment = { id: body.request_id, amount: signed, balance_after: credits[wid].balance,
          reason: body.reason, actor_name: "站点管理员", created_at: new Date().toISOString() };
        records[wid].unshift(adjustment);
        saved.set(body.request_id, { credits: { ...credits[wid] }, adjustment });
      }
      if (mode === "uncertain") { mode = ""; return reply({ detail: "暂时无法确认调整结果" }, 503); }
      return reply(saved.get(body.request_id));
    }
    return reply({});
  });
  await page.goto(`${base}/admin/users/u1`);
  await page.getByRole("heading", { name: "王晓明", exact: true }).waitFor();
  const dialog = () => page.getByRole("dialog", { name: "调整工作区积分" });
  const start = async (index = 0) => {
    await page.getByRole("button", { name: "调整积分", exact: true }).nth(index).click();
    await dialog().getByText("当前余额", { exact: true }).waitFor();
  };
  const fill = async (amount, direction = "增加积分") => {
    await dialog().getByLabel("调整方式").selectOption({ label: direction });
    await dialog().getByLabel("积分数量").fill(amount);
    await dialog().getByLabel("调整原因").fill("客服补发与余额核对");
  };
  await start();
  check(await dialog().getByRole("button", { name: "下一步" }).isDisabled(), "Empty form must be disabled");
  await fill("200");
  await dialog().getByRole("button", { name: "下一步" }).click();
  check(calls.length === 0, "Preview must not change balance");
  await dialog().getByRole("button", { name: "确认调整" }).click();
  await dialog().waitFor({ state: "hidden" });
  await page.waitForFunction(() => document.activeElement?.id === "workspace-credits-w1");
  check(credits.w1.balance === 1200 && credits.w2.balance === 2000, "Wrong workspace updated");
  await start();
  await dialog().locator("summary").click();
  check(await dialog().getByText("客服补发与余额核对", { exact: true }).isVisible(), "History missing");
  await fill("1201", "扣减积分");
  check(await dialog().getByText("扣减积分不能超过当前余额", { exact: true }).isVisible(), "Overdraw not shown");
  await fill("100", "扣减积分");
  await dialog().getByRole("button", { name: "下一步" }).click();
  mode = "conflict";
  await dialog().getByRole("button", { name: "确认调整" }).click();
  await dialog().getByRole("button", { name: "刷新余额", exact: true }).waitFor();
  check(await dialog().getByLabel("积分数量").inputValue() === "100", "Conflict lost input");
  await dialog().getByRole("button", { name: "刷新余额", exact: true }).click();
  await dialog().getByRole("button", { name: "下一步" }).click();
  await dialog().getByRole("button", { name: "确认调整" }).click();
  await dialog().waitFor({ state: "hidden" });
  await start(1);
  await fill("50");
  mode = "uncertain";
  await dialog().getByRole("button", { name: "下一步" }).click();
  await dialog().getByRole("button", { name: "确认调整" }).click();
  await dialog().getByRole("button", { name: "重试确认", exact: true }).waitFor();
  check(await dialog().getByLabel("积分数量").isDisabled(), "Uncertain request must retain immutable input");
  await dialog().getByRole("button", { name: "重试确认", exact: true }).click();
  await dialog().waitFor({ state: "hidden" });
  check(calls.at(-1).request_id === calls.at(-2).request_id && credits.w2.balance === 2050,
    "Uncertain retry created duplicate credit");
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 950 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await start(); await fill("500");
    await dialog().getByRole("button", { name: "下一步" }).click();
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Page overflow ${width}`);
    await page.screenshot({ path: `output/playwright/issue53-adjust-${width}.png` });
    await page.keyboard.press("Escape");
    await dialog().getByRole("button", { name: "放弃修改", exact: true }).click();
    await dialog().waitFor({ state: "hidden" });
  }
  await page.goto(`${base}/admin/workspaces/w1`);
  await page.getByRole("heading", { name: "产品知识库", exact: true }).waitFor();
  readFails = true;
  await page.getByRole("button", { name: "调整积分", exact: true }).click();
  await dialog().getByRole("button", { name: "重试", exact: true }).waitFor();
  readFails = false;
  await dialog().getByRole("button", { name: "重试", exact: true }).click();
  await dialog().getByLabel("积分数量").waitFor();
  await page.keyboard.press("Escape");
  await dialog().waitFor({ state: "hidden" });
  await page.reload();
  await start();
  check(await dialog().getByText(`${credits.w1.balance.toLocaleString("zh-CN")} 积分`, { exact: true }).count() > 0,
    "Refresh did not restore balance");
  await dialog().getByLabel("积分数量").focus(); await page.keyboard.type("20");
  await page.keyboard.press("Tab"); await page.keyboard.type("键盘验收");
  check(await dialog().getByLabel("调整原因").inputValue() === "键盘验收", "Keyboard navigation failed");
  await page.keyboard.press("Escape");
  await dialog().getByRole("button", { name: "放弃修改", exact: true }).click();
  return { passed: true, viewportWidths: [1440, 700, 390], uniqueAdjustments: saved.size };
}
