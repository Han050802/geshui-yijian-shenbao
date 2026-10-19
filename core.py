"""Batch zero-return orchestration. No desktop or browser interaction lives here."""
from __future__ import annotations

import csv
import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Protocol


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Company:
    name: str
    tax_id: str
    month: str
    expected_people: int
    allow_missing_insurance: bool = False
    generate_zero: bool = False

    @property
    def key(self) -> str:
        return f"{self.name.strip()}:{self.month}"

    def validate(self) -> None:
        if not self.name.strip():
            raise ValidationError("公司名称不能为空")
        # Company rows are located by name. A tax id is optional, and checked
        # only when one was actually supplied.
        if self.tax_id.strip() and not re.fullmatch(r"[A-Z0-9]{18}", self.tax_id.strip()):
            raise ValidationError("税号须为18位大写字母或数字")
        if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", self.month):
            raise ValidationError("税款所属月须为YYYY-MM")
        if self.month.startswith("0000"):
            raise ValidationError("年份不能为0000")
        if type(self.expected_people) is not int or self.expected_people <= 0:
            raise ValidationError("预期人数须为正整数")
        if type(self.allow_missing_insurance) is not bool or type(self.generate_zero) is not bool:
            raise ValidationError("允许选项必须为布尔值")


CSV_FIELDS = ["公司名称", "税号", "税款所属月", "预期人数", "允许三险一金为空", "允许生成零工资"]
RESULT_FIELDS = ["公司名称", "税号", "税款所属月", "状态", "原因", "证据"]


def _boolean(value: str, field: str) -> bool:
    if value not in ("是", "否"):
        raise ValidationError(f"{field}须为是或否")
    return value == "是"


def validate_companies(companies: Iterable[Company]) -> list[Company]:
    items = list(companies)
    if not items:
        raise ValidationError("公司名单不能为空")
    keys = set()
    tax_ids = set()
    for company in items:
        company.validate()
        if company.key in keys:
            raise ValidationError(f"重复公司与所属月：{company.name} {company.month}")
        keys.add(company.key)
        tax_id = company.tax_id.strip()
        if tax_id:
            if tax_id in tax_ids:
                raise ValidationError(f"重复税号与所属月：{company.name} {company.month}")
            tax_ids.add(tax_id)
    return items


def load_companies(path: str | Path) -> list[Company]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != CSV_FIELDS:
            raise ValidationError("CSV表头必须依次为：" + ",".join(CSV_FIELDS))
        companies = []
        for row_number, row in enumerate(reader, 2):
            try:
                if None in row or any(value is None for value in row.values()):
                    raise ValidationError("CSV列数不正确")
                row = {key: value.strip() for key, value in row.items()}
                people = row["预期人数"]
                if not re.fullmatch(r"[1-9]\d*", people):
                    raise ValidationError("预期人数须为正整数")
                company = Company(row["公司名称"], row["税号"], row["税款所属月"], int(people),
                                  _boolean(row["允许三险一金为空"], "允许三险一金为空"),
                                  _boolean(row["允许生成零工资"], "允许生成零工资"))
                company.validate()
                companies.append(company)
            except (ValueError, KeyError) as exc:
                raise ValidationError(f"CSV第{row_number}行：{exc}") from exc
    return validate_companies(companies)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        # POSIX directory sync strengthens rename durability; Windows does not
        # expose directory handles through os.open, but file contents are synced.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StateStore:
    """Fail closed on corrupt state; caller holds the global process lock."""
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.records = {}
        if self.path.exists():
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if payload.get("version") != 1 or not isinstance(payload.get("records"), dict):
                raise ValidationError("状态文件格式错误，禁止继续申报")
            self.records = payload["records"]
            for record in self.records.values():
                if not isinstance(record, dict) or record.get("state") not in ("SUBMITTING", "UNKNOWN", "SUCCESS"):
                    raise ValidationError("状态文件含未知记录，禁止继续申报")
                steps = record.get("steps", {})
                if not isinstance(steps, dict):
                    raise ValidationError("后续办理状态格式错误，禁止继续")
                for name, step in steps.items():
                    if (name not in ("wechat", "workbench") or not isinstance(step, dict)
                            or step.get("state") not in ("PENDING", "IN_PROGRESS", "UNKNOWN", "DONE")):
                        raise ValidationError("后续办理状态含未知记录，禁止继续")
                    if (name == "wechat" and step["state"] == "DONE"
                            and (not isinstance(step.get("destination"), str) or not step["destination"].strip())):
                        raise ValidationError("微信完成记录缺少发送对象，禁止自动重发")

    def get(self, company: Company) -> dict | None:
        record = self.records.get(company.key)
        return copy.deepcopy(record) if record else None

    def set(self, company: Company, state: str, reason: str = "", evidence: str = "") -> None:
        if state not in ("SUBMITTING", "UNKNOWN", "SUCCESS"):
            raise ValueError("Invalid durable state")
        updated = copy.deepcopy(self.records)
        record = updated.get(company.key)
        if record is None:
            record = {"steps": {name: {"state": "PENDING"} for name in ("wechat", "workbench")}}
        record.update(state=state, reason=reason, evidence=evidence,
                      updated_at=datetime.now(timezone.utc).isoformat())
        updated[company.key] = record
        _atomic_write(self.path, json.dumps({"version": 1, "records": updated}, ensure_ascii=False, indent=2))
        self.records = updated

    def step(self, company: Company, name: str) -> dict | None:
        record = self.get(company) or {}
        return record.get("steps", {}).get(name)

    def set_step(self, company: Company, name: str, state: str, *, destination: str = "", reason: str = "") -> None:
        if name not in ("wechat", "workbench") or state not in ("PENDING", "IN_PROGRESS", "UNKNOWN", "DONE"):
            raise ValueError("Invalid follow-up state")
        if name == "wechat" and state == "DONE" and (not isinstance(destination, str) or not destination.strip()):
            raise ValueError("Receipt destination is required")
        updated = copy.deepcopy(self.records)
        record = updated.get(company.key)
        if record is None or record["state"] != "SUCCESS":
            raise ValidationError("仅在税局成功已核验后记录后续办理状态")
        record.setdefault("steps", {})[name] = {
            "state": state, "destination": destination, "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write(self.path, json.dumps({"version": 1, "records": updated}, ensure_ascii=False, indent=2))
        self.records = updated


class Adapter(Protocol):
    def prepare(self, company: Company) -> str: ...
    def reconcile(self, company: Company) -> str: ...
    def check_zero(self, company: Company) -> None: ...
    def calculate(self, company: Company) -> None: ...
    def check_attachments(self, company: Company) -> None: ...
    def check_summary(self, company: Company) -> None: ...
    def submit(self, company: Company) -> None: ...
    def verify(self, company: Company) -> bool: ...
    def evidence(self, company: Company, state: str) -> str: ...


class Runner:
    def __init__(self, adapter: Adapter, store: StateStore, log: Callable[[str], None] | None = None):
        self.adapter, self.store = adapter, store
        self.log = log or (lambda message: None)

    def _log(self, message: str) -> None:
        # Diagnostics must not change submission control flow.
        try:
            self.log(message)
        except Exception:
            pass

    def _evidence(self, company: Company, state: str) -> str:
        try:
            return str(self.adapter.evidence(company, state) or "")
        except Exception:
            return ""

    def run(self, companies: Iterable[Company], result_path: str | Path | None = None) -> list[dict]:
        items = validate_companies(companies)
        results = []
        stopped = False
        for company in items:
            if stopped:
                results.append(self._row(company, "SKIPPED", "前一公司异常，批次已停止", ""))
                continue
            phase = "PREPARE"
            submitted = False
            try:
                self._log(f"{company.name} {company.month}：核验公司身份与税款所属月")
                previous = self.store.get(company)
                if previous:
                    phase = "RECONCILE"
                    self._log("存在历史提交记录：只读取税局结果，不复制工资、不算税、不再次发送")
                    if self.adapter.reconcile(company) != "READY":
                        raise RuntimeError("未能进入历史申报核验页面")
                    prepared = "ALREADY_SUCCESS"
                else:
                    prepared = self.adapter.prepare(company)
                if prepared not in ("READY", "ALREADY_SUCCESS"):
                    raise RuntimeError("prepare未返回明确状态")
                if prepared != "ALREADY_SUCCESS":
                    phase = "CHECK_ZERO"
                    self._log("完整流程：检查工资表与零金额")
                    self.adapter.check_zero(company)
                    phase = "CALCULATE"
                    self._log("完整流程：税款计算")
                    self.adapter.calculate(company)
                    phase = "CHECK_ATTACHMENTS"
                    self._log("完整流程：检查附表")
                    self.adapter.check_attachments(company)
                    phase = "CHECK_SUMMARY"
                    self._log("完整流程：核对申报汇总与当前申报状态")
                    self.adapter.check_summary(company)
                status = self.adapter.filing_status_text()
                if prepared == "ALREADY_SUCCESS" or (isinstance(status, str) and "申报成功" in status):
                    phase = "VERIFY"
                    self._log("核验已有申报记录，不再发送")
                    if self.adapter.verify(company) is not True:
                        raise RuntimeError("已有提交记录未核验成功，禁止重复提交")
                else:
                    if status not in ("待申报", "未申报"):
                        raise RuntimeError("申报状态不明确，禁止发送")
                    phase = "PERSIST_SUBMITTING"
                    self.store.set(company, "SUBMITTING", "提交前已持久化")
                    submitted = True
                    phase = "SUBMIT"
                    self.adapter.submit(company)
                    phase = "VERIFY"
                    if self.adapter.verify(company) is not True:
                        raise RuntimeError("提交后未取得明确申报成功结果")
                phase = "PERSIST_SUCCESS"
                evidence = self._evidence(company, "SUCCESS")
                self.store.set(company, "SUCCESS", "已核验申报成功", evidence)
                results.append(self._row(company, "SUCCESS", "已核验申报成功", evidence))
                self._log(f"{company.name} {company.month}：申报成功已核验")
            except Exception as exc:
                try:
                    diagnostic = getattr(self.adapter, "record_error", None)
                    if diagnostic:
                        diagnostic(phase, exc)
                except Exception:
                    pass
                previous = self.store.get(company)
                already_recorded = bool(previous and previous["state"] in ("SUBMITTING", "UNKNOWN", "SUCCESS"))
                state = "UNKNOWN" if submitted or already_recorded else "FAILED"
                # Do not log arbitrary adapter exception text: it may contain
                # credentials. Adapter diagnostic codes can be reviewed locally.
                reason = f"{phase}失败（{type(exc).__name__}）"
                # Only the adapter's explicitly logged diagnostic is safe to
                # show; arbitrary exception strings may include credentials.
                detail = getattr(self.adapter, "last_error", "")
                if type(exc).__name__ == "DesktopError" and isinstance(detail, str) and detail:
                    reason += "：" + detail.splitlines()[0]
                if submitted:
                    reason += "；本次发送结果未确认，请核对扣缴端状态"
                elif already_recorded:
                    reason += "；历史发送结果仍待核验，本次未再次发送，请人工核对扣缴端"
                else:
                    reason += "；本次尚未执行发送申报"
                evidence = self._evidence(company, state)
                if state == "UNKNOWN" and not (previous and previous["state"] == "SUCCESS"):
                    try:
                        self.store.set(company, "UNKNOWN", reason, evidence)
                    except Exception:
                        reason += "；状态写盘失败，请保留原状态文件"
                results.append(self._row(company, state, reason, evidence))
                self._log(f"{company.name} {company.month}：{reason}")
                stopped = True
        if result_path is not None:
            write_results(result_path, results)
        return results

    @staticmethod
    def _row(company: Company, state: str, reason: str, evidence: str) -> dict:
        return dict(zip(RESULT_FIELDS, (company.name, company.tax_id, company.month, state, reason, evidence)))


def write_results(path: str | Path, results: list[dict]) -> None:
    import io
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=RESULT_FIELDS)
    writer.writeheader()
    writer.writerows(results)
    _atomic_write(Path(path), "\ufeff" + stream.getvalue())

