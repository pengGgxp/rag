// Run with playwright-cli run-code --filename=scripts/check-empty-image-config.js.
async (page) => {
  const origin = new URL(page.url()).origin;
  const id = '09b8019f-36f7-4eef-9ed9-496e3c07fab7';
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const connection = { id: 'fixture-connection', name: 'Test connection', version: 1, platform: 'deepseek', enabled: true };
  const provider = { id, name: 'deepseek-flash', model: 'deepseek-flash', connection_id: connection.id,
    connection_name: connection.name, connection_version: 1, version: 1, enabled: true,
    supports_chat: true, supports_images: false, chat_protocol: 'chat_completions', api_protocol: 'chat_completions',
    image_config: {}, prices: [], available: true, is_default: false };
  const fixtures = {
    '/auth/options': { first_run: false, setup_available: false },
    '/auth/me': { user: { id: 'fixture-user', name: 'Test admin', email: 'admin@example.test', is_site_admin: true },
      workspace_id: 'fixture-workspace', workspaces: [{ id: 'fixture-workspace', name: 'Test workspace', role: 'admin' }], csrf_token: 'fixture' },
    '/site/admin/model-connections': [connection],
    ['/site/admin/llm-providers/' + id]: provider,
    '/site/admin/model-connections/image-presets': [],
    '/site/admin/model-connections/model-presets': [],
    '/site/announcement': { enabled: false },
  };
  let writes = 0;
  await page.route(url => url.pathname.startsWith('/api/') || url.origin === 'http://localhost:8000', async route => {
    const path = new URL(route.request().url()).pathname.replace(/^[/]api/, '');
    if (route.request().method() !== 'GET') { writes++; return route.fulfill({ status: 405, json: {} }); }
    return route.fulfill({ json: fixtures[path] ?? {} });
  });
  const results = [];
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto(origin + '/admin/models/' + id);
    await page.getByLabel('显示名称', { exact: true }).waitFor({ timeout: 15000 });
    if (await page.getByRole('button', { name: '保存模型', exact: true }).isEnabled()) throw new Error('Opening a legacy config made it dirty');
    await page.getByRole('tab', { name: '价格', exact: true }).click();
    await page.getByRole('tab', { name: '模型设置', exact: true }).click();
    await page.getByLabel('图片生成', { exact: true }).check();
    await page.getByRole('combobox', { name: '图片能力预设', exact: true }).waitFor();
    await page.getByLabel('图片生成', { exact: true }).uncheck();
    if (await page.getByRole('button', { name: '保存模型', exact: true }).isEnabled()) throw new Error('Toggling back changed legacy configuration');
    await page.screenshot({ path: 'output/playwright/empty-image-config-' + width + '.png', fullPage: true });
    results.push({ width, rendered: true });
  }
  if (writes || errors.length) throw new Error(JSON.stringify({ writes, errors }));
  return { results, writes, errors };
}
