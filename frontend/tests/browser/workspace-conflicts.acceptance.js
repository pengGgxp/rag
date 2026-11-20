// Real UI writes in a unique local fixture; only setup and assertions use APIs.
async (page) => {
  const assert = (ok, text) => { if (!ok) throw new Error(text); };
  assert(new URL(page.url()).hostname === 'localhost', '只允许本地验收');
  const session = await (await page.request.get('http://localhost:8000/auth/me')).json();
  assert(session.user.email === 'issue100-local@example.com', '只允许临时账号');
  const headers = { 'X-RAG-Request': '1', 'X-CSRF-Token': session.csrf_token };
  const root = `冲突回归-${Date.now()}`;
  const put = async (path, content) => {
    const result = await page.request.put('http://localhost:8000/files', { headers, data: { path, content } });
    assert(result.ok(), '临时文件准备失败');
    return result.json();
  };
  const api = async path => (await page.request.get('http://localhost:8000' + path)).json();
  const option = name => page.getByRole('option', { name, exact: true });
  const button = name => page.getByRole('button', { name, exact: true });
  const text = async path => {
    const item = (await api('/resources/browse?q=' + encodeURIComponent(path))).items.find(item => item.path === path);
    return (await api(`/files/${item.id}/text`)).content;
  };
  for (const path of [root, `${root}/source`]) {
    assert((await page.request.post('http://localhost:8000/resources/directories', { headers, data: { path } })).ok(), '准备空目录标记失败');
  }
  await put(`${root}/source/package/a.txt`, 'source-a');
  await put(`${root}/source/package/b.txt`, 'source-b');
  await put(`${root}/destination/package/a.txt`, 'target-a');
  await put(`${root}/destination/package/b.txt`, 'target-b');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('http://localhost:3000/resources?path=' + encodeURIComponent(root + '/source'));
  await option('package').click(); await page.keyboard.press('Control+x');
  await button('返回上级').click(); await option('destination').dblclick();
  await page.getByRole('listbox', { name: '资源内容' }).focus(); await page.keyboard.press('Control+v');
  await page.getByRole('dialog', { name: '目标位置有同名项目' }).waitFor();
  await button('合并文件夹').click();
  await page.getByRole('combobox', { name: `处理 ${root}/destination/package/a.txt` }).selectOption('replace');
  await page.getByRole('combobox', { name: `处理 ${root}/destination/package/b.txt` }).selectOption('keep_both');
  await button('应用所选操作').click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  await page.waitForFunction(() => !document.querySelector('[aria-label="刷新资源"]')?.disabled);
  await option('package').dblclick(); await option('b (2).txt').waitFor();
  assert(await text(`${root}/destination/package/a.txt`) === 'source-a', '逐文件替换未生效');
  assert(await text(`${root}/destination/package/b.txt`) === 'target-b', '保留两份不应覆盖原文件');
  assert(await text(`${root}/destination/package/b (2).txt`) === 'source-b', '保留两份缺少新文件');
  assert((await api('/resources/browse?path=' + encodeURIComponent(root + '/source'))).items.length === 0, '剪切应清除来源');
  await option('a.txt').click(); await option('b (2).txt').click({ modifiers: ['Shift'] });
  await page.getByText('已选中 2 项', { exact: true }).waitFor();
  assert(await page.getByRole('listbox', { name: '资源内容' }).getByRole('option', { selected: true }).count() === 2, 'Shift范围选择失败');
  const upload = page.locator('input[type=file]').first();
  await upload.setInputFiles([{ name: 'upload.py', mimeType: 'text/x-python', buffer: Buffer.from('print(1)') }, { name: 'binary.bin', mimeType: 'application/octet-stream', buffer: Buffer.from([0, 255, 1]) }]);
  await option('binary.bin').waitFor(); await option('upload.py').waitFor();
  await upload.setInputFiles({ name: 'upload.py', mimeType: 'text/x-python', buffer: Buffer.from('print(2)') });
  const conflict = page.getByRole('dialog', { name: '目标位置有同名项目' }); await conflict.waitFor();
  await button('取消').click(); assert(await text(`${root}/destination/package/upload.py`) === 'print(1)', '取消不应覆盖');
  await upload.setInputFiles({ name: 'upload.py', mimeType: 'text/x-python', buffer: Buffer.from('print(3)') });
  await conflict.waitFor(); await button('替换').click(); await conflict.waitFor({ state: 'hidden' });
  await page.waitForFunction(() => !document.querySelector('[aria-label="刷新资源"]')?.disabled);
  assert(await text(`${root}/destination/package/upload.py`) === 'print(3)', '替换上传未写入当前目录');
  await upload.setInputFiles({ name: 'upload.py', mimeType: 'text/x-python', buffer: Buffer.from('print(4)') });
  await conflict.waitFor(); await button('保留两份').click(); await option('upload (2).py').waitFor();
  // A real request failure is recoverable without discarding the visible files.
  await page.route('**/resources/browse?**', route => route.abort('failed'), { times: 1 });
  const failure = page.getByRole('alert').filter({ has: button('重试') });
  await button('刷新资源').click(); await failure.waitFor();
  await button('重试').click(); await failure.waitFor({ state: 'hidden' });
  await option('upload (2).py').waitFor();
  await page.screenshot({ path: 'output/playwright/issue100-conflict-recovery.png', fullPage: true });
  return 'PASS 目录逐项合并、剪切粘贴、Shift多选、脚本/二进制上传、取消/替换/保留两份、请求失败重试';
}
