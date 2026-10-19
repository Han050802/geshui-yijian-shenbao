"""Durable recovery regressions: fake adapters only, never tax UI or messages."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
from core import Company, Runner, StateStore, ValidationError


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        base_patch = patch.object(app, "BASE", self.base)
        base_patch.start()
        self.addCleanup(base_patch.stop)
        self.company = Company(app.TEST_COMPANY_NAME, "", "2026-09", 1)
        self.path = self.base / "data/state.json"
        self.store = StateStore(self.path)
        self.events = []
        self.controller = app.Controller.__new__(app.Controller)
        self.controller.check_wechat_mappings = MagicMock()  # Recovery is independent of first-send pairing.
        self.controller.emit = lambda kind, value: self.events.append((kind, value))
        self.controller.stop = threading.Event()
        self.controller.settings = {}
        self.controller.web = MagicMock()
        self.controller.web.read_companies.return_value = [self.company]
        self.controller.web.mark_success.return_value = True
        self.controller.ensure_web = MagicMock()
        self.controller.forward_wechat = MagicMock(return_value="离线回执群")
        self.adapter = MagicMock()
        self.adapter.prepare.return_value = "READY"
        self.adapter.reconcile.return_value = "READY"
        self.adapter.filing_status_text.return_value = "待申报"
        self.adapter.verify.return_value = True
        self.adapter.evidence.return_value = ""
        adapter_patch = patch("tax_desktop.TaxDesktop", return_value=self.adapter)
        adapter_patch.start()
        self.addCleanup(adapter_patch.stop)

    def execute(self, task="test_partial"):
        self.controller.execute(task, {"writeback": True})
        return [value for kind, value in self.events if kind == "result"][-1]

    def write_receipt(self, path=None):
        path = path or app.receipt_result_path(self.company)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"company": self.company.name, "taxLabel": self.company.month,
                                    "sentTo": "离线回执群"}), encoding="utf-8")

    def legacy_record(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"version": 1, "records": {
            self.company.key: {"state": "SUCCESS"}}}), encoding="utf-8")
        self.store = StateStore(self.path)

    def test_each_historical_state_only_reconciles_on_pending_screen(self):
        for state in ("SUBMITTING", "UNKNOWN", "SUCCESS"):
            with self.subTest(state=state):
                self.adapter.reset_mock()
                self.adapter.verify.return_value = False
                self.store.set(self.company, state)
                row = Runner(self.adapter, self.store).run([self.company])[0]
                self.assertEqual(row["状态"], "UNKNOWN")
                self.adapter.reconcile.assert_called_once_with(self.company)
                self.adapter.verify.assert_called_once_with(self.company)
                for action in ("prepare", "check_zero", "calculate", "check_attachments", "check_summary", "submit"):
                    getattr(self.adapter, action).assert_not_called()

    def test_unknown_live_status_without_history_cannot_send(self):
        self.adapter.filing_status_text.return_value = "状态加载中"
        self.assertEqual(Runner(self.adapter, self.store).run([self.company])[0]["状态"], "FAILED")
        self.adapter.submit.assert_not_called()
        self.assertIsNone(self.store.get(self.company))

    def test_failed_reconcile_does_not_fall_back_to_preparing(self):
        self.store.set(self.company, "SUBMITTING")
        self.adapter.reconcile.side_effect = RuntimeError("old dialog cannot be safely closed")
        self.assertEqual(Runner(self.adapter, self.store).run([self.company])[0]["状态"], "UNKNOWN")
        self.adapter.prepare.assert_not_called()
        self.adapter.submit.assert_not_called()

    def test_test_company_feedback_timeout_is_unknown_after_send(self):
        self.adapter.verify.side_effect = TimeoutError("mock feedback timeout")
        row = self.execute("test_company")[0]
        self.assertEqual(row["状态"], "UNKNOWN")
        self.assertIn("发送结果未确认", row["原因"])
        self.assertEqual(StateStore(self.path).get(self.company)["state"], "UNKNOWN")
        self.adapter.submit.assert_called_once_with(self.company)
        self.controller.web.mark_success.assert_not_called()
        self.controller.forward_wechat.assert_not_called()

    def test_prepare_with_uncertain_history_never_writes_drafts(self):
        self.store.set(self.company, "UNKNOWN")
        self.adapter.verify.return_value = False
        self.assertEqual(self.execute("prepare")[0]["状态"], "UNKNOWN")
        self.adapter.prepare.assert_not_called()
        self.adapter.calculate.assert_not_called()
        self.adapter.submit.assert_not_called()

    def test_writeback_retry_does_not_resend_receipt(self):
        self.controller.web.mark_success.return_value = False
        self.assertEqual(self.execute()[0]["状态"], "WRITEBACK_PENDING")
        self.assertEqual(StateStore(self.path).step(self.company, "wechat")["state"], "DONE")
        self.controller.web.mark_success.return_value = True
        self.assertEqual(self.execute()[0]["状态"], "SUCCESS")
        self.adapter.submit.assert_called_once()
        self.controller.forward_wechat.assert_called_once()
        self.assertEqual(StateStore(self.path).step(self.company, "workbench")["state"], "DONE")

    def test_receipt_unknown_never_replays_on_restart(self):
        self.controller.forward_wechat.return_value = ""
        self.assertEqual(self.execute()[0]["状态"], "WRITEBACK_PENDING")
        self.assertEqual(StateStore(self.path).step(self.company, "wechat")["state"], "UNKNOWN")
        self.assertEqual(self.execute()[0]["状态"], "WRITEBACK_PENDING")
        self.controller.forward_wechat.assert_called_once()
        self.controller.web.mark_success.assert_not_called()

    def test_receipt_in_progress_is_persisted_before_sender_runs(self):
        self.store.set(self.company, "SUCCESS")
        def inspect(_company):
            self.assertEqual(StateStore(self.path).step(self.company, "wechat")["state"], "IN_PROGRESS")
            return "离线回执群"
        self.controller.forward_wechat.side_effect = inspect
        self.assertEqual(self.controller._forward_once(self.company, self.store), "离线回执群")

    def test_receipt_write_failure_before_launch_never_sends(self):
        self.store.set(self.company, "SUCCESS")
        with patch.object(self.store, "set_step", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.controller._forward_once(self.company, self.store)
        self.controller.forward_wechat.assert_not_called()

    def test_crash_after_child_receipt_recovers_without_resend(self):
        self.store.set(self.company, "SUCCESS")
        self.store.set_step(self.company, "wechat", "IN_PROGRESS")
        self.write_receipt()
        loaded = StateStore(self.path)
        self.assertEqual(self.controller._forward_once(self.company, loaded), "离线回执群")
        self.controller.forward_wechat.assert_not_called()
        self.assertEqual(StateStore(self.path).step(self.company, "wechat")["state"], "DONE")

    def test_successful_send_then_parent_write_failure_recovers(self):
        self.store.set(self.company, "SUCCESS")
        real_set = self.store.set_step
        def fail_on_done(company, name, state, **kwargs):
            if state == "DONE":
                raise OSError("disk full after child success")
            return real_set(company, name, state, **kwargs)
        def send(_company):
            self.write_receipt()
            return "离线回执群"
        self.controller.forward_wechat.side_effect = send
        with patch.object(self.store, "set_step", side_effect=fail_on_done):
            with self.assertRaises(OSError):
                self.controller._forward_once(self.company, self.store)
        self.assertEqual(self.controller._forward_once(self.company, StateStore(self.path)), "离线回执群")
        self.controller.forward_wechat.assert_called_once()

    def test_legacy_success_without_receipt_does_not_guess_or_resend(self):
        self.legacy_record()
        self.assertEqual(self.controller._forward_once(self.company, self.store), "")
        self.controller.forward_wechat.assert_not_called()
        self.assertEqual(self.store.step(self.company, "wechat")["state"], "UNKNOWN")

    def test_matching_legacy_receipt_can_be_recovered(self):
        self.legacy_record()
        self.write_receipt(self.base / "data/wechat-forward-result.json")
        self.assertEqual(self.controller._forward_once(self.company, self.store), "离线回执群")
        self.controller.forward_wechat.assert_not_called()

    def test_wrong_company_receipt_cannot_resolve_unknown(self):
        self.store.set(self.company, "SUCCESS")
        self.store.set_step(self.company, "wechat", "UNKNOWN")
        self.write_receipt()
        path = app.receipt_result_path(self.company)
        receipt = json.loads(path.read_text())
        receipt["company"] = "另一家公司"
        path.write_text(json.dumps(receipt))
        self.assertEqual(self.controller._forward_once(self.company, self.store), "")
        self.controller.forward_wechat.assert_not_called()

    def test_non_string_receipt_destination_is_not_success(self):
        for value in (True, 123, ["群"], {"name": "群"}, None, " "):
            with self.subTest(value=value):
                self.write_receipt()
                path = app.receipt_result_path(self.company)
                payload = json.loads(path.read_text())
                payload["sentTo"] = value
                path.write_text(json.dumps(payload))
                self.assertEqual(app.read_forward_result(path, self.company.name, self.company.month), "")

    def test_uncertain_receipt_stops_before_next_company(self):
        second = Company("第二家公司", "", "2026-09", 1)
        self.controller.web.read_companies.return_value = [self.company, second]
        self.controller.forward_wechat.return_value = ""
        rows = self.execute()
        self.assertEqual([row["状态"] for row in rows], ["WRITEBACK_PENDING", "SKIPPED"])
        self.adapter.prepare.assert_called_once_with(self.company)
        self.adapter.submit.assert_called_once_with(self.company)
        self.assertIsNone(StateStore(self.path).get(second))

    def test_reverification_preserves_followup_states(self):
        self.store.set(self.company, "SUCCESS")
        self.store.set_step(self.company, "wechat", "DONE", destination="离线回执群")
        self.store.set_step(self.company, "workbench", "DONE")
        Runner(self.adapter, self.store).run([self.company])
        self.assertEqual(self.store.step(self.company, "wechat")["destination"], "离线回执群")
        self.assertEqual(self.store.step(self.company, "workbench")["state"], "DONE")

    def test_invalid_followup_state_fails_closed(self):
        self.store.set(self.company, "SUCCESS")
        payload = json.loads(self.path.read_text())
        payload["records"][self.company.key]["steps"]["wechat"] = {"state": "DONE"}
        self.path.write_text(json.dumps(payload))
        with self.assertRaises(ValidationError):
            StateStore(self.path)

    def test_reading_record_cannot_mutate_durable_stage(self):
        self.store.set(self.company, "SUCCESS")
        record = self.store.get(self.company)
        record["steps"]["wechat"]["state"] = "DONE"
        self.assertEqual(self.store.step(self.company, "wechat")["state"], "PENDING")


if __name__ == "__main__":
    unittest.main()
