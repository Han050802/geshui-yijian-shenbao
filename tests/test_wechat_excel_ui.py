"""Offline UI orchestration checks: no real Tk window, WeChat or messages."""
from __future__ import annotations

import ctypes
import importlib.util
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import MethodType, ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
WECHAT = ROOT / "wechat"

fake_report = ModuleType("wechat_report")
fake_report.BASE_DIR = WECHAT
fake_report.setup_logging = Mock()
fake_report.search_group = Mock()
fake_report.wait_for_group = Mock()
fake_report.execute_report = Mock()
fake_report.probe_wechat = Mock()
fake_report.restart_wechat = Mock()


class FakeClientError(RuntimeError):
    can_restart = False


class FakeCancelled(RuntimeError):
    pass


fake_report.WeChatClientError = FakeClientError
fake_report.GroupWaitCancelled = FakeCancelled
spec = importlib.util.spec_from_file_location("wechat_excel_ui_tests_app", WECHAT / "app.py")
ui = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"wechat_report": fake_report}), patch.object(sys, "path", [str(WECHAT), *sys.path]):
    spec.loader.exec_module(ui)


class Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class UIHarness(SimpleNamespace):
    """Bind the real callbacks without initializing Tk or touching a desktop."""
    def __init__(self, path, from_geshui=False):
        super().__init__()
        for name, value in ui.ReportApp.__dict__.items():
            if callable(value) and name != "__init__":
                setattr(self, name, MethodType(value, self))
        self.from_geshui = from_geshui
        self.company_name = "甲公司" if from_geshui else ""
        self.tax_label = "2026年10月"
        self.company_var = Var("甲公司")
        self.mapping_var = Var(str(path))
        self.target_var = Var("不可直接信任的界面群名")
        self.new_group_var = Var("新财税群")
        self.status_var = Var()
        self.message_box = Mock()
        self.message_box.get.return_value = "已完成申报"
        self.company_box = Mock()
        self.new_group_entry = Mock()
        self.cancel_button = Mock()
        self.send_button = Mock()
        self._busy_widgets = [self.send_button]
        self.running = False
        self._job_kind = ""
        self._last_sent = ""
        self._delivery_attempted = False
        self._delivery_completed = False
        self._closing = False
        self._close_when_idle = False
        self.exit_code = 1 if from_geshui else 0
        self.result_file = path.parent / "result.json"
        self.company_file = path.parent / "company.json"
        self.stop_event = threading.Event()
        self.log_queue = queue.Queue()
        self.ui_queue = queue.Queue()
        self._append_log = Mock()
        self._start_worker = Mock()
        self.after = Mock()
        self.deiconify = Mock()
        self.lift = Mock()
        self.attributes = Mock()
        self.withdraw = Mock()
        self.update_idletasks = Mock()
        self.destroy = Mock()
        self._log_handler = Mock()


class ExcelUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "配对.xlsx"
        self.store = ui.GroupMappingStore(self.path)
        self.store.create()
        self.store.upsert("甲公司", "财税核对群", notes="保留备注")
        self.app = UIHarness(self.path)
        self.dialogs = patch.object(ui, "messagebox").start()
        self.addCleanup(patch.stopall)
        for name in ("search_group", "wait_for_group", "execute_report", "probe_wechat", "restart_wechat"):
            setattr(fake_report, name, Mock())

    def form(self):
        return self.app._form()

    def pair_data(self, **kwargs):
        return dict(company_name="乙公司", group_name="新财税群", mapping_file=str(self.path),
                    expected_group=None, wait=False, **kwargs)

    def test_target_comes_from_excel_not_company_or_display(self):
        data = self.form()
        self.assertEqual(data["target_group"], "财税核对群")
        self.assertEqual(data["fallback_group"], "")
        self.assertEqual(self.app.target_var.get(), "财税核对群")

    def test_form_rereads_after_excel_edit(self):
        self.form()
        self.store.upsert("甲公司", "新配对群")
        self.assertEqual(self.form()["target_group"], "新配对群")

    def test_automatic_company_cannot_be_changed_in_ui(self):
        self.app.from_geshui = True
        self.app.company_name = "甲公司"
        self.app.company_var.set("乙公司")
        self.store.upsert("乙公司", "乙公司群")
        self.assertEqual(self.form()["target_group"], "财税核对群")

    def test_automatic_company_dropdown_only_contains_current_company(self):
        self.app.from_geshui = True
        self.app.company_name = "甲公司"
        self.store.upsert("乙公司", "乙公司群")
        self.app._refresh_mapping()
        self.app.company_box.configure.assert_called_with(values=["甲公司"])

    def test_unmapped_company_never_falls_back_to_old_target(self):
        self.app.company_var.set("未配对公司")
        self.app._send_clicked()
        self.app._start_worker.assert_not_called()
        self.assertEqual(self.app.target_var.get(), "")
        fake_report.execute_report.assert_not_called()

    def test_settings_remove_legacy_target_and_fallback(self):
        with patch.object(ui, "load_config", return_value={"target_group": "旧公司", "fallback_group": "文件传输助手", "extra": 7}):
            config = self.app._settings()
        self.assertNotIn("target_group", config)
        self.assertNotIn("fallback_group", config)
        self.assertEqual(config["extra"], 7)
        self.assertEqual(config["company_name"], "甲公司")

    def test_default_mapping_path_follows_program_root(self):
        with patch.object(ui, "APP_ROOT", self.base):
            self.assertEqual(ui.mapping_path(), self.base / "data" / "微信群配对.xlsx")
            self.assertEqual(ui.mapping_path("data/test.xlsx"), self.base / "data/test.xlsx")
            self.assertEqual(ui.mapping_setting(self.base / "data/test.xlsx"), str(Path("data/test.xlsx")))

    def test_external_absolute_mapping_path_is_preserved(self):
        with patch.object(ui, "APP_ROOT", self.base / "another"):
            self.assertEqual(ui.mapping_setting(self.path), str(self.path.resolve()))
            self.assertEqual(ui.mapping_path(str(self.path)), self.path)

    def test_from_geshui_resolves_before_scheduling_send(self):
        self.app.from_geshui = True
        self.app.company_file.write_text(json.dumps({"company": "甲公司", "taxLabel": "2026年10月"}), encoding="utf-8")
        self.app._start_from_geshui()
        self.assertEqual(self.app.target_var.get(), "财税核对群")
        self.app.after.assert_called_once_with(800, self.app._send_clicked)

    def test_from_geshui_unmapped_stays_open_for_repair(self):
        self.app.from_geshui = True
        self.app.company_file.write_text(json.dumps({"company": "未配对公司"}), encoding="utf-8")
        self.app._start_from_geshui()
        self.app.after.assert_not_called()
        self.app.destroy.assert_not_called()
        self.assertIn("暂停", self.app.status_var.get())
        self.assertFalse(self.app.result_file.exists())

    def test_send_uses_resolved_group_and_empty_fallback(self):
        self.app._finish = Mock()
        fake_report.execute_report.return_value = "财税核对群"
        self.app._capture_and_send(self.form())
        fake_report.execute_report.assert_called_once_with("财税核对群", "", "已完成申报", before_send=self.app._mark_delivery_attempted)
        self.assertEqual(self.app._last_sent, "财税核对群")

    def test_mapping_change_after_button_click_prevents_send(self):
        data = self.form()
        self.store.upsert("甲公司", "改动后的群")
        with self.assertRaisesRegex(RuntimeError, "发生变化"):
            self.app._capture_and_send(data)
        fake_report.execute_report.assert_not_called()
        self.assertFalse(self.app._delivery_attempted)

    def test_unexpected_report_destination_cannot_succeed(self):
        fake_report.execute_report.return_value = "另一个群"
        self.app._finish = Mock()
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            self.app._capture_and_send(self.form())
        self.app._finish.assert_not_called()
        self.assertEqual(self.app._last_sent, "")

    def test_auto_send_cannot_retry_after_ambiguous_attempt(self):
        self.app.from_geshui = True
        self.app.company_name = "甲公司"
        def attempt_then_fail(*args, **kwargs):
            kwargs["before_send"]()
            raise RuntimeError("窗口丢失")
        fake_report.execute_report.side_effect = attempt_then_fail
        with self.assertRaises(RuntimeError):
            self.app._capture_and_send(self.form())
        self.app._send_clicked()
        self.app._start_worker.assert_not_called()
        self.assertTrue(self.app._delivery_attempted)

    def test_pre_send_failure_allows_repair_and_retry(self):
        self.app.from_geshui = True
        self.app.company_name = "甲公司"
        fake_report.execute_report.side_effect = RuntimeError("未找到已配对群")
        with self.assertRaises(RuntimeError):
            self.app._capture_and_send(self.form())
        self.assertFalse(self.app._delivery_attempted)
        with patch.object(ui, "save_config"):
            self.app._send_clicked()
        self.app._start_worker.assert_called_once()

    def test_send_button_stays_disabled_after_any_other_action(self):
        self.app.from_geshui = True
        self.app._delivery_attempted = True
        self.app._set_busy(False)
        self.app.send_button.configure.assert_called_with(state="disabled")

    def test_search_only_never_sends_or_captures(self):
        self.app._finish = Mock()
        fake_report.search_group.return_value = "财税核对群"
        self.app._search_only(self.form())
        fake_report.search_group.assert_called_once_with("财税核对群")
        fake_report.execute_report.assert_not_called()
        self.app._finish.assert_called_once()

    def test_search_mismatch_fails(self):
        fake_report.search_group.return_value = "财税核对群近似名称"
        with self.assertRaises(RuntimeError):
            self.app._search_only(self.form())
        fake_report.execute_report.assert_not_called()

    def test_existing_group_saved_only_after_verified_search(self):
        self.app._finish = Mock()
        fake_report.search_group.return_value = "新财税群"
        self.app._pair_group_worker(self.pair_data())
        self.app.ui_queue.get_nowait()()
        self.assertEqual(self.store.resolve("乙公司"), "新财税群")
        fake_report.search_group.assert_called_once_with("新财税群")
        fake_report.execute_report.assert_not_called()
        self.app._finish.assert_called_once()

    def test_existing_group_not_found_does_not_create_row(self):
        fake_report.search_group.return_value = ""
        before = self.path.read_bytes()
        with self.assertRaises(RuntimeError):
            self.app._pair_group_worker(self.pair_data())
        self.assertEqual(self.path.read_bytes(), before)

    def test_wait_for_group_registers_confirmed_name(self):
        self.app._finish = Mock()
        fake_report.wait_for_group.return_value = "新财税群"
        data = self.pair_data()
        data["wait"] = True
        self.app._pair_group_worker(data)
        self.app.ui_queue.get_nowait()()
        fake_report.wait_for_group.assert_called_once_with("新财税群", self.app.stop_event, on_status=self.app.log_queue.put)
        fake_report.search_group.assert_not_called()
        self.assertEqual(self.store.resolve("乙公司"), "新财税群")

    def test_cancel_before_write_leaves_workbook_unchanged(self):
        self.app._finish = Mock()
        self.app.stop_event.set()
        fake_report.wait_for_group.return_value = "新财税群"
        before = self.path.read_bytes()
        data = self.pair_data()
        data["wait"] = True
        self.app._pair_group_worker(data)
        self.assertEqual(self.path.read_bytes(), before)
        self.app._finish.assert_called_once_with(False, "已取消等待，未写入 Excel。", cancelled=True)

    def test_cancel_after_verification_before_ui_commit_prevents_write(self):
        self.app._finish = Mock()
        fake_report.wait_for_group.return_value = "新财税群"
        data = self.pair_data()
        data["wait"] = True
        before = self.path.read_bytes()
        self.app._pair_group_worker(data)
        self.assertEqual(self.path.read_bytes(), before)
        self.app.stop_event.set()
        self.app.ui_queue.get_nowait()()
        self.assertEqual(self.path.read_bytes(), before)
        self.app._finish.assert_called_once_with(False, "已取消等待，未写入 Excel。", cancelled=True)

    def test_locked_workbook_cannot_report_success(self):
        self.app._finish = Mock()
        fake_report.search_group.return_value = "新财税群"
        self.path.with_name("~$" + self.path.name).touch()
        self.app._pair_group_worker(self.pair_data())
        self.app.ui_queue.get_nowait()()
        self.app._finish.assert_called_once()
        self.assertFalse(self.app._finish.call_args.args[0])
        self.assertIn("Excel 未保存", self.app._finish.call_args.args[1])

    def test_pair_snapshot_rejects_change_while_waiting(self):
        self.app._finish = Mock()
        data = self.pair_data()
        self.store.upsert("乙公司", "其他人刚修改的群")
        fake_report.search_group.return_value = "新财税群"
        self.app._pair_group_worker(data)
        self.app.ui_queue.get_nowait()()
        self.assertEqual(self.store.resolve("乙公司"), "其他人刚修改的群")
        self.app._finish.assert_called_once()
        self.assertFalse(self.app._finish.call_args.args[0])

    def test_pairing_form_snapshots_existing_group(self):
        self.dialogs.askyesno.return_value = True
        data = self.app._pairing_form()
        self.assertEqual(data["expected_group"], "财税核对群")
        self.assertEqual(data["company_name"], "甲公司")

    def test_declined_pairing_update_never_starts_worker(self):
        self.dialogs.askyesno.return_value = False
        self.app._begin_pairing(wait=False)
        self.app._start_worker.assert_not_called()

    def test_overlapping_actions_are_rejected(self):
        self.app.running = True
        self.assertFalse(self.app._launch_job("search", "搜索", Mock(), {}))
        self.app._start_worker.assert_not_called()

    def test_new_job_clears_stale_sent_destination(self):
        self.app._last_sent = "旧群"
        self.app._launch_job("search", "搜索", Mock(), {})
        self.assertEqual(self.app._last_sent, "")
        self.assertTrue(self.app.running)

    def test_finish_only_queues_until_ui_drains(self):
        self.app._job_kind = "search"
        self.app.running = True
        self.app._finish(True, "找到群")
        self.assertTrue(self.app.running)
        self.app.deiconify.assert_not_called()
        self.app.ui_queue.get_nowait()()
        self.assertFalse(self.app.running)
        self.app.deiconify.assert_called_once()

    def test_search_success_does_not_write_tax_delivery_result(self):
        self.app.from_geshui = True
        self.app._job_kind = "search"
        self.app._last_sent = "旧群"
        self.app._finish(True, "找到群")
        self.app.ui_queue.get_nowait()()
        self.assertFalse(self.app.result_file.exists())
        self.app.after.assert_not_called()

    def test_send_success_writes_company_receipt_and_closes(self):
        self.app.from_geshui = True
        self.app.company_name = "甲公司"
        self.app._job_kind = "send"
        self.app._last_sent = "财税核对群"
        self.app._finish(True, "已发送")
        self.assertFalse(self.app.result_file.exists())
        self.app.ui_queue.get_nowait()()
        result = json.loads(self.app.result_file.read_text(encoding="utf-8"))
        self.assertEqual(result["company"], "甲公司")
        self.assertEqual(result["sentTo"], "财税核对群")
        self.assertEqual(self.app.exit_code, 0)
        self.assertTrue(self.app._delivery_completed)
        self.app.after.assert_called_once_with(1200, self.app._close_after_send)

    def test_receipt_write_failure_does_not_allow_repeat(self):
        self.app.from_geshui = True
        self.app._job_kind = "send"
        self.app._last_sent = "财税核对群"
        self.app._delivery_attempted = True
        with patch.object(ui, "write_forward_result", side_effect=OSError("只读目录")):
            self.app._finish(True, "已发送")
            self.app.ui_queue.get_nowait()()
        self.assertEqual(self.app.exit_code, 1)
        self.app.after.assert_not_called()
        self.app._set_busy(False)
        self.app.send_button.configure.assert_called_with(state="disabled")

    def test_close_during_wait_requests_cancel_before_destroy(self):
        self.app.running = True
        self.app._job_kind = "wait"
        self.app._on_close()
        self.assertTrue(self.app.stop_event.is_set())
        self.assertTrue(self.app._close_when_idle)
        self.app.destroy.assert_not_called()
        self.app._finish(False, "已取消", cancelled=True)
        self.app.ui_queue.get_nowait()()
        self.app.destroy.assert_called_once()

    def test_standalone_worker_takes_shared_desktop_lock(self):
        work = Mock()
        with patch.object(ui, "DesktopTaskLock") as lock, patch.object(ctypes, "windll", create=True):
            self.app._run_worker(work, {"example": 1})
        lock.assert_called_once_with()
        lock.return_value.__enter__.assert_called_once()
        lock.return_value.__exit__.assert_called_once()
        work.assert_called_once_with({"example": 1})

    def test_automatic_child_uses_parent_lock(self):
        self.app.from_geshui = True
        work = Mock()
        with patch.object(ui, "DesktopTaskLock") as lock, patch.object(ctypes, "windll", create=True):
            self.app._run_worker(work, {})
        lock.assert_not_called()
        work.assert_called_once_with({})

    def test_worker_error_is_enqueued_and_never_touches_tk(self):
        work = Mock(side_effect=FakeCancelled())
        self.app._job_kind = "wait"
        with patch.object(ui, "DesktopTaskLock"), patch.object(ctypes, "windll", create=True):
            self.app._run_worker(work, {})
        self.assertEqual(self.app.ui_queue.qsize(), 1)
        self.app.after.assert_not_called()
        self.app.deiconify.assert_not_called()

    def test_mutex_rejects_existing_tax_task(self):
        kernel = Mock()
        kernel.CreateMutexW.return_value = 123
        with patch.object(ctypes, "WinDLL", return_value=kernel, create=True), patch.object(ctypes, "get_last_error", return_value=183, create=True):
            with self.assertRaisesRegex(RuntimeError, "正在使用桌面"):
                with ui.DesktopTaskLock():
                    self.fail("entered despite an existing task")
        self.assertEqual(kernel.CreateMutexW.call_args.args[2], "Local\\TaxZeroBatchRunner_v1")
        kernel.CloseHandle.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
