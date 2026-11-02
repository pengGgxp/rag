async page => {
  const id = '99999999-9999-4999-8999-999999999999';
  let compactions = 0;
  await page.route('http://localhost:8000/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let data = [];
    if (path === '/auth/me') data = {user:{id:'u',name:'Test',email:'test@example.test'},workspace_id:'w',csrf_token:'test',workspaces:[{id:'w',name:'Team',role:'admin'}]};
    else if (path === '/auth/options') data = {first_run:false,setup_available:false};
    else if (path === '/models/available') data = {default_provider_id:'m',providers:[{id:'m',name:'Test',model:'test',available:true,supports_tools:false,context_capacity:272000,max_output_tokens:128000}]};
    else if (path === '/site/announcement') data = {announcement:null};
    else if (path === '/search/availability') data = {available:false};
    else if (path.endsWith('/context')) data = {status:'idle',capacity:272000,output_reserve:128000,estimated_input_tokens:53400,eligible:true};
    else if (path.endsWith('/compact')) {
      compactions++;
      return route.fulfill({contentType:'text/event-stream',body:'event: done\ndata: {"status":"completed"}\n\n'});
    } else if (path === '/conversations/'+id) data = {id,title:'Context test',knowledge_bases:[],active_request_id:null,skill_ids:[],mcp_server_ids:[],messages:[{id:'a',sequence:1,role:'assistant',content:'测试回答',status:'completed',citations:[],traces:[]}]};
    if (path.startsWith('/agent/')) return route.fulfill({status:500,json:{detail:'测试失败'}});
    return route.fulfill({json:data});
  });
  const results = [];
  for (const width of [1440,700,390]) {
    await page.setViewportSize({width,height:900});
    await page.goto('http://localhost:3110/?conversation='+id);
    const context = page.getByRole('button',{name:'上下文',exact:true});
    await context.getByText('已用 20%').waitFor();
    await context.click();
    const panel = page.getByRole('dialog',{name:'上下文',exact:true});
    await panel.waitFor();
    await panel.getByText('共 272k · 已用 53.4k', {exact:true}).waitFor();
    if (/Token|输出预留|安全余量|75%/.test(await panel.innerText())) throw Error('Technical budget copy remains');
    await page.getByRole('button',{name:'关闭上下文'}).click();
    const input = page.getByRole('textbox',{name:'向知识库提问'});
    await input.fill('保留草稿 /compact'); await input.press('End');
    await page.getByRole('option',{name:/compact/}).waitFor();
    await input.press('Enter');
    await page.getByRole('button',{name:'确认压缩'}).waitFor();
    await page.getByRole('button',{name:'取消',exact:true}).click();
    await page.getByRole('button',{name:'关闭上下文'}).click();
    if (await input.inputValue() !== '保留草稿 ') throw Error('Draft lost or command submitted');
    if (compactions) throw Error('Cancellation submitted a request');
    if (await page.evaluate(()=>document.documentElement.scrollHeight>innerHeight+1)) throw Error('Outer scroll overflow');
    await page.screenshot({path:'output/playwright/context-simple-'+width+'.png'});
    results.push({width,percent:20,draftPreserved:true});
  }
  const input = page.getByRole('textbox',{name:'向知识库提问'});
  await input.fill('/压缩'); await input.press('End');
  await page.getByRole('option',{name:/compact/}).click();
  await page.getByRole('button',{name:'确认压缩'}).waitFor();
  if (compactions) throw Error('Command bypassed confirmation');
  await Promise.all([page.waitForRequest(r=>r.url().endsWith('/compact')), page.getByRole('button',{name:'确认压缩'}).click()]);
  if (compactions !== 1) throw Error('Expected one compaction request');
  return {results,compactions};
}
