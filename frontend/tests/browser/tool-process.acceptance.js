// Run with playwright-cli run-code --filename after the local production build.
// All API traffic is intercepted; no production data, model or cloud calls.
async (page) => {
  const assert = (condition, text) => { if (!condition) throw new Error(text); };
  const base = "http://localhost:3127";
  const api = "http://localhost:8127";
  const build = "frontend/.next";
  const stamp = "2026-11-14T10:00:00Z";
  const errors = [], unexpected = [];
  page.on("pageerror", error => errors.push(error.message));
  const tool = (id, name, label, extra = {}) => ({
    call_id: id, name, label, kind: "tool", status: "completed", outcome: "success", ...extra,
  });
  const thinking = (id, round) => ({ id, type: "thinking", round, status: "completed" });
  const activity = id => ({ id: `activity-${id}`, type: "activity", call_id: id });
  const text = (id, value) => ({ id, type: "text", text: value });
  const file = (id, path) => ({ id, path, version: 1, size_bytes: 2048 });
  const image = { task_id: "image", status: "running", terminal: false, summary: "图片正在生成",
    credits: "1", cancel_requested: false, files: [] };
  let imageState = structuredClone(image), thinkingReads = 0;
  const tools = [
    tool("bad", "write_file", "生成文件", { status: "error", outcome: "feedback", summary: "path 必须是文本" }),
    tool("write", "write_file", "生成文件", { execution_id: "write-execution", files: [file("html", "算法可视化.html")], summary: "文件已生成" }),
    tool("read", "read_file", "读取文件", { summary: "内容已核对" }),
    tool("preview", "start_preview", "启动成果预览", {
      preview: { id: "preview", name: "排序算法", status: "ready", terminal: false, summary: "预览就绪", url: null },
      files: [file("pdf", "算法说明.pdf")],
    }),
    tool("image", "generate_image", "生成图片", { image_task: image }),
    tool("image-status", "get_image_task", "生成图片", { image_task: image }),
  ];
  const blocks = [{ ...text("intro", "我会先制作可交互页面，再核对生成的文件。"), stage: "commentary" },
    thinking("think1", 1), activity("bad"), thinking("think2", 2),
    ...tools.slice(1).map(item => activity(item.call_id)),
    text("final", "已制作排序算法可视化页面和 PDF 说明。可以打开预览，或下载文件。")];
  const user = { id: "user-message", role: "user", sequence: 0, status: "completed",
    content: "做一个排序算法可视化页面，再给我一张配图。", citations: [], traces: [], refused: false };
  const assistant = { id: "answer", role: "assistant", sequence: 1, status: "completed",
    request_id: "request", content: blocks.filter(item => item.type === "text").map(item => item.text).join("\n\n"),
    citations: [], traces: [], refused: false, tool_events: tools, display_blocks: blocks };
  const initial = { id: "fixture", title: "排序算法可视化与说明", title_source: "manual", revision: 1,
    updated_at: stamp, active_request_id: null, knowledge_bases: [], compactions: [],
    skill_ids: [], mcp_server_ids: [], question_workbench: false, messages: [user, assistant] };
  let current = structuredClone(initial);
  const submissions = [], supplements = [], stops = [];
  let simulateComposer = false, dropped = false;
  const computer = { id: "fixture-computer", status: "ready", terminal: false, summary: "云电脑运行中",
    credits: "0.5", credits_per_minute: "0.5", seconds: 60, idle_ends_at: null, max_ends_at: null };
  const preview = tools.find(item => item.call_id === "preview").preview;
  // Serve the actual prerendered app and chunks in-browser, without a listening service.
  await page.route(`${base}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === "/") return route.fulfill({ path: `${build}/server/app/index.html`, contentType: "text/html" });
    if (path === "/icon.svg") return route.fulfill({ path: `${build}/../app/icon.svg`, contentType: "image/svg+xml" });
    if (/^\/_next\/static\/[a-zA-Z0-9_./-]+$/.test(path) && !path.includes("..")) {
      return route.fulfill({ path: `${build}/${path.replace("/_next/", "")}`,
        contentType: path.endsWith(".js") ? "application/javascript" : path.endsWith(".css") ? "text/css" : undefined });
    }
    return route.fulfill({ status: 404, body: "local fixture route" });
  });
  await page.route(`${api}/**`, async route => {
    const path = new URL(route.request().url()).pathname;
    const headers = { "access-control-allow-origin": base, "access-control-allow-credentials": "true",
      "access-control-allow-headers": "*", "access-control-allow-methods": "GET,POST,DELETE,OPTIONS" };
    const send = body => route.fulfill({ json: body, headers });
    if (route.request().method() === "OPTIONS") return route.fulfill({ status: 204, headers });
    if (path === "/auth/options") return send({ oauth_name: null, first_run: false, setup_available: false, password_reset: false });
    if (path === "/auth/me") return send({ user: { id: "fixture-user", name: "本地验收", email: "fixture@example.invalid" },
      workspace_id: "fixture-workspace", workspaces: [{ id: "fixture-workspace", name: "验收工作区", role: "editor" }], csrf_token: "fixture" });
    if (path === "/knowledge-bases") return send([]);
    if (path === "/health") return send({ status: "ok", database: "ok", redis: "ok" });
    if (path === "/site/announcement") return send({ announcement: null });
    if (path === "/site/contact") return send({ administrator_contact: "" });
    if (path === "/question-workbench" || path === "/search/availability") return send({ available: false });
    if (path === "/models/available") return send({ default_provider_id: "fixture-model",
      providers: [{ id: "fixture-model", name: "验收模型", model: "fixture", available: true,
        supports_images: true, context_capacity: 128000, max_output_tokens: 4000 }] });
    if (path === "/conversations") return send({ items: [current], next_cursor: null });
    if (path === "/conversations/fixture") return send(current);
    if (path === "/conversations/fixture/context") return send({ estimated_input_tokens: 1200,
      capacity: 128000, output_reserve: 4000, eligible: true, status: "idle" });
    if (simulateComposer && path.endsWith("/steer")) {
      const input = route.request().postDataJSON();
      supplements.push(input);
      return send({ id: input.message_id, status: "pending" });
    }
    if (simulateComposer && path.endsWith("/stop")) {
      stops.push(route.request().postDataJSON().request_id);
      current.active_request_id = null;
      current.messages[1].status = "cancelled";
      return send(current.messages[1]);
    }
    if (simulateComposer && path.endsWith("/messages")) {
      const input = route.request().postDataJSON();
      submissions.push(input);
      const sequence = current.messages.length;
      current.messages.push({ ...user, id: `user-${sequence}`, sequence, content: input.query },
        { ...assistant, sequence: sequence + 1, id: `reply-${sequence}`, request_id: input.request_id,
          status: "completed", content: "下一轮已完成", tool_events: [], display_blocks: [] });
      return send({ request_id: input.request_id, status: "queued" });
    }
    if (path.endsWith("/thinking")) {
      thinkingReads++;
      return send({ blocks: [{ id: "think1", text: "先核对文件路径和页面需求。" }, { id: "think2", text: "已调整参数，继续生成。" }] });
    }
    if (path.endsWith("/tools/write-execution")) return send({ text: "文件：算法可视化.html", missing: false, redacted: false, truncated: false, next_offset: null });
    if (path.endsWith("/events")) {
      if (simulateComposer && !dropped) { dropped = true; return route.abort("connectionreset"); }
      const turn = path.split("/").at(-2);
      const answer = current.messages.find(item => item.request_id === turn);
      return route.fulfill({ contentType: "text/event-stream", headers,
        body: simulateComposer && answer && answer.status !== "streaming"
          ? `event: done\ndata: ${JSON.stringify(answer)}\n\n` : 'event: heartbeat\ndata: {}\n\n' });
    }
    if (path === "/cloud-computer/catalog") return send({ available: true, credits_per_minute: 0.5, idle_seconds: 600 });
    if (path === "/conversations/fixture/cloud-computer") return send({ computer });
    if (path === "/conversations/fixture/cloud-computer/commands") return send({ items: [] });
    if (path === "/conversations/fixture/preview") return send({ preview });
    if (path === "/previews/preview") return send({ preview, computer, conversation_id: "fixture" });
    if (path === "/image-tasks/image") return send(imageState);
    const resourceId = path.match(/^\/resources\/([^/]+)$/)?.[1];
    if (resourceId) return send(file(resourceId, resourceId === "html" ? "算法可视化.html" : "算法说明.pdf"));
    if (/^\/resources\/[^/]+\/preview-access$/.test(path)) return send({
      revision: "fixture", kind: "unsupported", size_bytes: 2048, url: "", etag: "fixture", headers: {},
    });
    if (path.endsWith("/download")) return route.fulfill({ headers: { ...headers, "content-disposition": "attachment; filename=fixture.html" },
      contentType: "text/html", body: "<!doctype html><title>local fixture</title>" });
    unexpected.push(path);
    return route.fulfill({ status: 404, headers, json: { detail: "unmocked fixture route" } });
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${base}/?conversation=fixture`);
  const process = page.getByRole("button", { name: "展开处理过程", exact: true });
  await process.waitFor();
  assert(await process.count() === 1, "连续操作应只有一个过程入口");
  assert(await page.locator(".image-task-result").count() === 1, "查询同一图片任务不能重复呈现产物");
  assert(await page.locator(".activity-details").count() === 0, "详情应默认收起");
  assert(!await page.getByText("我会先制作可交互页面，再核对生成的文件。", { exact: true }).isVisible(), "过程说明随活动收起");
  assert(!await page.getByText("path 必须是文本", { exact: true }).count(), "参数反馈不应铺在正文");
  assert(await page.getByRole("link", { name: "查看文件：算法可视化.html", exact: true }).isVisible(), "第一个文件应在消息下方");
  assert(await page.getByRole("link", { name: "查看文件：算法说明.pdf", exact: true }).isVisible(), "第二个文件不能被分组吞掉");
  assert(await page.getByRole("button", { name: "打开预览 · 排序算法", exact: true }).isVisible(), "预览入口应直接可用");
  assert(thinkingReads === 0, "收起过程不能提前读取思考");

  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await process.scrollIntoViewIfNeeded();
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px 不能横向溢出`);
    if (width <= 700) assert((await process.boundingBox()).height >= 44, "手机过程入口须有44px点击高度");
    await page.screenshot({ path: `output/playwright/tool-process-${width}-collapsed.png`, fullPage: true });
    await process.focus();
    await page.keyboard.press("Enter");
    await page.getByRole("list", { name: "处理过程", exact: true }).waitFor();
    assert(await page.getByText("已反馈模型", { exact: true }).isVisible(), "参数反馈应明确但不冒充失败");
    assert(await page.locator(".activity-feedback .activity-state-label").evaluate(el => {
      const probe = document.createElement("span"); probe.style.color = "var(--danger)"; document.body.append(probe);
      const neutral = getComputedStyle(el).color !== getComputedStyle(probe).color; probe.remove(); return neutral;
    }), "反馈不能使用任务失败色");
    await page.screenshot({ path: `output/playwright/tool-process-${width}-expanded.png`, fullPage: true });
    await page.getByRole("button", { name: "收起处理过程", exact: true }).click();
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await process.click();
  await page.getByRole("button", { name: "展开第 1 轮思考", exact: true }).click();
  await page.getByText("先核对文件路径和页面需求。", { exact: true }).waitFor();
  await page.getByText("查看操作详情", { exact: true }).click();
  await page.getByText("文件：算法可视化.html", { exact: true }).waitFor();
  assert(thinkingReads === 1, "仅在用户展开思考后读取内容");
  await page.getByRole("button", { name: "收起处理过程", exact: true }).click();
  await page.getByRole("link", { name: "查看文件：算法可视化.html", exact: true }).click();
  await page.getByText("此格式请下载后查看", { exact: true }).waitFor();
  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载原文件", exact: true }).click();
  assert((await download).suggestedFilename().includes("算法可视化"), "下载入口应保留真实文件名");
  await page.keyboard.press("Escape");

  imageState = { ...image, status: "completed", terminal: true, summary: "图片已生成", files: [file("picture", "排序配图.png")] };
  await page.getByRole("link", { name: "查看文件：排序配图.png", exact: true }).waitFor({ timeout: 12000 });
  assert(await page.locator(".activity-details").count() === 0, "独立任务更新不能自动展开过程");
  await page.screenshot({ path: "output/playwright/tool-process-image-completed.png", fullPage: true });
  imageState = { ...image, status: "failed", terminal: true, summary: "图片生成未完成，请重新尝试" };
  await page.reload();
  await page.getByText("生成图片 · 失败：图片生成未完成，请重新尝试", { exact: true }).waitFor();
  assert(await page.locator(".chat-message-error").count() === 0, "独立图片失败不能改写回答状态");

  // Poll restored server state while preserving the user's disclosure choice.
  current = structuredClone(initial);
  current.active_request_id = "request";
  current.messages[1] = { ...assistant, status: "streaming", content: "", tool_events: [tools[0]],
    display_blocks: [thinking("think1", 1), activity("bad")] };
  await page.reload();
  await page.getByText("正在调整操作", { exact: true }).waitFor();
  await page.getByRole("button", { name: "展开处理过程", exact: true }).click();
  current.messages[1].tool_events.push(tool("fixed", "write_file", "生成文件", { status: "running" }));
  current.messages[1].display_blocks.push(activity("fixed"));
  await page.getByText("生成文件 · 进行中", { exact: true }).waitFor({ timeout: 10000 });
  assert(await page.getByRole("button", { name: "收起处理过程", exact: true }).count() === 1, "追加操作不能收起用户展开的过程");
  await page.getByRole("button", { name: "收起处理过程", exact: true }).click();
  current.active_request_id = null;
  current.messages[1].status = "completed";
  current.messages[1].tool_events[1].status = "completed";
  current.messages[1].display_blocks.push(text("answer", "文件已生成。"));
  current.messages[1].content = "文件已生成。";
  await page.getByText("文件已生成。", { exact: true }).waitFor({ timeout: 10000 });
  assert(await page.locator(".activity-details").count() === 0, "完成不能自动展开过程");
  await page.reload();
  await page.getByText("处理记录 · 2 项操作", { exact: true }).waitFor();
  assert(await page.locator(".activity-details").count() === 0, "刷新后仍采用同一分组规则");

  current.messages[1] = { ...assistant, status: "failed", error_detail: "本次回答未能完成，请重试",
    tool_events: [
      tool("balance", "run_command", "云电脑执行", { status: "error", outcome: "blocked", summary: "积分不足，请充值后重试" }),
      tool("unknown", "write_file", "生成文件", { status: "uncertain", outcome: "uncertain", summary: "请先核对已有输出，勿重复运行" }),
    ], display_blocks: [activity("balance"), activity("unknown")] };
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.setViewportSize({ width: 390, height: 1000 });
  await page.reload();
  await page.getByText("云电脑执行 · 操作受限：积分不足，请充值后重试", { exact: true }).waitFor();
  assert(await page.getByText("生成文件 · 结果待确认：请先核对已有输出，勿重复运行", { exact: true }).isVisible(), "结果不确定必须保持可见");
  assert(await page.locator(".chat-message-error").isVisible(), "真实回答失败必须保持可见");
  await page.screenshot({ path: "output/playwright/tool-process-errors-390.png", fullPage: true });

  simulateComposer = true;
  const openTurn = async () => {
    current = structuredClone(initial);
    current.active_request_id = "request";
    current.messages[1] = { ...assistant, status: "streaming", content: "已收到原问题",
      tool_events: [], display_blocks: [], created_at: new Date().toISOString() };
    await page.reload();
    await page.getByRole("button", { name: "停止回答", exact: true }).waitFor();
  };
  await openTurn();
  const composer = page.getByRole("textbox", { name: "发送消息" });
  await page.getByText("正在恢复对话连接…", { exact: true }).waitFor();
  await composer.fill("输入法还在组词");
  await composer.dispatchEvent("keydown", { key: "Enter", isComposing: true, bubbles: true });
  assert(supplements.length === 0, "输入法确认不能提交补充");
  await composer.fill("补充约束");
  await composer.press("Enter");
  await page.getByText("待补充", { exact: true }).waitFor();
  assert(supplements.length === 1, "同轮补充只提交一次");
  current.messages[1].display_blocks.push({ id: supplements[0].message_id, type: "user", text: "补充约束" });
  await page.reload();
  await page.locator(".turn-steering").getByText("补充约束", { exact: true }).waitFor();
  assert(supplements.length === 1, "刷新不能再次提交补充");
  await composer.fill("下一轮再整理");
  await composer.press("Tab");
  const heldRow = page.locator(".turn-held-input").filter({ hasText: "下一轮再整理" });
  await heldRow.getByRole("button", { name: "取回编辑" }).click();
  assert(await composer.inputValue() === "下一轮再整理");
  await composer.press("Tab");
  await heldRow.waitFor();
  await page.screenshot({ path: "output/playwright/issue139-composer-390.png", fullPage: true });
  await composer.press("Escape");
  await page.getByText("已停止生成", { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('textarea[aria-label="发送消息"]').value === "下一轮再整理");
  assert(stops[0] === "request" && submissions.length === 0, "普通停止恢复草稿且不发送排队消息");
  await openTurn();
  await composer.fill("现在就发送");
  await page.getByRole("button", { name: "中断并发送", exact: true }).click();
  await page.getByText("下一轮已完成", { exact: true }).waitFor();
  assert(stops.length === 2 && submissions.length === 1 && submissions[0].query === "现在就发送", "中断后只立即提交所选输入");
  await openTurn();
  await composer.fill("自动接续");
  await page.getByRole("button", { name: "排到下一轮", exact: true }).click();
  current.active_request_id = null;
  current.messages[1].status = "failed";
  current.messages[1].error_detail = "回答暂时中断";
  await page.getByText("下一轮已完成", { exact: true }).waitFor();
  assert(submissions.length === 2 && submissions[1].query === "自动接续", "通常终止失败后串行发送队列");
  assert(errors.length === 0, `浏览器异常：${errors.join("; ")}`);
  assert(unexpected.length === 0, `缺失夹具：${[...new Set(unexpected)].join(", ")}`);
  return "PASS: grouped tools, details, downloads, late images, 3 widths, reconnect, input recovery, steering, queue, stop and immediate submission.";
}
