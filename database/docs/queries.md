# Примеры запросов

## Карточка компании по ИНН

```sql
SELECT c.company_id, c.gur_id, c.name_short_ru, c.inn, c.ogrn, k.name_ru AS country,
       c.address_ru, c.sanctions_count_intl, c.website,
       array_agg(DISTINCT s.section) AS sections
FROM company c
LEFT JOIN country k USING (country_id)
LEFT JOIN company_section s USING (company_id)
WHERE c.inn = '5050108496'
GROUP BY c.company_id, k.name_ru;
```

ИНН не уникален (филиалы) — запрос может вернуть несколько строк.

## Санкции компании

```sql
SELECT j.name_uk, s.is_sanctioned, s.listed_on, s.doc_url
FROM company_sanction s
JOIN sanction_jurisdiction j ON j.code = s.jurisdiction
JOIN company c USING (company_id)
WHERE c.gur_id = 354
ORDER BY s.listed_on NULLS LAST;
```

## Компании ВПК, ещё не попавшие под санкции ЕС

```sql
SELECT c.name_short_ru, c.inn
FROM company c
WHERE EXISTS (SELECT 1 FROM company_section s
              WHERE s.company_id = c.company_id
                AND s.section IN ('rostec', 'uav/companies', 'tools/company'))
  AND NOT EXISTS (SELECT 1 FROM company_sanction s
                  WHERE s.company_id = c.company_id AND s.jurisdiction = 'EU' AND s.is_sanctioned);
```

Для `UA` так делать нельзя — см. [data_notes.md](data_notes.md#санкции-украины).

## Дерево Ростеха: все потомки холдинга

```sql
WITH RECURSIVE sub AS (
    SELECT company_id, 0 AS depth FROM company WHERE gur_id = 167          -- АТ "ОПК"
    UNION ALL
    SELECT e.company_id, sub.depth + 1
    FROM company_edge e JOIN sub ON e.related_id = sub.company_id
    WHERE e.kind = 'parent' AND e.source = 'rostec_tree'
)
SELECT sub.depth, c.name_short_ru, c.inn
FROM sub JOIN company c USING (company_id)
ORDER BY sub.depth, c.name_short_ru;
```

## Заводы с немецким и японским оборудованием

```sql
SELECT c.name_short_ru, c.inn, SUM(t.quantity) AS machines, c.sanctions_count_intl
FROM tool t
JOIN company c ON c.company_id = t.plant_company_id
JOIN tool_country tc ON tc.tool_id = t.tool_id
JOIN country k ON k.country_id = tc.country_id
WHERE k.name_en IN ('Germany', 'Japan')
GROUP BY c.company_id
ORDER BY machines DESC
LIMIT 20;
```

## Цепочка «иностранный производитель → станок → завод → вооружение»

```sql
SELECT m.name AS manufacturer, t.name_en AS machine, c.name_short_ru AS plant, w.name_ru AS weapon
FROM tool t
JOIN equipment_manufacturer m USING (manufacturer_id)
JOIN company c ON c.company_id = t.plant_company_id
JOIN company_weapon cw ON cw.company_id = c.company_id
JOIN weapon w USING (weapon_slug)
WHERE m.name ILIKE 'DMG%';
```

## Кто поставляет компоненты для вооружения

```sql
SELECT c.name_short_ru, p.name_ru AS component
FROM company_weapon_component cwc
JOIN company c USING (company_id)
JOIN component p USING (part_id)
WHERE cwc.weapon_slug = 'page-su-57'
ORDER BY 1;
```

## Производители БпЛА с характеристиками моделей

```sql
SELECT c.name_short_ru, m.name_en, m.purpose_en,
       jsonb_object_agg(s.spec_key, s.value_en) FILTER (WHERE s.spec_key IS NOT NULL) AS specs
FROM company_uav_model cm
JOIN company c USING (company_id)
JOIN uav_model m USING (uav_model_id)
LEFT JOIN uav_model_spec s USING (uav_model_id)
GROUP BY c.name_short_ru, m.uav_model_id
ORDER BY 1, 2;
```

## Доказательства из госзакупок

```sql
SELECT c.name_short_ru, t.name_ru, e.published_on, e.source_urls
FROM tool_evidence e
JOIN tool t USING (tool_id)
JOIN company c ON c.company_id = t.plant_company_id
WHERE e.evidence_type = 'procurement'
ORDER BY e.published_on DESC NULLS LAST;
```

## Заполненность колонок

Для обновления цифр в [tables.md](tables.md) после загрузки (пример для `company`; для массивов считать `cardinality(col) > 0`):

```sql
SELECT count(*) AS rows,
       count(inn) AS inn, count(ogrn) AS ogrn, count(address_uk) AS address,
       count(description_uk) AS description, count(website) AS website
FROM company;
```

Число строк по всем таблицам: `uv run company-db status`.
