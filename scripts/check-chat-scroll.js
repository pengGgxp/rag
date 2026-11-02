async page => {
  const id = '99999999-9999-4999-8999-999999999999';
  let count = 12;
  await page.route('http://localhost:8000/**', async route => {
    const path = new URL(route.request().url()).pathname;
    let data = [];
    if(path === '/auth/me') data = {user:{id:'u',name:'Test',email:'test@example.test'},workspace_id:'w',csrf_token:'test',workspaces:[{id:'w',name:'Product team',role:'admin'}]};
    else if(path === '/auth/options') data = {first_run:false,setup_available:false,password_reset:false};
    else if(path === '/models/available') data = {default_provider_id:'m',providers:[{id:'m',name:'Test',model:'test',available:true,supports_tools:true,context_capacity:64000,max_output_tokens:4096}]};
    else if(path === '/site/announcement') data = {announcement:null};
    else if(path === '/search/availability') data = {available:false};
    else if(path.endsWith('/context')) data = {status:'idle',capacity:64000,output_reserve:4096,estimated_input_tokens:100,eligible:false};
    return route.fulfill({json:data,headers:{'Access-Control-Allow-Origin':'http://localhost:3110','Access-Control-Allow-Credentials':'true','Access-Control-Allow-Headers':'*'}});
  });
  await page.route('http://localhost:8000/conversations/'+id, route => route.fulfill({json:{id,title:'滚动检查',knowledge_bases:[],active_request_id:null,updated_at:new Date().toISOString(),skill_ids:[],mcp_server_ids:[],messages:Array.from({length:count},(_,i)=>({id:'a'+i,sequence:i,role:'assistant',content:('正文段落\n\n').repeat(12),display_blocks:[{id:'t'+i,type:'text',text:('正文段落\n\n').repeat(12)+'注释[^1]\n\n[^1]: 脚注内容'},{id:'b'+i,type:'activity',call_id:'call'+i}],tool_events:[{call_id:'call'+i,name:'web_search',label:'搜索网页',kind:'tool',status:'completed',summary:'找到结果'}],status:'completed',citations:[{chunk_id:'c'+i,filename:'参考.pdf',knowledge_base_name:'资料',knowledge_base_id:'kb'}],traces:[],refused:false}))},headers:{'Access-Control-Allow-Origin':'http://localhost:3110','Access-Control-Allow-Credentials':'true'}}));
  const results=[];
  for(const width of [1440,700,390]) {
    await page.setViewportSize({width,height:900});
    for(const size of [12,1,0]) {
      count=size;
      await page.goto('http://localhost:3110/?conversation='+id);
      await page.getByRole('textbox',{name:'向知识库提问'}).waitFor();
      if(size) await page.locator('.chat-message').last().waitFor();
      for(const edge of ['top','bottom']) {
        await page.locator('.chat-messages').evaluate((e,edge)=>{e.style.scrollBehavior='auto';e.scrollTop=edge==='top'?0:e.scrollHeight;},edge);
        const metrics=await page.evaluate(()=>({page:document.documentElement.scrollHeight,viewport:innerHeight,scroll:document.querySelector('.chat-messages').scrollHeight,client:document.querySelector('.chat-messages').clientHeight}));
        if(metrics.page>metrics.viewport+1) throw Error(JSON.stringify({width,size,edge,...metrics}));
        if(size===12 && metrics.scroll<=metrics.client) throw Error('History no longer scrolls');
        results.push({width,size,edge,...metrics});
      }
      if(size) {
        const summary=page.locator('.source-disclosure > summary').last();
        await summary.click(); await summary.click();
        const state=await page.locator('.conversation-activity .sr-only').last().evaluate(e=>({text:e.textContent,container:e.offsetParent?.className}));
        if(!state.text || state.container!=='chat-messages') throw Error(JSON.stringify(state));
      }
      if(size===12) {
        await page.locator('.chat-messages').evaluate(e=>{e.scrollTop=0;});
        await page.getByRole('button',{name:'回到最新'}).click();
        const remaining=await page.locator('.chat-messages').evaluate(e=>e.scrollHeight-e.clientHeight-e.scrollTop);
        if(remaining>1) throw Error('Latest button did not reach bottom');
      }
      if(size===12) await page.screenshot({path:'output/playwright/chat-scroll-fixed-'+width+'.png'});
    }
  }
  return results;
}
