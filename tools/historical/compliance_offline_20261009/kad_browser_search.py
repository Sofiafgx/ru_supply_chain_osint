import json,time,random,pathlib
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
from parsers.registry_sources import KadParser
root=pathlib.Path(__file__).resolve().parents[3] / 'data/compliance_offline_20261009'
executable='/Users/sophia/Library/Caches/ms-playwright/chromium-1208/chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing'
result={'transport':'Playwright-Stealth','inn':'1646019914','period_from':'2022-01-01','period_to':'2026-10-08'}
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True,executable_path=executable)
 context=browser.new_context(locale='ru-RU')
 Stealth().apply_stealth_sync(context)
 page=context.new_page()
 try:
  page.goto('https://kad.arbitr.ru/Version/Change?mode=Full&returnUrl=%2F',wait_until='domcontentloaded',timeout=30000)
  result['initial_title']=page.title()
  result['initial_text']=page.locator('body').inner_text()[:600]
  page.get_by_placeholder('название, ИНН или ОГРН',exact=True).fill(result['inn'])
  dates=page.get_by_placeholder('дд.мм.гггг',exact=True)
  dates.nth(0).fill('01.01.2022');dates.nth(1).fill('08.10.2026')
  dates.nth(1).press('Tab')
  time.sleep(random.uniform(5,10))
  close=page.locator('.js-promo_notification-popup-close')
  if close.is_visible():close.click(timeout=8000)
  page.locator('h2.b-part-header_member').click(timeout=8000)
  with page.expect_response(lambda r:'/Kad/SearchInstances' in r.url,timeout=20000) as pending:
   page.get_by_role('button',name='Найти',exact=True).click(timeout=8000)
  response=pending.value
  body=response.body();(root/'raw'/'kad_browser_search.html').write_bytes(body)
  result['http_status']=response.status
  parsed=KadParser().parse(body.decode('utf-8'),'html',response.url) if response.status==200 else None
  result['status']='access_blocked' if response.status in (401,403,429,451,503) else 'search_response_received'
  if parsed:
   result['warnings']=parsed.warnings
   result['cases']=[json.loads(r.model_dump_json()) for r in parsed.records]
   if parsed.warnings:result['status']='unrecognized_response'
 except Exception as e:result['status']='browser_search_failed';result['error']=str(e)
 finally:
  (root/'raw'/'kad_browser_after_search.html').write_text(page.content())
  result['final_title']=page.title();result['final_text']=page.locator('body').inner_text()[:1600]
 context.close();browser.close()
(root/'kad_browser_search.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(result,ensure_ascii=False)[:2400])
