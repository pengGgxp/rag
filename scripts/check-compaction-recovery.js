async page => {
  const id = '99999999-9999-4999-8999-999999999999';
  let compressed = false;
  let thinkingReads = 0;
  let attempts = 0;
  let messages = [];
  const initial = () => [{id:'q',sequence:0,role:'user',content:'请核对资料',status:'completed',citations:[],traces:[]},
    {id:'a',request_id:'original',sequence:1,role:'assistant',content:'已核对。',status:'completed',citations:[],traces:[],
      display_blocks:[{id:'thinking-1',type:'thinking',round:1,status:'completed'},{id:'text-1',type:'text',text:'已核对。'}]}];
  await page.route('http://localhost:8000/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let data = [];
    if (path === '/auth/me') data = {user:{id:'u',name:'Test',email:'test@example.test'},workspace_id:'w',csrf_token:'test',workspaces:[{id:'w',name:'Team',role:'admin'}]};
    else if (path === '/auth/options') data = {first_run:false,setup_available:false};
    else if (path === '/models/available') data = {default_provider_id:'m',providers:[{id:'m',name:'Test',model:'test',available:true,supports_tools:true,context_capacity:272000,max_output_tokens:128000}]};
    else if (path === '/site/announcement') data = {announcement:null};
    else if (path === '/search/availability') data = {available:false};
    else if (path.endsWith('/thinking')) {
      thinkingReads++;
      if (attempts++ === 0) return route.fulfill({status:503,json:{detail:'暂时无法读取'}});
      data = {blocks:[{id:'thinking-1',text:'核对资料后确认结论。'}]};
    } else if (path.endsWith('/context')) data = {status:compressed?'completed':'idle',capacity:272000,output_reserve:128000,estimated_input_tokens:compressed?12000:53400,eligible:!compressed};
    else if (path.endsWith('/compact')) {
      compressed = true;
      const events = [['compaction',{status:'running'}],['context',{status:'completed',capacity:272000,output_reserve:128000,estimated_input_tokens:12000,eligible:false}],['done',{status:'completed'}]];
      return route.fulfill({contentType:'text/event-stream',body:events.map(([name,data])=>`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`).join('')});
    } else if (path === '/conversations/'+id) data = {id,title:'压缩恢复检查',knowledge_bases:[],active_request_id:null,skill_ids:[],mcp_server_ids:[],messages,
      compactions:compressed?[{id:'checkpoint',after_sequence:1,created_at:'2026-11-02T07:00:00Z'}]:[]};
    return route.fulfill({json:data});
  });
  const results = [];
  for (const width of [1440,700,390]) {
    compressed = false; attempts = 0; messages = initial();
    await page.setViewportSize({width,height:900});
    await page.emulateMedia({reducedMotion:'reduce'});
    await page.goto('http://localhost:3111/?conversation='+id);
    const thinking = page.getByRole('button',{name:'展开第 1 轮思考'});
    await thinking.click();
    await page.getByText('暂时无法读取，请重试',{exact:true}).waitFor();
    await page.getByRole('button',{name:'重试',exact:true}).click();
    await page.getByText('核对资料后确认结论。',{exact:true}).waitFor();
    const context = page.getByRole('button',{name:'上下文',exact:true});
    await context.getByText('已用 20%').waitFor();
    await context.click();
    await page.getByRole('button',{name:'压缩上下文',exact:true}).click();
    await page.getByRole('button',{name:'确认压缩',exact:true}).click();
    await page.locator('.conversation-compaction').getByText('上下文已压缩',{exact:true}).waitFor();
    await context.getByText('已用 5%').waitFor();
    await page.reload();
    await page.locator('.conversation-compaction').waitFor();
    await page.getByRole('button',{name:'展开第 1 轮思考'}).click();
    await page.getByText('核对资料后确认结论。',{exact:true}).waitFor();
    await context.getByText('已用 5%').waitFor();
    if (await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1 || document.documentElement.scrollHeight>innerHeight+1)) throw Error('Page overflow');
    await page.screenshot({path:`output/playwright/compaction-recovery-${width}.png`});
    results.push({width,markerRestored:true,thinkingRestored:true,usedPercent:5});
  }
  return {results,thinkingReads};
}
