# УМПК: блоки керування, окрім «Комета-М»

## Підтверджені компоненти та прив'язка до конкретного блоку

### Takeaway
Найсильніша доказова прив'язка — два чіпи у системі керування «СМАРТ» ФАБ-500Т з УМПК-ПД. Для старішого дослідженого «СМАРТ» НАКО опублікувала STM32F411CE і MAX 490 ESA, але не встановила однозначно повну назву боєприпасу відповідного зразка.

### Cited Findings
- Портал називає боєприпас «ФАБ-500Т з УМПК-ПД» та окремо виділяє систему «СМАРТ», «Комета-М12», БІНС, вузол батарей, сервопривід і систему розкладання крил. Це дозволяє не переносити електроніку «Комета-М12» у «СМАРТ». — [War&Sanctions, УМПК-ПД](https://war-sanctions.gur.gov.ua/page-umpk-pd)
- У «СМАРТ» прямо розміщений трансивер з повним опублікованим маркуванням `SIPEX SP 3243EUE 2346L KC002U2`. Рядки 2346L і KC002U2 збережені як маркування зразка; електронна модель — сімейство SP3243EU. — [СМАРТ](https://war-sanctions.gur.gov.ua/components/part/5155); [картка з маркуванням](https://war-sanctions.gur.gov.ua/page-umpk-pd/6873)
- У «СМАРТ» прямо розміщений понижувальний перетворювач `XLSEMI XL4015E1 34943`; останнє число збережене як рядок маркування. — [СМАРТ](https://war-sanctions.gur.gov.ua/components/part/5155); [картка](https://war-sanctions.gur.gov.ua/page-umpk-pd/6874)
- Datasheet розробника визначає XL4015 як 5A, 180kHz, 36V Buck DC-to-DC Converter, це не MCU, FPGA, ADC або DAC. — [XLSEMI datasheet](https://www.xlsemi.com/datasheet/XL4015-EN.pdf)
- SP3243E — RS-232 transceiver; документація нинішнього правовласника включає SP3243EUEA-L/TR та інші варіанти, проте цей суфікс не дописаний до знайденого маркування. — [MaxLinear SP3243EU](https://www.maxlinear.com/product/interface/serial-transceivers/rs-232/sp3243eu)
- НАКО на с.54 описує STM MCU як центральний процесор «СМАРТ», MAXIM трансивер як вузол зв'язку. Додаток 1 на с.70 дає опубліковані позначення `STM32F411CE` та `MAX 490 ESA`. Не додано жодного відсутнього suffix/lot. — [NAKO The Makers of Ruin, 12.03.2025, pp.54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- MAX490 — low-power RS-485/RS-422 transceiver, MAX490ESA — документований варіант. — [Analog Devices MAX490](https://www.analog.com/en/products/max490.html)
- Trap Aggressor від 12.04.2024 прямо називає зразок «УМПК ФАБ-500М62», говорить про Sipex RS-232 та Analog Devices RS-845/RS-422, проте в нинішній мігрованій статті після підпису компонентів відсутня сама таблиця/ілюстрація з точними моделями. Не застосовано прив'язку моделей з окремого звіту НАКО до цього боєприпасу. — [Trap Aggressor, мігрована першоджерельна публікація StateWatch](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/kerovani-aviabomby-khto-vyhotovliaie-ta-napovniuie-smertonosnu-dlia-ukrainy-zbroiu/)

### Inferences
- У старішому записі `weapon_name: null`, бо точна назва боєприпасу не прив'язана до конкретної дослідженої плати НАКО. Публікація Trap Aggressor про ФАБ-500М62 не доводить, що НАКО досліджувала той самий зразок. — [НАКО pp.54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf); [Trap Aggressor](https://statewatch.org.ua/publications/rozsliduvannia/sanktsii/kerovani-aviabomby-khto-vyhotovliaie-ta-napovniuie-smertonosnu-dlia-ukrainy-zbroiu/)

### Gaps
- Повний комерційний суфікс STM32F411CE не опублікований; його не домислено. Поле part_number відтворює доступне маркування, а не обіцяє повністю встановлену каталожну SKU. — [НАКО p.70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- НАКО p.70 містить `XSEMI XL401SE1 23418` (буква S, не цифра 5). НАКО p.54 називає його XLSEMI buck converter, але це можливі помилки транскрипції. Не включено як впевнено ідентифіковану точну модель та не виправлено навмання до XL4015E1. — [НАКО pp.54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- Окремо виключені реле `AXICOM IMO6 12VDC` (не чіп), інерціальний модуль `24300467` (серійне/невстановлене модельне маркування), припущені MSI313C/MSI313B, кварц YXC 16.000, дискретні MOSFET DTQ6610/DTQ6615 та BC226, неповне SMBJxxA; тип/вендор BC226 і MSI не підтверджені самим НАКО та виділені курсивом у додатку. — [СМАРТ](https://war-sanctions.gur.gov.ua/components/part/5155); [НАКО pp.54–55,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- GD32F103 TBU6 GDQN566 DJ2132 GD ARM у глобальному додатку НАКО не прив'язаний до конкретного російського блоку; не перенесено до «СМАРТ». SP3243EUE у цьому додатку теж не має надійного локального зв'язку з конкретною платою старішого зразка. — [НАКО p.70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)

## Виробник блока та країни реєстрації компаній-розробників

### Takeaway
Для УМПК-ПД системи «СМАРТ» виробником підтверджено «Темп-Авиа», ІПН 5243001887. Для старішого зразка ІПН залишено null: НАКО описує участь підприємства у розробці УМПК, але не доводить завод-виробник саме дослідженої плати.

### Cited Findings
- Виробник системи «СМАРТ» УМПК-ПД — АТ АНПП «ТЕМП-АВІА». — [картка блока](https://war-sanctions.gur.gov.ua/components/part/5155)
- Портал прямо називає підприємство постачальником систем управління й наведення «СМАРТ» для УМПК. — [картка компанії](https://war-sanctions.gur.gov.ua/components/companies/319)
- ІПН 5243001887 показаний у листі самого підприємства від 03.12.2024 та російському урядовому реєстрі. — [корпоративний лист Темп-Авиа](https://mai.ru/upload/iblock/abf/pa67cqbddzu11b81satuzwkq9ukbeyrp/AO-ANPP-TEMP_AVIA.pdf); [МЧС, реєстр ліцензій](https://digital.mchs.gov.ru/fgpn/license/52-06-2006-000293)
- XLSEMI — Shanghai Xinlong Semiconductor Technology; офіційний сайт дає китайську юридичну назву та адресу штаб-квартири Шанхай. — [XLSEMI](https://www.xlsemi.com/)
- Sipex Corporation зареєстрована у США, перереєстрація в Delaware 2003 року; збережено оригінального розробника Sipex, а не нинішнього власника MaxLinear. — [SEC filing Sipex](https://www.sec.gov/Archives/edgar/data/1007800/000089161806000354/f23025e10vq.htm)
- НАКО відносить STMicroelectronics до Швейцарії, але користувач просить саме країну реєстрації: STMicroelectronics N.V. офіційно організована за законами Нідерландів, юридичне місце Amsterdam. Тому в JSON Нідерланди. — [ST investors FAQ](https://investors.st.com/resources/faqs); [НАКО p.70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- НАКО відносить Maxim Integrated до США, а MAX490 у чинній документації Analog Devices збережений як розробка Maxim. — [НАКО p.70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf); [datasheet MAX490](https://www.analog.com/media/en/technical-documentation/data-sheets/MAX1487-MAX491.pdf)

### Inferences
- Російський виробник у новому записі визначений через конкретний блок, а не через головного інтегратора КТРВ чи виробника самої бомби «Базальт». — [УМПК-ПД](https://war-sanctions.gur.gov.ua/page-umpk-pd); [СМАРТ](https://war-sanctions.gur.gov.ua/components/part/5155)

### Gaps
- Без фізичного серійного номера/паспорта плати старого НАКО зразка неможливо гарантувати тотожний виробник, тому null. — [NAKO Methodology and Limitations, pp.7–8](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)

## Кількості та CHPL

### Takeaway
Жодне опрацьоване джерело не публікує однозначну норму витрат чіпів на один готовий блок. Усі units_per_system null і requires_manual_verification true; CHPL залишено null, оскільки первинні картки не встановлюють шість цифр HS для конкретних SKU.

### Cited Findings
- Портал публікує перелік компонентів, а не BOM; НАКО описує компоненти уламків і прямо зазначає, що не мала фізичного доступу до них. — [СМАРТ](https://war-sanctions.gur.gov.ua/components/part/5155); [НАКО pp.7–8,54,70](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- НАКО на p.52 відносить знайдені класи до CHPL, включно з 854231/854232/854239/852691, але не дає індивідуального HS для кожного model/lot. — [НАКО p.52](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf)
- BIS CHPL визначений шестизначними HS: 854231 процесори/контролери, 854232 пам'ять, 854233 підсилювачі, 854239 інші інтегральні схеми, 852691 радіонавігаційна апаратура. Не весь розділ 8526 автоматично CHPL. — [BIS official search result for CHPL](https://www.bis.gov/search/results?searchBar=Common+High+Priority+Items+List)

### Inferences
- Відомі MCU/transceiver/power management IC, найімовірніше, підпадають під категорії 854231 або 854239, але це аналітична класифікація, не підтверджений HS конкретної деталі; final assembly може встановити true за своєю задекларованою політикою класифікації, не змішуючи це з даними teardown. — [BIS CHPL](https://www.bis.gov/search/results?searchBar=Common+High+Priority+Items+List); [XL4015 datasheet](https://www.xlsemi.com/datasheet/XL4015-EN.pdf); [SP3243EU](https://www.maxlinear.com/product/interface/serial-transceivers/rs-232/sp3243eu)

### Gaps
- Кількість фото, видимі два DC/DC у відкритій стороні плати, кількість найменувань чи `Всього: 8` на порталі не є виробничою нормою r_kj. Дані не використано як кількість на готовий блок. — [NAKO plate photo, p.54](https://nako.org.ua/wp-content/uploads/2025/12/2025-03-12-09_47_40-glide_bombs.pdf); [War&Sanctions UMPK-PD](https://war-sanctions.gur.gov.ua/page-umpk-pd)
