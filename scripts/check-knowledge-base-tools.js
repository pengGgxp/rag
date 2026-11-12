async (page) => {
  const api = 'http://localhost:18193';
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const report = async () => (await page.request.get(api + '/__fixture/report')).json();
  const fixture = await (await page.request.get(api + '/__fixture/session')).json();
  assert((await report()).receipts.length === 0, '请重启隔离夹具后验收，避免重复执行');
  await page.context().addCookies([{ name: 'rag_session', value: fixture.cookie, domain: 'localhost', path: '/' }]);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('http://localhost:3193/?chat=1');
  const send = async (query, answer) => {
    const input = page.getByRole('textbox', { name: '发送消息', exact: true });
    await input.fill(query);
    await page.getByRole('button', { name: '发送问题', exact: true }).click();
    await page.getByText(answer, { exact: true }).waitFor();
  };
  await send('请创建知识库，名称为“' + fixture.long_name + '”，描述为合成验收资料，标签为验收。', '知识库已创建，入口在活动下方。');
  const conversationUrl = page.url();
  const created = await report();
  assert(created.receipts.length === 1 && created.libraries.length === 2, '创建记录或幂等回执不正确');
  const library = created.libraries.find(item => item.name === fixture.long_name);
  const activity = created.messages[0].tool_events.find(item => item.name === 'create_knowledge_base');
  assert(activity.status === 'completed' && activity.knowledge_base.id === library.id, '活动未持久化真实对象');
  const link = page.getByRole('link', { name: '打开知识库 · ' + fixture.long_name, exact: true });
  assert((await link.getAttribute('href')).includes(library.id), '知识库入口不是实际 ID');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.reload();
    await link.waitFor();
    assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '长名称导致页面横向溢出');
    if (width === 390) assert((await link.boundingBox()).height >= 44, '手机入口小于 44px');
    await page.screenshot({ path: 'output/playwright/issue93-created-' + width + '.png' });
  }
  await link.focus();
  await page.keyboard.press('Enter');
  await page.waitForURL('**/?library=' + library.id + '&view=documents');
  await page.getByRole('heading', { name: fixture.long_name, exact: true }).waitFor();
  await page.goto(conversationUrl);
  await page.setViewportSize({ width: 1440, height: 900 });
  await send('请修改刚创建知识库的描述为“修改后的合成描述”，其他字段保持。', '知识库描述已修改。');
  await send('请查询处理状态验收知识库的文档处理状态。', '待处理资料正在排队，暂不可检索；就绪资料可供检索。');
  await send('请列出当前可访问知识库。', '已列出当前可访问的知识库。');
  await send('知识库是什么？', '知识库用于整理可检索的资料。');
  await send('请将不存在的知识库改名为测试。', '知识库暂不可访问，请重新选择。');
  const final = await report();
  assert(final.receipts.length === 2 && final.libraries.length === 2, '只读查询或失败操作产生了写入');
  const updated = final.libraries.find(item => item.id === library.id);
  assert(updated.name === fixture.long_name && updated.description === '修改后的合成描述', '部分更新覆盖了其他字段');
  const failed = final.messages.flatMap(item => item.tool_events).filter(item => item.status === 'error');
  assert(failed.length === 1 && !failed[0].knowledge_base, '失败活动错误显示了成功入口');
  const states = final.observations.flatMap(item => item.results).find(item => item.documents)?.documents;
  assert(states?.some(item => item.document_status === 'queued' && !item.searchable), '排队状态没有正确返回');
  assert(states?.some(item => item.document_status === 'ready' && item.searchable), '就绪状态没有正确返回');
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.reload();
    await page.getByText('知识库暂不可访问，请重新选择。', { exact: true }).waitFor();
    await page.getByText('知识库暂不可访问，请重新选择。', { exact: true }).scrollIntoViewIfNeeded();
    assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '活动恢复后页面横向溢出');
    await page.screenshot({ path: 'output/playwright/issue93-completed-' + width + '.png' });
  }
  assert((await report()).receipts.length === 2, '刷新重新执行了写操作');
  return { library_id: library.id, receipts: final.receipts.length, activities: final.messages.flatMap(item => item.tool_events).length,
    states: states.map(item => ({ status: item.document_status, searchable: item.searchable })), widths: [1440, 390] };
}
