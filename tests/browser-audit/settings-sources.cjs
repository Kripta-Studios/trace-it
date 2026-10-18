const {chromium,expect}=require('../../frontend/node_modules/@playwright/test')
const fs=require('node:fs'),path=require('node:path')
;(async()=>{
 const browser=await chromium.launch({headless:true}),page=await browser.newPage()
 const results=[]
 try{
  await page.goto('http://127.0.0.1:18174/nexia/trace-it/processes/1/definition/fuentes')
  await page.getByLabel('Fecha de corte').fill('2026-09-19')
  const uploaded=page.waitForResponse(r=>r.url().endsWith('/sources/workbook'))
  await page.locator('input[type=file]').first().setInputFiles(path.resolve('.context/500-sombras-de-alberto/FINAL_v7_DEFINITIVO_ahorasi.xlsx'))
  const r=await uploaded,body=await r.json()
  await expect(page.getByText(`${body.warnings.length} avisos al cargar el Excel`,{exact:true})).toBeVisible()
  await page.getByText('Ver detalles',{exact:true}).click()
  await expect(page.getByText('Falta un campo · Pedidos_2026!452 · supplier_tax_id',{exact:true})).toBeVisible()
  results.push({scenario:'real workbook warnings displayed',status:r.status(),count:body.warnings.length,sources:body.sources})
  if(process.env.TRACE_ERP_SYNC==='1'){
   const synced=page.waitForResponse(r=>r.url().endsWith('/sources/erp/sync'))
   await page.getByRole('button',{name:'Sincronizar',exact:true}).click()
   const response=await synced,data=await response.json()
   results.push({scenario:'positive ERP sync',status:response.status(),data})
   expect(response.status()).toBe(200)
   await expect(page.getByRole('cell',{name:'erp',exact:true})).toBeVisible()
  }
 }catch(error){process.exitCode=1;results.push({error:String(error)});await page.screenshot({path:'/tmp/trace-settings-sources-failure.png',fullPage:true})}
 finally{fs.writeFileSync('/tmp/trace-settings-sources.json',JSON.stringify(results,null,2));console.log(JSON.stringify(results));await browser.close()}
})()
