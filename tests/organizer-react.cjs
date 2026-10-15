/* Offline browser regression. All APIs are mocked; never connects to a music library. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {chromium}=require('../frontend/node_modules/playwright');
const root=path.resolve(__dirname,'../backend/static');
const base='http://music-info.test';
(async()=>{
 const {DEFAULTS}=await import('../frontend/src/model.js');
 const browser=await chromium.launch({headless:true,channel:process.env.UI_BROWSER_CHANNEL||undefined});
 const results=[];
 async function scenario(name,fn,options={}){
  const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage();
  const errors=[],writes=[];let cfg={...DEFAULTS},writable=options.writable!==false,failedStatus=!!options.failedStatus,phase=0;
  let plan={plan_id:'fixture-plan',created:1792070000,status:options.planStatus||'ready',cfg:{...DEFAULTS},counts:{move:105,wash:1,duplicate:1,junk:1,error:1,sidecar:1},applied:{}};
  const items=Array.from({length:105},(_,i)=>({ord:i,name:'测试曲目 '+i+'.flac',src:'music/未整理/待整理/'+i+'.flac',target:'music/已整理/歌手/专辑/'+i+'.flac',kind:'move',status:'pending',reason:'离线样本'}));
  page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',async route=>{const req=route.request(),u=new URL(req.url());
   assert.equal(u.origin,base,'Unexpected network request');
   if(!u.pathname.startsWith('/api/')){let f=path.join(root,u.pathname==='/'?'index.html':u.pathname);if(!f.startsWith(root))return route.abort();return route.fulfill({status:200,contentType:f.endsWith('.js')?'text/javascript':f.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(f)});}
   const respond=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
   if(req.method()!=='GET'){writes.push({path:u.pathname,body:req.postDataJSON()});
    if(u.pathname==='/api/organize/preview'){const b=req.postDataJSON();if((b.classify_rules||'').includes('INVALID'))return respond({ok:false,error:'YAML 格式错误'});return respond({ok:true,rules:b.classify_rules?1:0,paths:[{sample:'样本',path:'music/已整理/歌手/专辑/歌名.flac',rule:b.classify_rules?'示例':''}]});}
    if(u.pathname==='/api/organize/settings'){cfg={...cfg,...req.postDataJSON()};return respond({ok:true,created:[],settings:cfg});}
    if(u.pathname==='/api/organize/scan'){phase=1;plan={...plan,status:'scanning'};return respond({job_id:'scan-job',plan_id:plan.plan_id});}
    if(u.pathname==='/api/organize/apply'){phase=1;plan={...plan,status:'applying'};return respond({job_id:'apply-job',plan_id:plan.plan_id,total:109,remaining:0});}
    if(u.pathname.endsWith('/cancel')){phase=2;return respond({ok:true});}
    if(u.pathname==='/api/organize/mkdir')return respond({created:true,path:'music/new'});
    if(u.pathname.endsWith('/discard'))return respond({ok:true});
    return respond({detail:'Unexpected write'},500);
   }
   if(u.pathname==='/api/organize/status'){if(failedStatus)return respond({detail:'测试读取失败'},503);return respond({settings:cfg,defaults:DEFAULTS,media_root:'/media',media_writable:writable,paths:Object.fromEntries(['source','library','duplicates','junk'].map(k=>[k,{rel:cfg[k],abs:'/media/'+cfg[k],host:cfg.host_root+'/'+cfg[k],exists:true,files:k==='source'?5:0,bytes:1234}])),problems:[],effective_name_template:cfg.name_template||'{title}',running_plans:[]});}
   if(u.pathname==='/api/organize/plans')return respond({plans:[plan]});
   if(u.pathname==='/api/organize/browse'){const rel=u.searchParams.get('path')||'';return respond({path:rel,abs:'/media'+(rel?'/'+rel:''),parent:rel?'':null,dirs:rel?[]:['music','new'],dirs_total:rel?0:2,files:0,roles_exact:[],roles_below:['source','library']});}
   if(u.pathname==='/api/organize/plan/fixture-plan'){if(phase===2)plan={...plan,status:'ready'};if(u.searchParams.get('view')==='full'){const k=u.searchParams.get('kind'),offset=Number(u.searchParams.get('offset')||0),limit=Number(u.searchParams.get('limit')||100);const rows=k==='move'?items:k==='junk,error'?[{ord:999,name:'错误样本',kind:'error',status:'failed',reason:'读取失败'}]:[];return respond({...plan,total:rows.length,items:rows.slice(offset,offset+limit)});}return respond(plan);}
   if(u.pathname.startsWith('/api/tasks/'))return respond({status:phase===2?'completed':'running',counts:{completed:0},total:109});
   if(u.pathname==='/api/list')return respond({dirs:[],files:[]});
   if(u.pathname==='/api/system')return respond({storage:{database:'SQLite',journal_mode:'wal',jobs:0,active_jobs:0}});
   if(u.pathname==='/api/settings')return respond({settings:{proxy:{enabled:false,url:''}}});
   if(u.pathname==='/api/tasks')return respond({jobs:[]});
   if(u.pathname.startsWith('/api/plugins'))return respond({plugins:[],stats:{}});
   if(u.pathname==='/api/history')return respond({versions:[]});
   if(u.pathname==='/api/sources')return respond({sources:[]});
   return respond({});
  });
  const test={page,writes,errors,setWritable:v=>writable=v,recover:()=>failedStatus=false};
  try{await page.goto(base);await page.waitForSelector('[data-react-section="organizer"]',{state:'attached',timeout:10000}).catch(e=>{if(!failedStatus)throw e;});await fn(test);assert.deepEqual(errors,[],'Browser errors');results.push({name,passed:true,writes:writes.map(w=>w.path)});}
  catch(e){await page.screenshot({path:path.join(__dirname,'failure.png'),fullPage:true});throw new Error(name+': '+e.stack+'\nBrowser errors: '+errors.join('; '));}
  finally{await context.close();}
 }
 try{
  await scenario('read-only initialization and legacy workbench',async({page,writes})=>{assert.equal(writes.length,0);assert.equal(await page.locator('#view-work').isVisible(),true);assert.equal(await page.locator('#tag-form input,#tag-form textarea').count()>0,true);await page.locator('.nav-btn[data-view="organize"]').click();await page.getByRole('heading',{name:'自动整理',exact:true}).waitFor();assert.equal(writes.length,0);});
  await scenario('plan pagination and error category',async({page,writes})=>{await page.locator('.nav-btn[data-view="organize"]').click();await page.getByRole('button',{name:'查看',exact:true}).click();await page.getByText('测试曲目 99.flac',{exact:true}).waitFor();assert.equal(await page.locator('.rx-item').count(),100);await page.getByRole('button',{name:'加载更多（100 / 105）'}).click();await page.getByText('测试曲目 104.flac',{exact:true}).waitFor();assert.equal(await page.locator('.rx-item').count(),105);await page.getByRole('tab',{name:/冗余\/失败.*2/}).click();await page.getByText('错误样本',{exact:true}).waitFor();assert.equal(writes.length,0);});
  await scenario('apply confirmation cancellation sends no write',async({page,writes})=>{await page.locator('.nav-btn[data-view="organize"]').click();await page.getByRole('button',{name:'查看',exact:true}).click();await page.getByRole('button',{name:'执行整理（109 项）',exact:true}).click();await page.getByRole('dialog').waitFor();assert.equal(writes.length,0);await page.keyboard.press('Escape');assert.equal(await page.getByRole('dialog').count(),0);assert.equal(writes.length,0);});
  await scenario('apply confirmation explicit payload and stop',async({page,writes})=>{await page.locator('.nav-btn[data-view="organize"]').click();await page.getByRole('button',{name:'查看',exact:true}).click();await page.getByRole('button',{name:'执行整理（109 项）',exact:true}).click();await page.getByRole('button',{name:'确认执行整理',exact:true}).click();await page.getByRole('button',{name:'安全停止',exact:true}).waitFor();const sent=writes.find(w=>w.path==='/api/organize/apply');assert.deepEqual(sent.body,{plan_id:'fixture-plan',confirmed:true});await page.getByRole('button',{name:'安全停止',exact:true}).click();assert(writes.some(w=>w.path==='/api/tasks/apply-job/cancel'));});
  await scenario('read-only mount disables apply',async({page,writes})=>{await page.locator('.nav-btn[data-view="organize"]').click();await page.getByRole('button',{name:'查看',exact:true}).click();assert.equal(await page.getByRole('button',{name:'执行整理（109 项）',exact:true}).isDisabled(),true);assert.equal(writes.length,0);},{writable:false});
  for(const planStatus of ['done','failed','cancelled','applying'])await scenario('non-ready plan cannot execute: '+planStatus,async({page,writes})=>{await page.locator('.nav-btn[data-view="organize"]').click();await page.getByRole('button',{name:'查看',exact:true}).click();assert.equal(await page.getByRole('button',{name:'执行整理（109 项）',exact:true}).isDisabled(),true);assert.equal(writes.length,0);},{planStatus});
  await scenario('settings draft survives tab and background refresh',async({page,writes})=>{await page.locator('.nav-btn[data-view="settings"]').click();await page.locator('#settings-tabs [data-tab="organize"]').click();await page.getByRole('tab',{name:'重命名规则',exact:true}).click();await page.getByLabel('文件命名模板',{exact:true}).fill('{artist} - {title}');await page.getByRole('tab',{name:'洗版策略',exact:true}).click();await page.getByRole('tab',{name:'重命名规则',exact:true}).click();await page.evaluate(()=>window.dispatchEvent(new Event('music-info:organizer-refresh')));assert.equal(await page.getByLabel('文件命名模板',{exact:true}).inputValue(),'{artist} - {title}');assert.equal(writes.length,0);await page.getByRole('button',{name:'保存配置',exact:true}).click();await page.getByText('整理配置已保存，不会移动音乐文件。').last().waitFor();assert.equal(writes.find(w=>w.path==='/api/organize/settings').body.name_template,'{artist} - {title}');});
  await scenario('directory picker uses relative paths and cancel preserves draft',async({page,writes})=>{await page.locator('.nav-btn[data-view="settings"]').click();await page.locator('#settings-tabs [data-tab="organize"]').click();await page.getByRole('button',{name:'点击选择',exact:true}).first().click();await page.getByRole('dialog').waitFor();await page.getByRole('button',{name:'音乐库',exact:true}).click();await page.getByRole('button',{name:'▱ new',exact:true}).click();await page.getByRole('button',{name:'用这个目录',exact:true}).click();assert.equal(await page.getByLabel('待整理文件夹（源）',{exact:true}).inputValue(),'/media/new');await page.getByRole('button',{name:'保存配置',exact:true}).click();await page.getByText('整理配置已保存，不会移动音乐文件。').last().waitFor();assert.equal(writes.find(w=>w.path==='/api/organize/settings').body.source,'new');});
  await scenario('YAML preview error shown and no configuration write',async({page,writes})=>{await page.locator('.nav-btn[data-view="settings"]').click();await page.locator('#settings-tabs [data-tab="organize"]').click();await page.getByRole('tab',{name:'二级分类策略',exact:true}).click();await page.getByLabel('二级分类 YAML').fill('INVALID');await page.getByRole('button',{name:'预览示例路径',exact:true}).click();await page.getByText('YAML 格式错误',{exact:true}).waitFor();assert.deepEqual(writes.map(w=>w.path),['/api/organize/preview']);});
  await scenario('unsaved navigation cancellation',async({page,writes})=>{await page.locator('.nav-btn[data-view="settings"]').click();await page.locator('#settings-tabs [data-tab="organize"]').click();await page.getByLabel('未知歌手占位名',{exact:true}).fill('草稿');page.once('dialog',d=>d.dismiss());await page.locator('.nav-btn[data-view="work"]').click();assert.equal(await page.locator('#pane-organize').isVisible(),true);assert.equal(await page.getByLabel('未知歌手占位名',{exact:true}).inputValue(),'草稿');assert.equal(writes.length,0);});
  await scenario('desktop and mobile layout without overflow',async({page,writes})=>{for(const width of [1920,1440,1080,768,390]){await page.setViewportSize({width,height:900});await page.locator('.nav-btn[data-view="organize"]').click();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Organize overflow '+width);await page.locator('.nav-btn[data-view="settings"]').click();await page.locator('#settings-tabs [data-tab="organize"]').click();assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Settings overflow '+width);}assert.equal(writes.length,0);await page.screenshot({path:path.join(__dirname,'react-settings-mobile.png'),fullPage:true});});
  await scenario('status failure and retry recover',async({page,recover,writes})=>{await page.locator('.nav-btn[data-view="organize"]').click();await page.getByText('读取失败：测试读取失败').first().waitFor();recover();await page.getByRole('button',{name:'重新读取',exact:true}).first().click();await page.locator('[data-react-section="organizer"]').waitFor();assert.equal(writes.length,0);},{failedStatus:true});
  console.log(JSON.stringify({passed:results.length,scenarios:results},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
