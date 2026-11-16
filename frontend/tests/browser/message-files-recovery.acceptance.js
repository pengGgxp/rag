// 在 message-files.acceptance.js 完成后运行；真实文件 API + 合成任务状态。
// 单独验证停止后的生图迟到保存、跨处理段重复任务和刷新恢复。
async page => {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const api = "http://127.0.0.1:8136";
  const conversationId = new URL(page.url()).searchParams.get("conversation");
  const conversation = await (await page.request.get(`${api}/conversations/${conversationId}`)).json();
  const answer = conversation.messages.find(item => item.role === "assistant");
  const file = answer.tool_events.flatMap(item => item.files || []).find(item => item.path === "当前图片.png");
  assert(file, "需要完整验收生成的真实图片资源");
  let ready = false, reads = 0;
  const task = { task_id: "issue106-late-image", status: "running", terminal: false,
    summary: "图片正在生成", credits: "0", cancel_requested: true, files: [], updated_at: "2026-11-16T00:00:00Z" };
  answer.status = "cancelled";
  answer.content = "回答已停止，保留实际保存的文件。";
  answer.tool_events = ["image-first", "image-repeat"].map(call_id => ({
    call_id, name: "generate_image", label: "生成图片", kind: "tool", status: "cancelled", image_task: task,
  }));
  answer.display_blocks = [
    { id: "p1", type: "activity", call_id: "image-first" },
    { id: "t1", type: "text", text: answer.content },
    { id: "p2", type: "activity", call_id: "image-repeat" },
  ];
  await page.route(`${api}/conversations/${conversationId}`, route => route.fulfill({
    json: conversation,
    headers: { "access-control-allow-origin": "http://127.0.0.1:3136", "access-control-allow-credentials": "true" },
  }));
  await page.route(`${api}/image-tasks/${task.task_id}`, async route => {
    reads++;
    await route.fulfill({ json: ready ? { ...task, status: "cancelled", terminal: true,
      summary: "已停止，已保存文件", files: [file], updated_at: "2026-11-16T00:00:01Z" } : task,
      headers: { "access-control-allow-origin": "http://127.0.0.1:3136", "access-control-allow-credentials": "true" } });
  });
  await page.reload();
  await page.getByText(answer.content, { exact: true }).waitFor();
  assert(await page.locator(".message-file").count() === 0, "未保存时不能先造文件入口");
  ready = true;
  await page.reload();
  await page.locator(".message-file").waitFor();
  assert(await page.locator(".message-file").count() === 1, "两个处理段的同任务迟到结果必须只出现一次");
  assert(await page.locator(".image-task-files").count() === 0, "过程区不能重复显示图片附件");
  await page.locator(".message-file").click();
  await page.locator(".resource-image-stage img").waitFor();
  await page.keyboard.press("Escape");
  await page.reload();
  await page.locator(".message-file").waitFor();
  assert(reads >= 2 && await page.locator(".message-file").count() === 1, "刷新后仍须恢复实际文件");
  await page.screenshot({ path: "output/playwright/issue106/stopped-image-recovery.png", fullPage: true });
  await page.unroute(`${api}/conversations/${conversationId}`);
  await page.unroute(`${api}/image-tasks/${task.task_id}`);
  await page.reload();
  console.log("验收通过：合成生图任务的迟到保存、停止、跨段去重和刷新；文件预览仍使用真实鉴权 API。");
}
