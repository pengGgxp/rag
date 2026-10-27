// Run through playwright-cli run-code --filename=scripts/check-settings-navigation.js.
async (page) => {
  const base = new URL(page.url()).origin;
  await page.unrouteAll({ behavior: 'wait' });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const fixtures = {
    '/auth/options': { oauth_name: null, password_reset: false, setup_available: false, first_run: false },
    '/auth/me': { user: { id: 'u1', name: 'Test Admin', email: 'admin@example.com', has_password: true, oauth_linked: false }, workspaces: [{ id: 'w1', name: 'Test Workspace', role: 'admin' }], workspace_id: 'w1', csrf_token: 'test' },
    '/health': { status: 'ok', database: 'ok', redis: 'ok' },
    '/knowledge-bases': [], '/conversations': [], '/agent/skills': [], '/agent/mcp': [],
    '/ingestion/options': { third_party_processor: null, max_pdf_pages: null },
    '/usage': { balance: 1200, lifetime_spent: 80, lifetime_granted: 1280, entries: [] },
    '/auth/members': [], '/audit-logs': { items: [], total: 0, page: 1, page_size: 10 },
    '/usage/policies': [], '/settings/llm-providers': [],
    '/payments/catalog': { enabled: false, packages: [] },
    '/payments/orders': { items: [], total: 0, page: 1, page_size: 10 },
  };
  await page.route(url => url.origin === 'http://localhost:8000' || (url.origin === base && url.pathname.startsWith('/api/')), async route => {
    const path = new URL(route.request().url()).pathname.replace(/^\/api/, '');
    if (!(path in fixtures)) throw new Error(`Missing API fixture: ${path}`);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(fixtures[path]) });
  });
  const check = (value, message) => { if (!value) throw new Error(message); };
  const results = [];
  const sections = [ ['workspace', '工作区概览'], ['billing', '积分与充值'], ['policies', '计费规则'], ['members', '工作区成员'], ['audit', '审计日志'], ['', '我的账号'] ];
  const setting = async (section, name) => {
    await page.getByRole('link', { name, exact: true }).click();
    await page.waitForURL(url => url.pathname === `/settings${section ? `/${section}` : ''}`);
    await page.getByRole('heading', { name, level: 1, exact: true }).waitFor();
    await page.waitForFunction(() => document.querySelector('.modal-fullscreen > div')?.style.opacity === '1');
    await page.waitForFunction(() => document.querySelectorAll('.modal-fullscreen').length === 1);
    check(await page.locator('.modal-fullscreen').count() === 1, `Duplicate settings layer at ${section}`);
  };
  const open = async () => {
    const nav = page.getByRole('button', { name: '打开导航', exact: true });
    if (!await page.locator('[data-account-trigger]').isVisible()) await nav.click();
    await page.locator('[data-account-trigger]').click();
    await page.getByRole('menuitem', { name: '工作区设置', exact: true }).click();
    await page.getByRole('heading', { name: '工作区概览', level: 1, exact: true }).waitFor();
  };
  const closed = async path => {
    await page.waitForURL(url => url.pathname + url.search === path);
    await page.locator('.modal-fullscreen').waitFor({ state: 'detached' });
  };
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(base);
  const search = page.getByRole('textbox', { name: '搜索知识库', exact: true });
  await search.fill('preserved search');
  await open();
  for (const [section, name] of sections) await setting(section, name);
  await page.screenshot({ path: 'output/playwright/settings-desktop.png' });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await closed('/');
  check(await search.inputValue() === 'preserved search', 'Knowledge search state lost');
  results.push('desktop: six sections, one close, preserved knowledge search');
  await page.goto(`${base}/?chat=1`);
  const draft = page.getByRole('textbox', { name: '向知识库提问', exact: true });
  await draft.fill('preserved draft');
  await open();
  await page.locator('.workspace-settings-links').getByRole('button', { name: '工作区成员', exact: true }).click();
  await page.getByRole('heading', { name: '工作区成员', level: 1, exact: true }).waitFor();
  await setting('billing', '积分与充值');
  await page.keyboard.press('Escape');
  await closed('/?chat=1');
  check(await draft.inputValue() === 'preserved draft', 'Chat draft lost');
  results.push('chat: overview button and sidebar, Escape, preserved draft');
  await page.goto(`${base}/?extensions=1`);
  const extensionSearch = page.getByRole('textbox', { name: '搜索扩展', exact: true });
  await extensionSearch.fill('preserved extension');
  await open();
  await setting('billing', '积分与充值');
  await setting('members', '工作区成员');
  await page.goBack();
  await closed('/?extensions=1');
  check(await extensionSearch.inputValue() === 'preserved extension', 'Extension search lost');
  await page.goForward();
  await page.getByRole('heading', { name: '工作区成员', level: 1, exact: true }).waitFor();
  check(await page.locator('.modal-fullscreen').count() === 1, 'Forward duplicated layer');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await closed('/?extensions=1');
  results.push('Agent: back closes, forward reopens final section, close restores Agent');
  for (const [section, name] of sections) {
    await page.goto(`${base}/settings${section ? `/${section}` : ''}?returnTo=%2F%3Fchat%3D1`);
    await page.getByRole('heading', { name, level: 1, exact: true }).waitFor();
    await page.reload();
    await page.getByRole('heading', { name, level: 1, exact: true }).waitFor();
    await page.waitForLoadState('networkidle');
    check(await page.locator('.modal-fullscreen').count() === 1, `Direct settings duplicated at ${section}`);
    await setting('billing', '积分与充值');
    await setting('audit', '审计日志');
    await page.getByRole('button', { name: '关闭', exact: true }).click();
    await closed('/?chat=1');
  }
  results.push('direct access: all six routes, reload, internal navigation and returnTo close');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(base);
  await open();
  check(await page.getByRole('dialog').count() === 1, 'Mobile settings stacked above drawer');
  for (const [section, name] of sections) await setting(section, name);
  check(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'Mobile horizontal overflow');
  await page.screenshot({ path: 'output/playwright/settings-mobile.png' });
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await closed('/');
  await page.waitForFunction(() => document.querySelector('[data-workspace-menu]') === document.activeElement);
  results.push('mobile 390px: six sections, one close, no horizontal overflow');
  check(errors.length === 0, `Browser exceptions: ${errors.join('; ')}`);
  return { passed: results, pageErrors: errors };
}
