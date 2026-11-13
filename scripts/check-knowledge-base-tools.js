async (page) => {
  const api = 'http://localhost:18193';
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const report = async () => (await page.request.get(api + '/__fixture/report')).json();
  const fixture = await (await page.request.get(api + '/__fixture/session')).json();
  assert((await report()).receipts.length === 0, '请重启隔离夹具后验收，避免重复执行');
  await page.context().addCookies([{ name: 'rag_session', value: fixture.cookie, domain: 'localhost', path: '/' }]);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('http://localhost:3193/');
  await page.getByRole('button', { name: /处理状态验收/ }).waitFor();
  await page.evaluate(() => { window.__issue93Document = 'same-spa-document'; });
  const sameDocument = async () => assert(await page.evaluate(() => window.__issue93Document === 'same-spa-document'), '回归流程意外刷新了页面');
  await page.getByRole('link', { name: '知识问答', exact: true }).click();
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
  const holdLibraryList = async () => {
    let release, started;
    const gate = new Promise(resolve => { release = resolve; });
    const requested = new Promise(resolve => { started = resolve; });
    let finish;
    const finished = new Promise(resolve => { finish = resolve; });
    await page.route(api + '/knowledge-bases', async route => {
      const response = await route.fetch();
      started();
      await gate;
      await route.fulfill({ response });
      finish();
    }, { times: 1 });
    return { release, requested, finished };
  };
  const delayedCreate = await holdLibraryList();
  await link.focus();
  await page.keyboard.press('Enter');
  await page.waitForURL('**/?library=' + library.id + '&view=documents');
  await delayedCreate.requested;
  await page.getByRole('status', { name: '正在加载', exact: true }).waitFor();
  assert(await page.getByRole('heading', { name: '无法访问这个知识库', exact: true }).count() === 0, '元数据加载时误报权限失败');
  delayedCreate.release();
  await delayedCreate.finished;
  await page.getByRole('heading', { name: fixture.long_name, exact: true }).waitFor({ timeout: 5000 });
  await page.getByText('合成验收资料', { exact: true }).waitFor();
  await page.getByRole('navigation', { name: '知识库', exact: true }).getByRole('button', { name: new RegExp(fixture.long_name) }).waitFor();
  await page.waitForFunction(() => ['.page-heading', '.documents-view'].every(selector => {
    const element = document.querySelector(selector);
    return element && Number(getComputedStyle(element).opacity) > 0.99;
  }));
  await page.screenshot({ path: 'output/playwright/issue93-created-library-1440.png' });
  await sameDocument();
  const staleChatList = await holdLibraryList();
  await page.goBack();
  await page.waitForURL(conversationUrl);
  assert(page.url() === conversationUrl, '未通过 SPA 返回原会话');
  await staleChatList.requested;
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await link.waitFor();
    assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '长名称导致页面横向溢出');
    if (width === 390) assert((await link.boundingBox()).height >= 44, '手机入口小于 44px');
    await page.screenshot({ path: 'output/playwright/issue93-created-' + width + '.png' });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await send('请修改刚创建知识库的描述为“修改后的合成描述”，其他字段保持。', '知识库描述已修改。');
  await link.last().click();
  await page.getByText('修改后的合成描述', { exact: true }).waitFor({ timeout: 5000 });
  const staleChatResponse = page.waitForResponse(response => response.url() === api + '/knowledge-bases');
  staleChatList.release();
  await staleChatList.finished;
  await (await staleChatResponse).finished();
  assert(await page.getByText('合成验收资料', { exact: true }).count() === 0, '详情保留了旧描述');
  await page.getByRole('button', { name: '配置', exact: true }).click();
  const settings = page.getByRole('region', { name: '知识库配置', exact: true });
  await settings.getByText(fixture.long_name, { exact: true }).waitFor();
  await settings.getByText('修改后的合成描述', { exact: true }).waitFor();
  await settings.getByText('验收', { exact: true }).waitFor();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '知识库详情横向溢出');
    await page.screenshot({ path: 'output/playwright/issue93-updated-' + width + '.png' });
  }
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '全部知识库', exact: true }).click();
  const card = page.getByRole('button', { name: new RegExp(fixture.long_name) });
  await card.getByText('修改后的合成描述', { exact: true }).waitFor();
  await page.route(api + '/knowledge-bases', route => route.fulfill({ status: 503, contentType: 'application/json',
    body: JSON.stringify({ detail: '知识库暂时无法加载，请重试' }) }), { times: 1 });
  await card.click();
  await page.getByText('知识库暂时无法加载，请重试', { exact: true }).first().waitFor();
  assert(await page.getByRole('heading', { name: '无法访问这个知识库', exact: true }).count() === 0, '请求失败被误报为权限失败');
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.getByText('修改后的合成描述', { exact: true }).waitFor();
  await sameDocument();
  await page.getByRole('link', { name: '知识问答', exact: true }).click();
  await page.getByRole('navigation', { name: '历史对话', exact: true }).getByRole('button').first().click();
  await page.waitForURL(conversationUrl);
  assert(page.url() === conversationUrl, '未通过 SPA 返回更新会话');
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
  await page.setViewportSize({ width: 390, height: 900 });
  await link.last().click();
  await page.getByText('修改后的合成描述', { exact: true }).waitFor();
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  await page.getByRole('navigation', { name: '知识库', exact: true }).getByRole('button', { name: new RegExp(fixture.long_name) }).waitFor();
  await page.keyboard.press('Escape');
  await page.goBack();
  await page.waitForURL(conversationUrl);
  await page.route(api + '/knowledge-bases', route => route.fulfill({ status: 200, contentType: 'application/json', body: '[]' }), { times: 1 });
  await link.last().click();
  await page.getByRole('heading', { name: '无法访问这个知识库', exact: true }).waitFor();
  assert(await page.getByText('修改后的合成描述', { exact: true }).count() === 0, '服务端未返回的对象仍被旧缓存展示');
  await sameDocument();
  await page.goBack();
  await page.waitForURL(conversationUrl);
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
    states: states.map(item => ({ status: item.document_status, searchable: item.searchable })), widths: [1440, 390],
    metadata_checks: ['same_document', 'delayed_list', 'stale_response', 'retry', 'authorized_list_miss'] };
}
