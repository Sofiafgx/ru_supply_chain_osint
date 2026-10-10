# Комета: перевірені прив'язки компонентів до блоків

## Які блоки, боєприпаси та виробники підтверджені?

### Takeaway
War&Sanctions дозволяє відокремити три конкретні розібрані модифікації: «Комета-МР4-01» у 9М723, «Комета-Р8» у 9М723-1 та «Комета-М12» у ФАБ-500Т з УМПК-ПД. ІПН виробника готових блоків АТ «ВНИИР-Прогресс» — 2130094170.

### Cited Findings
- Для МР4-01 сторінка явно називає ракету 9М723, виробника АТ «ВНІІР-ПРОГРЕС» та окремий розділ «Компоненти у складі», де є дев'ять позицій. Маркування у kometa.json транскрибовані без доповнення суфіксів із каталогів. — [War&Sanctions, блок 4191](https://war-sanctions.gur.gov.ua/components/part/4191)
- Для Р8 заголовок сторінки називає 9М723, але всі три вкладені компоненти вказують конкретно «ракети 9М723-1». У JSON використано цей точніший індекс, протиріччя заголовка збережено тут. — [War&Sanctions, блок 4419](https://war-sanctions.gur.gov.ua/components/part/4419)
- Для М12 сторінка блока має чотири компоненти. Три мають прочитане маркування; четвертий позначений тільки «ALTERA Cyclone», без моделі/суфікса. Його не внесено до строгого JSON, оскільки це не повне part_number. — [War&Sanctions, блок 5156](https://war-sanctions.gur.gov.ua/components/part/5156)
- М12 входить до УМПК-ПД; ця система встановлена на ФАБ-500Т, що прямо видно як складову та назву засобу ураження «ФАБ-500Т с УМПК-ПД». — [War&Sanctions, ФАБ-500Т з УМПК-ПД](https://war-sanctions.gur.gov.ua/ru/page-umpk-pd)
- Виробник виробляє саме модулі сімейства «Комета» та постачає варіанти МР4, Р8, М12; інші фірми Пантес, ПСБ, Авив — учасники кооперації, не заміна поля виробника готового блока. — [War&Sanctions, виробник](https://war-sanctions.gur.gov.ua/components/companies/5930)
- ІПН АТ/JSC VNIIR-Progress 2130094170, реєстраційний номер 1112130013270, дата реєстрації 14.10.2011 підтверджені записом 490 офіційного нормативного акта ЄС. — [EUR-Lex](https://eur-lex.europa.eu/legal-content/FI/TXT/HTML/?uri=OJ%3AL_202602161)

### Inferences
- Індекс 9М723-1 у рядку Р8 взятий із вкладених карток, а не домислений із заголовка. Усі компоненти цього рядка належать Р8: перелік сторінки блока, не загальний перелік ракети.

### Gaps
- Непрочитаний FPGA М12 не можна перетворювати на конкретну Cyclone V тільки за подібністю сусідніх модифікацій.
- Не можна поширювати склад МР4-01, Р8 чи М12 на всі модифікації «Комета-М».

## Що показують NAKO і Trap Aggressor про звичайну «Комета-М»?

### Takeaway
Обидва дослідження підтверджують роль «Комета-М» та виробника, але не дають верифікованої норми споживання чіпів на один готовий блок. NAKO описує функції компонентів; додаток часто має тільки сімейства або неповні/сумнівні маркування.

### Cited Findings
- NAKO, The Makers of Ruin, 12.03.2025, друковані сторінки 52–54: Комета-M містить Taoglas patch antenna, FPGA Altera, flash memory Altera, GNSS u-blox, 14-bit ADC Linear, DC/DC Linear, temperature sensor Analog Devices, 12-output clock generator/VCO AD та DAC AD. Це функціональний опис, не специфікація r_kj. — [NAKO report](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- Додаток 1, друкована сторінка 70: загальний перелік УМПК, не окрема BOM Комети. Наведені 5CEFA7F2317N і 5CE*A9****7N, EPCQ Series, LTM4613Y, LTM4615V та інше. Частково перевірені компоненти позначені курсивом. Не можна вважати цю таблицю ані точною моделлю всіх FPGA, ані доказом належності кожної позиції саме Кометі. — [NAKO report](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- Trap Aggressor розібрав УМПК ФАБ-500М62 і прямо називає виробником «Комета-М» АТ «ВНИИР-Прогресс». Повідомляє імпорт матриць Altera; конкретні кількості у блоці та повні FPGA part numbers у тексті не зазначено. — [Trap Aggressor / StateWatch](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/kerovani-aviabomby-khto-vyhotovliaie-ta-napovniuie-smertonosnu-dlia-ukrainy-zbroiu/)
- Trap Aggressor також описав «Комета-М» у російському Shahed-136 типу K, але це інша платформа, не підстава механічно переносити BOM авіабомб. — [Trap Aggressor / StateWatch](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/trap-aggressor-rozibrav-try-typy-shahed-shcho-vseredyni-rosiyskykh-bezpilotnykiv/)

### Inferences
- Опис 16 MB flash NAKO не ідентифікує її як EPCQ64A: 64 Mbit ≠ 16 MB. Не змішувати BOM різних блоків.

### Gaps
- Для класичної «Комета-М» з ФАБ-500М62 немає достатнього текстового доказу повних chip markings, щоб додати окремий строгий рядок без ручного прочитання ілюстрацій. Поки охоплення цього джерела — обґрунтування виробника та функцій, а не вигадана BOM.
- PDF успішно завантажено колегою: /tmp/umpk.pdf; extracted text /tmp/umpk.txt. Web fetch повідомив «content length too large», а direct site requests 403. Сторінки 52–54 і 70 прочитані з наданого локального тексту.

## Як закодовано кількість, країну та CHPL?

### Takeaway
У кожної позиції units_per_system=null та requires_manual_verification=true. CHPL=true для верифікованих інтегральних схем; у LTM4615 модульне виконання залишає необхідність підтвердження HS, тому is_chpl_list=null.

### Cited Findings
- War&Sanctions у жодному з трьох переліків не дає числа деталей на один готовий блок. Один запис картки та фото не є нормою виробничих витрат. — [МР4-01](https://war-sanctions.gur.gov.ua/components/part/4191), [Р8](https://war-sanctions.gur.gov.ua/components/part/4419), [М12](https://war-sanctions.gur.gov.ua/components/part/5156)
- CHPL офіційно містить HS 854231, 854232, 854233, 854239 та 852691. Це не всі коди груп 8542/8526. Усі інтегральні схеми у цих рядках є processors/controllers, memories чи other IC; true тут означає відповідність категорії, не персональний санкційний запис part_number. — [UK FCDO CHPL](https://www.gov.uk/government/publications/russia-sanctions-common-high-priority-items-list/russia-sanctions-common-high-priority-items-list)
- LTM4615 виробник описує як DC/DC μModule power module із контролером, FET, індукторами та іншими компонентами в корпусі. Без його підтвердженого HS не прирівнюємо автоматично до одиночного IC. — [Analog Devices LTM4615](https://www.analog.com/en/products/ltm4615.html)
- M10050-KB — bare GNSS receiver chip QFN28, не зовнішній GNSS module; component_class у JSON виправлено відповідно до первинного datasheet, хоча GUR одну картку називає модулем. — [u-blox product summary](https://content.u-blox.com/sites/default/files/UBX-M10050-KB_ProductSummary_UBX-20017986.pdf)
- Analog Devices зареєстрований у Massachusetts, США. — [SEC Form 10-K](https://www.sec.gov/Archives/edgar/data/6281/000000628124000204/adi-20241102.htm)
- Оригінальний розробник Linear Technology — Delaware corporation, спочатку California 1981; тому країна США. — [SEC Linear Form 10-K](https://www.sec.gov/Archives/edgar/data/791907/000079190714000099/lltc-20140629x10k.htm)
- Altera Corporation — Delaware corporation, тому original_vendor=Altera Corporation, країна США; не заміняємо розробника Intel через історичне володіння. — [SEC Altera](https://www.sec.gov/Archives/edgar/data/768251/000119312505071963/ddef14a.htm)
- u-blox Holding AG — корпорація відповідно до Swiss Code of Obligations із registered office Thalwil. Портал називає U-blox; datasheet розробника має copyright u-blox AG. Країна Швейцарія. — [u-blox articles](https://content.u-blox.com/sites/default/files/documents/u-blox%20Holding%20AG%20Statuten%20EN.pdf), [u-blox datasheet](https://content.u-blox.com/sites/default/files/UBX-M10050-KB_ProductSummary_UBX-20017986.pdf)

### Inferences
- CHPL=true для bare GNSS IC означає 854239 IC-category evidence, а не безпідставне застосування 852691 для зовнішньої апаратури.
- Позначення ADC/DAC, RF mixer, frequency synthesizer, clock generator, configuration flash, power module збережено за справжньою функцією. Приклади користувача не трактовано як закритий перелік із чотирьох класів.

### Gaps
- Точна кількість чіпів і норма r_kj у серійному виробництві залишаються невідомими для всіх 15 компонентних записів.
- LTM4615: HS може стосуватися static converter 850440 (також є в загальному CHPL), але без підтвердженого коду залишено null; користувач наголошує саме 8542/8526.
