// Local UI fixtures only; real import and filesystem behavior live in test_skill_resources.py.
async (page) => {
  const base = new URL(page.url()).origin;
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  const user = { id: "skill-ui-user", name: "导入验收", email: "skill-ui@example.test", is_site_admin: true, email_verified: true };
  const identity = { user, workspace_id: "skill-ui-workspace", csrf_token: "test", workspaces: [{ id: "skill-ui-workspace", name: "合成工作区", role: "admin" }] };
  const file = { id: "skill-ui-file", path: "skills/sample/SKILL.md", source: "resource_upload", content_type: "text/markdown", size_bytes: 32, updated_at: new Date().toISOString(), availability: "available" };
  let failure = false, calls = 0;
  await page.route("http://localhost:18195/**", async route => {
    const path = new URL(route.request().url()).pathname;
    const fixtures = {
      "/auth/me": identity, "/auth/options": {}, "/conversations": [], "/knowledge-bases": [],
      "/agent/skills": [], "/agent/mcp": [], "/question-workbench": { available: false },
      "/health": { status: "ok" }, "/usage": { balance: 1000 }, "/settings/llm-providers": [],
      "/site/admin/overview": {}, "/site/settings": {},
    };
    if (path === "/agent/skills/import" || path === "/agent/skills/import-folder") {
      calls++;
      return route.fulfill({ status: failure ? 413 : 201, json: failure ? { detail: "工作区存储空间不足，请增加额度后重试" } : { directory: "skills/sample", items: [file] } });
    }
    if (path === "/agent/skills/skill-ui-file") return route.fulfill({ json: file });
    return route.fulfill({ json: fixtures[path] ?? {} });
  });
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  await page.goto(base + "/extensions");
  await page.getByRole("button", { name: "导入 Skill 包", exact: true }).waitFor();
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.screenshot({ path: `output/playwright/issue95-resource-empty-${width}.png`, fullPage: true });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "页面横向溢出");
    const button = await page.getByRole("button", { name: "导入 Skill 包", exact: true }).boundingBox();
    check(button.height >= 44, "移动操作区域不足");
    check(!await page.getByText("保存并启用", { exact: true }).count(), "仍有发布操作");
  }
  await page.locator('input[type="file"]').first().setInputFiles({ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from("Read missing.md") });
  await page.getByRole("status").filter({ hasText: "已导入 1 个资源文件" }).waitFor();
  await page.screenshot({ path: "output/playwright/issue95-resource-success-390.png", fullPage: true });
  failure = true;
  await page.locator('input[type="file"]').first().setInputFiles({ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from("Retry") });
  await page.getByRole("alert").filter({ hasText: "空间不足" }).waitFor();
  failure = false;
  await page.locator('input[type="file"]').first().setInputFiles({ name: "SKILL.md", mimeType: "text/markdown", buffer: Buffer.from("Retry") });
  await page.getByRole("status").filter({ hasText: "已导入 1 个资源文件" }).waitFor();
  await page.goto(base + "/admin/skills/new");
  await page.getByRole("button", { name: "导入 Skill 包", exact: true }).waitFor();
  await page.screenshot({ path: "output/playwright/issue95-resource-admin-390.png", fullPage: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.screenshot({ path: "output/playwright/issue95-resource-admin-1440.png", fullPage: true });
  check(calls === 3, "导入次数不符");
  check(errors.length === 0, errors.join("\n"));
  return { result: "通过", widths: [1440, 700, 390], imports: calls, realBackend: false };
}
