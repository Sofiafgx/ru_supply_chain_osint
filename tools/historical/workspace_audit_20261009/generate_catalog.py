import ast, json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3]
OUT=ROOT/'data/workspace_audit_20261009'
code=json.loads((OUT/'code_inventory.json').read_text())
imports={}
for p in ROOT.glob('tests/*.py'):
    for n in ast.walk(ast.parse(p.read_text())):
        if isinstance(n,ast.ImportFrom) and n.module:imports.setdefault(n.module,set()).add(p.name)
roles={
 'main.py':'Запуск CLI джерел', 'config.py':'Завантаження конфігурації та старого proxy pool',
 'sources_cli.py':'Локальний/мережевий запуск парсерів і збереження результатів',
 'collection_cli.py':'Черга збору, архів джерел, пагінація контрактів, PDF, експорти',
 'collection_audit.py':'Пагінація, часові вікна, хеші, цілісність SQLite',
 'coverage_extension.py':'Імпорт додаткових локальних матеріалів і походження',
 'social_cli.py':'API-черга соціальної історії', 'social_collection.py':'Архів соціальних дописів, ідентичність, курсори, дедуплікація',
 'social_browser_import.py':'Валідація і транзакційний імпорт браузерних пакетів',
 'telegram_cli.py':'MTProto CLI; наявність коду не означає зібраний API-корпус',
 'telegram_public_cli.py':'Збір вебпревʼю Telegram', 'telegram_structure_cli.py':'Запуск структурованого витягу',
 'telegram_filter_cli.py':'Застосування довідника й NLP-фільтра та експорт',
 'trade_interval_cli.py':'Явна нормалізація митних полів, фільтри, інтервали',
 'local_trade_pipeline.py':'Локальні формати, словник роботодавців і кандидатні зіставлення',
 'kad_documents.py':'Черги пошуку/карток, статичні PDF-посилання, текстові кандидати',
 'kad_microbatch.py':'Обмежений HTTP-мікробатч КАД; не повна пагінація',
 'offline_contracts.py':'Похідний SQLite-індекс нормалізованого локального архіву',
 'supplier_enrichment.py':'Перевірка пропусків постачальника; назви лишаються кандидатами',
 'supplier_text_extraction.py':'Позначені ІПН і контекст текстів; не автоматичне встановлення ролей',
 'industrial_cli.py':'CLI оголошень, доказів і доступу',
 'industrial_followup_build.py':'Обʼєднання пілоту з повторним витягом; встановлено дефект двох хешів',
 'proxy_manager.py':'Старий транспорт; TLS вимкнено, адреси проксі друкуються',
 'analytics/proxy_model.py':'Старі припущення та граф; непридатний як модуль фактичних показників',
 'analytics/telegram_structure.py':'Правила згадок, дат і кандидатів подій',
 'analytics/telegram_relevance.py':'Довідник, NLP-звʼязки й обмеження релевантності',
 'parsers/bom_parser.py':'Заданий список компонентів; прямий вебвитяг не реалізований',
 'parsers/satellite_parser.py':'Неперевірений каталог/термальні події; дефект використання Asset',
 'parsers/registry_sources.py':'Checko, ЄІС, ClearSpending, КАД',
 'parsers/trade_sources.py':'Видимі таблиці та локальні торговельні формати',
 'parsers/customs_parser.py':'Сумісне перетворення тільки повних записів на TradeEdge',
 'parsers/procurement_parser.py':'Сумісний інтерфейс контрактів до TenderRecord',
 'parsers/industrial_labor.py':'Дати, бренди, моделі, відсікання рекомендацій; слабкі activity-мітки',
 'parsers/industrial_transport.py':'Новий транспорт із TLS і редагуванням секретів у журналі',
 'parsers/source_common.py':'Числа, дати, ІПН, HTML/таблиці й транспорт',
 'parsers/c4ads_parser.py':'Каталог/детальні публікації та PDF-посилання',
 'parsers/social_api.py':'API-відповіді, час, доступ, безпечні курсори',
 'parsers/telegram_parser.py':'Telegram API, курсори, медіа, архів',
 'parsers/telegram_public_parser.py':'Вебпревʼю, межі видимої історії, відновлення збору',
 'parsers/telegram_comments_parser.py':'API-коментарі, окремі курсори; фактичного коментарного корпусу не знайдено',
 'models/source_records.py':'Нормалізоване походження й результат парсингу',
 'models/schemas.py':'Старі схеми компаній, ребер, контрактів і BOM',
}
lines=['# Карта перевірки кожного Python-файлу','',
 'Для кожного наявного файлу прочитано текст і AST, перевірено синтаксис. Детальні висновки за основними модулями наведені в REPORT_UK.md. Наявність тестового імпорту — індикатор перевірок модуля, а не вимірювання покриття рядків; відсутність прямого імпорту не виключає непрямих перевірок.','',
 '| Файл | Рядків | Роль / межа | Прямі тестові імпорти |','|---|---:|---|---|']
for x in sorted(code,key=lambda x:x['path']):
    p=x['path'];module=p[:-3].replace('/','.')
    role=roles.get(p,'Тестові приклади і перевірки' if p.startswith('tests/') else 'Порожній пакет' if p.endswith('__init__.py') else 'Допоміжний скрипт обмеженого збору; переносність/повнота не доведена')
    lines.append(f'| [{p}]({ROOT/p}) | {x["lines"]} | {role} | '+', '.join(sorted(imports.get(module,set())))+' |')
lines+=['','## Допоміжні файли','',
 'SQL-міграції і роль у `db 2` описують схему та права, але не підтверджують локальної наявності таблиць PostgreSQL. JSON-конфігурації, схеми та списки джерел задають обсяг або формат; їх не враховано як уже зібрані операції. README/WORK_PLAN описують можливості та плани; фактичний результат перевірено за базами, файлами й журналами. XLSX є копією соціального експорту. Детальні CSV/JSON-каталоги містять кожен перевірений файл відповідного формату.','',
 'Браузерні скрипти у `data/compliance_offline_20261009/` читаються статично: їх не імпортували і не запускали, оскільки вони одразу виконують мережеві дії. Збережені результати браузерного збору перевірено окремо.']
(OUT/'CODE_REVIEW_UK.md').write_text('\n'.join(lines)+'\n')
files=json.loads((OUT/'file_inventory.json').read_text())
catalog=['# Реєстр усіх наявних файлів','',
 'Кожен рядок описує один фізичний файл; це не число незалежних доказів. SHA-256, структура CSV/JSON/XLSX і баз наведені у відповідних JSON. Секретні файли враховані лише за метаданими.','',
 '| Файл | Байти | Тип / роль |','|---|---:|---|---|']
for x in sorted(files,key=lambda x:x['path']):
    p=x['path'];suffix=Path(p).suffix
    role='Секретна конфігурація / маршрути; вміст не аналізовано' if Path(p).name in ('.env','proxies.txt') else 'Сирий знімок джерела / додатковий доказ' if '/raw/' in p else 'Контрольний приклад; не виробничий доказ' if p.startswith(('tests/','data/verification/','data/site_samples/','examples/')) else 'База даних' if suffix in ('.sqlite','.sqlite3') else 'Службовий стан SQLite' if p.endswith(('-wal','-shm')) else 'Код' if suffix in ('.py','.mjs','.sql') else 'Звіт / інструкції' if suffix=='.md' else 'Дані / схема / конфігурація / експорт'
    catalog.append(f'| [{p}]({ROOT/p}) | {x["bytes"]} | {role} |')
(OUT/'FILES_UK.md').write_text('\n'.join(catalog)+'\n')
print({'python_files':len(code),'files':len(files)})
