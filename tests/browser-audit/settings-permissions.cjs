const {chromium,expect}=require('../../frontend/node_modules/@playwright/test')
const fs=require('node:fs')
const base='http://127.0.0.1:18174/nexia/trace-it/'
;(async()=>{
 const browser=await chromium.launch({headless:true}),page=await browser.newPage()
 const results=[]
 try{
  const before=await (await page.request.get(`${base}api/processes/1/sources/erp`)).json()
  const keys=['entry_id','date','supplier_id','nif','purchase_order','amount','status']
  expect(before.rows).toBe(556)
  expect(before.data).toHaveLength(556)
  for(const row of before.data){
   expect(Object.keys(row).sort()).toEqual([...keys].sort())
   expect(row.date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
   expect(row.amount).toMatch(/^-?\d+(\.\d+)?$/)
  }
  results.push({scenario:'ERP field mapping matches configured contract',rows:before.rows,keys,sample:before.data[0],status:before.status,sourceId:before.id})
  await page.goto(`${base}settings?tab=identity`)
  await page.getByRole('button').filter({hasText:'mateo@trace-it.local'}).click()
  await expect(page.getByRole('button').filter({hasText:'mateo@trace-it.local'})).toHaveAttribute('aria-pressed','true')
  await page.goto(`${base}processes/1/definition/fuentes`)
  await page.waitForLoadState('networkidle')
  const logins=[];page.on('request',r=>{if(r.url().endsWith('/api/login'))logins.push(r.postDataJSON())})
  const rejected=page.waitForResponse(r=>r.url().endsWith('/sources/erp/sync'))
  await page.getByRole('button',{name:'Sincronizar',exact:true}).click()
  const response=await rejected
  expect(response.status()).toBe(403)
  await expect(page.getByText('Only a manager can do this',{exact:true})).toBeVisible()
  const after=await (await page.request.get(`${base}api/processes/1/sources/erp`)).json()
  expect(after.id).toBe(before.id)
  expect(logins).toEqual([])
  const selected=await page.evaluate(()=>sessionStorage.getItem('trace.identity'))
  expect(selected).toBe('mateo@trace-it.local')
  results.push({scenario:'operator sync cannot alter current ERP snapshot or selected identity',status:response.status(),sourceBefore:before.id,sourceAfter:after.id,selectedIdentity:selected,unexpectedLogins:logins})
 }catch(error){process.exitCode=1;results.push({error:String(error)})}
 finally{fs.writeFileSync('/tmp/trace-settings-permissions.json',JSON.stringify(results,null,2));console.log(JSON.stringify(results));await browser.close()}
})()
