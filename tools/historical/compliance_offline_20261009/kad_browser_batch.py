import json,time,random,pathlib,hashlib
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from parsers.registry_sources import KadParser
from kad_documents import challenge,extract_pdf,official_url
root=pathlib.Path(__file__).resolve().parents[3] / 'data/compliance_offline_20261009'
executable='/Users/sophia/Library/Caches/ms-playwright/chromium-1208/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
plan=json.loads(pathlib.Path('data/compliance_text_20261009/microbatch.json').read_text())
results=[];cases=[];document_checks=[]

def save():
 (root/'browser_microbatch_results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n')
 (root/'browser_cases.jsonl').write_text(''.join(json.dumps(c,ensure_ascii=False)+'\n' for c in cases))
 (root/'browser_document_checks.json').write_text(json.dumps(document_checks,ensure_ascii=False,indent=2)+'\n')

with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,executable_path=executable)
 context=browser.new_context(locale='ru-RU',accept_downloads=True)
 Stealth().apply_stealth_sync(context)
 page=context.new_page()
 page.goto('https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F',wait_until='domcontentloaded',timeout=30000)
 for target in plan['targets']:
  item={'inn':target['inn'],'names':target['names'],'period_from':'2022-01-01','period_to':'2026-10-08'}
  try:
   page.get_by_placeholder('название, ИНН или ОГРН',exact=True).fill(target['inn'])
   dates=page.get_by_placeholder('дд.мм.гггг',exact=True)
   dates.nth(0).fill('01.01.2022');dates.nth(1).fill('08.10.2026');dates.nth(1).press('Tab')
   time.sleep(random.uniform(5,10))
   close=page.locator('.js-promo_notification-popup-close')
   if close.is_visible():close.click(timeout=8000)
   page.locator('h2.b-part-header_member').click(timeout=8000)
   with page.expect_response(lambda r:'/Kad/SearchInstances' in r.url,timeout=20000) as pending:
    page.get_by_role('button',name='Найти',exact=True).click(timeout=8000)
   response=pending.value;body=response.body();path=root/'raw'/('browser_search_'+target['inn']+'.html');path.write_bytes(body)
   item.update(http_status=response.status,input_file=str(path.resolve()),input_sha256=hashlib.sha256(body).hexdigest(),request_payload=response.request.post_data_json)
   if response.status!=200 or challenge(body.decode('utf-8')):
    item['status']='access_blocked';results.append(item);save();break
   payload=item['request_payload']
   if target['inn'] not in [x.get('Name') for x in payload.get('Sides',[])]:raise ValueError('Search INN not in submitted request')
   parsed=KadParser().parse(body.decode('utf-8'),'html',response.url)
   if parsed.warnings:raise ValueError(str(parsed.warnings))
   for row in parsed.records:
    row.input_file=str(path.resolve());row.input_sha256=item['input_sha256']
    row.data.update(search_participant_inn=target['inn'],search_period_from='2022-01-01',search_period_to='2026-10-08')
    row.warnings.append('Search participation does not by itself establish buyer/supplier roles or breach.')
    cases.append(json.loads(row.model_dump_json()))
   total=BeautifulSoup(body,'html.parser').select_one('#documentsTotalCount')
   item.update(status='first_page_collected',page_records=len(parsed.records),reported_total=total.get('value') if total else None,pages_collected=1)
   results.append(item);save();print(json.dumps({k:item.get(k) for k in ['inn','status','page_records','reported_total']},ensure_ascii=False),flush=True)
  except Exception as e:
   item.update(status='failed',error=str(e));results.append(item);save();print(json.dumps(item,ensure_ascii=False)[:700],flush=True);break
 # Probe a card from the first successful search; retain browser cookies.
 if cases:
  card=context.new_page();case=cases[0];item={'case_id':case['record_id'],'source_url':case['source_url']}
  try:
   time.sleep(random.uniform(5,10));response=card.goto(official_url(case['source_url']),wait_until='domcontentloaded',timeout=30000)
   time.sleep(5);html=card.content();path=root/'raw'/('browser_card_'+case['record_id']+'.html');path.write_text(html)
   item.update(http_status=response.status if response else None,title=card.title(),input_file=str(path.resolve()),text_preview=card.locator('body').inner_text()[:800])
   item['links']=card.locator('a[href]').evaluate_all('(xs)=>xs.map(a=>({url:a.href,text:a.innerText})).filter(a=>/pdf|Document|Download/.test(a.url))')
   item['status']='access_challenge' if challenge(html) else 'card_received'
  except Exception as e:item.update(status='failed',error=str(e))
  document_checks.append(item);save();print(json.dumps(item,ensure_ascii=False)[:1300],flush=True)
 context.close();browser.close()
