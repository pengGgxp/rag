// Browser interaction regression with mocked delivery/provider responses; not live SMTP/WeChat acceptance.
async (page) => {
  await page.unrouteAll({ behavior: 'wait' });
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  let session = null;
  let flow = null;
  let saved = null;
  const options = { oauth_name: null, password_reset: true, email_code: true, wechat: true, require_verified_email: true, setup_available: false, first_run: false };
  const admin = { user: { id: 'u1', name: 'Test Admin', email: 'admin@example.com', has_password: true, oauth_linked: false, email_verified: true, wechat_linked: false, is_site_admin: true }, workspaces: [{ id: 'w1', name: 'Test Workspace', role: 'admin' }], workspace_id: 'w1', csrf_token: 'test' };
  const config = { smtp: { enabled: true, host: 'smtp.example.com', port: 587, tls_mode: 'starttls', username: 'mailer', from_address: 'mail@example.com', from_name: '知库', has_password: true }, wechat: { enabled: true, app_id: 'test-app', has_app_key: true, callback_url: 'https://api.example/auth/wechat/callback/<state>' }, policy: { require_verified_email: false, hourly_email_limit: 100 }, encryption_ready: true };
  const fixtures = {
    '/health': { status: 'ok', database: 'ok', redis: 'ok' }, '/knowledge-bases': [], '/conversations': [], '/agent/skills': [], '/agent/mcp': [],
    '/ingestion/options': { third_party_processor: null, max_pdf_pages: null }, '/usage': { balance: 1000, lifetime_spent: 0, lifetime_granted: 1000, entries: [] },
    '/auth/members': [], '/audit-logs': { items: [], total: 0, page: 1, page_size: 10 }, '/usage/policies': [], '/settings/llm-providers': [], '/payments/catalog': { enabled: false, packages: [] }, '/payments/orders': { items: [], total: 0, page: 1, page_size: 10 },
  };
  await page.route('http://localhost:8000/**', async route => {
    const req = route.request();
    const path = new URL(req.url()).pathname;
    const data = req.method() === 'POST' || req.method() === 'PUT' ? req.postDataJSON() : {};
    let result, status = 200;
    if (path === '/auth/options') result = options;
    else if (path === '/auth/me') { result = session || { detail: '请先登录' }; status = session ? 200 : 401; }
    else if (path === '/auth/flow') result = { flow };
    else if (path === '/auth/email/send-code') {
      flow = { purpose: data.purpose, stage: 'code', email: data.email || admin.user.email, retry_after: 60, return_to: data.return_to || '/' };
      result = { status: 'pending', flow };
    } else if (path === '/auth/email/verify') {
      if (data.code !== '123456') { status = 400; result = { detail: '验证码不正确' }; }
      else if (flow.purpose === 'register') { flow = { ...flow, stage: 'profile', retry_after: 0 }; result = { status: 'pending', flow }; }
      else { flow = null; session = admin; result = session; }
    } else if (path === '/auth/complete-registration') { flow = null; session = { ...admin, user: { ...admin.user, name: data.name } }; result = session; status = 201; }
    else if (path === '/auth/login') { flow = { purpose: 'verify', stage: 'email', email: data.email, retry_after: 0, return_to: '/' }; result = { status: 'pending', flow }; }
    else if (path === '/auth/logout') { session = null; result = null; status = 204; }
    else if (path === '/site/settings/auth') { if (req.method() === 'PUT') saved = data; result = config; }
    else if (path === '/site/settings/auth/test-email') result = { message: 'SMTP 服务已接受邮件，请检查收件箱及垃圾邮件' };
    else if (path in fixtures) result = fixtures[path];
    else throw new Error(`Missing API fixture ${req.method()} ${path}`);
    await route.fulfill({ status, contentType: 'application/json', ...(status === 204 ? {} : { body: JSON.stringify(result) }) });
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('http://localhost:3000/auth/login');
  await page.getByRole('tab', { name: '邮箱验证码', exact: true }).waitFor();
  await page.screenshot({ path: 'output/playwright/auth-login-desktop.png' });
  await page.getByRole('link', { name: '创建账号', exact: true }).click();
  await page.getByLabel('邮箱', { exact: true }).fill('new@example.com');
  await page.getByRole('button', { name: '发送验证码', exact: true }).click();
  await page.waitForURL(/\/auth\/verify-email/);
  await page.reload();
  check(await page.getByLabel('邮箱', { exact: true }).inputValue() === 'new@example.com', 'Email lost on refresh');
  await page.getByLabel('邮箱验证码', { exact: true }).fill('654321');
  await page.getByRole('button', { name: '验证并继续', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '验证码不正确' }).waitFor();
  check(await page.getByLabel('邮箱', { exact: true }).inputValue() === 'new@example.com', 'Email lost after invalid code');
  await page.getByLabel('邮箱验证码', { exact: true }).fill('123456');
  await page.getByRole('button', { name: '验证并继续', exact: true }).click();
  await page.getByRole('heading', { name: '完成账号设置', exact: true }).waitFor();
  await page.reload();
  await page.getByLabel('姓名', { exact: true }).fill('Browser Test');
  await page.getByLabel('密码', { exact: true }).fill('browser-test-password');
  await page.getByRole('button', { name: '完成注册', exact: true }).click();
  await page.waitForURL('http://localhost:3000/');
  await page.locator('[data-account-trigger]').click();
  await page.getByRole('menuitem', { name: '我的账号', exact: true }).click();
  await page.getByRole('heading', { name: '我的账号', level: 1, exact: true }).waitFor();
  check(await page.locator('.modal-fullscreen').count() === 1, 'Settings duplicated');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.locator('[data-account-trigger]').click();
  await page.getByRole('menuitem', { name: '站点管理', exact: true }).click();
  await page.getByRole('heading', { name: '认证与登录', exact: true }).waitFor();
  check(await page.locator('.modal-fullscreen').count() === 0, 'Site console nested in settings');
  await page.getByRole('tab', { name: '微信登录', exact: true }).click();
  await page.getByLabel('森遇应用 ID', { exact: true }).fill('replacement-app');
  await page.getByLabel('AppKey（已配置，留空保留）', { exact: true }).fill('mock-secret-never-stored');
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  await page.getByRole('status').filter({ hasText: '设置已保存' }).waitFor();
  check(saved?.wechat?.app_key === 'mock-secret-never-stored', 'Secret save payload missing');
  check(await page.getByLabel('AppKey（已配置，留空保留）', { exact: true }).inputValue() === '', 'Secret remains in input after save');
  await page.getByRole('tab', { name: '认证策略', exact: true }).click();
  await page.getByLabel('站点每小时发信上限', { exact: true }).fill('120');
  await page.getByRole('button', { name: '保存设置', exact: true }).click();
  await page.getByRole('tab', { name: '邮箱服务', exact: true }).click();
  await page.getByRole('button', { name: '发送测试邮件', exact: true }).click();
  await page.getByRole('status').filter({ hasText: 'SMTP 服务已接受' }).waitFor();
  await page.screenshot({ path: 'output/playwright/auth-admin-desktop.png' });
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.reload();
    await page.getByRole('heading', { name: '认证与登录' }).waitFor();
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Admin overflow at ${width}`);
    if (width === 390) await page.screenshot({ path: 'output/playwright/auth-admin-mobile.png' });
  }
  session = null;
  await page.goto('http://localhost:3000/auth/login');
  await page.getByRole('tab', { name: '密码登录', exact: true }).click();
  await page.getByLabel('邮箱', { exact: true }).fill('legacy@example.com');
  await page.getByLabel('密码', { exact: true }).fill('legacy-test-password');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByRole('heading', { name: '验证你的邮箱' }).waitFor();
  check(await page.getByLabel('邮箱', { exact: true }).inputValue() === 'legacy@example.com', 'Password pending result treated as session');
  await page.screenshot({ path: 'output/playwright/auth-verify-mobile.png' });
  await page.getByRole('link', { name: '返回登录', exact: true }).click();
  await page.getByRole('heading', { name: '登录知库', exact: true }).waitFor();
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.getByLabel('邮箱', { exact: true }).focus();
  await page.keyboard.press('Tab');
  check(await page.evaluate(() => document.activeElement?.tagName !== 'BODY'), 'Keyboard focus missing');
  await page.screenshot({ path: 'output/playwright/auth-login-mobile.png' });
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Mobile login overflow');
  check(failures.length === 0, failures.join('\n'));
  return { passed: ['registration, invalid code, refresh recovery, profile completion', 'account settings single layer, site console independent', 'three admin tabs, preserve/replace secret, test mail response', 'legacy password pending flow, desktop/mobile, keyboard and reduced motion'], realProviderAcceptance: 'pending SMTP credentials and human WeChat scan' };
}
