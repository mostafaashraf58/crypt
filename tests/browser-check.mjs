/* Start npm run dev first. Install dependencies with npm ci and npx playwright install chromium,
 * then run: node tests/browser-check.mjs. Set CHROMIUM_PATH for a system browser. */
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {mkdir} from 'node:fs/promises';
import path from 'node:path';
const require=createRequire(import.meta.url);
let chromium;
try { ({chromium}=await import('playwright')); }
catch { if(!process.env.PLAYWRIGHT_MODULE)throw new Error('Install playwright or set PLAYWRIGHT_MODULE to its module path'); ({chromium}=require(process.env.PLAYWRIGHT_MODULE)); }
const output=process.env.QA_OUTPUT_DIR||path.resolve('work/browser-tests');await mkdir(output,{recursive:true});
const browser=await chromium.launch({executablePath:process.env.CHROMIUM_PATH,headless:true,args:['--no-sandbox','--disable-dev-shm-usage']});
const context=await browser.newContext({viewport:{width:1440,height:1000},deviceScaleFactor:1});
const page=await context.newPage();const errors=[];const results=[];
page.on('pageerror',e=>errors.push(e.message));
const assertTrue=(value,message)=>{assert.ok(value,message);results.push(message);console.log('PASS',message);};
const go=async name=>{await page.getByRole('button',{name,exact:true}).first().click();await page.getByRole('heading',{name,exact:true,level:1}).waitFor();};
const dialog=()=>page.getByRole('dialog');
const close=async()=>{await dialog().getByRole('button',{name:'Close dialog'}).click();};
const toast=async pattern=>{await page.getByRole('status').filter({hasText:pattern}).waitFor();};
try {
 await page.goto(process.env.AXIOM_URL||'http://127.0.0.1:1420');await page.getByRole('heading',{name:'Overview',exact:true}).waitFor();
 assertTrue(await page.getByText('OFFLINE DEMO',{exact:true}).isVisible(),'Overview clearly identifies offline demo');
 assertTrue(await page.locator('canvas').count()>0,'Candlestick chart renders canvas');
 assertTrue(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth),'Desktop viewport has no horizontal page overflow');
 await page.screenshot({path:path.join(output,'axiom-desktop.png'),fullPage:true});
 for(const n of ['Markets','Portfolio','Orders','AI Models','Strategies','Backtesting','Connections','Risk & Controls','Settings','Overview'])await go(n);
 assertTrue(true,'All ten navigation screens render');
 await page.keyboard.press('Control+k');await dialog().waitFor();await dialog().getByRole('textbox',{name:'Search assets'}).fill('Tesla');
 await dialog().getByRole('button').filter({hasText:'TSLA'}).click();
 assertTrue((await page.locator('.asset-heading').textContent()).includes('TSLA'),'Global asset search selects Tesla in chart');
 await page.getByRole('button',{name:'4h',exact:true}).click();
 assertTrue(await page.getByRole('button',{name:'4h',exact:true}).getAttribute('class')==='selected','Interval selection updates');
 await page.getByRole('button',{name:'Indicators',exact:true}).click();
 assertTrue(await page.getByRole('button',{name:'Indicators · SMA 20',exact:true}).isVisible(),'SMA indicator toggles on');
 await page.keyboard.press('Control+k');await dialog().waitFor();await page.keyboard.press('Escape');
 assertTrue(await dialog().count()===0,'Escape closes modal');
 await go('Markets');await page.getByRole('textbox',{name:'Search markets'}).fill('Ethereum');
 const explorer=page.locator('.panel').filter({has:page.getByRole('heading',{name:'Market explorer'})});
 assertTrue(await explorer.locator('tbody tr').count()===1,'Market explorer search filters rows');
 await page.getByRole('textbox',{name:'Search markets'}).fill('');
 await page.getByRole('button',{name:'New trade',exact:true}).click();
 await dialog().locator('select[name="symbol"]').selectOption('BTC/USD');await dialog().locator('input[name="qty"]').fill('0.001');
 await dialog().getByRole('button',{name:'Place paper order',exact:true}).click();await toast('Paper order filled');await go('Orders');
 assertTrue(await page.locator('tbody tr').count()===1,'Paper buy appears in order history');
 assertTrue((await page.locator('tbody tr').first().textContent()).includes('BUY'),'Paper order records buy direction');
 await page.getByRole('button',{name:'New trade',exact:true}).click();await dialog().locator('select[name="symbol"]').selectOption('BTC/USD');await dialog().locator('select[name="side"]').selectOption('sell');await dialog().locator('input[name="qty"]').fill('0.001');await dialog().getByRole('button',{name:'Place paper order',exact:true}).click();await toast('Paper order filled');
 assertTrue(await page.locator('tbody tr').count()===2,'Paper sell adds a filled order');
 await page.getByRole('button',{name:'New trade',exact:true}).click();await dialog().locator('select[name="symbol"]').selectOption('BTC/USD');await dialog().locator('select[name="side"]').selectOption('sell');await dialog().locator('input[name="qty"]').fill('100');await dialog().getByRole('button',{name:'Place paper order',exact:true}).click();await toast(/position|quantity|held|enough|exceeds/i);
 assertTrue(await dialog().isVisible(),'Overselling is rejected and leaves form open');await close();
 await go('Strategies');await page.getByRole('button',{name:'Edit strategy',exact:true}).first().click();await dialog().locator('input[name="threshold"]').fill('70');await dialog().getByRole('button',{name:'Save strategy',exact:true}).click();await toast('Strategy saved');
 assertTrue((await page.locator('.section-grid .panel').first().textContent()).includes('70%'),'Strategy entry threshold persists');
 await go('AI Models');await page.getByRole('button',{name:'Add model',exact:true}).click();await dialog().locator('input[name="name"]').fill('QA deterministic model');await dialog().locator('input[name="endpoint"]').fill('mock://qa');await dialog().getByRole('button',{name:'Save endpoint',exact:true}).click();await toast('Model endpoint saved');
 const model=page.locator('.section-grid .panel').filter({has:page.getByRole('heading',{name:'QA deterministic model',exact:true})});
 assertTrue(await model.isVisible(),'Model registration adds a registry card');await model.getByRole('button',{name:'Health check',exact:true}).click();await toast('Deterministic mock model is ready');assertTrue(true,'Deterministic mock health check works');
 await model.getByRole('button',{name:'Remove QA deterministic model',exact:true}).click();await toast('Model removed');
 assertTrue(await model.count()===0,'Unassigned model can be removed');
 const http=page.locator('.section-grid .panel').filter({has:page.getByRole('heading',{name:'Local LSTM',exact:true})});await http.getByRole('button',{name:'Health check',exact:true}).click();await toast(/local desktop engine/);
 assertTrue(true,'HTTP model health is not falsely reported healthy offline');
 await go('Backtesting');await page.getByRole('button',{name:'Run backtest',exact:true}).click();await page.getByRole('heading',{name:'Backtest equity curve',exact:true}).waitFor();
 assertTrue(await page.getByText('OFFLINE RESULT',{exact:true}).isVisible(),'Backtest result identifies offline source');
 assertTrue(await page.locator('.equity-svg').count()===2,'Backtest produces equity and drawdown charts');
 assertTrue(await page.locator('tbody tr').count()>0,'Backtest produces simulated trades');
 await go('Connections');await page.getByRole('button',{name:'Connect account',exact:true}).first().click();
 assertTrue(await dialog().locator('input[name="key"]').isDisabled(),'Browser does not accept broker credentials');await close();
 await go('Risk & Controls');await page.getByRole('spinbutton',{name:'Maximum allocation percent'}).fill('24');await page.getByRole('button',{name:'Save limits',exact:true}).click();await toast('Risk limits saved');await go('Settings');await go('Risk & Controls');
 assertTrue(await page.getByRole('spinbutton',{name:'Maximum allocation percent'}).inputValue()==='24','Risk limit save survives navigation');
 await page.getByRole('button',{name:'Live',exact:true}).click();await go('Orders');await page.getByRole('button',{name:'New trade',exact:true}).click();await dialog().locator('input[name="qty"]').fill('0.001');await dialog().getByRole('button',{name:'Create proposal',exact:true}).click();await toast('Order proposal created');await page.getByRole('button',{name:'Review',exact:true}).click();
 assertTrue(await dialog().getByRole('button',{name:'Confirm & submit order',exact:true}).isDisabled(),'Offline live proposal cannot submit real order');await close();
 await page.getByRole('button',{name:'Cancel proposal',exact:true}).click();await toast('Proposal cancelled');assertTrue(true,'Live proposal can be cancelled');
 await page.getByRole('button',{name:'Paper',exact:true}).click();await go('Overview');await page.getByRole('button',{name:'Toggle color theme',exact:true}).click();
 assertTrue(await page.locator('.app.light').count()===1,'Light theme activates');await page.screenshot({path:path.join(output,'axiom-light.png'),fullPage:true});await page.getByRole('button',{name:'Toggle color theme',exact:true}).click();
 await page.getByRole('button',{name:'Start paper engine',exact:true}).click();await toast('Paper fixture replay started');await page.getByRole('button',{name:'Pause engine',exact:true}).waitFor();await page.getByRole('button',{name:'Pause engine',exact:true}).click();await toast('Strategies paused');assertTrue(true,'Paper engine starts and pauses');
 await page.setViewportSize({width:390,height:844});await page.waitForTimeout(300);assertTrue(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth),'Mobile390px has no horizontal page overflow');await page.screenshot({path:path.join(output,'axiom-mobile.png'),fullPage:true});
 assertTrue(errors.length===0,`No browser runtime errors (${errors.length})`);
 console.log(JSON.stringify({checks:results.length,results,errors},null,2));
 // Save a clean mobile fixture screen after the mutation tests.
 await page.evaluate(()=>localStorage.clear());await page.reload();await page.getByRole('heading',{name:'Overview',exact:true}).waitFor();await page.evaluate(()=>window.scrollTo(0,0));await page.waitForTimeout(200);await page.screenshot({path:path.join(output,'axiom-mobile.png'),fullPage:true});
} finally {await context.close();await browser.close();}
