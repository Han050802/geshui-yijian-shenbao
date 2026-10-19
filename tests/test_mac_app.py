"""Pure client tests: fake HTTPS, fake Tk callbacks, synthetic temporary files.

The packaged Mac self-test separately constructs the real complete Tk UI.
Nothing here imports the Windows controller or opens real business files.
"""
import csv
import json
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bridge_protocol import BridgeError, Pairing
from mac_app import (ACTION_LABELS, SUBMITTING_TASKS, ClientState, ConnectionStore,
                     MacApp, NetworkWorker, submission_confirmation,
                     validate_settings, write_results_csv)


def settings():
    return {"url": "http://127.0.0.1:8765/", "work_month": "2026-10", "tax_month": "2026-09",
            "expected_people": 1, "generate_zero": False,
            "allow_missing_insurance": False, "writeback": True}


def job(*, job_id="job-one", request_id="request-one", status="running"):
    return {"id": job_id, "request_id": request_id, "task": "prepare", "status": status,
            "created_at": "2026-10-19T00:00:00Z", "finished_at": None, "error": None}


def status(*, server_id="server-one", active=None, last=None, recent=None, events=None, sequence=0):
    return {"protocol": 1, "server_id": server_id, "hostname": "Synthetic Windows",
            "settings": settings(), "active_job": active, "last_job": last,
            "recent_jobs": recent or [], "companies": [], "results": [],
            "events": events or [], "last_event_seq": sequence}


class SettingsTests(unittest.TestCase):
    def test_validation_sends_only_seven_allowed_fields(self):
        supplied = {**settings(), "expected_people": " 2 ", "unused_password": "not-sent"}
        actual = validate_settings(supplied)
        self.assertEqual(actual, {**settings(), "expected_people": 2})
        self.assertEqual(len(actual), 7)

    def test_invalid_month_people_or_url_cannot_be_submitted(self):
        for key, value in (("work_month", "2026-13"), ("tax_month", "0000-01"),
                           ("expected_people", "0"), ("expected_people", True),
                           ("url", "file:///tmp/test"), ("url", "https://user:secret@127.0.0.1/"),
                           ("generate_zero", "yes")):
            with self.subTest(key=key, value=type(value).__name__):
                with self.assertRaises(ValueError):
                    validate_settings({**settings(), key: value})

    def test_every_real_submit_confirmation_contains_both_months_and_scope(self):
        for task in SUBMITTING_TASKS:
            with self.subTest(task=task):
                confirmation = submission_confirmation(task, settings())
                self.assertIn("2026-10", confirmation)
                self.assertIn("2026-09", confirmation)
                self.assertIn("真实提交", confirmation)
                self.assertIn("范围：", confirmation)
                self.assertIn("实际", ACTION_LABELS[task])
        with self.assertRaises(ValueError):
            submission_confirmation("prepare", settings())


class ConnectionStoreTests(unittest.TestCase):
    def test_only_pairing_and_recovery_ids_are_saved_private_and_can_be_forgotten(self):
        pairing = Pairing(("127.0.0.1",), 8766, "a" * 64, "b" * 64)
        state = ClientState(server_id="server-one", connected=True)
        state.begin_submit("request-one")
        with tempfile.TemporaryDirectory(prefix="mac-pairing-test-") as directory:
            path = Path(directory) / "TaxZeroMac" / "connection.json"
            store = ConnectionStore(path)
            self.assertIsNone(store.load())
            store.save(pairing, state)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(raw), {"version", "pairing", "server_id", "tracked_server_id",
                                        "job_id", "job_status", "pending_request_id"})
            restored = store.load()
            self.assertEqual(restored["pairing"], pairing)
            self.assertEqual(restored["pending_request_id"], "request-one")
            self.assertTrue(ClientState.from_record(restored).unresolved)
            store.forget()
            self.assertFalse(path.exists())


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.state = ClientState()

    def test_disconnected_and_active_job_states_block_new_actions(self):
        self.assertFalse(self.state.can_submit)
        self.state.apply_status(status(active=job()))
        self.assertTrue(self.state.connected)
        self.assertTrue(self.state.busy)
        self.assertFalse(self.state.can_submit)

    def test_lost_submit_response_recovers_by_request_id_from_completed_history(self):
        self.state.apply_status(status())
        self.state.begin_submit("request-one")
        self.state.connected = False
        self.assertFalse(self.state.can_submit)
        self.state.apply_status(status(last=job(status="completed")))
        self.assertIsNone(self.state.pending_request_id)
        self.assertEqual(self.state.job_id, "job-one")
        self.assertEqual(self.state.job_status, "completed")
        self.assertTrue(self.state.can_submit)

    def test_unmatched_request_stays_blocked_after_reconnect(self):
        self.state.apply_status(status())
        self.state.begin_submit("missing-request")
        self.state.apply_status(status(last=job(status="completed")))
        self.assertTrue(self.state.unresolved)
        self.assertEqual(self.state.pending_request_id, "missing-request")
        self.assertFalse(self.state.can_submit)

    def test_stored_running_job_recovers_by_server_and_job_id(self):
        state = ClientState.from_record({"server_id": "server-one", "job_id": "job-one",
                                         "job_status": "running"})
        self.assertTrue(state.unresolved)
        state.apply_status(status(recent=[job(status="completed")]))
        self.assertFalse(state.unresolved)
        self.assertEqual(state.view_job["status"], "completed")

    def test_changed_server_cannot_resolve_old_job_even_if_ids_match(self):
        self.state.apply_status(status())
        self.state.begin_submit("request-one")
        self.state.apply_status(status(server_id="server-two", last=job(status="completed")))
        self.assertTrue(self.state.unresolved)
        self.assertEqual(self.state.tracked_server_id, "server-one")
        self.assertFalse(self.state.can_submit)

    def test_running_job_missing_from_history_stays_unresolved(self):
        self.state.apply_status(status(active=job()))
        self.state.apply_status(status())
        self.assertTrue(self.state.unresolved)
        self.assertFalse(self.state.can_submit)

    def test_new_server_does_not_display_previous_servers_completed_job(self):
        self.state.apply_status(status(last=job(status="completed")))
        self.state.apply_status(status(server_id="server-two"))
        self.assertIsNone(self.state.view_job)
        self.assertIsNone(self.state.job_id)
        self.assertTrue(self.state.can_submit)

    def test_matching_reply_releases_uncertainty_but_busy_job_blocks_next_action(self):
        self.state.apply_status(status())
        self.state.begin_submit("request-one")
        self.state.acknowledge(job())
        self.assertFalse(self.state.unresolved)
        self.assertIsNone(self.state.pending_request_id)
        self.assertFalse(self.state.can_submit)
        self.assertTrue(self.state.busy)

    def test_wrong_submit_receipt_does_not_clear_pending_request(self):
        self.state.apply_status(status())
        self.state.begin_submit("request-one")
        with self.assertRaises(ValueError):
            self.state.acknowledge(job(request_id="another-request"))
        self.assertEqual(self.state.pending_request_id, "request-one")
        self.assertTrue(self.state.unresolved)

    def test_event_cursor_deduplicates_and_recovers_server_process_reset(self):
        first = {"seq": 9, "kind": "log", "value": "synthetic event"}
        self.assertEqual(self.state.apply_status(status(events=[first], sequence=9)), [first])
        self.assertEqual(self.state.apply_status(status(events=[first], sequence=9)), [])
        reset = {"seq": 1, "kind": "log", "value": "new process"}
        self.assertEqual(self.state.apply_status(status(events=[reset], sequence=1)), [reset])
        self.assertEqual(self.state.last_event_seq, 1)


class WorkerTests(unittest.TestCase):
    def test_failed_post_is_called_once_in_a_background_thread(self):
        called_from = []
        client = Mock()

        def fail_once(**_arguments):
            called_from.append(threading.get_ident())
            raise BridgeError("Synthetic transport failure", uncertain=True)

        client.submit.side_effect = fail_once
        worker = NetworkWorker()
        try:
            worker.put(1, "submit", client, task="prepare", settings=settings(), request_id="synthetic-request")
            reply = worker.replies.get(timeout=2)
            self.assertIsInstance(reply.error, BridgeError)
            self.assertEqual(reply.operation, "submit")
            self.assertEqual(client.submit.call_count, 1)
            self.assertEqual(len(called_from), 1)
            self.assertNotEqual(called_from[0], threading.get_ident())
            self.assertTrue(worker.requests.empty())
        finally:
            worker.close()
            worker.thread.join(timeout=2)
        client.stop.assert_not_called()

    def test_closing_worker_never_stops_or_submits_a_windows_task(self):
        worker = NetworkWorker()
        worker.close()
        worker.thread.join(timeout=2)
        self.assertFalse(worker.thread.is_alive())
        self.assertTrue(worker.replies.empty())


def fake_app():
    app = MacApp.__new__(MacApp)
    app.root = Mock()
    app.state = ClientState(server_id="server-one", connected=True)
    app.pending_operations = set()
    app.variables = {key: Mock(get=Mock(return_value=value)) for key, value in settings().items()}
    app._save_connection = Mock(return_value=True)
    app._queue = Mock()
    app._log = Mock()
    app._refresh_controls = Mock()
    app.progress_var = Mock()
    app.connection_var = Mock()
    app.pairing_var = Mock()
    app.closed = False
    app.generation = 1
    app.after_id = "fake-after"
    app.worker = Mock()
    return app


class CallbackTests(unittest.TestCase):
    def test_paste_updates_code_without_connecting_or_sending(self):
        app = fake_app()
        app.root.clipboard_get.return_value = "  synthetic-connection-code  "
        app.paste_pairing()
        app.pairing_var.set.assert_called_once_with("synthetic-connection-code")
        app._queue.assert_not_called()

    @patch("mac_app.messagebox.showinfo")
    def test_paste_rejects_empty_or_non_text_clipboard_without_erasing_code(self, show):
        from tkinter import TclError
        for value in ("", "   ", None, b"non-text", TclError("no clipboard text")):
            with self.subTest(kind=type(value).__name__):
                app = fake_app()
                if isinstance(value, Exception):
                    app.root.clipboard_get.side_effect = value
                else:
                    app.root.clipboard_get.return_value = value
                app.paste_pairing()
                app.pairing_var.set.assert_not_called()
                app._queue.assert_not_called()
        self.assertEqual(show.call_count, 5)

    def test_paste_does_not_access_clipboard_while_busy_or_pending(self):
        for pending in (False, True):
            app = fake_app()
            if pending:
                app.pending_operations.add("status")
            else:
                app.state.active_job = job()
            app.paste_pairing()
            app.root.clipboard_get.assert_not_called()

    @patch("mac_app.messagebox.askyesno", return_value=False)
    def test_cancelled_confirmation_sends_nothing(self, ask):
        for task in SUBMITTING_TASKS:
            app = fake_app()
            app.action(task)
            app._queue.assert_not_called()
            app._save_connection.assert_not_called()
            self.assertIsNone(app.state.pending_request_id)
        self.assertEqual(ask.call_count, 3)

    @patch("mac_app.messagebox.askyesno", return_value=True)
    def test_confirmed_submit_persists_request_before_one_post(self, ask):
        app = fake_app()
        order = []
        app._save_connection.side_effect = lambda: order.append("persist") or True
        app._queue.side_effect = lambda *_args, **_kwargs: order.append("post")
        app.action("run")
        self.assertEqual(order, ["persist", "post"])
        ask.assert_called_once()
        app._queue.assert_called_once()
        self.assertEqual(app._queue.call_args.args, ("submit",))
        self.assertEqual(app._queue.call_args.kwargs["request_id"], app.state.pending_request_id)
        # A second click while the first receipt is unresolved cannot queue again.
        app.action("run")
        app._queue.assert_called_once()

    @patch("mac_app.messagebox.showinfo")
    @patch("mac_app.messagebox.askyesno")
    def test_status_change_during_confirmation_prevents_post(self, ask, show):
        app = fake_app()

        def change_state(*_args, **_kwargs):
            app.state.connected = False
            return True

        ask.side_effect = change_state
        app.action("run")
        app._queue.assert_not_called()
        app._save_connection.assert_not_called()
        show.assert_called_once()

    @patch("mac_app.messagebox.askyesno")
    def test_prepare_is_explicitly_non_submitting_and_does_not_ask_real_submit_confirmation(self, ask):
        app = fake_app()
        app.action("prepare")
        ask.assert_not_called()
        app._queue.assert_called_once()
        self.assertEqual(app._queue.call_args.kwargs["task"], "prepare")
        self.assertIn("尚不发送申报", app._log.call_args.args[0])

    @patch("mac_app.messagebox.showerror")
    @patch("mac_app.messagebox.askyesno", return_value=True)
    def test_recovery_file_failure_prevents_post(self, _ask, show):
        app = fake_app()
        app._save_connection.return_value = False
        app.action("run")
        app._queue.assert_not_called()
        self.assertIsNone(app.state.pending_request_id)
        show.assert_called_once()

    def test_network_failure_preserves_uncertain_request_and_never_queues_retry(self):
        app = fake_app()
        app.state.begin_submit("request-one")
        app._network_failed("submit", BridgeError("synthetic failure", uncertain=True))
        self.assertFalse(app.state.connected)
        self.assertTrue(app.state.unresolved)
        self.assertEqual(app.state.pending_request_id, "request-one")
        app._queue.assert_not_called()

    def test_definite_http_rejection_clears_only_unaccepted_request(self):
        app = fake_app()
        app.state.begin_submit("request-one")
        app._network_failed("submit", BridgeError("rejected", status=409, code="busy", uncertain=False))
        self.assertIsNone(app.state.pending_request_id)
        self.assertFalse(app.state.connected)
        app._queue.assert_not_called()

    def test_stop_targets_only_observed_active_job(self):
        app = fake_app()
        app.state.active_job = job()
        app.stop()
        app._queue.assert_called_once_with("stop", job_id="job-one")

    def test_window_close_only_stops_polling_and_worker(self):
        app = fake_app()
        app.state.active_job = job()
        app.close()
        app.root.after_cancel.assert_called_once_with("fake-after")
        app.worker.close.assert_called_once()
        app.root.destroy.assert_called_once()
        app._queue.assert_not_called()


class ExportTests(unittest.TestCase):
    def test_csv_preserves_result_fields_in_user_selected_synthetic_path(self):
        rows = [{"公司名称": "合成测试企业", "税号": "", "税款所属月": "2026-09",
                 "状态": "PREPARED", "原因": "合成,多行\n草稿", "证据": "", "新增字段": "fixture"}]
        with tempfile.TemporaryDirectory(prefix="mac-csv-test-") as directory:
            path = Path(directory) / "results.csv"
            write_results_csv(path, rows)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                self.assertEqual(list(csv.DictReader(stream)), rows)


if __name__ == "__main__":
    unittest.main()
