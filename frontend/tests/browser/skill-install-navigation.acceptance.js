// 使用已登录临时 owner 的 playwright-cli run-code --filename 执行。
// 仅本地隔离预览；真实安装和详情响应，延迟不修改响应内容。只清理本轮夹具。
async (page) => {
  const assert = (ok, message) => { if (!ok) throw Error(message); };
  const web = 'http://localhost:3000';
  const api = 'http://localhost:8000';
  assert(new URL(page.url()).origin === web, '仅允许本地隔离预览');
  const session = await (await page.request.get(api + '/auth/me')).json();
  assert(session.user.email === 'issue100-local@example.com', '仅允许临时验收 owner');
  const headers = { 'X-RAG-Request': '1', 'X-CSRF-Token': session.csrf_token };
  // 预先在隔离数据库创建此一次性源包维护账号；不提升 owner 的站点权限。
  const admin = await page.context().browser().newContext();
  const login = await admin.request.post(api + '/auth/login', { headers: { 'X-RAG-Request': '1' }, data: { email: 'issue100-navigation-admin@example.com', password: 'local-navigation-fixture-100' } });
  assert(login.ok(), '临时源包管理员登录失败');
  const adminSession = await (await admin.request.get(api + '/auth/me')).json();
  const adminHeaders = { 'X-RAG-Request': '1', 'X-CSRF-Token': adminSession.csrf_token };
  const name = `导航回归-${Date.now()}`;
  const draft = { name, description: '仅本轮导航验收', markdown: '# 导航回归\n\n仅验证安装后的导航。', files: [], enabled: true };
  const created = await admin.request.post(api + '/site/admin/skills', { headers: adminHeaders, data: draft });
  assert(created.ok(), `创建临时源包失败：${created.status()}`);
  const pack = await created.json();
  const detail = api + '/agent/skills/platform/' + pack.id;
  const install = detail + '/install';
  const url = `${web}/extensions/skills/${pack.id}?platform=1`;
  const button = label => page.getByRole('button', { name: label, exact: true });
  const link = label => page.getByRole('link', { name: label, exact: true });
  const assertNoUnderline = async label => {
    const decoration = await link(label).evaluate(element => getComputedStyle(element).textDecorationLine);
    assert(decoration === 'none', `${label} 不应显示下划线`);
  };
  const results = [];
  let delayedReads = 0;
  const delayedDetail = async route => {
    const actual = await route.fetch();
    delayedReads++;
    await new Promise(resolve => setTimeout(resolve, 900));
    await route.fulfill({ response: actual });
  };
  async function cleanInstall() {
    const response = await page.request.get(api + '/agent/skills');
    assert(response.ok(), '读取临时安装失败');
    const paths = (await response.json()).filter(skill => skill.name === name && skill.directory === `.skills/${name}`).map(skill => skill.directory);
    if (!paths.length) return;
    const preview = await page.request.post(api + '/resources/operations/delete-preview', { headers, data: { paths } });
    assert(preview.ok(), '临时目录删除预检失败');
    const { confirmation } = await preview.json();
    const deleted = await page.request.post(api + '/resources/operations/delete', { headers, data: { paths, confirmation, request_id: crypto.randomUUID() } });
    assert(deleted.ok(), '临时目录清理失败');
  }
  try {
    await page.goto(`${web}/extensions/skills/new?returnTo=%2Fextensions`);
    await link('返回 Skill 仓库').waitFor();
    await assertNoUnderline('返回 Skill 仓库');
    await assertNoUnderline('在工作区查看');
    results.push('添加 Skill 页面两个导航链接均无下划线');

    for (const [width, label, target] of [[1440, '返回 Skill 仓库', '/extensions'], [700, '在工作区查看', '/resources'], [390, '在工作区查看', '/resources']]) {
      await cleanInstall();
      await page.setViewportSize({ width, height: 900 });
      await page.goto(url);
      await button('安装到当前工作区').waitFor();
      // GET 如仍存在就延迟真实响应；去掉多余 GET 后，首次导航也必须成功。
      await page.route(detail, delayedDetail);
      await button('安装到当前工作区').click();
      await page.getByRole('status').filter({ hasText: `已安装到 .skills/${name}` }).waitFor();
      await link(label).click();
      await page.waitForURL(current => current.pathname === target, { timeout: 3000 });
      if (target === '/resources') {
        assert(new URL(page.url()).searchParams.get('path') === `.skills/${name}`, '必须定位真实安装目录');
        await page.getByRole('option', { name: 'SKILL.md', exact: true }).waitFor();
      } else await button(name).waitFor();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width}px 整页溢出`);
      await page.screenshot({ path: `output/playwright/issue100-install-navigation/${width}-first-click.png`, fullPage: true });
      results.push(`${width}px ${label}：首次点击成功`);
      await page.unroute(detail, delayedDetail);
      await page.goto(url);
      await link('在工作区查看').waitFor();
      await page.screenshot({ path: `output/playwright/issue100-install-navigation/${width}-installed-detail.png`, fullPage: true });
    }

    await cleanInstall();
    await page.goto(url);
    await button('安装到当前工作区').waitFor();
    let finishWrite;
    let wrote;
    const writeFinished = new Promise(resolve => { wrote = resolve; });
    const hold = new Promise(resolve => { finishWrite = resolve; });
    const pendingInstall = async route => {
      const actual = await route.fetch();
      wrote();
      await hold;
      await route.fulfill({ response: actual });
    };
    await page.route(install, pendingInstall);
    try {
      await button('安装到当前工作区').click();
      await writeFinished;
      assert(await button('安装到当前工作区').isDisabled(), '写入响应未完成时禁重复安装');
      await link('返回 Skill 仓库').click();
      assert(new URL(page.url()).pathname.endsWith(pack.id), '写入中仍须保护导航');
    } finally { finishWrite(); }
    await page.getByRole('status').filter({ hasText: '已安装到 .skills/' }).waitFor();
    await page.unroute(install, pendingInstall);
    results.push('真实安装响应未完成时仍保护导航及重复提交');

    // 安装后的重复请求返回真实 existing 路径，同样应解除保护。
    await cleanInstall();
    await page.goto(url);
    await button('安装到当前工作区').waitFor();
    const installed = await page.request.post(install, { headers });
    assert(installed.ok(), '准备真实已安装分支失败');
    await button('安装到当前工作区').click();
    await page.getByRole('status').filter({ hasText: `工作区已有：.skills/${name}` }).waitFor();
    await link('在工作区查看').click();
    await page.waitForURL(current => current.pathname === '/resources');
    results.push('already_installed 真实路径及首次导航');

    // 使用管理员停用自己的临时包制造真实 404，不伪造网络错误。
    await cleanInstall();
    await page.goto(url);
    await button('安装到当前工作区').waitFor();
    const disabled = await admin.request.put(api + '/site/admin/skills/' + pack.id, { headers: adminHeaders, data: { ...draft, enabled: false, version: pack.version } });
    assert(disabled.ok(), '停用临时源包失败');
    const disabledValue = await disabled.json();
    await button('安装到当前工作区').click();
    await page.getByRole('alert').filter({ hasText: 'Skill 不存在或已停用' }).waitFor();
    assert(await link('在工作区查看').count() === 0 && await button('安装到当前工作区').isEnabled(), '失败不得显示成功路径，必须可重试');
    const enabled = await admin.request.put(api + '/site/admin/skills/' + pack.id, { headers: adminHeaders, data: { ...draft, version: disabledValue.version } });
    assert(enabled.ok(), '重新启用临时源包失败');
    await button('安装到当前工作区').click();
    await page.getByRole('status').filter({ hasText: '已安装到 .skills/' }).waitFor();
    await link('返回 Skill 仓库').click();
    await page.waitForURL(current => current.pathname === '/extensions');
    results.push('真实 404 无虚假成功，恢复后可重试及首次返回');

    const own = (await (await page.request.get(api + '/agent/skills')).json()).find(skill => skill.name === name);
    assert(own, '必须发现真实安装文件');
    await page.goto(`${web}/extensions/skills/${own.id}?action=edit`);
    const editor = page.getByRole('textbox', { name: 'Skill 内容' });
    await editor.waitFor();
    const original = await editor.inputValue();
    await editor.fill(original + '\n未保存导航保护');
    await link('返回 Skill 仓库').click();
    await page.getByText('有尚未保存的修改，要放弃并离开吗？', { exact: true }).waitFor();
    await button('继续编辑').click();
    assert(await editor.inputValue() === original + '\n未保存导航保护', '保留草稿');
    await link('返回 Skill 仓库').click();
    await button('放弃修改').click();
    await page.waitForURL(current => current.pathname === '/extensions');
    results.push('未保存编辑继续/放弃离开保护保持');
    return { results, delayedActualDetailResponses: delayedReads, simulatedResponses: 0 };
  } finally {
    await page.unroute(detail, delayedDetail);
    await cleanInstall();
    const current = await admin.request.get(api + '/site/admin/skills/' + pack.id);
    if (current.ok()) {
      const deleted = await admin.request.delete(api + '/site/admin/skills/' + pack.id, { headers: adminHeaders, data: { version: (await current.json()).version } });
      assert(deleted.ok(), '临时源包清理失败');
    }
    await admin.close();
  }
}
