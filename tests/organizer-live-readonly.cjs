/* Explicit online smoke test: all non-GET/HEAD requests are blocked, including scan/settings/apply. */
const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require('../frontend/node_modules/playwright');
(async()=>{
 const base=process.env.MUSIC_INFO_URL;
 if(!base)throw new Error('Set MUSIC_INFO_URL explicitly; this test never assumes a server.');
 const allowed=new URL(base).origin;
 const browser=await chromium.launch({headless:true,channel:process.env.UI_BROWSER_CHANNEL||undefined});
 const errors=[],writes=[],badAssets=[];
 try{
  const context=await browser.newContext({viewport:{width:1440,height:900}});
  await context.route('**/*',route=>{const r=route.request();if(new URL(r.url()).origin!==allowed)return route.abort();if(!['GET','HEAD'].includes(r.method())){writes.push({method:r.method(),path:new URL(r.url()).pathname});return route.abort();}return route.continue();});
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.on('response',r=>{if(r.url().includes('/assets/')&&!r.ok())badAssets.push({url:r.url(),status:r.status()});});
  await page.goto(base,{waitUntil:'networkidle'});
  assert(await page.locator('#view-work').isVisible(),'Legacy workbench visible');
  const system=await page.evaluate(()=>fetch('/api/system').then(r=>r.json()));assert.equal(system.app_version,'2026.10-workflow.4');
  await page.locator('.nav-btn[data-view="organize"]').click();await page.locator('[data-react-section="organizer"]').waitFor();
  await page.getByRole('heading',{name:'整理规则',exact:true}).waitFor();
  const view=page.locator('[data-react-section="organizer"]');
  if(await view.getByRole('button',{name:'查看',exact:true}).count()){await view.getByRole('button',{name:'查看',exact:true}).first().click();await page.locator('[data-testid="plan-detail"]').waitFor();}
  await page.screenshot({path:path.join(__dirname,'react-live-organizer.png'),fullPage:true});
  await page.getByRole('button',{name:'整理设置',exact:true}).click();
  await page.getByRole('tab',{name:'基础配置',exact:true}).waitFor();
  assert(await page.getByLabel('待整理文件夹（源）',{exact:true}).getAttribute('readonly')!==null);
  for(const label of ['重命名规则','二级分类策略','洗版策略','变量与语法','基础配置'])await page.getByRole('tab',{name:label,exact:true}).click();
  await page.getByRole('button',{name:'点击选择',exact:true}).first().click();await page.getByRole('dialog').waitFor();await page.getByRole('button',{name:'用这个目录',exact:true}).waitFor({state:'visible'});await page.getByRole('button',{name:'取消',exact:true}).click();
  for(const width of [1440,768,390]){await page.setViewportSize({width,height:900});assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'Settings horizontal overflow '+width);}
  await page.setViewportSize({width:1440,height:900});await page.screenshot({path:path.join(__dirname,'react-live-settings.png'),fullPage:true});
  await page.locator('.nav-btn[data-view="work"]').click();assert(await page.locator('#view-work').isVisible());
  assert.deepEqual(errors,[]);assert.deepEqual(badAssets,[]);assert.deepEqual(writes,[]);
  console.log(JSON.stringify({passed:true,backendVersion:system.app_version,reactSections:2,browserErrors:errors,failedAssets:badAssets,blockedWriteAttempts:writes,checked:'legacy navigation, real plans, five settings tabs, directory browsing cancellation, desktop/tablet/mobile layout'},null,2));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
