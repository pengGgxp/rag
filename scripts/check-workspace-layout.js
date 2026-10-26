// Run with playwright-cli run-code --filename=scripts/check-workspace-layout.js.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: 'wait' });
  const errors = [];
  const onError = error => errors.push(error.message);
  page.on('pageerror', onError);
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const library = { id: 'kb-layout', name: '产品资料库', description: '团队产品资料与使用说明', tags: [], status: 'ready', document_count: 1 };
  const citation = { chunk_id: 'chunk-layout', filename: '产品说明.md', text: '知库支持混合检索和可追溯引用。', char_start: 0, char_end: 20, page_number: 1, version_number: 1, knowledge_base_id: library.id, knowledge_base_name: library.name };
  const answer = { id: 'answer', sequence: 1, role: 'assistant', content: '知库支持混合检索和可追溯引用。[chunk-layout]', status: 'completed', citations: [citation], traces: [], refused: false };
  const first = { id: 'conversation-layout-1', title: '产品使用说明', knowledge_bases: [{ id: library.id, name: library.name }], active_request_id: null, updated_at: new Date().toISOString(), messages: [answer], skill_ids: [], mcp_server_ids: [] };
  const second = { ...first, id: 'conversation-layout-2', title: '第二个历史对话', messages: [] };
  const created = { ...second, id: 'conversation-layout-new', title: '新建的真实交互测试' };
  const fixtures = {
    '/auth/options': { oauth_name: null, password_reset: true, setup_available: false, first_run: false },
    '/auth/me': { user: { id: 'u1', name: '布局测试用户', email: 'layout@example.com', has_password: true, email_verified: true, oauth_linked: false }, workspaces: [{ id: 'w1', name: '测试工作区', role: 'admin' }], workspace_id: 'w1', csrf_token: 'test' },
    '/knowledge-bases': [library], '/health': { embedding: { model: 'test', dimension: 1024 }, llm: { model: 'test' } },
    '/ingestion/options': { third_party_processor: null, max_pdf_pages: null },
    '/conversations': [first, second],
    [`/conversations/${first.id}`]: first, [`/conversations/${second.id}`]: second, [`/conversations/${created.id}`]: created,
    [`/knowledge-bases/${library.id}/documents`]: [{ id: 'doc-layout', filename: '产品说明.md', status: 'ready', size_bytes: 1024, version_number: 1, is_active: true, created_at: new Date().toISOString() }],
    '/agent/skills': [{ id: 'skill-layout', name: '资料整理', description: '整理引用', enabled: true }], '/agent/mcp': [],
    '/usage': { balance: 1200, lifetime_spent: 80, lifetime_granted: 1280, entries: [] },
    '/usage/policies': [], '/settings/llm-providers': [], '/auth/members': [],
    '/audit-logs': { items: [], total: 0, page: 1, page_size: 10 },
    '/payments/catalog': { enabled: false, packages: [] }, '/payments/orders': { items: [], total: 0, page: 1, page_size: 10 },
  };
  let libraryFailure = false;
  let delay = false;
  let stopCalls = 0;
  let slowStream = false;
  await page.route(url => url.origin === 'http://localhost:8000' || (url.origin === base && url.pathname.startsWith('/api/')), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, '');
    if (delay && ['/knowledge-bases', `/conversations/${first.id}`, '/agent/skills', '/agent/mcp'].includes(path)) await new Promise(resolve => setTimeout(resolve, 600));
    if (path === '/knowledge-bases' && libraryFailure) return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ detail: '测试：暂时无法读取资料' }) });
    if (path.endsWith('/stop')) { stopCalls++; return route.fulfill({ status: 204 }); }
    if (path === '/conversations' && route.request().method() === 'POST') return route.fulfill({ json: created });
    if (path.endsWith('/messages')) {
      if (slowStream) await new Promise(resolve => setTimeout(resolve, 2000));
      const data = route.request().postDataJSON();
      const completed = { ...answer, request_id: data.request_id };
      created.messages = [completed];
      return route.fulfill({ contentType: 'text/event-stream', body: `event: message\ndata: ${JSON.stringify({ ...completed, content: '', status: 'streaming' })}\n\nevent: delta\ndata: ${JSON.stringify({ text: completed.content })}\n\nevent: done\ndata: ${JSON.stringify(completed)}\n\n` });
    }
    if (path === `/agent/skills/skill-layout`) return route.fulfill({ json: { ...fixtures['/agent/skills'][0], markdown: '---\nname: 资料整理\ndescription: 整理引用\n---\n整理资料。', files: [] } });
    check(path in fixtures, `Missing fixture: ${path}`);
    await route.fulfill({ json: fixtures[path] });
  });
  const results = [];
  const nav = async label => {
    if (!await page.locator('.primary-navigation').isVisible()) await page.getByRole('button', { name: '打开导航', exact: true }).click();
    await page.locator('.primary-navigation').getByRole('link', { name: label, exact: true }).click();
  };
  const header = async title => {
    await page.locator('.workspace-header h1').filter({ hasText: title }).waitFor();
    check(await page.locator('.workspace-header').count() === 1, 'Duplicate header');
    check(await page.locator('.workspace').count() === 1, 'Duplicate shell');
    check(await page.locator('.workspace h1').count() === 1, 'Duplicate page title');
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal overflow');
  };
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(base);
  await header('知识库');
  const sidebar = await page.locator('.workspace > .sidebar').elementHandle();
  const geometry = [];
  for (const [label, title] of [['知识库', '知识库'], ['知识问答', '新对话'], ['Agent 扩展', 'Agent 扩展']]) {
    await nav(label); await header(title);
    geometry.push(await page.evaluate(() => {
      const rect = selector => { const r = document.querySelector(selector).getBoundingClientRect(); return [r.x, r.y, r.width, r.height]; };
      return { header: rect('.workspace-header'), title: rect('.workspace-header h1'), account: rect('[data-account-trigger]') };
    }));
    check(await sidebar.evaluate(node => node.isConnected), 'Sidebar remounted during mode switch');
    await page.screenshot({ path: `output/playwright/issue34-${label}-desktop.png` });
  }
  check(geometry.every(item => item.header[0] === 232 && item.header[3] === 60 && item.title[1] === geometry[0].title[1] && JSON.stringify(item.account) === JSON.stringify(geometry[0].account)), 'Inconsistent desktop geometry');
  results.push('desktop: stable sidebar DOM, identical 60px header/title baseline/account geometry');
  await page.goBack(); await header('新对话');
  await page.goForward(); await header('Agent 扩展');
  await page.reload(); await header('Agent 扩展');
  await page.getByRole('tab', { name: /Skills/ }).click();
  await page.getByRole('button', { name: '编辑 资料整理', exact: true }).click();
  await page.getByRole('dialog').waitFor(); await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('tab', { name: /MCP/ }).click();
  await page.getByRole('button', { name: '添加 MCP', exact: true }).click();
  await page.getByRole('dialog').waitFor(); await page.getByRole('button', { name: '关闭', exact: true }).click();
  results.push('mode history, refresh, MCP dialog and Skill editor');
  await nav('知识库');
  await page.getByRole('button', { name: /产品资料库/ }).click();
  await header(library.name);
  for (const name of ['检索测试', '配置', '文档']) {
    await page.getByRole('navigation', { name: '知识库管理', exact: true }).getByRole('button', { name, exact: name !== '文档' }).click();
    await page.waitForURL(url => url.searchParams.get('view') === ({ '检索测试': 'retrieval', '配置': 'settings', '文档': 'documents' }[name]));
    await header(library.name);
  }
  await page.reload(); await header(library.name);
  await page.getByRole('button', { name: '上传文档', exact: true }).click();
  await page.getByRole('dialog').waitFor(); await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: '前往问答', exact: true }).click();
  await header('新对话');
  await page.getByRole('button', { name: first.title, exact: true }).click(); await header(first.title);
  await page.getByRole('button', { name: second.title, exact: true }).click(); await header(second.title);
  await page.goBack(); await header(first.title);
  await page.goForward(); await header(second.title);
  await page.goBack(); await header(first.title);
  await page.reload(); await header(first.title);
  await page.getByRole('button', { name: `查看来源：${citation.filename}`, exact: true }).click();
  await page.getByRole('dialog').waitFor(); await page.getByRole('button', { name: '关闭', exact: true }).click();
  results.push('library sections/upload, conversation history/back/forward/refresh, citation dialog');
  const openSettings = async () => {
    await page.locator('[data-account-trigger]').click();
    await page.getByRole('menuitem', { name: '工作区设置', exact: true }).click();
    await page.getByRole('heading', { name: '工作区概览', level: 1, exact: true }).waitFor();
    await page.getByRole('link', { name: '积分与充值', exact: true }).click();
    await page.getByRole('heading', { name: '积分与充值', level: 1, exact: true }).waitFor();
    check(await page.locator('.modal-fullscreen').count() === 1, 'Multiple settings layers');
  };
  const draft = page.getByRole('textbox', { name: '向知识库提问', exact: true });
  await draft.fill('设置关闭后保留的输入');
  await openSettings();
  await page.keyboard.press('Escape'); await header(first.title);
  check(await draft.inputValue() === '设置关闭后保留的输入', 'Settings reset chat draft');
  await page.goForward(); await page.getByRole('heading', { name: '积分与充值', level: 1, exact: true }).waitFor();
  await page.getByRole('button', { name: '关闭', exact: true }).click(); await header(first.title);
  check(await draft.inputValue() === '设置关闭后保留的输入', 'Settings forward/close reset chat');
  results.push('single settings layer, Escape/back/forward, preserved chat and draft');
  await page.getByRole('button', { name: '新建对话', exact: true }).click(); await header('新对话');
  await draft.fill('请介绍知库'); await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByText('知库支持混合检索和可追溯引用。', { exact: false }).first().waitFor();
  await page.waitForURL(url => url.searchParams.get('conversation') === created.id);
  await header(created.title);
  check(stopCalls === 0, 'Creating conversation URL stopped generation');
  results.push('new conversation URL replacement preserves SSE and citations');
  await page.getByRole('button', { name: '新建对话', exact: true }).click(); await header('新对话');
  slowStream = true;
  await draft.fill('离开时停止生成'); await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByRole('button', { name: '停止回答', exact: true }).waitFor();
  await nav('Agent 扩展'); await header('Agent 扩展');
  await page.waitForTimeout(150);
  check(stopCalls === 1, 'Leaving chat did not stop generation');
  slowStream = false;
  results.push('leaving chat aborts the stream and calls stop');
  await nav('知识问答'); await header('新对话');
  slowStream = true;
  await draft.fill('手动停止生成'); await page.getByRole('button', { name: '发送问题', exact: true }).click();
  await page.getByRole('button', { name: '停止回答', exact: true }).click();
  await page.getByRole('button', { name: '发送问题', exact: true }).waitFor();
  check(stopCalls === 2, 'Stop button did not call stop');
  await nav('Agent 扩展'); await header('Agent 扩展');
  slowStream = false;
  results.push('explicit stop button completes without leaving the conversation');
  library.name = '超长知识库名称用于验证页面标题不会挤出右侧操作区域'.repeat(4);
  first.title = '超长对话标题用于验证导出和重命名操作始终可见'.repeat(4);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: width === 390 ? 844 : 900 });
    for (const [query, title] of [['', '知识库'], ['?library=kb-layout&view=documents', library.name], [`?conversation=${first.id}`, first.title], ['?extensions=1', 'Agent 扩展']]) {
      await page.goto(`${base}/${query}`); await header(title);
      const box = await page.locator('.workspace-header').boundingBox();
      check(box.height === (width === 390 ? 56 : 60), `Header height ${box.height}`);
      if (query.includes('library=') || query.includes('conversation=')) {
        const actions = await page.locator('.workspace-header-actions').boundingBox();
        check(actions.x + actions.width <= width, 'Header actions overflow');
      }
      if (query.includes('library=')) {
        const row = page.locator('.document-table tbody tr').first();
        await row.locator('.row-actions').waitFor();
        const status = await row.locator('.status-badge').boundingBox();
        const actions = await row.locator('.row-actions').boundingBox();
        check(actions.y >= status.y + status.height || actions.x >= status.x + status.width, 'Document actions overlap status');
      }
      await page.waitForTimeout(250);
      await page.screenshot({ path: `output/playwright/issue34-${width}-${query ? query.includes('library') ? 'library' : query.includes('conversation') ? 'chat' : 'extensions' : 'catalog'}.png` });
      await page.reload(); await header(title);
    }
  }
  for (const width of [701, 1100]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(`${base}/?library=kb-layout&view=documents`); await header(library.name);
    check((await page.locator('.workspace > .sidebar').boundingBox()).width === 218, 'Intermediate sidebar width');
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`${base}/?extensions=1`); await header('Agent 扩展');
  await page.getByRole('button', { name: '打开导航', exact: true }).click();
  const dialog = page.getByRole('dialog'); await dialog.waitFor();
  const drawer = await dialog.boundingBox(); check(drawer.x === 0 && drawer.y === 0 && drawer.width === 260, 'Drawer geometry');
  for (let i = 0; i < 12; i++) {
    await page.keyboard.press('Tab');
    check(await dialog.evaluate(node => node.contains(document.activeElement)), 'Focus escaped drawer');
  }
  await page.screenshot({ path: 'output/playwright/issue34-drawer-mobile.png' });
  await page.keyboard.press('Escape'); await dialog.waitFor({ state: 'detached' });
  await page.waitForFunction(() => document.querySelector('[data-workspace-menu]') === document.activeElement);
  await nav('知识问答'); await header('新对话');
  await page.emulateMedia({ reducedMotion: 'reduce' });
  check(await page.locator('.workspace-header').evaluate(node => getComputedStyle(node).transitionDuration === '0s'), 'Reduced motion ignored');
  results.push('1440/390px: long titles, actions, refresh, no overflow; drawer focus trap/Escape/restore; reduced motion');
  libraryFailure = true;
  await page.goto(base); await header('知识库');
  await page.getByRole('alert').filter({ hasText: '测试：暂时无法读取资料' }).first().waitFor();
  await page.getByRole('textbox', { name: '搜索知识库', exact: true }).fill('保留搜索');
  libraryFailure = false;
  await page.getByRole('button', { name: '重试', exact: true }).click();
  await page.getByText('全部知识库', { exact: false }).waitFor();
  check(await page.getByRole('textbox', { name: '搜索知识库', exact: true }).inputValue() === '保留搜索', 'Retry cleared search');
  delay = true;
  for (const query of ['', `?conversation=${first.id}`, '?extensions=1']) {
    await page.goto(`${base}/${query}`);
    await page.locator('.workspace-skeleton').first().waitFor();
    check(await page.locator('.workspace-header').isVisible(), 'Loading hid header');
    await page.locator('.workspace-skeleton').first().waitFor({ state: 'detached' });
  }
  results.push('local loading skeletons retain header; API error retry preserves input');
  await page.goto(`${base}/?library=kb-layout&view=unknown`);
  await page.waitForURL(url => url.searchParams.get('view') === 'documents');
  await header(library.name);
  results.push('invalid library section corrected with replace');
  page.off('pageerror', onError);
  check(errors.length === 0, `Browser exceptions: ${errors.join('; ')}`);
  return { passed: results, geometry, pageErrors: errors, api: 'mock fixtures; no real model or payment calls' };
}
