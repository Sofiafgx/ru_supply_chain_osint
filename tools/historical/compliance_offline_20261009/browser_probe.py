import json,time,random,pathlib
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
root=pathlib.Path(__file__).resolve().parents[3] / 'data/compliance_offline_20261009'
executable='/Users/sophia/Library/Caches/ms-playwright/chromium-1208/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
results=[]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,executable_path=executable)
 context=browser.new_context(locale='ru-RU',accept_downloads=True)
 Stealth().apply_stealth_sync(context)
 page=context.new_page()
 for name,url in [('kad','https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F'),('opendata','https://clearspending.ru/opendata/')]:
  item={'name':name,'url':url}
  try:
   response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
   time.sleep(random.uniform(5,10))
   item.update(http_status=response.status if response else None,final_url=page.url,title=page.title())
   (root/'raw'/f'browser_{name}.html').write_text(page.content())
   page.screenshot(path=str(root/'raw'/f'browser_{name}.png'),full_page=False)
   item['text_preview']=page.locator('body').inner_text()[:1400]
   item['links']=page.locator('a[href]').evaluate_all('(links)=>links.map(a=>({url:a.href,text:a.innerText})).filter(a=>/zip|json|gz|2024/.test(a.url)||/июн|данн/.test(a.text))')
   if name=='kad':item['inputs']=page.locator('input').evaluate_all('(xs)=>xs.map(x=>({id:x.id,name:x.name,placeholder:x.placeholder,type:x.type}))')
  except Exception as error:item['error']=str(error)
  results.append(item);print(json.dumps(item,ensure_ascii=False)[:2500],flush=True)
 context.close();browser.close()
(root/'browser_access.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
