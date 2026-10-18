// Real browser audit of an isolated stack. No production origin is allowed.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('../../frontend/node_modules/@playwright/test');
const base = process.env.AUDIT_BASE || 'http://127.0.0.1:18174/nexia/trace-it';
if (!['127.0.0.1', 'localhost'].includes(new URL(base).hostname)) throw new Error('Local audit only');
const artifacts = process.env.AUDIT_ARTIFACTS || '/home/kripta/trace-it-deep-evidence-20261018/execution';
const stage = process.argv[2] || 'inspect';
const statePath = path.join(artifacts, 'state.json');
const state = fs.existsSync(statePath) ? JSON.parse(fs.readFileSync(statePath)) : {};

(async () => {
  const browser = await chromium.launch({headless:true});
  const context = await browser.newContext({viewport:{width:1440,height:1100}});
  const page = await context.newPage();
  page.setDefaultTimeout(25000);
  const evidence = {stage, started:new Date().toISOString(), errors:[], requests:[]};
  const responseBodies=[];
  page.on('download',download=>{
    evidence.downloads ||= [];
    evidence.downloads.push(download.suggestedFilename());
  });
  page.on('request', request => {
    if(request.method()==='POST') console.log('POST',new URL(request.url()).pathname);
  });
  page.on('pageerror', error => evidence.errors.push(error.message));
  page.on('response', response => {
    if (response.url().includes('/api/')) evidence.requests.push({method:response.request().method(),url:new URL(response.url()).pathname,status:response.status()});
    if(response.url().endsWith(`/processes/${state.processId}/files`)&&response.request().method()==='POST') {
      responseBodies.push(response.json().then(body=>{
        evidence.uploads ||= [];
        evidence.uploads.push({status:response.status(),body});
        if(body.instance_id){state.instances ||= {};state.instances[body.name]=body.instance_id;}
      }).catch(()=>{}));
    }
  });
  const visit = async route => {await page.goto(base+route); await page.waitForTimeout(1500)};
  try {
    if(stage === 'validation') {
      const original=JSON.parse(fs.readFileSync(path.join(artifacts,'process.json'),'utf8'));
      evidence.cases=[];
      for(const kind of ['blank-process','whitespace-process','blank-symbol','blank-outcome']) {
        const definition=structuredClone(original);
        definition.name=`Validation ${kind} ${Date.now()}`;
        definition.rules=[];
        if(kind==='blank-process') definition.name='';
        if(kind==='whitespace-process') definition.name='   ';
        if(kind==='blank-symbol') definition.symbols[0].name='';
        if(kind==='blank-outcome') definition.decision_types[0].name='';
        await visit('/processes/new');
        await page.getByRole('button',{name:'Importar JSON',exact:true}).click();
        await page.locator('textarea').fill(JSON.stringify(definition));
        const completed=page.waitForResponse(response=>response.url().endsWith('/processes/definition')&&response.request().method()==='POST');
        await page.getByRole('button',{name:'Cargar el proceso',exact:true}).click();
        const response=await completed;
        evidence.cases.push({kind,status:response.status(),body:await response.json()});
        await page.waitForTimeout(800);
      }
    } else if(stage === 'create') {
      await visit('/processes/new');
      await page.getByRole('button',{name:'Importar JSON',exact:true}).click();
      await page.locator('textarea').fill(fs.readFileSync(path.join(artifacts,'process.json'),'utf8'));
      const created = page.waitForResponse(response=>response.url().endsWith('/processes/definition')&&response.request().method()==='POST');
      await page.getByRole('button',{name:'Cargar el proceso',exact:true}).click();
      const response = await created;
      evidence.created = {status:response.status(),body:await response.json()};
      if(response.ok()) state.processId = evidence.created.body.process.id;
      await page.waitForTimeout(1500);
    } else if(stage === 'compile' || stage==='activate') {
      await visit(`/processes/${state.processId}/definition/manual`);
      const hrefs = await page.locator(`a[href*="/processes/${state.processId}/rules/"]`).evaluateAll(links=>links.map(link=>link.getAttribute('href')));
      state.ruleIds = [...new Set(hrefs.map(href=>Number(href.split('/').at(-1))))];
      evidence.rules=[];
      for(const id of state.ruleIds) {
        await visit(`/processes/${state.processId}/rules/${id}`);
        const button=page.getByRole('button',{name:'Compilar',exact:true});
        if(stage==='compile' && await button.count() && await button.isEnabled()) {
          const completed=page.waitForResponse(response=>response.url().endsWith(`/rules/${id}/compile`)&&response.request().method()==='POST',{timeout:300000});
          await button.click();
          const response=await completed;
          evidence.rules.push({id,status:response.status(),body:await response.json()});
          await page.waitForTimeout(1000);
        }
        const add=page.getByRole('button',{name:'Añadir a la versión',exact:true});
        if(await add.isEnabled()) {
          await add.click();
          await page.waitForTimeout(1500);
        }
      }
    } else if(stage==='publish') {
      await visit(`/processes/${state.processId}/panel?publicar=1`);
      await page.getByRole('button',{name:'Validate and backtest',exact:true}).click();
      await page.waitForTimeout(1500);
      const publish=page.getByRole('button',{name:'Publicar',exact:true});
      evidence.publishButtons=await publish.count();
      if(await publish.last().isEnabled()) {
        await publish.last().click();
        await page.waitForTimeout(1500);
      }
    } else if(stage==='document') {
      await visit(`/processes/${state.processId}/instances?i=${state.instances['07-html.html']}`);
      await page.getByRole('button',{name:'Abrir documento',exact:true}).click();
      await page.waitForTimeout(1500);
      evidence.frames=await page.locator('iframe').evaluateAll(nodes=>nodes.map(node=>({src:node.src,title:node.title})));
      evidence.dialogText=await page.getByRole('dialog').innerText();
    } else if(stage==='resolve') {
      await visit(`/processes/${state.processId}`);
      await page.getByRole('button').filter({hasText:'02-high.pdf'}).click();
      await page.locator('textarea').fill('Synthetic audit: approved exceptional airport travel expense after receipt review.');
      await page.getByRole('button',{name:/^Approve/i}).click();
      const done=page.waitForResponse(response=>response.url().endsWith(`/instances/${state.instances['02-high.pdf']}/resolve`)&&response.request().method()==='POST');
      await page.getByRole('button',{name:/^Approve/i}).click();
      const response=await done;
      evidence.resolution={status:response.status(),body:await response.json()};
      await page.waitForTimeout(1200);
    } else if(stage==='export') {
      await visit(`/processes/${state.processId}/instances`);
      const done=page.waitForResponse(response=>response.url().includes(`/processes/${state.processId}/export`));
      await page.getByRole('button',{name:'Exportar',exact:true}).click();
      const response=await done;
      evidence.export={status:response.status(),text:await response.text()};
      await page.waitForTimeout(1200);
    } else if(stage==='upload') {
      await visit(`/processes/${state.processId}`);
      const files=(process.argv[3]||'01-approved.pdf,02-high.pdf').split(',');
      evidence.files=files;
      const completed=page.waitForResponse(response=>response.request().method()==='POST'&&(
        response.url().endsWith(`/processes/${state.processId}/run`)||
        response.url().endsWith(`/processes/${state.processId}/files`)&&!response.ok()
      ),{timeout:300000});
      await page.locator('input[type=file]').first().setInputFiles(files.map(file=>path.join(artifacts,file)));
      const response=await completed;
      evidence.run={status:response.status(),body:await response.json()};
      await page.waitForTimeout(2500);
    } else {
      await visit(process.argv[3] || `/processes/${state.processId || ''}`);
    }
    evidence.url=page.url();
    evidence.body=await page.locator('body').innerText();
    evidence.inputs=await page.locator('input,textarea,button,select').evaluateAll(nodes=>nodes.map(node=>({tag:node.tagName,type:node.type,text:node.innerText,placeholder:node.getAttribute('placeholder'),aria:node.getAttribute('aria-label'),disabled:node.disabled})).filter(node=>node.tag!=='BUTTON'||node.text&&!/^\d+$/.test(node.text)));
    await page.screenshot({path:path.join(artifacts,`${stage}.png`),fullPage:true});
  } catch(error) {
    evidence.failure=error.message;
    evidence.body=await page.locator('body').innerText().catch(()=> '');
    await page.screenshot({path:path.join(artifacts,`${stage}-failure.png`),fullPage:true}).catch(()=>{});
    process.exitCode=1;
  } finally {
    await Promise.all(responseBodies);
    fs.writeFileSync(statePath,JSON.stringify(state,null,2));
    fs.writeFileSync(path.join(artifacts,`${stage}-result.json`),JSON.stringify(evidence,null,2));
    fs.appendFileSync(path.join(artifacts,'execution-log.jsonl'),JSON.stringify(evidence)+'\n');
    console.log(JSON.stringify({...evidence,
      requests:evidence.requests.filter(request=>request.method!=='GET'||request.status>=400),
      body:evidence.body?.slice(0,6500),
      rules:evidence.rules?.map(rule=>({id:rule.id,status:rule.status,valid:rule.body?.report?.valid})),
      uploads:evidence.uploads?.map(upload=>({status:upload.status,id:upload.body.instance_id,name:upload.body.name,created:upload.body.created,symbols:upload.body.symbols,error:upload.body.message})),
      cases:evidence.cases?.map(item=>({kind:item.kind,status:item.status,id:item.body?.process?.id,error:item.body?.detail||item.body?.message})),
    },null,2));
    await browser.close();
  }
})();
