# Налаштування та довідники

| Файли | Призначення |
|---|---|
| `collection_plan.json`, `c4ads_collection_plan.json`, `trade_scan_plan.json` | Обсяг, періоди та запити збору |
| `local_trade_pipeline.json` | Шляхи для локального імпорту митних даних; відносні шляхи рахуються від цієї папки |
| `facility_names.json`, `russian_defence_entities.json` | Назви й ідентифікатори для пошуку / зіставлення; сам довідник не є доказом поставки |
| `social_accounts.json`, `telegram_channels*.txt` | Облікові записи та канали збору |
| `research_methodology.json` | Правила та обмеження розрахунку метрик |
| `radar_buildings.geojson` | Геометрія для радарних прикладів; перевіряйте походження |
| `private/proxies.txt` | Приватний список маршрутів; вміст не включено до документації |

Ключі API й сесійні налаштування залишаються в `.env` у корені.
