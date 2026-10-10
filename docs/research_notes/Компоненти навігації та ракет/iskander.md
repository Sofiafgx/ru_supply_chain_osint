# Іскандер-М: блокові компоненти

## Конкретні блоки, російські виробники та маркування

### Takeaway
War&Sanctions явно прив'язує мікросхеми до двох навігаційних блоків ракети 9М723К5; це відрізняється від загального каталогу ракети. Записи Комета-МР4-01 та Комета-Р8 опрацьовує інший дослідник.

### Cited Findings
- БОПИ зі складу БІНС 9Б918: виробник АТ НІЦЕВТ; названа ракета 9М723К5. Детальна картка заводу уточнює індекс блоку 12.14.018.М. — [Блок 5527](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527); [НІЦЕВТ](https://war-sanctions.gur.gov.ua/components/companies/2461).
- ІПН НІЦЕВТ 7726019325 явно наведений у санкційному акті ЄС і розкритті емітента. — [EUR-Lex](https://eur-lex.europa.eu/legal-content/FI/TXT/?uri=CELEX%3A02014R0833-20250720); [розкриття](https://disclosure.1prime.ru/Portal/GetDocument.aspx?docId=df76632b42fb46e9aa2f73a9b72130c3&emId=7726019325).
- БНАП-Б 9Б919 до МА2Б: виробник АТ НІІ КП (космічного приладобудування), також 9М723К5. — [Блок 5519](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519); [підприємство](https://war-sanctions.gur.gov.ua/components/companies/3596).
- Банк Росії прямо ідентифікує АТ НІІ КП (повна назва збігається) з ІПН 7722488005. — [Банк Росії](https://www.cbr.ru/rbr/insideDKO/doc?id=41712).
- Країна у War&Sanctions для STMicroelectronics — Швейцарія, але користувач просить країну реєстрації вендора; STMicroelectronics N.V. зареєстрована в Нідерландах. — [офіційний FAQ](https://investors.st.com/resources/faqs); [юридичні умови](https://www.st.com/content/st_com/en/about/terms-of-use.html).

### Inferences
- original_vendor містить первинну компанію-розробника, а не сучасний конгломерат-покупець; імена в дужках War&Sanctions (Texas Instruments, AMD, Analog Devices тощо) позначають правонаступника/власника.
- is_chpl_list=true для однозначно названих інтегральних мікросхем ґрунтується на функціональному віднесенні до HS 854231 (процесори), 854232 (пам'ять), 854233 (підсилювачі), 854239 (інші ІС). Це класифікаційне зіставлення, не підтверджена митна декларація конкретного артикулу.

### Gaps
- Карточка SPARTAN у БОПИ публікує «XC235U TCG144AMS1929», що схоже на OCR/транскрипційну помилку. Збережено точно опубліковане маркування; не замінено на XC2S50 з іншого зразка НАКО. Потрібна ручна звірка фото.
- 904 3701, 907e3 963A25, 0721 2145 TJY, 193A3 GZ325 є повними опублікованими корпусними маркуваннями, але не повними комерційними артикулами. Суфікси не вигадано.
- Не включено неідентифіковані J LA, 845 EUA, MAX232 ESE 2503 (вендор не встановлений), неповні ADG50…/ATMLH…/TI47A.., пасивні компоненти та силові дискретні діоди/транзистори. Клас AG245 поданий як transceiver відповідно картці, попри можливу невідповідність перекладу «Трансивер».
- Бортовий навігаційний пристрій БНП-Б з БЦОМ Багет-83В названо й атрибутовано Корунд-М, але його картка не наводить компонентний склад, тому рядок порожніх chips не додавався. — [БНП-Б](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5035).

## НАКО: окремий досліджений зразок інерціальної системи

### Takeaway
Звіт НАКО “Who Navigates Iskander?” датований вереснем 2026, не називає індекс конкретного блоку. Його компоненти не можна автоматично приєднати до БОПИ іншого розібраного зразка.

### Cited Findings
- Прямий PDF (локально завантажений і прочитаний) пояснює INS для 9М723, містить компоненти AD7734, ADG509F, ADG409, ADG1409, ADUM1300 та фотографії. — [НАКО PDF](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf); [сторінка публікації](https://nako.org.ua/analisis/khto-keruie-iskanderom/).
- На PDF сторінці 11 (друкована 10) явно сказано, що 1BAYSKK трапляється один раз у дослідженій INS; пояснено, що це виробничий код, а другий рядок 65C23243 відповідає TI SN65C23243. Повне видиме маркування фото: «1BAYSKK G4 65C23243». — [НАКО PDF](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf).
- На PDF сторінці 13 (друкована 12) візуально перевірено фото Xilinx «SPARTAN XC2S50 TQG144AMS1401 A4677170A 5I» та Actel «ProASIC PLUS APA150 FG144 1216 R00MM CHN». Підпис прямо визначає обидва FPGA. — [НАКО PDF](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf).
- На PDF сторінці 12 підтверджено HT14 = SN74HCT14 та HT573 = SN74HCT573; подібні маркування у War&Sanctions БОПИ збережено без штучного додавання артикульних суфіксів. — [НАКО PDF](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf).

### Inferences
- Для окремого рядка НАКО tier2_manufacturer_inn=null: не можна взяти ІПН від загального виробника ракети КБМ чи іншого блоку.

### Gaps
- Один TI чіп в дослідженому зразку INS є кількістю спостережень, а не нормою на один завершений конкретний блок. В JSON залишено units_per_system=null. Parent may elect count=1 only if explicitly accepting specimen-level observed INS rather than normative completed-block BOM.
- Частину фото (Cypress SRAM та IDT bus switch) прочитано недостатньо точно; вони не включені, аби не вигадувати повне маркування.
- Пошук по Trap Aggressor для 9М723 та Іскандер-М не дав окремого первинного блокового звіту в доступних результатах; не стверджується, що таких звітів не існує.

## Кількості і Common High Priority List

### Takeaway
Жодна з прочитаних карток War&Sanctions не дає точної кількості chips на завершений блок. Кожен компонент має units_per_system=null і requires_manual_verification=true.

### Cited Findings
- CHPL є списком 50 шестизначних HS товарних груп; Tier 1 містить 854231, 854232, 854233, 854239. Це не список конкретних маркувань, і загальний чотиризначний 8526 не весь входить до списку (включено 852691). — [BIS CHPL search content](https://www.bis.gov/search/results?searchBar=Common+High+Priority+Items+List); [BIS regulation](https://www.bis.gov/regulations/ear/746).
- БОПИ /5527 і БНАП-Б /5519 показують «Компоненти у складі» з індивідуальними маркуваннями, без поля кількості чи BOM. — [БОПИ](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527); [БНАП-Б](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519).

### Inferences
- Наявність одного рядка/фото не дорівнює одній фізичній мікросхемі на блок. Канали (наприклад 4-channel ADC) не є кількістю окремих чіпів.

### Gaps
- Для точного r_kj потрібна специфікація або акт повного розбирання з однозначним перерахунком компонентів.

### Таблиця маркувань у кандидатному JSON

| Блок | Маркування | Функція | Первинний вендор | Джерело |
|---|---|---|---|---|
| БОПИ | AG245 PWZ2113 | transceiver | ON Semiconductor (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | PGA206UA 1AC9HGT | instrumentation_amplifier | Burr-Brown Corporation (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | Vortex86DX | microprocessor / SoC | DM&P Electronics (Тайвань) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | 21H17 D9SBV | memory | Micron Technology (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | 25Q64FVS1G 2205 | flash_memory | Winbond Electronics Corporation (Тайвань) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | 904 3701 | DC/DC_controller | Linear Technology Corporation (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | SPARTAN XC235U TCG144AMS1929 | FPGA | Xilinx Inc. (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | HT14 86K CFY8 | Schmitt_trigger_inverter | Texas Instruments (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | ADG 1409Y | analog_multiplexer | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | ADUM1300 BRWZ #1902 4477667.1 | digital_isolator | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | ATMLH912 50BL PH 1912PTY | memory | Atmel (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | XCF02S 78736 225 VS | configuration_memory | Xilinx Inc. (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | 907e3 963A25 | voltage_regulator | Linear Technology Corporation (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | AD7734 #1911 4528454.1 | ADC | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | DAC8420E #2301 6112647.1 | DAC | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | HT573 19K AXP9 | D_type_latch | Texas Instruments (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | ADG509AK #2201 5632440 | analog_multiplexer | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | OP177G #2239 7945 | operational_amplifier | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | OP484E #2124 340623 | operational_amplifier | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БОПИ | REF192 #230 4936 | voltage_reference | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5527) |
| БНАП-Б | XCR3032XL VQG44CMN2137 A6493160A 71 | CPLD | Xilinx Inc. (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | ADM3202 ARNZ | transceiver | Analog Devices (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | TGH.A 74ACT244 | buffer / line_driver | Fairchild Semiconductor (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | 0721 2145 TJY | operational_amplifier | Texas Instruments (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | MAX883 ESA +208 | linear_regulator | Maxim Integrated Products (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | 193A3 GZ325 | comparator | STMicroelectronics N.V. (Нідерланди) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | 46CDE1UG3 LM2576T -5.0 P+ | voltage_regulator | National Semiconductor (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| БНАП-Б | 49AKDYUG3 LM2575T -3.3 P+ | voltage_regulator | National Semiconductor (США) | [джерело](https://war-sanctions.gur.gov.ua/ballistics/iskander/part/5519) |
| INS НАКО | 1BAYSKK G4 65C23243 | transceiver | Texas Instruments (США) | [джерело](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
| INS НАКО | XILINX SPARTAN XC2S50 TQG144AMS1401 A4677170A 5I | FPGA | Xilinx Inc. (США) | [джерело](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
| INS НАКО | Actel ProASIC PLUS APA150 FG144 1216 R00MM CHN | FPGA | Actel Corporation (США) | [джерело](https://nako.org.ua/wp-content/uploads/2026/09/who-navigates-iskander.pdf) |
