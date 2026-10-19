"""Bridge reception/lifecycle tests: fake worker, temp data, no business I/O."""
from __future__ import annotations

import copy
import io
import json
import queue
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bridge_host import (BridgeManager, HostError, JOB_KEYS, MAX_BODY, SETTING_KEYS,
                         TASKS, _BridgeHandler, _BridgeServer, interactive_desktop_ready,
                         is_lan_address)
from core import Company


SETTINGS = {
    "url": "http://127.0.0.1:9/", "work_month": "2025-02", "tax_month": "2025-01",
    "expected_people": 1, "generate_zero": False, "allow_missing_insurance": False,
    "writeback": True, "password": "fixture-private-value", "selectors": {"private": "fixture"},
    "expected_people_by_name": {"fixture-only": 1}, "feedback_timeout": 180,
}


class FakeController:
    def __init__(self, emit):
        self.emit, self.jobs, self.stop = emit, queue.Queue(), threading.Event()
        self.executed = []
        self.operation = lambda task, settings: None

    def execute(self, task, settings):
        self.executed.append((task, copy.deepcopy(settings)))
        self.operation(task, settings)

    def run_next(self):
        task, settings = self.jobs.get_nowait()
        if task == "close":
            return
        self.stop.clear()  # Same boundary as the existing Controller.loop.
        try:
            self.execute(task, settings)
        except Exception as exc:
            self.emit("error", type(exc).__name__)
        finally:
            self.emit("idle", "")


class BridgeManagerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="bridge-unit-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.saved = []
        self.desktop = True
        self.manager = self.make_manager()

    def make_manager(self, **overrides):
        arguments = dict(server_id="fixture-server", hostname="fixture-host", controller_factory=FakeController,
                         desktop_ready=lambda: self.desktop,
                         settings_loader=lambda: copy.deepcopy(SETTINGS),
                         settings_saver=lambda settings: self.saved.append(copy.deepcopy(settings)))
        arguments.update(overrides)
        return BridgeManager(self.base, **arguments)

    def submit(self, task="read", settings=None, request_id=None):
        return self.manager.submit(request_id or str(uuid.uuid4()), task, settings or {})

    def test_persists_reception_before_enqueue_and_retains_private_local_settings(self):
        controller = self.manager.controller
        original_put = controller.jobs.put
        observed = []

        def check_durable_then_put(item):
            journal = json.loads((self.base / "data/mac_bridge/jobs.json").read_text(encoding="utf-8"))
            record = next(iter(journal["jobs"].values()))
            observed.append(record["job"]["status"])
            self.assertEqual(record["job"]["task"], item[0])
            original_put(item)

        controller.jobs.put = check_durable_then_put
        job = self.submit(settings={"expected_people": 2})
        self.assertEqual(observed, ["queued"])
        self.assertEqual(job["status"], "queued")
        self.assertEqual(controller.executed, [])
        self.assertEqual(self.saved[0]["expected_people"], 2)
        self.assertEqual(self.saved[0]["password"], SETTINGS["password"])
        self.assertEqual(self.saved[0]["selectors"], SETTINGS["selectors"])
        journal_text = (self.base / "data/mac_bridge/jobs.json").read_text(encoding="utf-8")
        self.assertNotIn(SETTINGS["password"], journal_text)
        self.assertNotIn("selectors", journal_text)

    def test_exact_whitelist_reuses_original_worker_entry(self):
        for task in sorted(TASKS):
            with self.subTest(task=task):
                job = self.submit(task)
                self.manager.controller.run_next()
                self.assertEqual(self.manager.controller.executed[-1][0], task)
                self.assertEqual(self.manager.status()["last_job"]["id"], job["id"])
                self.assertEqual(self.manager.status()["last_job"]["status"], "completed")
        for task in ("close", "exec", "shell", "python", "__import__", "RUN", ["run"]):
            with self.subTest(task=task), self.assertRaises(HostError) as error:
                self.submit(task)
            self.assertEqual(error.exception.status, 400)

    def test_idempotent_retry_before_and_after_completion(self):
        request = str(uuid.uuid4())
        first = self.submit("run", {"writeback": False}, request)
        self.assertEqual(self.submit("run", {"writeback": False}, request), first)
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)
        self.manager.controller.run_next()
        self.desktop = False
        duplicate = self.submit("run", {"writeback": False}, request)
        self.assertEqual(duplicate["id"], first["id"])
        self.assertEqual(duplicate["status"], "completed")
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.manager.controller.executed), 1)

    def test_request_id_conflict_and_busy_never_enqueue_extra_work(self):
        request = str(uuid.uuid4())
        self.submit("prepare", {}, request)
        with self.assertRaises(HostError) as error:
            self.submit("run", {}, request)
        self.assertEqual((error.exception.status, error.exception.code), (409, "request_conflict"))
        with self.assertRaises(HostError) as error:
            self.submit("read")
        self.assertEqual((error.exception.status, error.exception.code), (409, "busy"))
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)
        self.assertEqual(len(self.saved), 1)

    def test_concurrent_duplicate_has_one_durable_job_and_one_queue_item(self):
        barrier, replies, errors = threading.Barrier(2), [], []
        request = str(uuid.uuid4())

        def submit():
            try:
                barrier.wait(timeout=2)
                replies.append(self.submit("run", {}, request))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
        self.assertEqual(errors, [])
        self.assertEqual(len(replies), 2)
        self.assertEqual(replies[0]["id"], replies[1]["id"])
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)
        self.assertEqual(len(self.saved), 1)

    def test_restart_interrupts_accepted_jobs_without_replay(self):
        request = str(uuid.uuid4())
        original = self.submit("run", {}, request)
        recovered = self.make_manager()
        status = recovered.status()
        self.assertIsNone(status["active_job"])
        self.assertEqual(status["last_job"]["id"], original["id"])
        self.assertEqual(status["last_job"]["status"], "interrupted")
        self.assertEqual(status["recent_jobs"][0]["request_id"], request)
        self.assertTrue(recovered.controller.jobs.empty())
        self.assertEqual(recovered.submit(request, "run", {})["status"], "interrupted")
        self.assertTrue(recovered.controller.jobs.empty())
        again = self.make_manager()
        self.assertEqual(again.status()["last_job"], status["last_job"])

    def test_corrupt_journal_fails_closed_before_creating_controller(self):
        path = self.base / "data/mac_bridge/jobs.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"version":1,"jobs":{"broken":{}}}', encoding="utf-8")
        factory = Mock()
        with self.assertRaises(HostError) as error:
            self.make_manager(controller_factory=factory)
        self.assertEqual(error.exception.code, "journal_unavailable")
        factory.assert_not_called()
        self.assertIn("broken", path.read_text(encoding="utf-8"))

    def test_journal_write_failure_does_not_save_settings_or_enqueue(self):
        with patch("bridge_host._atomic_write", side_effect=OSError("fixture disk full")):
            with self.assertRaises(HostError) as error:
                self.submit("run")
        self.assertEqual(error.exception.code, "journal_unavailable")
        self.assertTrue(self.manager.controller.jobs.empty())
        self.assertEqual(self.saved, [])
        self.assertFalse(self.manager.busy)

    def test_settings_write_failure_is_recorded_and_not_replayed(self):
        self.manager._settings_saver = Mock(side_effect=OSError("fixture"))
        request = str(uuid.uuid4())
        job = self.submit("run", {}, request)
        self.assertEqual(job["status"], "failed")
        self.assertTrue(self.manager.controller.jobs.empty())
        self.assertEqual(self.submit("run", {}, request), job)

    def test_desktop_required_before_acceptance(self):
        self.desktop = False
        with self.assertRaises(HostError) as error:
            self.submit("read")
        self.assertEqual(error.exception.code, "desktop_unavailable")
        self.assertTrue(self.manager.controller.jobs.empty())
        self.assertFalse((self.base / "data/mac_bridge/jobs.json").exists())

    def test_desktop_rechecked_at_original_execute_boundary(self):
        self.submit("run")
        self.desktop = False
        self.manager.controller.run_next()
        self.assertEqual(self.manager.controller.executed, [])
        self.assertEqual(self.manager.status()["last_job"]["status"], "failed")

    def test_queued_stop_survives_original_worker_clearing_stop_event(self):
        job = self.submit("run")
        self.manager.stop(job["id"])
        self.assertTrue(self.manager.controller.stop.is_set())
        self.manager.controller.run_next()
        self.assertEqual(self.manager.controller.executed, [])
        self.assertEqual(self.manager.status()["last_job"]["status"], "interrupted")
        self.assertTrue(self.manager.controller.stop.is_set())

    def test_running_stop_waits_for_worker_idle(self):
        started, release = threading.Event(), threading.Event()

        def operation(task, settings):
            started.set()
            if not release.wait(timeout=3):
                raise RuntimeError("fixture wait timed out")

        self.manager.controller.operation = operation
        job = self.submit("run")
        worker = threading.Thread(target=self.manager.controller.run_next)
        worker.start()
        self.addCleanup(release.set)
        self.assertTrue(started.wait(timeout=2))
        self.assertEqual(self.manager.stop(job["id"])["status"], "running")
        self.assertTrue(self.manager.busy)
        release.set()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(self.manager.busy)
        self.assertEqual(self.manager.status()["last_job"]["status"], "interrupted")

    def test_stale_stop_cannot_interrupt_a_later_job(self):
        old = self.submit()
        self.manager.controller.run_next()
        current = self.submit("run")
        self.assertEqual(self.manager.stop(old["id"])["status"], "completed")
        self.assertFalse(self.manager.controller.stop.is_set())
        self.assertEqual(self.manager.status()["active_job"]["id"], current["id"])
        with self.assertRaises(HostError):
            self.manager.stop(str(uuid.uuid4()))
        self.assertFalse(self.manager.controller.stop.is_set())

    def test_close_stops_busy_job_before_sending_close_sentinel(self):
        self.submit("run")
        self.assertFalse(self.manager.request_close())
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)
        with self.assertRaises(HostError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "closing")
        self.manager.controller.run_next()
        self.assertTrue(self.manager.request_close())
        self.assertTrue(self.manager.request_close())
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)
        self.assertEqual(self.manager.controller.jobs.get_nowait(), ("close", None))

    def test_close_waits_for_worker_thread_to_exit(self):
        self.manager.controller.worker = Mock()
        self.manager.controller.worker.is_alive.return_value = True
        self.assertFalse(self.manager.request_close())
        self.manager.controller.worker.is_alive.return_value = False
        self.assertTrue(self.manager.request_close())
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)

    def test_failure_and_results_determine_terminal_state_only_at_idle(self):
        self.submit("run")
        task, settings = self.manager.controller.jobs.get_nowait()
        self.manager.controller.execute(task, settings)
        self.manager.emit("result", [{"公司名称": "fixture", "状态": "WRITEBACK_PENDING", "原因": "fixture reason"}])
        self.assertTrue(self.manager.busy)
        self.manager.emit("idle", "")
        status = self.manager.status()
        self.assertEqual(status["last_job"]["status"], "failed")
        self.assertEqual(status["results"][0]["状态"], "WRITEBACK_PENDING")
        self.assertEqual(status["results"][0]["原因"], "fixture reason")

    def test_prepare_and_verified_results_are_completed(self):
        self.manager.controller.operation = lambda *_: self.manager.emit("result", [{"状态": "PREPARED"}, {"状态": "VERIFIED"}])
        self.submit("prepare")
        self.manager.controller.run_next()
        self.assertEqual(self.manager.status()["last_job"]["status"], "completed")

    def test_controller_exception_is_failure(self):
        def fail(*_):
            raise RuntimeError("fixture")
        self.manager.controller.operation = fail
        self.submit("run")
        self.manager.controller.run_next()
        self.assertEqual(self.manager.status()["last_job"]["status"], "failed")

    def test_execution_journal_failure_prevents_business_work(self):
        self.submit("run")
        with patch("bridge_host._atomic_write", side_effect=OSError("fixture")):
            self.manager.controller.run_next()
        self.assertEqual(self.manager.controller.executed, [])
        self.assertEqual(self.manager.status()["last_job"]["status"], "failed")
        with self.assertRaises(HostError) as error:
            self.submit()
        self.assertEqual(error.exception.code, "journal_unavailable")

    def test_status_redacts_settings_companies_and_returns_detached_snapshots(self):
        self.manager._settings["url"] = "https://name:fixture-url-secret@127.0.0.1:9/path?token=fixture-query-secret#secret"
        self.manager.emit("companies", [Company("fixture", "FIXTURETAXID", "2025-01", 1)])
        status = self.manager.status()
        self.assertEqual(set(status["settings"]), set(SETTING_KEYS))
        self.assertEqual(status["settings"]["url"], "https://127.0.0.1:9/path")
        self.assertEqual(set(status["companies"][0]), {"key", "name", "month", "expected_people"})
        text = json.dumps(status)
        for hidden in (SETTINGS["password"], "selectors", "FIXTURETAXID", "fixture-url-secret", "fixture-query-secret"):
            self.assertNotIn(hidden, text)
        status["companies"][0]["name"] = "changed"
        self.assertEqual(self.manager.status()["companies"][0]["name"], "fixture")

    def test_status_cursors_and_recent_jobs_support_reconnection(self):
        for _ in range(22):
            self.submit()
            self.manager.controller.run_next()
        status = self.manager.status()
        self.assertEqual(len(status["recent_jobs"]), 20)
        self.assertEqual(status["recent_jobs"][0], status["last_job"])
        self.assertEqual(set(status["last_job"]), set(JOB_KEYS))
        self.assertEqual(self.manager.status(status["last_event_seq"])["events"], [])
        self.manager.emit("log", "fixture event")
        new = self.manager.status(status["last_event_seq"])
        self.assertEqual([event["value"] for event in new["events"]], ["fixture event"])
        self.assertEqual(new["last_event_seq"], status["last_event_seq"] + 1)

    def test_invalid_request_or_settings_never_reach_queue(self):
        invalid = ({"password": "x"}, {"selectors": {}}, {"expected_people": True},
                   {"expected_people": 0}, {"writeback": "false"}, {"generate_zero": 1},
                   {"work_month": "2025-13"}, {"tax_month": "0000-01"},
                   {"url": "file:///fixture"}, {"url": "http://user:secret@127.0.0.1/"})
        for settings in invalid:
            with self.subTest(settings=settings), self.assertRaises(HostError):
                self.submit(settings=settings)
        with self.assertRaises(HostError):
            self.manager.submit("not-uuid", "run", {})
        self.assertTrue(self.manager.controller.jobs.empty())
        self.assertEqual(self.saved, [])

    def test_network_scope_is_explicit_private_ipv4(self):
        for address in ("127.0.0.1", "127.2.3.4", "10.2.3.4", "172.16.1.2", "172.31.255.254", "192.168.2.3"):
            self.assertTrue(is_lan_address(address), address)
        for address in ("8.8.8.8", "0.0.0.0", "172.15.1.2", "172.32.1.2", "169.254.1.2", "100.64.0.1", "::1", "::ffff:127.0.0.1"):
            self.assertFalse(is_lan_address(address), address)
        self.assertFalse(_BridgeServer.verify_request(None, None, ("8.8.8.8", 1)))
        with patch("bridge_host.os.name", "posix"):
            self.assertFalse(interactive_desktop_ready())


class _MemorySocket:
    def __init__(self, raw):
        self.raw, self.sent = io.BytesIO(raw), bytearray()

    def makefile(self, *args):
        return self.raw

    def sendall(self, data):
        self.sent.extend(data)


class BridgeHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bridge-http-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.manager = BridgeManager(Path(self.temporary.name), server_id="fixture-server",
                                     controller_factory=FakeController, desktop_ready=lambda: True,
                                     settings_loader=lambda: copy.deepcopy(SETTINGS), settings_saver=lambda _: None)
        self.server = SimpleNamespace(manager=self.manager, token=b"a" * 64,
                                      limiter=SimpleNamespace(allow=lambda _: True), last_authenticated_at=0.0)

    def request(self, method="GET", path="/v1/status?after=0", body=None, *, headers=None, raw_body=None):
        raw = raw_body if raw_body is not None else json.dumps(body).encode() if body is not None else b""
        values = {"Host": "127.0.0.1", "Authorization": "Bearer " + "a" * 64, "Connection": "close"}
        if body is not None or raw_body is not None:
            values.update({"Content-Type": "application/json", "Content-Length": str(len(raw))})
        for key, value in (headers or {}).items():
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        request = f"{method} {path} HTTP/1.1\r\n" + "".join(f"{key}: {value}\r\n" for key, value in values.items()) + "\r\n"
        memory = _MemorySocket(request.encode() + raw)
        _BridgeHandler(memory, ("127.0.0.1", 12345), self.server)
        head, data = bytes(memory.sent).split(b"\r\n\r\n", 1)
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(data), head.decode()

    def test_authenticated_status_is_json_without_cors_or_private_settings(self):
        status, body, headers = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(body["protocol"], 1)
        self.assertIn("application/json", headers)
        self.assertIn("Cache-Control: no-store", headers)
        self.assertNotIn("Access-Control-Allow", headers)
        self.assertNotIn(SETTINGS["password"], json.dumps(body))

    def test_authentication_origin_and_rate_rejections_do_not_enqueue(self):
        for headers, expected in (({"Authorization": None}, 401), ({"Authorization": "Bearer wrong"}, 401),
                                  ({"Origin": "https://example.invalid"}, 403)):
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], expected)
        self.server.limiter.allow = lambda _: False
        self.assertEqual(self.request()[0], 429)
        self.assertTrue(self.manager.controller.jobs.empty())

    def test_post_contract_and_idempotency(self):
        payload = {"request_id": str(uuid.uuid4()), "task": "read", "settings": {"writeback": False}}
        status, first, _ = self.request("POST", "/v1/jobs", payload)
        self.assertEqual(status, 200)
        self.assertEqual(first["job"]["status"], "queued")
        self.assertEqual(self.request("POST", "/v1/jobs", payload)[1], first)
        self.assertEqual(self.manager.controller.jobs.qsize(), 1)
        self.assertEqual(self.manager.controller.executed, [])

    def test_body_limits_content_type_and_duplicate_keys(self):
        cases = [({"Content-Length": str(MAX_BODY + 1)}, b"", 413),
                 ({"Content-Type": "text/plain"}, b"{}", 415),
                 ({"Content-Length": None}, b"{}", 411),
                 ({"Transfer-Encoding": "chunked"}, b"{}", 400),
                 ({}, b'{"job_id":null,"job_id":"x"}', 400),
                 ({}, b'{"job_id":NaN}', 400), ({}, b"[]", 400), ({}, b"{", 400)]
        for headers, raw, expected in cases:
            with self.subTest(headers=headers, raw=raw):
                self.assertEqual(self.request("POST", "/v1/stop", headers=headers, raw_body=raw)[0], expected)
        self.assertTrue(self.manager.controller.jobs.empty())

    def test_only_explicit_json_job_and_stop_endpoints_mutate_state(self):
        self.assertEqual(self.request("POST", "/v1/exec", {"command": "fixture"})[0], 404)
        self.assertEqual(self.request("POST", "/v1/jobs", {"task": "run"})[0], 400)
        self.assertEqual(self.request("POST", "/v1/stop", {})[0], 400)
        self.assertEqual(self.request("GET", "/v1/jobs")[0], 404)
        self.assertEqual(self.request("OPTIONS")[0], 405)
        self.assertEqual(self.request("GET", "/v1/status?after=-1")[0], 400)
        self.assertEqual(self.request("GET", "/v1/status?after=1&after=2")[0], 400)
        self.assertEqual(self.request("POST", "/v1/stop", {"job_id": None})[1], {"job": None})
        self.assertTrue(self.manager.controller.jobs.empty())


if __name__ == "__main__":
    unittest.main()
