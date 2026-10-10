# Джерела та межі датасету

Перевірено 10.10.2026. Датасет містить **8 записів блоків та 49 компонентних записів**. У всіх `units_per_system: null` і `requires_manual_verification: true`: джерела не встановлюють однозначної норми r_kj на один завершений блок. Один рядок каталогу, фото або число каналів мікросхеми не є кількістю чіпів.

`part_number` — повне доступне опубліковане корпусне маркування, включно з lot/date-кодами, якщо вони наведені. Це не завжди повний каталожний SKU: відсутні суфікси не домислено. `STM32F411CE`, `Vortex86DX`, `904 3701`, `907e3 963A25`, `0721 2145 TJY` і `193A3 GZ325` потребують зіставлення з каталожними артикулами; клас і вендор останніх чотирьох відтворено за атрибуцією War&Sanctions. Функціональні класи збережено фактичними, зокрема пам’ять, CPLD, мультиплексор, генератор тактових сигналів і регулятор живлення.

`original_vendor` означає історичного розробника, а не автоматично сучасного власника марки. `headquarters_country` використано відповідно до запиту як країну реєстрації вендора; країна складання на корпусі не визначає це поле. Для STMicroelectronics N.V. це Нідерланди: [офіційний FAQ](https://investors.st.com/resources/faqs).

## CHPL

Перевірений [офіційний перелік FCDO](https://www.gov.uk/government/publications/russia-sanctions-common-high-priority-items-list/russia-sanctions-common-high-priority-items-list) містить HS6 **854231, 854232, 854233, 854239** і **852691**. Не всі підкатегорії 8542/8526 включені; зокрема 854290, 852610 і 852692 не можна автоматично позначати як включені.

47 значень `is_chpl_list: true` — **аналітичне зіставлення підтвердженого класу інтегральних схем** із категоріями 8542. Це не встановлені teardown-звітами індивідуальні митні коди конкретних part_number. GNSS M10050-KB є чіпом, а не зовнішнім навігаційним модулем: [u-blox datasheet](https://content.u-blox.com/sites/default/files/UBX-M10050-KB_ProductSummary_UBX-20017986.pdf). Два LTM4615 мають `is_chpl_list: null`: [опис Analog Devices](https://www.analog.com/en/products/ltm4615.html) показує багатокомпонентний силовий μModule; індивідуальний HS не підтверджено. Наявність 850440 у загальному CHPL не підміняє цю невизначеність.

## Виробники блоків та межі атрибуції

| Запис JSON (від 1) | Виробник / ІПН / джерело | Межа |
|---|---|---|
| 1–3 | АТ «ВНИИР-Прогресс», 2130094170; [War&Sanctions](https://war-sanctions.gur.gov.ua/components/companies/5930), [EUR-Lex, запис 490](https://eur-lex.europa.eu/legal-content/FI/TXT/HTML/?uri=OJ%3AL_202602161) | Конкретні МР4-01, Р8 та М12; склад не поширено на всю родину «Комета-М». |
| 4 | АТ «НІЦЕВТ», 7726019325; [блок і індекс](https://war-sanctions.gur.gov.ua/components/companies/2461), [розкриття емітента](https://disclosure.1prime.ru/Portal/GetDocument.aspx?docId=df76632b42fb46e9aa2f73a9b72130c3&emId=7726019325) | БОПИ 12.14.018.М зі складу БІНС 9Б918, 9М723К5. |
| 5 | АТ «НІІ КП», 7722488005; [підприємство](https://war-sanctions.gur.gov.ua/components/companies/3596), [Банк Росії](https://www.cbr.ru/rbr/insideDKO/doc?id=41712) | БНАП-Б 9Б919 до МА2Б, 9М723К5. |
| 6 | `null` | НАКО не встановила завод і індекс конкретного блока INS 9М723; ІПН виробника ракети не підставлено. |
| 7 | АТ АНПП «Темп-Авиа», 5243001887; [блок](https://war-sanctions.gur.gov.ua/components/part/5155), [лист підприємства](https://mai.ru/upload/iblock/abf/pa67cqbddzu11b81satuzwkq9ukbeyrp/AO-ANPP-TEMP_AVIA.pdf) | СМАРТ ФАБ-500Т з УМПК-ПД. |
| 8 | `null` | НАКО встановила СМАРТ УМПК, але не однозначну повну назву боєприпасу і завод дослідженої плати; `weapon_name` теж `null`. |

У записі 2 використано індекс 9М723-1 із вкладених карток Р8; батьківський заголовок дає 9М723. Прив’язка записів 3 і 7 до ФАБ-500Т з УМПК-ПД підтверджена [сторінкою системи](https://war-sanctions.gur.gov.ua/page-umpk-pd).

## Опрацьовані джерела та прогалини

Для класичної «Комета-М» НАКО дає функціональний опис на друкованих с.52–54, але додаток с.70 є глобальним переліком УМПК і має неповні або сумнівні маркування; його не перетворено на вигадану BOM окремої Комети. [НАКО, The Makers of Ruin](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf).

[Trap Aggressor / StateWatch, 12.04.2024](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/kerovani-aviabomby-khto-vyhotovliaie-ta-napovniuie-smertonosnu-dlia-ukrainy-zbroiu/) прямо досліджує УМПК ФАБ-500М62, називає Комета-М та її виробника, але доступна мігрована публікація не відтворює таблицю/ілюстрацію з повними моделями. Це джерело опрацьовано для атрибуції та перевірки прогалин; окремих компонентних записів воно не додає. Його ФАБ-500М62 не ототожнено зі зразком НАКО. [Дослідження Shahed типу K](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/trap-aggressor-rozibrav-try-typy-shahed-shcho-vseredyni-rosiyskykh-bezpilotnykiv/) стосується іншої платформи. Окремого доступного блокового звіту Trap Aggressor для 9М723 не встановлено; це не твердження про відсутність усіх таких публікацій.

Виключено помилкоподібне `SPARTAN XC235U TCG144AMS1929`: його не виправлено до FPGA іншого зразка. Також виключено ALTERA Cyclone без моделі, `5CE*A9****7N`, сумнівне `5CEFA7F2317N`, `XSEMI XL401SE1 23418`, неповні ADG50… / ATMLH… / TI47A.., неідентифіковані J LA / 845 EUA / W98MP та MAX232 без встановленого вендора. Недостатньо прочитані фото Cypress/IDT зі звіту НАКО про INS не включено. Реле, пасивні елементи та дискретні силові транзистори не включено до масиву чіпів. Джерела: [БОПИ](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527), [БНАП-Б](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519), [М12](https://war-sanctions.gur.gov.ua/components/part/5156), [НАКО, с.70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf).

НАКО повідомляє один `1BAYSKK` у конкретному зразку INS (PDF с.11, друкована с.10), але не встановлює індекс готового блока або його серійну BOM; це спостереження не перенесене до норми r_kj. [Who Navigates Iskander?](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf).

Додатково перевірено функції [SP3243EU](https://www.maxlinear.com/product/interface/serial-transceivers/rs-232/sp3243eu), [XL4015](https://www.xlsemi.com/datasheet/XL4015-EN.pdf), [MAX490](https://www.analog.com/en/products/max490.html). Реєстрацію вендорів підтверджують [SEC Sipex](https://www.sec.gov/Archives/edgar/data/1007800/000089161806000354/f23025e10vq.htm), [SEC Analog Devices](https://www.sec.gov/Archives/edgar/data/6281/000000628124000204/adi-20241102.htm), [SEC Linear Technology](https://www.sec.gov/Archives/edgar/data/791907/000079190714000099/lltc-20140629x10k.htm), [SEC Altera](https://www.sec.gov/Archives/edgar/data/768251/000119312505071963/ddef14a.htm), [статут u-blox](https://content.u-blox.com/sites/default/files/documents/u-blox%20Holding%20AG%20Statuten%20EN.pdf) і [сайт XLSEMI](https://www.xlsemi.com/). Для інших вендорів збережено країну за компонентними картками, з окремим виправленням ST відповідно до країни юридичної реєстрації.

Перевірено синтаксис, точний набір ключів, унікальність блоків і маркувань усередині блока, формат відомих ІПН, типи CHPL та правило `null` → `requires_manual_verification: true`. Джерела винесено сюди для збереження запитаної структури JSON.

## Джерело кожного компонента

Нумерація блоків і компонентів відповідає порядку масивів JSON. Для HTML джерело — розділ «Компоненти у складі»; для НАКО зазначено сторінку.

| block.component | part_number | source |
|---|---|---|
| 1.1 | `LT 1927 LTC2174 UKG-14 BT30768` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.2 | `M10050-KB A0100A 8456204 2240A3` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.3 | `AD9517-4 ABCPZ #2323 6317203,1 KOREA` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.4 | `5CEFA7F23I7N F BBCAU2319A TAIWAN S317MK07 314FA310B` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.5 | `LINEAR LTM4615V uModule №81547 е4 2309 MY` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.6 | `ADL 5355 ACPZ #1120 126356,1` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.7 | `ADF 4360-4 #2113 233521` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.8 | `ALTERA EPCQ64A 2052` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 1.9 | `AD9755 ASTZ #2132 492192,1` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4191) |
| 2.1 | `5CEFA7F23I7N F BBCAU2325A TAIWAN S326MK77 314FAMOB` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4419) |
| 2.2 | `2201 LTC2175 OKG-14 B641664` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4419) |
| 2.3 | `AD9517-4 ABCPZ #2311 6217978.1 KOREA` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/4419) |
| 3.1 | `LINEAR LTM4615 Umodule N24354 2425M` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/5156) |
| 3.2 | `AD9751 #2401` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/5156) |
| 3.3 | `M10050-KB A0100A 09277776 2351A3` | [War&Sanctions](https://war-sanctions.gur.gov.ua/components/part/5156) |
| 4.1 | `AG245 PWZ2113` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.2 | `PGA206UA 1AC9HGT` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.3 | `Vortex86DX` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.4 | `21H17 D9SBV` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.5 | `25Q64FVS1G 2205` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.6 | `904 3701` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.7 | `HT14 86K CFY8` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.8 | `ADG 1409Y` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.9 | `ADUM1300 BRWZ #1902 4477667.1` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.10 | `ATMLH912 50BL PH 1912PTY` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.11 | `XCF02S 78736 225 VS` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.12 | `907e3 963A25` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.13 | `AD7734 #1911 4528454.1` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.14 | `DAC8420E #2301 6112647.1` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.15 | `HT573 19K AXP9` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.16 | `ADG509AK #2201 5632440` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.17 | `OP177G #2239 7945` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.18 | `OP484E #2124 340623` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 4.19 | `REF192 #230 4936` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 5.1 | `XCR3032XL VQG44CMN2137 A6493160A 71` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.2 | `ADM3202 ARNZ` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.3 | `TGH.A 74ACT244` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.4 | `0721 2145 TJY` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.5 | `MAX883 ESA +208` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.6 | `193A3 GZ325` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.7 | `46CDE1UG3 LM2576T -5.0 P+` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 5.8 | `49AKDYUG3 LM2575T -3.3 P+` | [War&Sanctions](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 6.1 | `1BAYSKK G4 65C23243` | [NAKO PDF p.11 / printed p.10](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
| 6.2 | `XILINX SPARTAN XC2S50 TQG144AMS1401 A4677170A 5I` | [NAKO PDF p.13 / printed p.12](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
| 6.3 | `Actel ProASIC PLUS APA150 FG144 1216 R00MM CHN` | [NAKO PDF p.13 / printed p.12](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
| 7.1 | `SIPEX SP 3243EUE 2346L KC002U2` | [War&Sanctions](https://war-sanctions.gur.gov.ua/page-umpk-pd/6873) |
| 7.2 | `XLSEMI XL4015E1 34943` | [War&Sanctions](https://war-sanctions.gur.gov.ua/page-umpk-pd/6874) |
| 8.1 | `STM32F411CE` | [NAKO printed pp.54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf) |
| 8.2 | `MAX 490 ESA` | [NAKO printed pp.54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf) |
