import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Company, CSV_FIELDS, Runner, StateStore, ValidationError, load_companies


class FakeAdapter:
    def __init__(self, failure=None, verified=True):
        self.failure, self.verified = failure, verified
        self.calls = []

    def action(self, name):
        self.calls.append(name)
        if self.failure == name:
            raise RuntimeError("secret-password")

    def prepare(self, company):
        self.action("prepare")
        return "READY"

    def check_zero(self, company): self.action("check_zero")
    def calculate(self, company): self.action("calculate")
    def check_attachments(self, company): self.action("check_attachments")
    def check_summary(self, company): self.action("check_summary")
    def calculation_is_ready(self):
        self.action("calculation_is_ready")
        return False
    def filing_status_text(self):
        self.action("filing_status_text")
        return ""
    def submit(self, company): self.action("submit")
    def verify(self, company):
        self.action("verify")
        return self.verified
    def evidence(self, company, state): return "evidence.png"


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.company = Company("测试公司", "12345678901234567A", "2026-09", 2)
        self.store = StateStore(self.directory / "state.json")

    def run_adapter(self, adapter):
        logs = []
        rows = Runner(adapter, self.store, logs.append).run([self.company], self.directory / "results.csv")
        self.assertNotIn("secret-password", "".join(logs))
        return rows

    def test_wrong_identity_stops_without_submit(self):
        adapter = FakeAdapter("prepare")
        rows = self.run_adapter(adapter)
        self.assertEqual(rows[0]["状态"], "FAILED")
        self.assertNotIn("submit", adapter.calls)

    def test_nonzero_stops_without_submit(self):
        adapter = FakeAdapter("check_zero")
        self.run_adapter(adapter)
        self.assertNotIn("submit", adapter.calls)

    def test_submit_failure_unknown_and_retry_never_submits(self):
        adapter = FakeAdapter("submit")
        self.assertEqual(self.run_adapter(adapter)[0]["状态"], "UNKNOWN")
        self.assertEqual(self.store.get(self.company)["state"], "UNKNOWN")
        self.store = StateStore(self.directory / "state.json")
        retry = FakeAdapter(verified=False)
        retry.filing_status_text = lambda: "申报成功,无需缴款"
        self.assertEqual(self.run_adapter(retry)[0]["状态"], "UNKNOWN")
        self.assertNotIn("submit", retry.calls)
        self.assertIn("verify", retry.calls)

    def test_success_retry_verifies_without_submit(self):
        self.assertEqual(self.run_adapter(FakeAdapter())[0]["状态"], "SUCCESS")
        retry = FakeAdapter()
        retry.filing_status_text = lambda: "申报成功,无需缴款"
        self.assertEqual(self.run_adapter(retry)[0]["状态"], "SUCCESS")
        self.assertNotIn("submit", retry.calls)
        self.assertIn("verify", retry.calls)

    def test_failed_feedback_is_unknown(self):
        adapter = FakeAdapter(verified=False)
        self.assertEqual(self.run_adapter(adapter)[0]["状态"], "UNKNOWN")
        self.assertEqual(self.store.get(self.company)["state"], "UNKNOWN")

    def test_write_failure_never_submits(self):
        adapter = FakeAdapter()
        with patch.object(self.store, "set", side_effect=OSError("disk full")):
            self.assertEqual(self.run_adapter(adapter)[0]["状态"], "FAILED")
        self.assertNotIn("submit", adapter.calls)

    def test_submit_observes_durable_state(self):
        test = self
        class Checked(FakeAdapter):
            def submit(self, company):
                persisted = StateStore(test.directory / "state.json")
                test.assertEqual(persisted.get(company)["state"], "SUBMITTING")
                super().submit(company)
        self.run_adapter(Checked())

    def test_crash_state_only_verifies(self):
        self.store.set(self.company, "SUBMITTING")
        adapter = FakeAdapter()
        adapter.filing_status_text = lambda: "申报成功,无需缴款"
        self.assertEqual(self.run_adapter(adapter)[0]["状态"], "SUCCESS")
        self.assertNotIn("submit", adapter.calls)
        self.assertIn("check_summary", adapter.calls)
        self.assertIn("verify", adapter.calls)

    def test_batch_stops_and_records_skipped(self):
        next_company = Company("第二公司", "12345678901234567B", "2026-09", 1)
        adapter = FakeAdapter("calculate")
        rows = Runner(adapter, self.store).run([self.company, next_company])
        self.assertEqual([r["状态"] for r in rows], ["FAILED", "SKIPPED"])
        self.assertEqual(adapter.calls.count("prepare"), 1)

    def test_history_does_not_skip_full_flow_when_client_is_pending(self):
        for state in ("SUCCESS", "UNKNOWN", "SUBMITTING"):
            with self.subTest(state=state):
                self.store.set(self.company, state)
                adapter = FakeAdapter()
                adapter.filing_status_text = lambda: "待申报"
                self.assertEqual(self.run_adapter(adapter)[0]["状态"], "SUCCESS")
                self.assertEqual(adapter.calls, ["prepare", "check_zero", "calculate",
                    "check_attachments", "check_summary", "submit", "verify"])

    def test_live_success_does_full_checks_but_never_resubmits(self):
        self.store.set(self.company, "UNKNOWN")
        adapter = FakeAdapter()
        adapter.filing_status_text = lambda: "申报成功,无需缴款"
        self.assertEqual(self.run_adapter(adapter)[0]["状态"], "SUCCESS")
        self.assertEqual(adapter.calls, ["prepare", "check_zero", "calculate",
            "check_attachments", "check_summary", "verify"])

    def write_csv(self, rows):
        path = self.directory / "companies.csv"
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(CSV_FIELDS)
            writer.writerows(rows)
        return path

    def test_valid_csv(self):
        companies = load_companies(self.write_csv([["测试公司", self.company.tax_id, "2026-09", "2", "否", "是"]]))
        self.assertEqual(companies[0].expected_people, 2)
        self.assertTrue(companies[0].generate_zero)

    def test_name_only_csv(self):
        companies = load_companies(self.write_csv([
            ["甲公司", "", "2026-09", "1", "否", "否"],
            ["乙公司", "", "2026-09", "2", "是", "否"],
        ]))
        self.assertEqual([company.name for company in companies], ["甲公司", "乙公司"])
        self.assertEqual(companies[0].tax_id, "")
        self.assertEqual(companies[0].key, "甲公司:2026-09")
        with self.assertRaises(ValidationError):
            load_companies(self.write_csv([
                ["甲公司", "", "2026-09", "1", "否", "否"],
                ["甲公司", "", "2026-09", "1", "否", "否"],
            ]))

    def test_invalid_csv_and_duplicates(self):
        base = ["测试公司", self.company.tax_id, "2026-09", "2", "否", "否"]
        for index, value in ((1, "abc"), (2, "2026-13"), (3, "0"), (4, "true")):
            row = base.copy()
            row[index] = value
            with self.subTest(index=index), self.assertRaises(ValidationError):
                load_companies(self.write_csv([row]))
        with self.assertRaises(ValidationError):
            load_companies(self.write_csv([base, base]))
        with self.assertRaises(ValidationError):
            load_companies(self.write_csv([]))

    def test_corrupt_state_fails_closed(self):
        self.store.path.write_text(json.dumps({"version": 1, "records": {"x": {"state": "BOGUS"}}}))
        with self.assertRaises(ValidationError):
            StateStore(self.store.path)


if __name__ == "__main__":
    unittest.main()
