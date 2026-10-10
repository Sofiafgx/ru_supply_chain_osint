import json
import unittest

from parsers.industrial_labor import equipment_mentions, normalize_notice, notice_date, parse_notices


class IndustrialTests(unittest.TestCase):
    def row(self, description="Haas VF-3, Siemens 828D, Fanuc 0i-TF"):
        return {"title": "Оператор станков с ЧПУ", "description": description,
                "source_url": "https://joblab.ru/vacancy/12345678"}

    def test_models_and_controls(self):
        result, _ = normalize_notice(self.row(), "joblab")
        self.assertEqual(result[0]["equipment_brand_model"], "Haas VF-3; Fanuc 0i-TF; Siemens 828D")
        self.assertEqual(result[1]["equipment_mentions"][1]["equipment_type"], "cnc_control_or_component")

    def test_sodick_and_840d(self):
        mentions = equipment_mentions("Sodick AQ 600L, Содик; Siemens 840D")
        self.assertIn(("Sodick", "AQ 600L"), [(m["brand"], m["model"]) for m in mentions])
        self.assertIn(("Sodick", None), [(m["brand"], m["model"]) for m in mentions])
        self.assertIn(("Siemens", "840D"), [(m["brand"], m["model"]) for m in mentions])

    def test_trudvsem_api_identity_and_publication(self):
        data = {"results": {"vacancies": [{"vacancy": {
            "job-name": "Оператор ЧПУ", "duty": "Sodick AQ600L",
            "company": {"name": "Завод", "inn": "1646019914"},
            "creation-date": "2026-09-01", "date_modify": "2026-10-01",
            "vac_url": "https://trudvsem.ru/vacancy/example"}}]}}
        record = parse_notices(json.dumps(data), "trudvsem")["records"][0]
        self.assertEqual(record["company_tax_id"], "1646019914")
        self.assertEqual(record["posting_date"], "2026-09-01")
        self.assertIsNone(record["closure_date"])

    def test_brand_only_does_not_invent_model(self):
        m = equipment_mentions("Haas, Doosan; DMG Mori DMC 1035V; Doosan Puma 2600SY")
        self.assertEqual([(x["brand"], x["model"]) for x in m],
                         [("Haas", None), ("DMG Mori", "DMC 1035V"), ("Doosan", None), ("Doosan", "Puma 2600SY")])

    def test_excavators_and_general_siemens_rejected(self):
        for description in ("Экскаватор Doosan DX360LCA", "Работа в Siemens"):
            row = self.row(description);row["title"] = "Машинист"
            self.assertIsNone(normalize_notice(row, "joblab")[0])

    def test_no_deadline_as_closure(self):
        row = self.row();row.update({"application_deadline": "2026-10-15", "validThrough": "2026-10-16", "archived": True})
        result, _ = normalize_notice(row, "hh")
        self.assertIsNone(result[0]["closure_date"])
        self.assertEqual(result[1]["application_deadline"], "2026-10-15")

    def test_no_portal_inn(self):
        record, _ = normalize_notice(self.row(), "joblab")
        self.assertIsNone(record[0]["company_tax_id"])

    def test_repair_and_profile(self):
        row = self.row();row["title"] = "Ремонт станка Haas VF-3 с ЧПУ"
        self.assertEqual(normalize_notice(row, "rostender")[0][0]["activity_status_proxy"], "equipment_maintenance")
        row["title"] = "Поставка запчастей для ремонта станка Haas VF-3"
        self.assertIsNone(normalize_notice(row, "rostender")[0])
        self.assertIsNone(normalize_notice(row, "indparks")[0])

    def test_recommendations_not_scanned(self):
        html = '<h1>Оператор станков с ЧПУ</h1><div class="vacancy-view__body">Обработка металла</div><aside>Haas VF-3</aside>'
        result = parse_notices(html, "cznelb", "https://cznelb.ru/vacancy/123", "html")
        self.assertEqual(result["records"], [])
        self.assertEqual(result["notices_examined"], 1)

    def test_fkc_publication_not_update_date(self):
        v = {"data": [{"VacancySend": {"id": 1, "Name": "Оператор ЧПУ", "Responsibilities": "Haas VF-3", "PublicationDate": "/Date(1791444965227)/"}, "DateUpdateStr": "09.10.2026"}]}
        self.assertEqual(parse_notices(json.dumps(v), "fkc")["records"][0]["posting_date"], "2026-10-08")

    def test_dates(self):
        self.assertEqual(notice_date("26 июня 2026, 13:11"), "2026-06-26")
        self.assertIsNone(notice_date("3 дня назад"))
        self.assertIsNone(notice_date("31.02.2026"))
        self.assertEqual(notice_date("02.09.26"), "2026-09-02")

    def test_joblab_recommendations_and_portal_inn(self):
        html = '<h1>Оператор станков с ЧПУ</h1><p>26 июня 2026</p><table><tr><td>Требования *</td><td>Fanuc 320i</td></tr></table><aside>Haas VF-3</aside><footer>ИНН 7703756587</footer>'
        result = parse_notices(html, "joblab", "https://joblab.ru/vacancy/12345678", "html")
        self.assertEqual(result["records"][0]["equipment_brand_model"], "Fanuc 320i")
        self.assertIsNone(result["records"][0]["company_tax_id"])

    def test_tender_header_date_without_footer_brand(self):
        html = '<div>Тендер №12345678 от 02.09.26</div><h1>Тендер: Ремонт станка Haas VF-3 с ЧПУ</h1><footer>Siemens</footer>'
        result = parse_notices(html, "rostender", "https://rostender.info/region/spb/12345678-tender-remont", "html")
        self.assertEqual(result["records"][0]["posting_date"], "2026-09-02")
        self.assertEqual(result["records"][0]["equipment_brand_model"], "Haas VF-3")

    def test_hh_jsonld_valid_through(self):
        html = '<script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "Оператор ЧПУ", "description": "Haas VF-3", "datePosted": "2026-10-08", "validThrough": "2026-10-30"}) + '</script>'
        record = parse_notices(html, "hh", "https://hh.ru/vacancy/123", "html")["records"][0]
        self.assertIsNone(record["closure_date"])

    def test_hh_explicit_archive_date(self):
        html = '<title>Вакансия оператора (вакансия в архиве c 23 сентября 2026)</title><script type="application/ld+json">' + json.dumps({"@type": "JobPosting", "title": "Оператор ЧПУ", "description": "Haas VF-3", "datePosted": "2026-08-24"}) + '</script>'
        record = parse_notices(html, "hh", "https://hh.ru/vacancy/136562166", "html")["records"][0]
        self.assertEqual(record["closure_date"], "2026-09-23")

    def test_rt_listing_provenance(self):
        html = '<div class="tender-row__wrapper"><span class="tender__date-start">от 07.10.26</span><a class="tender-info__link" href="/region/spb/95503880-tender-remont">Ремонт станка Haas VF-3 с ЧПУ</a><div class="tender__date-end">Окончание (МСК) 12.10.2026 12:00</div></div>'
        result = parse_notices(html, "rostender", "https://rostender.info/category/tendery-frezernye-stanki", "html")
        self.assertEqual(result["records"][0]["posting_date"], "2026-10-07")
        self.assertIsNone(result["records"][0]["closure_date"])
        self.assertEqual(result["evidence"][0]["application_deadline"], "2026-10-12")
        self.assertIn("live_listing_card", result["evidence"][0]["verification_scope"])


if __name__ == "__main__":
    unittest.main()
