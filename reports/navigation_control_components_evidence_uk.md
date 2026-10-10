# Навігаційні та керувальні блоки: джерела й невизначеність

Дата перевірки: 10.10.2026. JSON і JSONL містять однакові 8 записів вузлів та 49 компонентних записів. Це перевірена вибірка попереднього корпусу «Комета», УМПК/УМПК-ПД та 9М723 «Іскандер-М», а не повний каталог усіх навігаційних виробів на трьох сайтах. Дані різних модифікацій та досліджених зразків не об'єднано в умовну специфікацію.

## Структура і правила

Застосовано назви `system_name`, `subsystem_name`, `manufacturer_inn`, `foreign_components`. Кожний рядок JSONL відповідає одному вузлу, а не одному чіпу. Схема збережена в `navigation_control_components.schema.json`.

`part_number` відтворює все доступне опубліковане корпусне маркування, включно з кодами партії/дати. Не дописано каталожних суфіксів. Скорочені маркування STM32F411CE, Vortex86DX, 904 3701, 907e3 963A25, 0721 2145 TJY та 193A3 GZ325 збережені як у джерелі; це не обов'язково повний артикул для замовлення. Сумнівні XC235U, 5CEFA7F2317N, XSEMI XL401SE1 і назва ALTERA Cyclone без моделі виключені. Класи компонентів описують фактичні функції; приклади FPGA, ADC/DAC, transceiver, microcontroller, clock generator не використано як примусовий закритий перелік.

У всіх 49 компонентів `units_per_system: null` та `requires_manual_verification: true`. Перелік типів, одна картка, фото одного корпусу або кількість каналів ADC не підтверджують число фізичних чіпів на завершений вузол. НАКО наводить одне спостереження TI 1BAYSKK у дослідженій INS, але не визначає межі конкретного готового блока та його повний склад; тому це спостереження не перенесено до r_kj.

Старіший зразок НАКО має встановлений модуль УМПК, тому `system_name` заповнено назвою модуля. Модель бомби до неї не домислено.

## Виробник і ІПН

У цій версії всі 8 `manufacturer_inn` дорівнюють `null`. Застосовано буквально умову «якщо підтверджено у звіті»: матеріали встановлюють назви виробників, але перевірені сторінки конкретних блоків не містять їхніх корпоративних ІПН. ІПН з EUR-Lex, Банку Росії або корпоративного листа не підставлено як начебто наведені у звіті технічної експертизи. Попередня версія допускала таке зовнішнє зіставлення, ця версія — ні. Персональні ІПН працівників на сторінці підприємства також не є ІПН підприємства.

| Запис | Підтверджений виробник вузла | Джерело атрибуції |
|---|---|---|
| 1–3, Комета-МР4-01, Р8, М12 | АТ «ВНІІР-ПРОГРЕС» | [картка виробника](https://war-sanctions.gur.gov.ua/components/companies/5930) та картки блоків нижче |
| 4, БОПИ 12.14.018.М | АТ «НІЦЕВТ» | [картка блока](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| 5, БНАП-Б 9Б919 | АТ «НІІ КП» | [картка блока](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| 6, INS НАКО | Не встановлений для конкретного вузла | [НАКО, Who Navigates Iskander?](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
| 7, СМАРТ УМПК-ПД | АТ АНПП «Темп-Авиа» | [картка блока](https://war-sanctions.gur.gov.ua/components/part/5155) |
| 8, старіший СМАРТ УМПК | Не підтверджений для дослідженої плати | [НАКО, The Makers of Ruin, с.54](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf) |

## Юрисдикції вендорів

`original_vendor` — історичний розробник, а не автоматично сучасний власник марки. `headquarters_country` означає країну юридичної реєстрації, а не країну виготовлення корпусу або місце штаб-квартири. STMicroelectronics N.V. зареєстрована в Нідерландах: [офіційний FAQ](https://investors.st.com/resources/faqs). Для XLSEMI юрисдикція залишена `null`: відома китайська адреса сама по собі не підтверджує реєстрацію конкретної юридичної особи.

Попередні джерела юридичної реєстрації: [Analog Devices, SEC](https://www.sec.gov/Archives/edgar/data/6281/000000628124000204/adi-20241102.htm), [Linear Technology, SEC](https://www.sec.gov/Archives/edgar/data/791907/000079190714000099/lltc-20140629x10k.htm), [Altera, SEC](https://www.sec.gov/Archives/edgar/data/768251/000119312505071963/ddef14a.htm), [Sipex, SEC](https://www.sec.gov/Archives/edgar/data/1007800/000089161806000354/f23025e10vq.htm), [u-blox, корпоративний статут](https://content.u-blox.com/sites/default/files/documents/u-blox%20Holding%20AG%20Statuten%20EN.pdf). Для u-blox статут стосується Holding AG; розробник на [datasheet](https://content.u-blox.com/sites/default/files/UBX-M10050-KB_ProductSummary_UBX-20017986.pdf) позначений u-blox AG. Підтверджено країну групи, але окрему реєстрову картку дочірньої AG тут не отримано; тому для двох u-blox AG у новому датасеті юрисдикцію залишено `null`.

Додатково перевірено: [Texas Instruments, статут](https://www.sec.gov/Archives/edgar/data/97476/000009747615000003/txn-12312014xexhibit3a.htm), [Xilinx, SEC](https://www.sec.gov/Archives/edgar/data/743988/000095012310054845/c01883def14a.htm), [Atmel, SEC](https://www.sec.gov/Archives/edgar/data/827054/000119312516481130/d117659d424b3.htm), [National Semiconductor, SEC](https://www.sec.gov/Archives/edgar/vprr/0901/09012225.pdf), [Actel, SEC](https://www.sec.gov/Archives/edgar/data/907687/000119312510223094/dsc14d9.htm), [Maxim, SEC](https://www.sec.gov/Archives/edgar/data/743316/000074331616000066/ex-101q2_16.htm), [ON Semiconductor, SEC](https://www.sec.gov/Archives/edgar/data/1097864/000162828024003201/on-20231231.htm), [Fairchild, статут](https://www.sec.gov/Archives/edgar/data/1036960/000119312512206938/d346889dex301i.htm), [Micron, SEC](https://www.sec.gov/Archives/edgar/data/723125/000110465925069019/0001104659-25-069019-index-headers.html), [Burr-Brown, SEC](https://www.sec.gov/Archives/edgar/data/715577/000095015300000161/0000950153-00-000161-d1.html). Ці джерела підтверджують США для відповідних історичних юридичних осіб.

Тайвань: [Winbond, річний звіт, примітка 1](https://www.winbond.com.tw/export/sites/winbond/about-winbond/investor/financial-information/annual-report/report/2023-Winbond-Electronics-Corporation-Annual-Report_Final.pdf), [DMP Electronics Inc., державний реєстр, №23274902](https://findbiz.nat.gov.tw/fts/company/23274902?fhl=en). У масиві збережено найменування DM&P Electronics за атрибуцією компонента.

## CHPL

[Офіційний CHPL FCDO](https://www.gov.uk/government/publications/russia-sanctions-common-high-priority-items-list/russia-sanctions-common-high-priority-items-list) включає 854231, 854232, 854233, 854239 та 852691. Чотиризначні 8542 і 8526 не можна трактувати як повністю включені групи.

47 `is_chpl_list: true` — аналітичне зіставлення встановленого функціонального класу інтегральної схеми з цими позиціями, а не персональний перелік part_number або підтверджена митна декларація. Це окремо від правила кількості: значення r_kj не оцінювались. Для двох [LTM4615 μModule](https://www.analog.com/en/products/ltm4615.html) залишено `null`: багатокомпонентний силовий модуль потребує індивідуального визначення HS; це не `false`.

## Охоплення джерел

Наданий [каталог Trap](https://trap.org.ua/publications/) під час перевірки повернув 404. Використано доступний первинний матеріал [Trap Aggressor / StateWatch про УМПК ФАБ-500М62, 12.04.2024](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/kerovani-aviabomby-khto-vyhotovliaie-ta-napovniuie-smertonosnu-dlia-ukrainy-zbroiu/). Він підтверджує виробника Комета-М і функції іноземних компонентів, але доступний текст не містить повних моделей із втраченої ілюстрації; компонентних записів із нього не домислено.

НАКО: [The Makers of Ruin, с.52–54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf) і [Who Navigates Iskander?, PDF с.11–13](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf). Глобальний додаток УМПК не використано як повну BOM конкретної Комети. Непрочитані Cypress/IDT ілюстрації не внесено.

Наданий [розділ War&Sanctions /tools/](https://war-sanctions.gur.gov.ua/tools/) стосується виробничого обладнання. Компоненти вузлів взято з розділу «Компоненти у зброї» та вкладених карток конкретних блоків. Прив'язка УМПК-ПД до ФАБ-500Т: [сторінка системи](https://war-sanctions.gur.gov.ua/page-umpk-pd). Для Р8 збережено точніший 9М723-1 із вкладених карток, хоча заголовок батьківської сторінки має 9М723.

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
