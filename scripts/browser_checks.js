async (page) => {
  const checks = [], errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.goto('http://127.0.0.1:8044/projects/knowledgeops');
  await page.getByRole('button', {name:'Find a policy', exact:true}).waitFor();
  const run = async () => {
    await page.getByRole('button', {name:'Run the search', exact:false}).click();
    await page.waitForFunction(() => document.querySelector('#run-status').textContent.startsWith('Completed'), {timeout:15000});
  };
  await run();
  if (!(await page.locator('#result-content').innerText()).includes('EUR 140')) throw Error('Current policy missing');
  await page.locator('#citation-1 summary').click();
  if (!(await page.locator('#citation-1').innerText()).includes('source') && !(await page.locator('#citation-1').innerText()).includes('Source:')) throw Error('Citation not inspectable');
  checks.push('default cited answer and source expansion');
  await page.getByRole('button',{name:'Technical',exact:true}).click();
  if (!(await page.locator('#timeline').innerText()).includes('filter_applied')) throw Error('Technical trace missing');
  await page.getByRole('button',{name:'JSON',exact:true}).click();
  if (!(await page.locator('#json-trace').innerText()).includes('SANDBOX_DETERMINISTIC')) throw Error('JSON trace missing');
  await page.getByRole('button',{name:'Overview',exact:true}).click();
  checks.push('overview technical and JSON views');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('button',{name:'Download evidence'}).click();
  const download = await downloadPromise;
  if (!download.suggestedFilename().endsWith('.json')) throw Error('Evidence export failed');
  checks.push('evidence download');
  await page.getByRole('button',{name:'Replay this exact run'}).click();
  await page.waitForFunction(()=>document.querySelector('#run-status').textContent.startsWith('Completed'));
  if (!(await page.locator('#result-content').innerText()).includes('RECORDED REPLAY')) throw Error('Replay label wrong');
  checks.push('exact-input replay label');
  await page.getByRole('button',{name:'Test restricted access',exact:true}).click();
  await run();
  const denied = await page.locator('#result-content').innerText();
  if (!denied.includes('Not enough evidence') || denied.includes('420000') || denied.includes('reserve-plan')) throw Error('Permission boundary failed');
  await page.getByLabel('Demo access role').selectOption('finance');
  await run();
  if (!(await page.locator('#result-content').innerText()).includes('420000')) throw Error('Authorized finance answer missing');
  if (!(await page.locator('#comparison').innerText()).includes('changed')) throw Error('Comparison missing');
  checks.push('role edit changes permission outcome and comparison');
  await page.getByRole('button',{name:'Compare conflicting revisions',exact:true}).click(); await run();
  if (!(await page.locator('#result-content').innerText()).includes('sources disagree')) throw Error('Conflict failed');
  await page.getByLabel('Policy revision').selectOption('previous'); await run();
  if (!(await page.locator('#result-content').innerText()).includes('EUR 110')) throw Error('Historical failed');
  checks.push('conflict abstention and historical revision');
  for (const name of ['Find an incident runbook','Ask beyond the evidence','Test an injected instruction']) {
    await page.getByRole('button',{name,exact:true}).click(); await run();
    const answer = await page.locator('#result-content').innerText();
    if (name==='Find an incident runbook' ? !answer.includes('incident commander') : !answer.includes('Not enough evidence')) throw Error('Scenario failed: '+name);
  }
  checks.push('incident missing-evidence and injection scenarios');
  await page.setViewportSize({width:375,height:900});
  await page.getByLabel('What would you like to know?').fill('Keep this edited question');
  await page.getByRole('button',{name:'Menu',exact:true}).click();
  if ((await page.locator('#sidebar').getAttribute('aria-modal')) !== 'true') throw Error('Mobile dialog not modal');
  await page.keyboard.press('Escape');
  if (!(await page.getByLabel('What would you like to know?').inputValue()).includes('Keep this')) throw Error('Drawer lost state');
  if (await page.evaluate(()=>document.activeElement.id) !== 'menu') throw Error('Focus did not return');
  checks.push('mobile drawer Escape focus return and form preservation');
  await page.goto('http://127.0.0.1:8044/projects/knowledgeops');
  await page.route('**/events*', route => route.request().url().includes('format=json') ? route.continue() : route.abort());
  await run();
  if (!(await page.locator('#result-content').innerText()).includes('EUR 140')) throw Error('Polling fallback failed');
  checks.push('polling fallback after SSE transport failure');
  await page.unroute('**/events*');
  await page.locator('#try-it').scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/mobile-result.png'});
  const viewports=[];
  for (const width of [320,375,430,768,1280,1440]) {
    await page.setViewportSize({width,height:950});
    const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
    if(overflow) throw Error('Horizontal overflow '+width);
    viewports.push({width,overflow});
  }
  return {status:'PASS',checks,viewports,page_errors:errors};
}
