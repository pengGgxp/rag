async (page) => {
  const input = page.getByRole('textbox', { name: '发送消息', exact: true });
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const api = 'http://localhost:8126';
  const ready = () => page.waitForFunction(() => !!document.querySelector('.composer textarea')?.getAttribute('aria-activedescendant'));
  const settleCaret = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const query = async text => {
    const response = page.waitForResponse(response => response.url().startsWith(`${api}/conversations/knowledge-catalog?`)
      && new URL(response.url()).searchParams.get('q') === text.slice(1));
    await input.fill('');
    await input.fill(text);
    await response;
    await ready();
  };
  const fixture = await (await page.request.get(`${api}/__fixture/session`)).json();
  await page.context().addCookies([{ name: 'rag_session', value: fixture.cookie, domain: 'localhost', path: '/' }]);
  await page.goto('http://localhost:3110/?chat=1');
  await input.waitFor();
  let submitted;
  page.on('request', request => {
    if (request.method() === 'POST' && /\/conversations\/[^/]+\/messages$/.test(request.url())) {
      submitted = request.postDataJSON();
    }
  });
  const overflow = async () => assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '页面横向溢出');
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await query('@');
    await page.getByRole('option', { name: '产品资料 知识库', exact: true }).waitFor();
    await ready();
    assert(await page.getByRole('option').count() === 5, '三类分组或同名文件缺失');
    await page.screenshot({ path: `output/playwright/issue86-resource-menu-${width}.png` });
    await overflow();
    await input.press('Escape');
    assert(await page.getByRole('listbox').count() === 0, 'Escape 未关闭补全');
    await query('@项目');
    await page.getByRole('option', { name: '项目说明.txt 项目/交付/项目说明.txt 资源文件', exact: true }).waitFor();
    await ready();
    await input.press('ArrowDown');
    await input.press(width === 700 ? 'Enter' : 'Tab');
    assert(await input.inputValue() === '@资源/项目/交付/项目说明.txt ', '键盘选择混淆文档和资源');
    assert(await page.locator('.composer-mention-link').count() === 1, '资源标记缺失');
    await page.screenshot({ path: `output/playwright/issue86-resource-selected-${width}.png` });
    await page.reload();
    await input.waitFor();
    await page.waitForFunction(() => document.querySelector('.composer textarea')?.value.includes('@资源/项目/交付/'));
    assert(await page.locator('.composer-mention-link').count() === 1, '刷新丢失资源 ID');
    await overflow();
    await input.fill('');
    assert(await page.locator('.composer-mention-link').count() === 0, '删除文本遗留资源 ID');
  }
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.route(`${api}/conversations/knowledge-catalog**`, async route => {
    await new Promise(resolve => setTimeout(resolve, 300));
    await route.continue();
  });
  await input.fill('@');
  await page.getByText('正在查找资料…', { exact: true }).waitFor();
  await page.getByRole('option', { name: '产品资料 知识库', exact: true }).waitFor();
  await page.unroute(`${api}/conversations/knowledge-catalog**`);
  await input.fill('@不存在');
  await page.getByText('没有匹配的资料', { exact: true }).waitFor();
  await page.route(`${api}/conversations/knowledge-catalog**`, route => route.fulfill({ status: 503,
    json: { detail: '资料暂不可用，请重试' }, headers: { 'Access-Control-Allow-Origin': 'http://localhost:3110',
      'Access-Control-Allow-Credentials': 'true' } }));
  await input.fill('@');
  await page.getByText('资料暂不可用，请重试', { exact: true }).waitFor();
  await page.unroute(`${api}/conversations/knowledge-catalog**`);
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.getByRole('option', { name: '产品资料 知识库', exact: true }).waitFor();
  await ready();
  await page.getByRole('option', { name: '产品资料 知识库', exact: true }).click();
  await settleCaret();
  await input.focus();
  await page.keyboard.insertText('@项目');
  await page.getByRole('option', { name: '项目说明.txt 产品资料 文档', exact: true }).waitFor();
  await ready();
  await page.getByRole('option', { name: '项目说明.txt 产品资料 文档', exact: true }).click();
  await settleCaret();
  await input.focus();
  await page.keyboard.insertText('@交付验收');
  await page.getByRole('option').waitFor();
  await ready();
  await page.getByRole('option').click();
  await settleCaret();
  assert(await page.locator('.composer-mention-link').count() === 3, '混合提及选择失败');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByText('已收到文件引用，本轮没有执行文件操作。', { exact: true }).waitFor();
  assert(submitted.resource_ids.length === 1 && submitted.document_ids.length === 1
    && submitted.knowledge_base_ids.length === 1, `混合提及 ID 提交错误：${JSON.stringify(submitted)}`);
  await page.reload();
  await page.getByText('已收到文件引用，本轮没有执行文件操作。', { exact: true }).waitFor();
  assert(await page.locator('[title^="资源文件 ·"]').count() === 1, '消息资源引用刷新丢失');
  await page.screenshot({ path: 'output/playwright/issue86-resource-history-390.png' });
  await overflow();
  await input.fill('@项目');
  await page.getByRole('option', { name: '项目说明.txt 项目/交付/项目说明.txt 资源文件', exact: true }).click();
  await page.evaluate(async api => {
    const identity = await (await fetch(`${api}/auth/me`, { credentials: 'include' })).json();
    await fetch(`${api}/__fixture/remove`, { method: 'POST', credentials: 'include',
      headers: { 'X-RAG-Request': '1', 'X-CSRF-Token': identity.csrf_token } });
  }, api);
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByText('提及的资源文件已不可用，请重新选择', { exact: true }).waitFor();
  assert((await input.inputValue()).includes('@资源/项目/交付/'), '拒绝发送后丢失草稿');
  assert(await page.locator('.composer-mention-link').count() === 1, '拒绝发送后丢失提及');
  await page.screenshot({ path: 'output/playwright/issue86-resource-stale-390.png' });
  const toast = await page.locator('.toast').boundingBox();
  assert(toast.x >= 0 && toast.x + toast.width <= 390, '手机错误通知超出屏幕');
  return '1440/700/390px 三类分组、文件名筛选、键盘 Enter/Tab/Escape、草稿刷新/移除、混合 ID 发送、历史恢复、加载/错误重试/空结果、失效引用草稿保留及 reduced motion 检查通过';
}
