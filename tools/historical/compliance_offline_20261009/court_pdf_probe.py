import json,time,random,pathlib
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from kad_documents import challenge,official_url,extract_pdf
root=pathlib.Path(__file__).resolve().parents[3] / 'data/compliance_offline_20261009'
executable='/Users/sophia/Library/Caches/ms-playwright/chromium-1208/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
candidates=json.loads((root/'document_candidates.json').read_text());results=[]
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,executable_path=executable)
 context=browser.new_context(locale='ru-RU',accept_downloads=True);Stealth().apply_stealth_sync(context);page=context.new_page()
 page.goto('https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F',wait_until='domcontentloaded',timeout=30000)
 for case in candidates:
  item={'case_id':case['record_id'],'case_number':case['data']['case_number'],'source_url':case['source_url']}
  try:
   time.sleep(random.uniform(5,10));response=page.goto(official_url(case['source_url']),wait_until='domcontentloaded',timeout=30000);time.sleep(5)
   html=page.content();path=root/'raw'/('older_card_'+case['record_id']+'.html');path.write_text(html)
   item.update(http_status=response.status if response else None,title=page.title(),input_file=str(path.resolve()),text_preview=page.locator('body').inner_text()[-4000:])
   item['links']=page.locator('a[href]').evaluate_all('(xs)=>xs.map(a=>({url:a.href,text:a.innerText})).filter(a=>/pdf|Document|Download/i.test(a.url))')
   item['status']='access_challenge' if challenge(html) else 'card_received'
   for link in item['links']:
    url=official_url(link['url'])
    if '.pdf' not in url.lower():continue
    time.sleep(random.uniform(5,10));download=context.request.get(url,timeout=20000)
    if not download.ok:raise ValueError('PDF HTTP '+str(download.status))
    content=download.body();record=extract_pdf(content,case['record_id'],url,root/'documents')
    item.setdefault('documents',[]).append({'pdf':record['file'],'sha256':record['sha256'],'pages':len(record['pages']),'text_status':record['text_status']})
    break
  except Exception as e:item.update(status='failed',error=str(e))
  results.append(item);(root/'court_pdf_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n');print(json.dumps(item,ensure_ascii=False)[:2100],flush=True)
 context.close();browser.close()
