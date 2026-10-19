import unittest
from unittest.mock import Mock

from core import ValidationError
from web_workbench import browser_network_failure, browser_profile_locked, filing_status, parse_rows, Workbench


class WebValidationTests(unittest.TestCase):
    def test_network_errors_retry_without_proxy(self):
        self.assertTrue(browser_network_failure(Exception("Page.goto: net::ERR_CONNECTION_TIMED_OUT")))
        self.assertTrue(browser_network_failure(Exception("Page.goto: net::ERR_PROXY_CONNECTION_FAILED")))
        timeout = type("TimeoutError", (Exception,), {})("Page.goto: Timeout 30000ms exceeded")
        self.assertTrue(browser_network_failure(timeout))
        self.assertFalse(browser_network_failure(Exception("公司表格必须唯一")))
        locked = type("TargetClosedError", (Exception,), {})("BrowserType.launch_persistent_context: Target page, context or browser has been closed")
        self.assertTrue(browser_profile_locked(locked))
        self.assertFalse(browser_profile_locked(Exception("公司表格必须唯一")))
    def setUp(self):
        self.settings = {"work_month": "2026-10", "tax_month": "2026-09", "url": "https://example.test/"}
        self.row = {"has_individual_tax": True, "name": "甲公司", "tax_id": "91310000123456789A",
                    "button_text": "个税", "aria_pressed": None, "password_present": True}

    def test_pending_explicit_defaults_and_override(self):
        config = dict(self.settings, expected_people_by_tax_id={self.row["tax_id"]: 3},
                      allow_missing_insurance=True, generate_zero=True)
        company = parse_rows([self.row], config)[0]
        self.assertEqual(company.name, "甲公司")
        self.assertEqual(company.month, "2026-09")
        self.assertEqual(company.expected_people, 3)
        self.assertTrue(company.allow_missing_insurance)
        self.assertTrue(company.generate_zero)

    def test_completed_skipped(self):
        for marker in ("✓", "✔", "√"):
            self.assertEqual(parse_rows([dict(self.row, button_text="个税 " + marker)], self.settings), [])
        self.assertEqual(parse_rows([dict(self.row, aria_pressed="true")], self.settings), [])

    def test_no_individual_tax_is_not_automatically_zero(self):
        self.assertEqual(parse_rows([{"name": "普通公司", "has_individual_tax": False}], self.settings), [])

    def test_duplicate_rejected_even_completed(self):
        with self.assertRaises(ValidationError):
            parse_rows([self.row, dict(self.row, button_text="✓个税")], self.settings)

    def test_name_only_row_is_enough(self):
        company = parse_rows([dict(self.row, tax_id="")], self.settings)[0]
        self.assertEqual(company.name, "甲公司")
        self.assertEqual(company.tax_id, "")
        self.assertEqual(company.key, "甲公司:2026-09")

    def test_people_override_by_company_name(self):
        config = dict(self.settings, expected_people_by_name={"甲公司": 4})
        company = parse_rows([dict(self.row, tax_id="")], config)[0]
        self.assertEqual(company.expected_people, 4)

    def test_duplicate_names_rejected_without_tax_id(self):
        with self.assertRaises(ValidationError):
            parse_rows([dict(self.row, tax_id=""), dict(self.row, tax_id="")], self.settings)

    def test_password_box_not_required(self):
        for row in (dict(self.row, password_present=False),
                    {key: value for key, value in self.row.items() if key != "password_present"}):
            company = parse_rows([row], self.settings)[0]
            self.assertEqual(company.name, "甲公司")

    def test_missing_fields_rejected(self):
        for field, value in (("name", ""), ("tax_id", "abc"),
                             ("button_text", "个税 待确认"), ("aria_pressed", "mixed")):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                parse_rows([dict(self.row, **{field: value})], self.settings)

    def test_boolean_configuration_not_coerced(self):
        with self.assertRaises(ValidationError):
            parse_rows([self.row], dict(self.settings, generate_zero="false"))

    def test_url_explicit_month_and_zero_pending_scope(self):
        workbench = Workbench(self.settings, ".")
        self.assertEqual(workbench._url(),
                         "https://example.test/finance/tax?ym=2026-10&tab=pending&situation=all_zero")
        workbench.settings["work_month"] = ""
        with self.assertRaises(ValidationError):
            workbench._url()

    def test_unknown_status_fails_closed(self):
        with self.assertRaises(ValidationError):
            filing_status("已申报", None)

    def test_both_months_required(self):
        for key in ("work_month", "tax_month"):
            config = dict(self.settings)
            del config[key]
            with self.assertRaises(ValidationError):
                parse_rows([self.row], config)

    def test_non_individual_tax_duplicate_still_rejected(self):
        with self.assertRaises(ValidationError):
            parse_rows([self.row, {"has_individual_tax": False, "tax_id": self.row["tax_id"]}], self.settings)

    def test_verify_url_same_work_month_and_origin(self):
        workbench = Workbench(dict(self.settings, verify_url="/finance/tax?ym=2026-10&tab=all"), ".")
        workbench.page = Mock(url=workbench._url())
        self.assertEqual(workbench._verification_url(), "https://example.test/finance/tax?ym=2026-10&tab=all")
        for invalid in ("https://foreign.test/finance/tax?ym=2026-10&tab=all",
                        "/finance/tax?ym=2026-09&tab=all", "/finance/tax?ym=2026-10&tab=pending"):
            workbench.settings["verify_url"] = invalid
            with self.assertRaises(ValidationError):
                workbench._verification_url()

    def test_visible_enabled_next_page_rejected(self):
        workbench = Workbench(dict(self.settings, selectors={"pagination": "#next"}), ".")
        node = Mock()
        node.is_visible.return_value = True
        node.is_enabled.return_value = True
        node.get_attribute.return_value = None
        locator = Mock()
        locator.count.return_value = 1
        locator.nth.return_value = node
        workbench.page = Mock()
        workbench.page.locator.return_value = locator
        with self.assertRaises(ValidationError):
            workbench._check_pagination()
        node.is_enabled.return_value = False
        workbench._check_pagination()

    def test_discovered_verification_link_must_be_unique(self):
        workbench = Workbench(self.settings, ".")
        workbench.page = Mock(url=workbench._url())
        nodes = [Mock(), Mock()]
        nodes[0].get_attribute.return_value = "/finance/tax?ym=2026-10&tab=all"
        nodes[1].get_attribute.return_value = "/finance/tax?ym=2026-10&tab=done"
        for node in nodes:
            node.is_visible.return_value = True
        links = Mock()
        links.count.return_value = 1
        links.nth.side_effect = lambda i: nodes[i]
        workbench.page.get_by_role.return_value = links
        self.assertIn("tab=all", workbench._verification_url())
        links.count.return_value = 2
        with self.assertRaises(ValidationError):
            workbench._verification_url()


if __name__ == "__main__":
    unittest.main()
