// Run against a frontend with controlled API fixtures; never sends model requests.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: "wait" });
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const id = "76c20d93-5f9a-41c5-998e-9962bd76cd53";
  const source = { chunk_id: id, filename: "ai的一些用法.pdf", text: "关键信息应写入文件，不能只依赖上下文。",
    char_start: 0, char_end: 25, page_number: 1, version_number: 1,
    knowledge_base_id: "kb-citations", knowledge_base_name: "AI 笔记" };
  const content = `## 协作方法\n\n关键信息应写入文件。[chunk_id: ${id}]\n\n` +
    `旧格式也支持：[${id}]\n\n> 这是引用段落。[ chunk_id： ${id} ]\n\n` +
    `代码示例：\`[chunk_id: ${id}]\`\n\n` +
    `\`\`\`text\n[chunk_id: ${id}]\n\`\`\`\n\n` +
    `[已有链接 [chunk_id: ${id}]](https://example.com)\n\n未知来源保持原文：[chunk_id: unknown]`;
  const message = { id: "answer-citation", sequence: 1, role: "assistant", content,
    status: "completed", citations: [source], traces: [], refused: false,
    display_blocks: [{ id: "text-1", type: "text", text: content }] };
  const conversation = { id: "citation-regression", title: "AI 协作方法", knowledge_bases: [],
    active_request_id: null, updated_at: new Date().toISOString(), messages: [message],
    skill_ids: [], mcp_server_ids: [] };
  const identity = { user: { id: "u-citation", name: "验收用户", email: "citation@example.com",
    has_password: true, email_verified: true, wechat_linked: false, is_site_admin: false },
    workspaces: [{ id: "w-citation", name: "验收工作区", role: "admin" }],
    workspace_id: "w-citation", csrf_token: "test" };
  const fixtures = {
    "/auth/options": { setup_available: false, first_run: false }, "/auth/me": identity,
    "/knowledge-bases": [], "/agent/skills": [], "/agent/mcp": [],
    "/health": { status: "ok" }, "/ingestion/options": { max_upload_bytes: 209715200 },
    "/models/available": { default_provider_id: "one", providers: [
      { id: "one", name: "验收模型", model: "test", available: true }] },
    "/conversations": [conversation], "/conversations/citation-regression": conversation,
    "/conversations/citation-regression/context": { eligible: false, status: "idle",
      estimated_input_tokens: 100, capacity: null, output_reserve: null },
    "/site/announcement": { announcement: null },
    "/usage": { balance: 1000, pending_credits: 0, entries: [], lifetime_spent: 0, lifetime_granted: 1000 },
  };
  await page.route(url => url.origin === "http://localhost:8000" ||
    (url.origin === base && url.pathname.startsWith("/api/")), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, "");
    check(path in fixtures, `Missing fixture: ${path}`);
    return route.fulfill({ json: fixtures[path] });
  });
  const results = [];
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(`${base}/?conversation=${conversation.id}`);
    const buttons = page.locator(".answer-text .inline-citation");
    await buttons.first().waitFor();
    check(await buttons.count() === 3, "Not all authorized prose markers became citations");
    check(await buttons.allTextContents().then(values => values.every(value => value === "1")),
      "Repeated sources received inconsistent numbers");
    check(await page.locator(".answer-text code").count() === 2, "Code blocks were changed");
    check(await page.locator(".answer-text a[href='https://example.com']").count() === 1,
      "Existing link was changed");
    await buttons.first().focus();
    await page.keyboard.press("Enter");
    const preview = page.getByRole("dialog");
    await preview.waitFor();
    await preview.getByRole("heading", { name: source.filename, exact: true }).waitFor();
    await preview.getByText(source.text, { exact: true }).waitFor();
    await page.waitForTimeout(400);
    await page.screenshot({ path: `output/playwright/issue50-citation-preview-${width}.png` });
    await page.keyboard.press("Escape");
    await preview.waitFor({ state: "detached" });
    await page.reload();
    await buttons.first().waitFor();
    check(await buttons.count() === 3, "Refresh lost source rendering");
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "Horizontal overflow");
    await page.waitForTimeout(400);
    await page.screenshot({ path: `output/playwright/issue50-citations-${width}.png` });
    results.push({ width, citations: 3, preview: true, keyboard: true, refresh: true });
  }
  check(!errors.length, errors.join("\n"));
  return { results, errors };
}
