// Run through playwright-cli run-code --filename after signing into a local site-admin account.
async (page) => {
  const base = 'http://localhost:3000';
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  const assert = (condition, message) => { if (!condition) throw new Error(message); };
  const nav = page.getByRole('navigation', { name: '后台导航' });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto(base + '/admin/users');
  await page.getByRole('textbox', { name: '搜索用户' }).fill('siteadmin-acceptance@example.com');
  await page.getByRole('button', { name: '查询', exact: true }).click();
  await page.waitForURL(/q=siteadmin/);
  await page.getByRole('link', { name: 'Site Admin Acceptance', exact: true }).waitFor();
  await page.reload();
  assert(await page.getByRole('textbox', { name: '搜索用户' }).inputValue() === 'siteadmin-acceptance@example.com', 'Search not restored');
  await page.getByRole('link', { name: 'Site Admin Acceptance', exact: true }).click();
  await page.getByRole('heading', { name: 'Site Admin Acceptance', exact: true }).waitFor();
  await page.getByRole('button', { name: '重发验证邮件', exact: true }).click();
  await page.getByRole('dialog').waitFor();
  await page.getByRole('button', { name: '确认', exact: true }).click();
  await page.getByRole('alert').filter({ hasText: '站点邮件服务尚未启用' }).waitFor();
  await page.keyboard.press('Escape');
  assert(await page.getByRole('dialog').count() === 0, 'Dialog remained open');
  await nav.getByRole('link', { name: '工作区', exact: true }).click();
  await page.locator('tbody a').first().waitFor();
  await page.locator('tbody a').first().click();
  await page.getByRole('heading', { name: '积分摘要', exact: true }).waitFor();
  await page.reload();
  await page.getByRole('heading', { name: '积分摘要', exact: true }).waitFor();
  await nav.getByRole('link', { name: '站点审计', exact: true }).click();
  await page.getByRole('heading', { name: '站点审计', exact: true }).waitFor();
  await page.locator('tbody tr').first().waitFor();
  await page.screenshot({ path: 'output/playwright/site-admin-audit-desktop.png' });
  await nav.getByRole('link', { name: '认证与登录', exact: true }).click();
  await page.getByRole('tab', { name: '微信登录', exact: true }).click();
  await page.getByRole('link', { name: '打开森遇聚合登录平台', exact: true }).waitFor();
  await page.goBack(); await page.getByRole('heading', { name: '站点审计', exact: true }).waitFor();
  await page.goForward(); await page.getByRole('heading', { name: '认证与登录', exact: true }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  for (const path of ['/admin', '/admin/users', '/admin/workspaces', '/admin/audit', '/admin/auth']) {
    await page.goto(base + path);
    await nav.waitFor(); await page.locator('.admin-main h1').waitFor();
    await page.locator('.admin-skeleton').waitFor({ state: 'detached' });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'Horizontal page overflow: ' + path);
    await page.screenshot({ path: 'output/playwright/site-admin-' + (path.split('/')[2] || 'overview') + '-mobile.png', fullPage: true });
  }
  await page.keyboard.press('Tab');
  assert(await page.evaluate(() => document.activeElement !== document.body), 'Keyboard focus missing');
  assert(errors.length === 0, errors.join('; '));
  console.log('Site admin desktop/mobile navigation, refresh, search, errors, keyboard and reduced-motion checks passed.');
}
