async (page) => {
  const kb = '11111111-1111-4111-8111-111111111111';
  const doc = '22222222-2222-4222-8222-222222222222';
  const secondDoc = '55555555-5555-4555-8555-555555555555';
  let duplicateNames = false;
  const model = '33333333-3333-4333-8333-333333333333';
  let submitted;
  const conversation = { id: '44444444-4444-4444-8444-444444444444', title: 'Test', knowledge_bases: [{ id: kb, name: 'Project notes' }], messages: [], skill_ids: [], mcp_server_ids: [], active_request_id: null, updated_at: new Date().toISOString() };
  await page.unroute('http://localhost:8000/**');
  await page.route('http://localhost:8000/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let data = [];
    if (path.endsWith('/messages') && route.request().method() === 'POST') {
      submitted = route.request().postDataJSON();
      return route.fulfill({ status: 409, json: { detail: '提及的文档已不可用，请重新选择' }, headers: { 'Access-Control-Allow-Origin': 'http://localhost:3110', 'Access-Control-Allow-Credentials': 'true' } });
    }
    if (path === '/conversations' && route.request().method() === 'POST' || path === `/conversations/${conversation.id}`) data = conversation;
    else if (path.endsWith('/context')) data = { status: 'idle', capacity: 64000, output_reserve: 4096, estimated_input_tokens: 100, eligible: false };
    else if (path === '/auth/options') data = { first_run: false, setup_available: false, password_reset: false };
    else if (path === '/auth/me') data = { user: { id: 'u', name: 'Test', email: 'test@example.test' }, workspace_id: 'w', csrf_token: 'test', workspaces: [{ id: 'w', name: 'Product team', role: 'admin' }] };
    else if (path === '/knowledge-bases') data = [{ id: kb, name: 'Project notes', status: 'active', document_count: 2, tags: [], description: '' }];
    else if (path.endsWith('/documents')) data = [{ id: doc, knowledge_base_id: kb, filename: '产品需求与验收说明.pdf', status: 'ready', is_active: true }, { id: secondDoc, knowledge_base_id: kb, filename: duplicateNames ? '产品需求与验收说明.pdf' : '市场调研与访谈结果.pdf', status: 'ready', is_active: true }];
    else if (path === '/models/available') data = { default_provider_id: model, providers: [{ id: model, name: 'Test model', model: 'test', available: true, supports_tools: true, context_capacity: 64000, max_output_tokens: 4096 }] };
    else if (path === '/site/announcement') data = { announcement: null };
    else if (path === '/search/availability') data = { available: false };
    else if (path === '/agent/skills/platform') data = [{ id: 's', name: '写作助手', description: '整理资料与撰写文章', enabled: true }];
    await route.fulfill({ json: data, headers: { 'Access-Control-Allow-Origin': 'http://localhost:3110', 'Access-Control-Allow-Credentials': 'true', 'Access-Control-Allow-Headers': '*' } });
  });
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`http://localhost:3110/?chat=1&library=${kb}`);
    const input = page.getByRole('textbox', { name: '向知识库提问' });
    await input.waitFor();
    await input.fill('@');
    await page.getByRole('option', { name: /产品需求/ }).waitFor();
    await page.screenshot({ path: `output/playwright/issue73-mention-${width}.png` });
    await input.press('Enter');
    if (await input.inputValue() !== '@产品需求与验收说明.pdf ') throw new Error('Mention insertion failed');
    await page.locator('.document-mention-menu').waitFor({ state: 'detached' });
    await input.press('End');
    await input.press('A');
    const alignment = await page.evaluate(() => {
      const a = document.querySelector('.composer textarea');
      const b = document.querySelector('.composer-mention-overlay');
      return [a, b].map(el => { const s = getComputedStyle(el); return [s.fontFamily, s.fontSize, s.fontWeight, s.lineHeight, el.getBoundingClientRect().width]; });
    });
    if (JSON.stringify(alignment[0]) !== JSON.stringify(alignment[1])) throw new Error(`Overlay mismatch: ${JSON.stringify(alignment)}`);
    await page.screenshot({ path: `output/playwright/issue73-selected-${width}.png` });
    await input.fill('/');
    await page.getByRole('option', { name: /写作助手/ }).waitFor();
    await page.screenshot({ path: `output/playwright/issue73-slash-${width}.png` });
    await input.press('Enter');
    if (await input.inputValue() !== '使用「写作助手」 ') throw new Error('Slash insertion failed');
    if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Horizontal overflow');
  }
  const input = page.getByRole('textbox', { name: '向知识库提问' });
  duplicateNames = true;
  await page.goto(`http://localhost:3110/?chat=1&library=${kb}`);
  await input.fill('@');
  await page.getByRole('option', { name: /产品需求/ }).first().click();
  await input.press('End');
  await page.keyboard.insertText('@');
  await page.getByRole('option', { name: /产品需求/ }).click();
  await input.evaluate(el => el.setSelectionRange(0, '@产品需求与验收说明.pdf '.length));
  await input.press('Backspace');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByText('提及的文档已不可用，请重新选择', { exact: true }).waitFor();
  if (submitted.document_ids.length !== 1 || submitted.document_ids[0] !== secondDoc) throw new Error('Duplicate filename changed document identity');
  duplicateNames = false;
  await page.goto(`http://localhost:3110/?chat=1&library=${kb}`);
  await input.fill('@');
  await page.getByRole('option', { name: /产品需求/ }).click();
  await input.press('Escape');
  await input.press('End');
  await input.press('A');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByText('提及的文档已不可用，请重新选择', { exact: true }).waitFor();
  if (submitted.document_ids.length !== 1 || submitted.document_ids[0] !== doc) throw new Error('Document ID not submitted');
  await input.waitFor({ state: 'visible' });
  await page.waitForFunction(() => document.querySelector('.composer textarea')?.value.includes('@产品需求'));
  if (!(await page.locator('.composer-mention-link').count())) throw new Error('Error lost mention');
  await input.fill('new question');
  if (await page.locator('.composer-mention-link').count()) throw new Error('Deletion retained mention');
  return '1440/700/390 selection, overlay metrics, slash, keyboard, width; ID submission, error recovery and removal passed';
}
