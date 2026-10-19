"""Pure parsing tests. Never instantiate TaxDesktop or touch a real desktop."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Company
from tax_desktop import (
    DesktopError, choose_client_indexes, choose_confirm_button, choose_copy_button,
    choose_close_button, choose_continue_declare, choose_continue_tax_button, choose_copy_option,
    choose_defer_button, choose_exact_button, choose_keyboard_close, choose_name_edit,
    choose_password_edit,
    choose_remark_point, choose_row_action, choose_single_enter, remark_is_complete,
    choose_search_button, compact, require_identity, require_month, require_zero,
    salary_totals, success_feedback,
)


class DesktopParsingTests(unittest.TestCase):
    def setUp(self):
        self.company = Company("测试公司", "12345678901234567A", "2026-09", 2)

    def footer(self, name="测试公司", tax_id="12345678901234567A"):
        return f"当前操作单位：{name}[{tax_id}] 关于"

    def salary(self, people="2", income="0.00", exempt="0.00"):
        return f"申报总人数：{people}人 收入总额：{income}元 免税收入合计：{exempt}元"

    def test_open_client_ignores_zero_size_application_window(self):
        title = "自然人电子税务局（扣缴端）"
        items = [
            (title, "TApplication", 0, 0),
            (title, "Tfrm_MainFrame", 1010, 688),
            ("GDI+ Window (EPPortalITS.exe)", "GDI+ Hook Window Class", 1, 1),
        ]
        self.assertEqual(choose_client_indexes(items), [1])

    def test_missing_or_multiple_main_windows(self):
        title = "自然人电子税务局（扣缴端）"
        self.assertEqual(choose_client_indexes([(title, "TApplication", 0, 0)]), [])
        self.assertEqual(choose_client_indexes([]), [])
        two = [(title, "Tfrm_MainFrame", 1010, 688), (title, "Tfrm_MainFrame", 1010, 688)]
        self.assertEqual(choose_client_indexes(two), [0, 1])

    def test_name_edit_is_the_middle_field(self):
        edits = [(0.138, 0.179, 1), (0.792, 0.179, 3), (0.467, 0.179, 2), (0.467, 0.256, 4)]
        self.assertEqual(choose_name_edit(edits)[2], 2)
        buttons = [(0.911, 0.258, 9), (0.826, 0.258, 8), (0.365, 0.365, 7)]
        self.assertEqual(choose_search_button(buttons, "query")[2], 8)
        self.assertEqual(choose_search_button(buttons, "reset")[2], 9)

    def test_enter_or_login_is_chosen_on_the_company_row(self):
        items = [
            {"text": "甲公司", "x": 0.2, "y": 0.55},
            {"text": "进入", "x": 0.9, "y": 0.55},
            {"text": "乙公司", "x": 0.2, "y": 0.66},
            {"text": "登录", "x": 0.9, "y": 0.66},
        ]
        self.assertEqual(choose_row_action(items, "甲公司")["x"], 0.9)
        self.assertEqual(choose_row_action(items, "乙公司")["text"], "登录")
        self.assertIsNone(choose_row_action(items, "丙公司"))

    def test_copy_option_is_previous_month_not_zero_wage(self):
        items = [
            {"text": "复制上月当期数据（不包含专项附加扣除数据，用户需手工补充）", "x": 0.48, "y": 0.29},
            {"text": "标准模板导入", "x": 0.2, "y": 0.49},
            {"text": "生成零工资记录，用户手工修改", "x": 0.3, "y": 0.68},
            {"text": "导入", "x": 0.48, "y": 0.89},
        ]
        self.assertEqual(choose_copy_option(items)["y"], 0.29)
        self.assertIsNone(choose_copy_button(items))
        items[-1] = {"text": "立即复制数据", "x": 0.48, "y": 0.89}
        self.assertEqual(choose_copy_button(items)["text"], "立即复制数据")
        result = [{"text": "保存", "x": 0.77}, {"text": "关闭", "x": 0.92}]
        self.assertEqual(choose_close_button(result)["text"], "关闭")
        self.assertIsNone(choose_close_button([result[0]]))
        tax = [{"text": "继续算税", "x": 0.77}, {"text": "暂不算税", "x": 0.92}]
        self.assertEqual(choose_continue_tax_button(tax)["text"], "继续算税")
        self.assertIsNone(choose_continue_tax_button([{"text": "暂不算税", "x": 0.5}]))
        declare = [{"text": "继续申报", "x": 0.42}, {"text": "取消", "x": 0.58}]
        self.assertEqual(choose_continue_declare(declare)["text"], "继续申报")
        self.assertIsNone(choose_continue_declare(declare + [{"text": "作废申报", "x": 0.2}]))

    def test_confirm_button_is_left_of_cancel(self):
        items = [{"text": "确定", "x": 0.424}, {"text": "取消", "x": 0.587}]
        self.assertEqual(choose_confirm_button(items)["x"], 0.424)
        swapped = [{"text": "确定", "x": 0.6}, {"text": "取消", "x": 0.4}]
        self.assertIsNone(choose_confirm_button(swapped))

    def test_remark_point_uses_the_column_when_the_name_is_cut_off(self):
        items = [
            {"text": "备注", "x": 0.72, "y": 0.40},
            {"text": "商贸有限公司", "x": 0.12, "y": 0.62},
            {"text": "进入", "x": 0.90, "y": 0.62},
        ]
        point = choose_remark_point(items, "京万瑞铭诚商贸有限公司")
        self.assertEqual((point["x"], point["y"]), (0.72, 0.62))
        self.assertEqual(choose_row_action(items, "京万瑞铭诚商贸有限公司")["x"], 0.90)
        short = [
            {"text": "有限公司", "x": 0.12, "y": 0.55},
            {"text": "进入", "x": 0.90, "y": 0.55},
        ]
        self.assertIsNone(choose_row_action(short, "京万瑞铭诚商贸有限公司"))
        self.assertEqual(choose_single_enter(short)["x"], 0.90)
        self.assertFalse(remark_is_complete("Aa12..."))
        self.assertTrue(remark_is_complete("Aa123456"))
        self.assertIsNone(choose_single_enter(short + [{"text": "进入", "x": 0.90, "y": 0.70}]))
        self.assertEqual(choose_password_edit([(0.54, 0.27, 1), (0.48, 0.55, 2), (0.41, 0.70, 3)])[2], 2)
        self.assertEqual(choose_exact_button([{"text": "确定", "x": 0.59}, {"text": "取消", "x": 0.43}], "确定")["x"], 0.59)
        keyboard = [{"text": "Q", "x": 0.2}, {"text": "关闭", "x": 0.8}]
        self.assertEqual(choose_keyboard_close(keyboard)["x"], 0.8)
        self.assertIsNone(choose_keyboard_close(keyboard + [{"text": "确定", "x": 0.5}]))

    def test_defer_button_is_the_right_hand_stay_action(self):
        items = [
            {"text": "立即跳转", "x": 0.422, "y": 0.945},
            {"text": "暂不跳转", "x": 0.579, "y": 0.945},
        ]
        self.assertEqual(choose_defer_button(items)["x"], 0.579)
        self.assertIsNone(choose_defer_button([items[0]]))
        swapped = [
            {"text": "暂不跳转", "x": 0.422, "y": 0.945},
            {"text": "立即跳转", "x": 0.579, "y": 0.945},
        ]
        self.assertIsNone(choose_defer_button(swapped))

    def test_duplicate_row_action_is_rejected(self):
        items = [
            {"text": "甲公司", "x": 0.2, "y": 0.5},
            {"text": "进入", "x": 0.7, "y": 0.5},
            {"text": "进入", "x": 0.9, "y": 0.5},
        ]
        self.assertIsNone(choose_row_action(items, "甲公司"))

    def test_exact_footer_identity(self):
        require_identity(self.footer(), self.company)

    def test_footer_missing_final_ocr_bracket_can_match_exact_identity(self):
        require_identity("当前操作单位：测试公司[12345678901234567A", self.company)

    def test_wrong_or_substring_company_fails(self):
        for name in ("另一公司", "测试公司分公司", "北京测试公司"):
            with self.subTest(name=name), self.assertRaises(DesktopError):
                require_identity(self.footer(name=name), self.company)

    def test_name_only_matches_footer_company_name(self):
        company = Company("测试公司", "", "2026-09", 2)
        require_identity(self.footer(), company)
        require_identity(self.footer(tax_id="ABCDEFGHIJKLMNOPQR"), company)
        require_identity("当前操作单位：测试公司[12345678901234567A", company)

    def test_name_only_wrong_or_duplicate_footer_fails(self):
        company = Company("测试公司", "", "2026-09", 2)
        for text in (self.footer(name="另一公司"),
                     self.footer() + self.footer(),
                     self.footer(name="测试公司分公司"),
                     "当前操作单位：北京测试公司[12345678901234567A]"):
            with self.subTest(text=text), self.assertRaises(DesktopError):
                require_identity(text, company)

    def test_wrong_duplicate_or_partial_taxid_fails(self):
        for text in (self.footer(tax_id="12345678901234567B"),
                     self.footer() + self.footer(),
                     self.footer() + " 第二税号[12345678901234567B]",
                     "当前操作单位：测试公司[X12345678901234567A]"):
            with self.subTest(text=text), self.assertRaises(DesktopError):
                require_identity(text, self.company)

    def test_month_must_be_client_month(self):
        require_month("2026年09月 综合所得申报", self.company)
        require_month("2026年9月", self.company)
        for text in ("2026年10月", "2026年09月2026年10月", "2026-09", "2026年13月", ""):
            with self.subTest(text=text), self.assertRaises(DesktopError):
                require_month(text, self.company)

    def test_explicit_zero_formats(self):
        for value in ("0.00", "0.00元", "+0.00", "-0.00", "０．００", "0,000.00"):
            require_zero(value)

    def test_positive_and_negative_nonzero_rejected(self):
        for value in ("0.01", "-0.01", "+1.00", "-1,000.00", "100.00"):
            with self.subTest(value=value), self.assertRaises(DesktopError):
                require_zero(value)

    def test_ambiguous_or_malformed_ocr_rejected(self):
        for value in ("O.OO", "0.0", "0", "", "0,,0.00", ",0.00", "00,00.00", "0.00元元", "0.00 0.00", "0.000"):
            with self.subTest(value=value), self.assertRaises(DesktopError):
                require_zero(value)

    def test_salary_people_and_zero_totals(self):
        self.assertEqual(salary_totals(self.salary()), 2)
        self.assertEqual(salary_totals(self.salary(people="0")), 0)

    def test_salary_nonzero_and_incomplete_totals_rejected(self):
        for text in (self.salary(income="-1.00"), self.salary(income="1.00"),
                     self.salary(exempt="0.01"), self.salary(income="0,,0.00"),
                     self.salary(people="O"), self.salary(income="O.OO"),
                     "申报总人数2人收入总额0.00元", self.salary(income="0.000")):
            with self.subTest(text=text), self.assertRaises(DesktopError):
                salary_totals(text)

    def test_salary_duplicate_labels_rejected(self):
        for extra in ("申报总人数3人", "收入总额99.00元", "免税收入合计1.00元"):
            with self.subTest(extra=extra), self.assertRaises(DesktopError):
                salary_totals(self.salary() + extra)

    def test_feedback_requires_both_current_status_and_result(self):
        self.assertTrue(success_feedback("申报成功，无需缴款", "反馈结果：申报成功"))
        self.assertFalse(success_feedback("待申报", "申报成功"))
        self.assertFalse(success_feedback("申报成功，无需缴款", ""))

    def test_contradictory_or_payment_feedback_is_not_success(self):
        for status, feedback in (("申报成功，无需缴款", "申报成功申报失败"),
                                 ("申报成功，无需缴款", "处理失败"),
                                 ("申报成功，需缴款", "申报成功")):
            with self.subTest(status=status, feedback=feedback), self.assertRaises(DesktopError):
                success_feedback(status, feedback)


if __name__ == "__main__":
    unittest.main()
