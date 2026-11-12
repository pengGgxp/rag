async (page) => {
  const api = 'http://localhost:8131';
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const fixture = await (await page.request.get(api + '/__fixture/session')).json();
  await page.context().addCookies([{ name: 'rag_session', value: fixture.cookie, domain: 'localhost', path: '/' }]);
  await page.goto('http://localhost:3191/?chat=1');
  const input = page.getByRole('textbox', { name: '发送消息', exact: true });
  await input.waitFor();
  await input.fill('@数量');
  const option = page.getByRole('option', { name: '数量.txt 验收/数量.txt 资源文件', exact: true });
  await option.waitFor();
  await option.click();
  await input.fill((await input.inputValue()) + '请先读取数量，再将 17 改为 23 并追加 checked 行，最后重新读取核对。');
  await page.getByRole('button', { name: '发送问题', exact: true }).click();
  const answer = page.getByText('修改前数量为 17，修改后重新读取数量为 23，新增 checked 行已保存。', { exact: true });
  await answer.waitFor();
  const report = await (await page.request.get(api + '/__fixture/report')).json();
  assert(report.creates === 1 && report.starts === 3, '文件读改再读没有分别执行');
  assert(report.commands.length === 3 && new Set(report.commands.map(item => item.id)).size === 3, '命令记录错误');
  assert(report.commands[0].digest === report.commands[2].digest, '两次读取参数并不相同');
  assert(new Set(report.commands.map(item => item.request_id)).size === 3, '不同调用复用了请求标识');
  assert(report.commands.every(item => item.status === 'completed'), '存在未完成命令');
  assert(report.observations[0].content === 'quantity 17\n' && report.observations[2].content === 'quantity 23\nchecked\n', '模型看到旧正文');
  const downloaded = await page.request.get(api + '/resources/' + fixture.resource.id + '/download');
  assert(downloaded.status() === 200, '下载接口返回 ' + downloaded.status());
  assert((await downloaded.body()).toString() === report.content, '下载正文与模型重新读取结果不同');
  const pendingDownload = page.waitForEvent('download');
  await page.getByRole('link', { name: '下载 数量.txt', exact: true }).click();
  const download = await pendingDownload;
  await download.saveAs('output/playwright/issue91-downloaded-count.txt');
  assert(!(await download.failure()), '浏览器下载失败');
  for (const width of [1440, 700, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.reload();
    await answer.waitFor();
    assert(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)), '页面横向溢出');
    await page.screenshot({ path: 'output/playwright/issue91-completed-' + width + '.png' });
  }
  const refreshed = await (await page.request.get(api + '/__fixture/report')).json();
  assert(refreshed.starts === 3 && refreshed.commands.length === 3, '刷新重新执行了文件操作');
  return { starts: report.starts, commands: report.commands, sha256: report.sha256,
    refresh_starts: refreshed.starts, widths: [1440, 700, 390] };
}
