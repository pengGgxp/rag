// Run with playwright-cli run-code --filename after logging into the disposable preview.
// Refuses non-local pages and accounts. All writes use a new fixture directory.
async (page) => {
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  assert(new URL(page.url()).hostname === 'localhost', '只允许本地验收');
  const session = await (await page.request.get('http://localhost:8000/auth/me')).json();
  assert(session.user.email === 'issue100-local@example.com', '只允许临时验收账号');
  const headers = { 'X-RAG-Request': '1', 'X-CSRF-Token': session.csrf_token };
  const api = async (path, data) => {
    const response = data ? await page.request.post(`http://localhost:8000${path}`, { headers, data })
      : await page.request.get(`http://localhost:8000${path}`);
    assert(response.ok(), `验收准备失败 ${response.status()} ${path}`);
    return response.json();
  };
  const put = async (data) => {
    const response = await page.request.put(`http://localhost:8000/workspaces/${session.workspace_id}/files`, { headers, data });
    assert(response.ok(), `临时文件写入失败 ${response.status()}`);
    return response.json();
  };
  const option = name => page.getByRole('option', { name, exact: true });
  const button = name => page.getByRole('button', { name, exact: true });
  const address = page.getByRole('group', { name: '当前路径，双击或按 Enter 编辑' });
  async function navigate(path) {
    await address.focus(); await page.keyboard.press('Enter');
    await page.getByRole('textbox', { name: '地址栏', exact: true }).fill(`工作区/${path}`);
    const response = page.waitForResponse(r => r.url().includes('/resources/browse?') && r.status() === 200);
    await page.keyboard.press('Enter'); await response;
    await page.getByRole('listbox', { name: '工作区内容' }).getAttribute('aria-busy').then(async value => {
      if (value === 'true') await page.waitForFunction(() => document.querySelector('[aria-label="工作区内容"]')?.getAttribute('aria-busy') === 'false');
    });
  }
  async function create(kind, name, cancel = false) {
    await button('新建').click(); await button(kind).click();
    const input = page.getByRole('textbox', { name: '输入新项目名称', exact: true });
    await input.waitFor();
    await page.waitForFunction(() => document.activeElement?.getAttribute('aria-label') === '输入新项目名称');
    if (cancel) { await input.press('Escape'); return; }
    await input.fill(name); await input.press('Enter');
    await page.waitForFunction(() => !document.querySelector('[aria-label="输入新项目名称"]'));
    await option(name).waitFor();
  }
  await page.goto('http://localhost:3000/resources');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await create('文件夹', '', true);
  const root = `浏览器回归-${Date.now()}`;
  await create('文件夹', root); await option(root).dblclick();
  await create('文本文档', 'one.txt'); await option('one.txt').dblclick();
  const editor = page.getByRole('textbox', { name: '文件内容', exact: true });
  await editor.fill('尚未保存的修改'); await button('取消').click();
  await button('继续编辑').click(); assert(await editor.inputValue() === '尚未保存的修改', '取消关闭须保留草稿');
  const old = (await api(`/resources/browse?path=${encodeURIComponent(root)}`)).items.find(item => item.name === 'one.txt');
  await put({ path: old.path, content: '其他入口的新内容', revision: old.revision });
  await button('保存').click();
  await page.getByRole('alert').filter({ hasText: '文件已变化' }).waitFor();
  assert(await editor.inputValue() === '尚未保存的修改', '409须保留输入');
  await button('重新加载').click(); await button('放弃修改').click();
  await page.waitForFunction(() => document.querySelector('[aria-label="文件内容"]')?.value === '其他入口的新内容');
  await editor.fill('保存后的内容'); await button('保存').click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  await option('one.txt').click(); await page.keyboard.press('F2');
  await page.getByRole('textbox', { name: '重命名 one.txt', exact: true }).fill('renamed.txt');
  await page.keyboard.press('Enter'); await option('renamed.txt').waitFor();
  await option('renamed.txt').click(); await page.keyboard.press('Control+c'); await page.keyboard.press('Control+v');
  await page.getByRole('dialog', { name: '目标位置有同名项目' }).waitFor();
  await button('保留两份').click(); await option('renamed (2).txt').waitFor();
  await page.getByRole('textbox', { name: '搜索工作区', exact: true }).fill('renamed (2)');
  const result = option('renamed (2).txt').filter({ hasText: root });
  await result.waitFor(); await result.click(); await page.keyboard.press('F2');
  await page.getByRole('textbox', { name: '重命名 renamed (2).txt', exact: true }).fill('search-renamed.txt');
  await page.keyboard.press('Enter');
  await page.waitForFunction(() => !document.querySelector('.resource-inline-input'));
  assert((await api(`/resources/browse?path=${encodeURIComponent(root)}`)).items.some(item => item.name === 'search-renamed.txt'), '搜索改名必须保留父目录');
  await button('清除搜索').click(); await option('search-renamed.txt').waitFor();
  await create('文件夹', '移动目标');
  await option('renamed.txt').click(); await option('search-renamed.txt').click({ modifiers: ['Control'] });
  await option('renamed.txt').click({ button: 'right' });
  await page.getByRole('menuitem', { name: '复制 2 项', exact: true }).waitFor();
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('Escape');
  await option('renamed.txt').dragTo(option('移动目标'));
  await option('renamed.txt').waitFor({ state: 'hidden' });
  await option('移动目标').dblclick(); await option('search-renamed.txt').waitFor();
  await page.getByRole('listbox', { name: '工作区内容' }).focus(); await page.keyboard.press('Alt+ArrowUp');
  await option('移动目标').waitFor(); await option('移动目标').click(); await page.keyboard.press('Delete');
  const deletion = page.getByRole('dialog', { name: '删除确认' });
  await deletion.getByText('将删除 2 个文件和 1 个文件夹。', { exact: false }).waitFor();
  await put({ path: `${root}/移动目标/late.txt`, content: '确认后新增加的内容' });
  await button('确认删除').click();
  await deletion.getByText('将删除 3 个文件和 1 个文件夹。', { exact: false }).waitFor();
  await deletion.getByRole('alert').filter({ hasText: '内容已变化' }).waitFor();
  await button('确认删除').click(); await deletion.waitFor({ state: 'hidden' });
  await option('移动目标').waitFor({ state: 'hidden' });
  await navigate('分页验收');
  await page.getByRole('button', { name: '继续加载（已显示 100 项）', exact: true }).click();
  await page.getByRole('button', { name: '继续加载（已显示 200 项）', exact: true }).waitFor();
  await page.getByRole('listbox', { name: '工作区内容' }).focus(); await page.keyboard.press('Control+a');
  await page.getByText('已选中 505 项', { exact: true }).waitFor();
  await page.keyboard.press('Delete');
  await deletion.getByText('将删除 505 个文件和 0 个文件夹。', { exact: false }).waitFor();
  await button('取消').click();
  await navigate('项目资料');
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 960 });
    for (const view of ['卡片', '列表']) {
      await button(view).click();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${width} ${view}存在横向溢出`);
      await page.screenshot({ path: `output/playwright/issue100-${width}-${view === '卡片' ? 'grid' : 'list'}.png`, fullPage: true });
    }
  }
  return 'PASS 创建/取消/焦点、未保存保护、409保留/恢复、搜索改名、多选菜单/拖拽、递归删除重确认、505分页全选、三尺寸两种视图';
}
