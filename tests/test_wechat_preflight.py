"""Real temporary Excel files; all tax and message boundaries remain mocked."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import app
from core import Company, StateStore, ValidationError
from wechat.group_mapping import GroupMappingStore


class WeChatPreflightTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        guard = patch.object(app, "BASE", self.base)
        guard.start()
        self.addCleanup(guard.stop)
        self.company = Company("示例甲公司", "", "2026-09", 1)
        self.store = StateStore(self.base / "data/state.json")
        self.controller = app.Controller.__new__(app.Controller)
        self.controller.emit = MagicMock()
        self.controller.stop = threading.Event()
        self.controller.web = MagicMock()
        self.controller.web.read_companies.return_value = [self.company]
        self.controller.ensure_web = MagicMock()

    def pair(self, company="示例甲公司", group="甲方财务服务群"):
        mapping = GroupMappingStore(self.base / "data/微信群配对.xlsx")
        mapping.create()
        mapping.upsert(company, group)
        return mapping

    def test_missing_excel_stops_before_tax_preparation(self):
        with patch("tax_desktop.TaxDesktop") as desktop:
            with self.assertRaisesRegex(ValidationError, "尚未开始申报"):
                self.controller.execute("test_partial", {"writeback": True})
            desktop.return_value.prepare.assert_not_called()
            desktop.return_value.submit.assert_not_called()
        self.assertFalse((self.base / "data/state.json").exists())

    def test_batch_requires_every_pending_company_pair(self):
        self.pair()
        other = Company("示例乙公司", "", "2026-09", 1)
        with self.assertRaisesRegex(ValidationError, "示例乙公司"):
            self.controller.check_wechat_mappings([self.company, other], self.store)

    def test_exact_company_full_name_required(self):
        self.pair("示例甲公司旧名")
        with self.assertRaises(ValidationError):
            self.controller.check_wechat_mappings([self.company], self.store)

    def test_valid_excel_pair_is_loaded_without_wechat_operations(self):
        self.pair()
        self.controller.check_wechat_mappings([self.company], self.store)
        self.assertEqual(app.wechat_mapping_store().resolve(self.company.name), "甲方财务服务群")

    def test_successful_receipt_does_not_require_current_mapping(self):
        self.store.set(self.company, "SUCCESS")
        self.store.set_step(self.company, "wechat", "DONE", destination="原成功群")
        self.controller.check_wechat_mappings([self.company], self.store)

    def test_uncertain_receipt_does_not_turn_into_new_send(self):
        self.store.set(self.company, "SUCCESS")
        self.store.set_step(self.company, "wechat", "UNKNOWN")
        self.controller.check_wechat_mappings([self.company], self.store)
        self.assertEqual(self.store.step(self.company, "wechat")["state"], "UNKNOWN")

    def test_saved_receipt_can_recover_even_when_excel_removed(self):
        path = app.receipt_result_path(self.company)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"company": self.company.name, "taxLabel": self.company.month,
                                    "sentTo": "原成功群"}), encoding="utf-8")
        self.controller.check_wechat_mappings([self.company], self.store)

    def test_relative_excel_path_survives_moving_program_folder(self):
        custom = self.base / "data/财务配对.xlsx"
        mapping = GroupMappingStore(custom)
        mapping.create()
        mapping.upsert(self.company.name, "迁移后群全名")
        config = self.base / "wechat/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"mapping_file": "data/财务配对.xlsx", "target_group": "旧公司关键词"}), encoding="utf-8")
        self.assertEqual(app.wechat_mapping_store().resolve(self.company.name), "迁移后群全名")

    def test_invalid_config_never_falls_back_to_company_name(self):
        config = self.base / "wechat/config.json"
        config.parent.mkdir()
        config.write_text("not JSON", encoding="utf-8")
        with self.assertRaises(ValidationError):
            app.wechat_mapping_store()

    def test_invalid_excel_extension_keeps_actionable_error(self):
        config = self.base / "wechat/config.json"
        config.parent.mkdir()
        config.write_text(json.dumps({"mapping_file": "data/groups.csv"}), encoding="utf-8")
        with self.assertRaisesRegex(ValidationError, "xlsx"):
            app.wechat_mapping_store()


if __name__ == "__main__":
    unittest.main()
