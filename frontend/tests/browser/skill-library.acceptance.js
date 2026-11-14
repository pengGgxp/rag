// Local disposable preview only. Seed a catalog package named 仓库交互验收 first.
async (page) => {
  const assert = (ok, text) => { if (!ok) throw new Error(text); };
  const session = await (await page.request.get('http://localhost:8000/auth/me')).json();
  assert(new URL(page.url()).hostname === 'localhost' && session.user.email === 'issue100-local@example.com', '只允许临时本地账号');
  // Remove only this script's prior disposable fixture packages before a repeat.
  const headers = { 'X-RAG-Request': '1', 'X-CSRF-Token': session.csrf_token };
  const skills = await (await page.request.get('http://localhost:8000/agent/skills')).json();
  const paths = skills.filter(skill => ['仓库交互验收', '工作区修改后的方法'].includes(skill.name) && /^\.skills\/仓库交互验收(?: \(\d+\))?$/.test(skill.directory)).map(skill => skill.directory);
  if (paths.length) {
    const preview = await (await page.request.post('http://localhost:8000/resources/operations/delete-preview', { headers, data: { paths } })).json();
    assert((await page.request.post('http://localhost:8000/resources/operations/delete', { headers, data: { paths, confirmation: preview.confirmation, request_id: crypto.randomUUID() } })).ok(), '清理临时夹具失败');
  }
  const button = name => page.getByRole('button', { name, exact: true });
  await page.goto('http://localhost:3000/extensions');
  await page.getByRole('tab', { name: '未安装', exact: true }).click();
  await page.getByRole('textbox', { name: '搜索 Skill' }).fill('仓库交互验收');
  await button('仓库交互验收').click();
  await page.getByRole('heading', { name: '仓库交互验收', exact: true }).first().waitFor();
  const download = page.waitForEvent('download');
  await button('下载包').click();
  await (await download).saveAs('output/playwright/issue100-platform-skill.zip');
  await button('安装到当前工作区').click();
  await page.getByRole('status').filter({ hasText: '已安装到 .skills/' }).waitFor();
  await page.getByRole('link', { name: '在工作区查看', exact: true }).click();
  await page.getByRole('option', { name: 'SKILL.md', exact: true }).waitFor();
  await page.getByRole('option', { name: 'SKILL.md', exact: true }).dblclick();
  const editor = page.getByRole('textbox', { name: '文件内容', exact: true });
  const initial = await editor.inputValue();
  const changed = initial.replace('仓库交互验收', '工作区修改后的方法');
  assert(initial !== changed, '清单应含正确平台名称');
  await editor.fill(changed); await button('保存').click();
  await page.getByRole('dialog').waitFor({ state: 'hidden' });
  await page.getByRole('link', { name: '扩展', exact: true }).click();
  await button('工作区修改后的方法').click();
  await button('编辑 Skill').click();
  const skillEditor = page.getByRole('textbox', { name: 'Skill 内容' });
  assert((await skillEditor.inputValue()).includes('工作区修改后的方法'), '管理页应读取工作区修改');
  await skillEditor.fill(changed + '\n尚未保存');
  await page.getByRole('link', { name: '返回 Skill 仓库', exact: true }).click();
  await page.getByText('有尚未保存的修改，要放弃并离开吗？', { exact: true }).waitFor();
  await button('继续编辑').click();
  await button('保存').click(); await page.getByRole('status').filter({ hasText: '已保存' }).waitFor();
  await button('卸载').click(); await button('确认卸载').click();
  await page.getByRole('button', { name: '仓库交互验收', exact: true }).waitFor();
  await button('仓库交互验收').click();
  await button('安装到当前工作区').click();
  await page.getByRole('status').filter({ hasText: '已安装到 .skills/' }).waitFor();
  await page.getByRole('link', { name: '返回 Skill 仓库', exact: true }).click();
  await page.getByRole('link', { name: '添加 Skill', exact: true }).click();
  await page.locator('input[type=file][accept=".md,.zip"]').setInputFiles('output/playwright/issue100-platform-skill.zip');
  await page.getByRole('status').filter({ hasText: '已导入 3 个资源文件' }).waitFor();
  await page.getByRole('link', { name: '在工作区查看', exact: true }).click();
  await page.getByRole('option', { name: 'SKILL.md', exact: true }).waitFor();
  assert(await page.getByRole('group', { name: '当前路径，双击或按 Enter 编辑' }).innerText().then(text => /\(\d+\)/.test(text)), '同名导入须消歧定位');
  await page.screenshot({ path: 'output/playwright/issue100-skill-directory.png', fullPage: true });
  return 'PASS 统一仓库筛选/搜索/详情/下载/安装、工作区修改与说明同源、未保存保护、卸载重装、ZIP导入消歧与真实路径定位';
}
