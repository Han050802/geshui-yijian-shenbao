"""Native Mac companion. All tax/WeChat work stays in the paired Windows app.

This module deliberately does not import the Windows application. The GUI owns
no tax account, browser, or business-state files. A single background worker
makes HTTPS calls; a failed POST is never queued again automatically.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import queue
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from bridge_protocol import BridgeClient, BridgeError, Pairing, parse_pairing


ACTION_LABELS = {
    "read": "读取网页名单",
    "check_client": "检查扣缴端",
    "prepare": "准备并检查（不提交）",
    "run": "一键申报（实际提交）",
    "test_company": "测试公司（实际申报）",
    "test_partial": "测试部分 0（实际申报）",
}
SUBMITTING_TASKS = frozenset(("run", "test_company", "test_partial"))
BUSY_STATUSES = frozenset(("queued", "running"))
JOB_STATUSES = BUSY_STATUSES | {"completed", "failed", "interrupted"}
STATUS_LABELS = {
    "queued": "等待执行", "running": "正在执行", "completed": "执行结束",
    "failed": "执行失败", "interrupted": "已中断，请核对 Windows 结果",
}
BOOL_FIELDS = ("generate_zero", "allow_missing_insurance", "writeback")
TEXT_FIELDS = ("url", "work_month", "tax_month", "expected_people")
RESULT_FIELDS = ("公司名称", "税号", "税款所属月", "状态", "原因", "证据")
MONTH_PATTERN = re.compile(r"[1-9]\d{3}-(0[1-9]|1[0-2])\Z")


def validate_settings(values: dict) -> dict:
    """Return only the seven editable settings, without loading Windows files."""
    result = {}
    for key in TEXT_FIELDS:
        value = values.get(key, "")
        if key == "expected_people" and type(value) is int:
            value = str(value)
        if not isinstance(value, str):
            raise ValueError("月份请填写 YYYY-MM，人数请填写正整数。")
        result[key] = value.strip()
    if any(not MONTH_PATTERN.fullmatch(result[key]) for key in ("work_month", "tax_month")):
        raise ValueError("网页工作月份和税款所属月份请填写 YYYY-MM。")
    if not re.fullmatch(r"[1-9]\d*", result["expected_people"]):
        raise ValueError("每家预期人数请填写正整数。")
    result["expected_people"] = int(result["expected_people"])
    try:
        address = urlsplit(result["url"])
        valid_address = (address.scheme in ("http", "https") and address.hostname
                         and address.username is None and address.password is None)
        address.port  # Reject malformed ports before sending a job.
    except ValueError:
        valid_address = False
    if not valid_address:
        raise ValueError("工作台地址请填写完整的 http:// 或 https:// 地址，勿填账号密码。")
    for key in BOOL_FIELDS:
        if type(values.get(key)) is not bool:
            raise ValueError("请检查三个勾选项。")
        result[key] = values[key]
    return result


def submission_confirmation(task: str, settings: dict) -> str:
    if task not in SUBMITTING_TASKS:
        raise ValueError("该动作不属于实际申报。")
    scope = {
        "run": "本次网页名单中的企业",
        "test_company": "Windows 端配置的测试公司",
        "test_partial": "网页标记为“部分 0 申报”的企业",
    }[task]
    return (
        f"操作：{ACTION_LABELS[task]}\n"
        f"网页工作月份：{settings['work_month']}\n"
        f"税款所属月份：{settings['tax_month']}\n"
        f"每家预期人数：{settings['expected_people']}\n"
        f"范围：{scope}\n\n"
        "确认后会在 Windows 端真实提交个税申报，并按原流程处理回填和微信。\n"
        "请核对企业范围和月份，确认 Windows 已开机、登录并保持桌面可用。\n\n"
        "是否继续实际申报？"
    )


def connection_path() -> Path:
    return Path.home() / "Library" / "Application Support" / "TaxZeroMac" / "connection.json"


def _atomic_json(path: Path, value: dict, *, private: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=path.name + "-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        if private:
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ConnectionStore:
    """Only pairing credentials and opaque recovery IDs, never business settings."""

    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path is not None else connection_path()

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_size > 32_768:
            raise ValueError("保存的连接信息格式不正确。")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("保存的连接信息格式不正确。")
        pairing = parse_pairing(value.get("pairing", ""))
        recovery = {}
        for key in ("server_id", "tracked_server_id", "job_id", "job_status", "pending_request_id"):
            item = value.get(key)
            if item is not None and (not isinstance(item, str) or len(item) > 256):
                raise ValueError("保存的连接信息格式不正确。")
            recovery[key] = item
        os.chmod(self.path, 0o600)
        return {"pairing": pairing, **recovery}

    def save(self, pairing: Pairing, state: "ClientState") -> None:
        _atomic_json(self.path, {"version": 1, "pairing": pairing.encode(), **state.recovery_record()})

    def forget(self) -> None:
        self.path.unlink(missing_ok=True)


def _job(value: Any) -> dict | None:
    if value is None:
        return None
    if (not isinstance(value, dict) or not isinstance(value.get("id"), str)
            or not value["id"] or value.get("status") not in JOB_STATUSES):
        raise ValueError("Windows 任务状态格式不正确。")
    return dict(value)


@dataclass
class ClientState:
    """Recovery bookkeeping, kept independent of Tk and the HTTP worker."""

    server_id: str = ""
    tracked_server_id: str = ""
    job_id: str | None = None
    job_status: str | None = None
    pending_request_id: str | None = None
    active_job: dict | None = None
    view_job: dict | None = None
    connected: bool = False
    unresolved: bool = False
    last_event_seq: int = 0
    companies: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)

    @classmethod
    def from_record(cls, record: dict) -> "ClientState":
        state = cls(server_id=record.get("server_id") or "",
                    tracked_server_id=record.get("tracked_server_id") or record.get("server_id") or "",
                    job_id=record.get("job_id"), job_status=record.get("job_status"),
                    pending_request_id=record.get("pending_request_id"))
        state.unresolved = bool(state.pending_request_id or state.job_status in BUSY_STATUSES)
        return state

    def recovery_record(self) -> dict:
        return {"server_id": self.server_id, "tracked_server_id": self.tracked_server_id,
                "job_id": self.job_id, "job_status": self.job_status,
                "pending_request_id": self.pending_request_id}

    @property
    def busy(self) -> bool:
        return bool(self.active_job and self.active_job.get("status") in BUSY_STATUSES)

    @property
    def can_submit(self) -> bool:
        return self.connected and not self.busy and not self.unresolved

    def begin_submit(self, request_id: str) -> None:
        if not self.can_submit:
            raise ValueError("请先连接并等待当前任务状态明确。")
        self.pending_request_id = request_id
        self.tracked_server_id = self.server_id
        self.job_id = None
        self.job_status = None
        self.unresolved = True

    def acknowledge(self, value: dict) -> None:
        job = _job(value)
        if job is None or job.get("request_id") != self.pending_request_id:
            raise ValueError("Windows 任务回执与本次请求不一致。")
        self._track(job)
        self.pending_request_id = None
        self.unresolved = False
        self.active_job = job if job["status"] in BUSY_STATUSES else None

    def reject_submit(self) -> None:
        self.pending_request_id = None
        self.unresolved = False
        self.job_id = None
        self.job_status = None

    def _track(self, job: dict) -> None:
        self.view_job = job
        self.job_id, self.job_status = job["id"], job["status"]
        self.tracked_server_id = self.server_id

    def apply_status(self, value: dict) -> list[dict]:
        if (not isinstance(value, dict) or value.get("protocol") != 1
                or not isinstance(value.get("server_id"), str) or not value["server_id"]
                or not isinstance(value.get("settings"), dict)):
            raise ValueError("Windows 连接端版本或状态格式不正确。")
        active, last = _job(value.get("active_job")), _job(value.get("last_job"))
        recent = value.get("recent_jobs", [])
        if not isinstance(recent, list):
            raise ValueError("Windows 任务历史格式不正确。")
        candidates = [item for item in (active, last, *[_job(item) for item in recent]) if item]
        for key in ("companies", "results", "events"):
            if not isinstance(value.get(key, []), list) or any(not isinstance(row, dict) for row in value.get(key, [])):
                raise ValueError("Windows 返回列表格式不正确。")
        sequence = value.get("last_event_seq", 0)
        if type(sequence) is not int or sequence < 0:
            raise ValueError("Windows 进度位置不正确。")
        server_changed = bool(self.server_id and self.server_id != value["server_id"])
        if server_changed and not self.pending_request_id and self.job_status not in BUSY_STATUSES:
            self.job_id = self.job_status = self.view_job = None
            self.tracked_server_id = ""
        if self.server_id != value["server_id"] or sequence < self.last_event_seq:
            self.last_event_seq = 0
        self.server_id = value["server_id"]
        self.active_job = active if active and active["status"] in BUSY_STATUSES else None
        matched = None
        if not self.tracked_server_id or self.tracked_server_id == self.server_id:
            if self.pending_request_id:
                matched = next((job for job in candidates if job.get("request_id") == self.pending_request_id), None)
            elif self.job_id:
                matched = next((job for job in candidates if job["id"] == self.job_id), None)
        if matched:
            self._track(matched)
            self.pending_request_id = None
            self.unresolved = False
        elif self.pending_request_id or (self.job_id and self.job_status in BUSY_STATUSES):
            self.unresolved = True
        else:
            self.unresolved = False
            if active or last:
                self._track(active or last)
        # An active job from another client still blocks this client's actions.
        if self.active_job and not self.unresolved:
            self._track(self.active_job)
        self.companies = list(value.get("companies", []))
        self.results = list(value.get("results", []))
        events = [event for event in value.get("events", [])
                  if type(event.get("seq")) is int and self.last_event_seq < event["seq"] <= sequence]
        self.last_event_seq = max(self.last_event_seq, sequence)
        self.connected = True
        return events


@dataclass(frozen=True)
class NetworkReply:
    generation: int
    operation: str
    value: dict | None = None
    error: Exception | None = None


class NetworkWorker:
    """One serial worker; queued operations are each attempted exactly once."""

    def __init__(self):
        self.requests: queue.Queue = queue.Queue()
        self.replies: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name="TaxZeroMac-network", daemon=True)
        self.thread.start()

    def put(self, generation: int, operation: str, client: BridgeClient, **arguments) -> None:
        if self.closed.is_set():
            raise RuntimeError("连接窗口已关闭。")
        self.requests.put((generation, operation, client, arguments))

    def _run(self) -> None:
        while not self.closed.is_set():
            request = self.requests.get()
            if request is None or self.closed.is_set():
                return
            generation, operation, client, arguments = request
            try:
                if operation in ("connect", "status"):
                    value = client.status(**arguments)
                elif operation == "submit":
                    value = client.submit(**arguments)
                elif operation == "stop":
                    value = client.stop(**arguments)
                else:
                    raise ValueError("未知操作。")
                reply = NetworkReply(generation, operation, value=value)
            except Exception as exc:
                reply = NetworkReply(generation, operation, error=exc)
            self.replies.put(reply)

    def close(self) -> None:
        # Do not send a stop request when a user merely closes the Mac window.
        self.closed.set()
        self.requests.put(None)


class MacApp:
    def __init__(self, root: tk.Tk, *, read_saved: bool = True, enable_network: bool = True,
                 store: ConnectionStore | None = None, client_factory=BridgeClient):
        self.root, self.client_factory = root, client_factory
        self.state = ClientState()
        self.client: BridgeClient | None = None
        self.pairing: Pairing | None = None
        self.store = store if store is not None else (ConnectionStore() if read_saved else None)
        self.worker = NetworkWorker() if enable_network else None
        self.generation = 0
        self.pending_operations: set[str] = set()
        self.after_id = None
        self.closed = False
        self.user_pairing_accessed = False
        self.settings_dirty = False
        self.settings_server_id = ""
        self.loading_settings = False
        self.next_poll = 0.0
        self.log_lines = 0
        self._last_store_record = None
        self._build_ui()
        if read_saved:
            self._restore_connection()
        self._refresh_controls()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.stop())
        if self.worker is not None:
            self.after_id = self.root.after(100, self._drain)

    def _build_ui(self) -> None:
        self.root.title("个税一键申报 · Mac")
        self.root.geometry("1020x780")
        self.root.minsize(900, 700)
        outer = ttk.Frame(self.root, padding=(18, 14))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="个税一键申报", font=("TkDefaultFont", 22, "bold")).pack(anchor="w")
        ttk.Label(outer, text="Mac 操作 · Windows 执行", foreground="#425166").pack(anchor="w", pady=(2, 5))
        ttk.Label(outer, text="Windows 须开机、登录且桌面可用；两台电脑须在同一局域网。", foreground="#875a14").pack(anchor="w")
        ttk.Label(outer, text="关闭此窗口或断线只暂停查看，Windows 任务继续；需要停止时请点“停止”。",
                  foreground="#5a6472").pack(anchor="w", pady=(2, 9))

        pairing_row = ttk.Frame(outer)
        pairing_row.pack(fill="x")
        ttk.Label(pairing_row, text="Windows 连接码").pack(side="left")
        self.pairing_var = tk.StringVar(value="")
        self.pairing_entry = ttk.Entry(pairing_row, textvariable=self.pairing_var, show="•")
        self.pairing_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.pairing_entry.bind("<Return>", lambda _event: self.connect())
        self.paste_button = ttk.Button(pairing_row, text="粘贴连接码", command=self.paste_pairing)
        self.paste_button.pack(side="left", padx=(0, 6))
        self.connect_button = ttk.Button(pairing_row, text="连接", command=self.connect)
        self.connect_button.pack(side="left")
        self.forget_button = ttk.Button(pairing_row, text="忘记连接", command=self.forget)
        self.forget_button.pack(side="left", padx=(6, 0))
        self.connection_var = tk.StringVar(value="请从 Windows 连接窗口复制连接码，粘贴后点“连接”。")
        ttk.Label(outer, textvariable=self.connection_var, foreground="#425166", wraplength=960).pack(anchor="w", pady=(5, 9))

        fields = ttk.LabelFrame(outer, text="本次设置 · 连接后读取 Windows 设置", padding=(10, 7))
        fields.pack(fill="x")
        self.variables: dict[str, tk.Variable] = {}
        self.setting_widgets: list = []
        for key, value in (("url", ""), ("work_month", ""), ("tax_month", ""), ("expected_people", "1")):
            self.variables[key] = tk.StringVar(value=value)
        ttk.Label(fields, text="工作台地址").grid(row=0, column=0, sticky="w", pady=3)
        url_entry = ttk.Entry(fields, textvariable=self.variables["url"])
        url_entry.grid(row=0, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=3)
        self.setting_widgets.append(url_entry)
        for column, key, label in ((0, "work_month", "网页工作月份"), (2, "tax_month", "税款所属月份"),
                                   (4, "expected_people", "每家预期人数")):
            ttk.Label(fields, text=label).grid(row=1, column=column, sticky="w", pady=4, padx=(10 if column else 0, 0))
            entry = ttk.Entry(fields, textvariable=self.variables[key], width=13)
            entry.grid(row=1, column=column + 1, sticky="ew", padx=(8, 0), pady=4)
            self.setting_widgets.append(entry)
            fields.columnconfigure(column + 1, weight=1)
        for column, key, label, default in ((0, "generate_zero", "工资表为空时生成零工资", False),
                                             (2, "allow_missing_insurance", "允许三险一金为空", False),
                                             (4, "writeback", "成功后回填工作台", True)):
            self.variables[key] = tk.BooleanVar(value=default)
            widget = ttk.Checkbutton(fields, text=label, variable=self.variables[key])
            widget.grid(row=2, column=column, columnspan=2, sticky="w", pady=(5, 2))
            self.setting_widgets.append(widget)
        for variable in self.variables.values():
            variable.trace_add("write", self._settings_changed)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(10, 4))
        self.action_buttons = {}
        for index, (task, label) in enumerate(ACTION_LABELS.items()):
            button = ttk.Button(actions, text=label, command=lambda selected=task: self.action(selected))
            button.grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 7 if index % 3 < 2 else 0), pady=3)
            actions.columnconfigure(index % 3, weight=1)
            self.action_buttons[task] = button
        ttk.Label(outer, text="准备并检查：填写草稿并停在提交前，尚不发送申报。两个“测试”按钮均会实际申报。",
                  foreground="#875a14").pack(anchor="w", pady=(0, 7))

        progress_row = ttk.Frame(outer)
        progress_row.pack(fill="x", pady=(0, 6))
        self.progress_var = tk.StringVar(value="尚未连接")
        ttk.Label(progress_row, textvariable=self.progress_var, wraplength=735).pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(progress_row, text="停止（Esc）", command=self.stop)
        self.stop_button.pack(side="right")
        self.export_button = ttk.Button(progress_row, text="导出结果 CSV", command=self.export_results)
        self.export_button.pack(side="right", padx=7)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)
        company_frame = ttk.Frame(notebook)
        result_frame = ttk.Frame(notebook)
        notebook.add(company_frame, text="企业名单")
        notebook.add(result_frame, text="申报结果")
        self.company_tree = self._tree(company_frame, (
            ("company", "公司名称", 370), ("month", "税款所属月", 130),
            ("people", "预期人数", 90), ("status", "结果", 240)), height=7)
        self.result_tree = self._tree(result_frame, (
            ("company", "公司名称", 230), ("month", "税款所属月", 100),
            ("status", "状态", 160), ("reason", "原因", 300), ("evidence", "Windows 证据路径", 200)), height=7)

        log_frame = ttk.LabelFrame(outer, text="Windows 运行进度", padding=5)
        log_frame.pack(fill="both", expand=True, pady=(8, 0))
        self.console = tk.Text(log_frame, height=6, wrap="word", state="disabled", relief="flat",
                               font=("TkDefaultFont", 11), highlightthickness=0)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.console.yview)
        self.console.configure(yscrollcommand=log_scroll.set)
        self.console.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    @staticmethod
    def _tree(parent, columns, height):
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree = ttk.Treeview(parent, columns=tuple(key for key, _label, _width in columns),
                            show="headings", height=height)
        for key, label, width in columns:
            tree.heading(key, text=label)
            tree.column(key, width=width, minwidth=70, stretch=True)
        vertical = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        horizontal = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        return tree

    def _settings_changed(self, *_arguments) -> None:
        if not self.loading_settings:
            self.settings_dirty = True

    def _restore_connection(self) -> None:
        if self.store is None:
            return
        self.user_pairing_accessed = True
        try:
            saved = self.store.load()
            if saved:
                self.pairing = saved["pairing"]
                self.pairing_var.set(self.pairing.encode())
                self.state = ClientState.from_record(saved)
                self.connection_var.set("已保存连接码。点“连接”恢复查看，程序不会自动发起任务。")
        except (OSError, ValueError, BridgeError):
            self.connection_var.set("保存的连接信息无法读取。请重新粘贴 Windows 连接码。")

    def _save_connection(self) -> bool:
        if self.store is None or self.pairing is None:
            return True
        record = (self.pairing.encode(), self.state.recovery_record())
        if record == self._last_store_record:
            return True
        try:
            self.store.save(self.pairing, self.state)
            self._last_store_record = record
            return True
        except OSError:
            self._log("连接恢复记录无法保存；请检查 Mac 用户目录的写入权限。")
            return False

    def paste_pairing(self) -> None:
        if self.closed or self.pending_operations or self.state.busy:
            return
        try:
            value = self.root.clipboard_get()
            if not isinstance(value, str) or not value.strip():
                raise ValueError("剪贴板中没有文字。")
        except (tk.TclError, ValueError, TypeError, UnicodeError):
            messagebox.showinfo("请先复制连接码", "剪贴板中没有可用的文字。请先复制 Windows 连接窗口里的完整连接码，再点“粘贴连接码”。", parent=self.root)
            return
        self.pairing_var.set(value.strip())

    def connect(self) -> None:
        if self.closed or self.worker is None or self.pending_operations:
            return
        try:
            pairing = parse_pairing(self.pairing_var.get())
        except BridgeError:
            messagebox.showerror("连接码需要修正", "请完整粘贴 Windows 连接窗口提供的连接码。", parent=self.root)
            return
        if self.pairing is not None and pairing != self.pairing:
            if self.state.busy or self.state.unresolved:
                messagebox.showinfo("请先核对当前任务", "当前任务仍在运行或待核实。请先在 Windows 核对，再忘记连接后切换电脑。", parent=self.root)
                return
            self.state = ClientState()
            self.settings_server_id = ""
            self.settings_dirty = False
        self.pairing = pairing
        self.client = self.client_factory(pairing, timeout=5)
        self.generation += 1
        self.state.connected = False
        self.connection_var.set("正在连接 Windows…")
        self._queue("connect", after=self.state.last_event_seq)
        self._refresh_controls()

    def _queue(self, operation: str, **arguments) -> None:
        if self.worker is None or self.client is None or self.closed:
            raise RuntimeError("尚未连接 Windows。")
        self.pending_operations.add(operation)
        self.worker.put(self.generation, operation, self.client, **arguments)

    def action(self, task: str) -> None:
        if task not in ACTION_LABELS or not self.state.can_submit or "submit" in self.pending_operations:
            return
        try:
            settings = validate_settings({key: variable.get() for key, variable in self.variables.items()})
        except (ValueError, tk.TclError) as exc:
            messagebox.showerror("设置需要修正", str(exc), parent=self.root)
            return
        if task in SUBMITTING_TASKS and not messagebox.askyesno(
                "确认实际申报", submission_confirmation(task, settings), parent=self.root,
                icon="warning", default=messagebox.NO):
            return
        # Tk continues handling status replies while a confirmation dialog is open.
        if not self.state.can_submit or "submit" in self.pending_operations:
            messagebox.showinfo("任务状态已变化", "Windows 的连接或任务状态已变化。请检查当前状态后再操作。", parent=self.root)
            return
        request_id = str(uuid.uuid4())
        self.state.begin_submit(request_id)
        # Persist the request ID BEFORE a POST, so reopening cannot silently lose it.
        if not self._save_connection():
            self.state.reject_submit()
            self._refresh_controls()
            messagebox.showerror("暂未发送", "恢复记录未能保存，本次任务尚未发送。请先修复 Mac 用户目录的写入权限。", parent=self.root)
            return
        if task == "prepare":
            self._log("准备并检查：将在 Windows 填写草稿并停在提交前，尚不发送申报。")
        self._queue("submit", task=task, settings=settings, request_id=request_id)
        self.progress_var.set("正在发送一次任务请求，等待 Windows 回执…")
        self._refresh_controls()

    def stop(self) -> None:
        if (not self.state.connected or not self.state.busy or self.closed
                or "stop" in self.pending_operations or "submit" in self.pending_operations):
            return
        self._queue("stop", job_id=self.state.active_job["id"])
        self._log("正在请求停止当前任务。已发送的申报仍需在 Windows 核验结果。")
        self._refresh_controls()

    def forget(self) -> None:
        if self.pending_operations:
            return
        if (self.state.busy or self.state.unresolved) and not messagebox.askyesno(
                "忘记连接", "忘记连接只删除这台 Mac 保存的连接码，不会停止 Windows 任务。\n"
                "请先在 Windows 核对正在运行或待核实的任务。仍要忘记连接吗？",
                parent=self.root, default=messagebox.NO):
            return
        try:
            if self.store is not None:
                self.store.forget()
        except OSError:
            messagebox.showerror("未能忘记连接", "保存的连接文件无法删除，请检查用户目录权限。", parent=self.root)
            return
        self.generation += 1
        self.client = self.pairing = None
        self.state = ClientState()
        self._last_store_record = None
        self.settings_server_id = ""
        self.settings_dirty = False
        self.pairing_var.set("")
        self.connection_var.set("已忘记连接。请粘贴 Windows 连接码后重新连接。")
        self._load_settings({})
        self._render_tables()
        self._clear_log()
        self._refresh_controls()

    def _drain(self) -> None:
        if self.closed or self.worker is None:
            return
        while True:
            try:
                reply = self.worker.replies.get_nowait()
            except queue.Empty:
                break
            if reply.generation != self.generation:
                continue
            self.pending_operations.discard(reply.operation)
            if reply.error is not None:
                self._network_failed(reply.operation, reply.error)
            else:
                try:
                    self._received(reply.operation, reply.value)
                except (ValueError, KeyError, TypeError) as exc:
                    # A malformed submit reply may still correspond to an accepted job.
                    self._network_failed(reply.operation, exc)
            self._refresh_controls()
        if (self.state.connected and not self.pending_operations
                and time.monotonic() >= self.next_poll):
            self._queue("status", after=self.state.last_event_seq)
        self.after_id = self.root.after(100, self._drain)

    def _received(self, operation: str, value: dict) -> None:
        if operation in ("connect", "status"):
            previous_server = self.state.server_id
            events = self.state.apply_status(value)
            if previous_server and previous_server != self.state.server_id:
                self._clear_log()
                self._log("Windows 连接身份已变化；旧任务只按原身份核对，不会重新发送。")
            if self.settings_server_id != self.state.server_id or not self.settings_dirty:
                self._load_settings(value["settings"])
                self.settings_server_id = self.state.server_id
            hostname = str(value.get("hostname") or "Windows")
            self.connection_var.set(f"已连接 {hostname} · 任务在 Windows 执行；连接码已保存在这台 Mac。")
            for event in events:
                if event.get("kind") == "log":
                    self._log(str(event.get("value", "")))
            if operation == "connect":
                self._log("连接成功，已恢复 Windows 当前状态。未自动发送任何任务。")
            self._render_tables()
        elif operation == "submit":
            self.state.acknowledge(value["job"])
            self.settings_dirty = False
            self._log("Windows 已接受任务，正在跟踪同一任务的进度。")
        elif operation == "stop":
            job = _job(value.get("job"))
            if job and self.state.active_job and job["id"] == self.state.active_job["id"]:
                self.state._track(job)
                self.state.active_job = job if job["status"] in BUSY_STATUSES else None
            self._log("Windows 已收到停止请求；请等待当前步骤结束并核验已提交的结果。")
        self.next_poll = time.monotonic() + (0.1 if operation in ("submit", "stop") else 1.0)
        self._save_connection()

    def _network_failed(self, operation: str, error: Exception) -> None:
        if operation == "submit":
            rejected = isinstance(error, BridgeError) and not error.uncertain
            if rejected:
                self.state.reject_submit()
                self._log("Windows 未接受本次任务。请重新连接并检查状态后再操作。")
            else:
                self._log("任务请求回执未确认。已保留请求编号；重新连接只核实同一任务，不会重发。")
        elif operation == "stop":
            self._log("停止请求未得到确认。不会自动重发；请重新连接查看，必要时在 Windows 停止。")
        else:
            self._log("连接已断开，已停止自动查看。Windows 任务可能继续；请重新连接恢复进度。")
        if isinstance(error, BridgeError):
            self._log(str(error)[:1200])
        self.state.connected = False
        self.connection_var.set("未连接 · 请确认 Windows 连接端已打开、未休眠，并与 Mac 在同一局域网，然后点“重新连接”。")
        self._save_connection()

    def _load_settings(self, settings: dict) -> None:
        self.loading_settings = True
        try:
            for key in TEXT_FIELDS:
                self.variables[key].set(str(settings.get(key, "1" if key == "expected_people" else "")))
            for key in BOOL_FIELDS:
                value = settings.get(key, key == "writeback")
                self.variables[key].set(value if type(value) is bool else False)
        finally:
            self.loading_settings = False
        self.settings_dirty = False

    def _refresh_controls(self) -> None:
        changing = "submit" in self.pending_operations or "stop" in self.pending_operations
        enabled = self.state.can_submit and not changing
        for button in self.action_buttons.values():
            button.configure(state="normal" if enabled else "disabled")
        for widget in self.setting_widgets:
            widget.configure(state="normal" if enabled else "disabled")
        self.stop_button.configure(state="normal" if self.state.connected and self.state.busy and not changing else "disabled")
        self.export_button.configure(state="normal" if self.state.results else "disabled")
        self.connect_button.configure(state="disabled" if self.pending_operations else "normal",
                                      text="重新连接" if self.pairing is not None else "连接")
        self.forget_button.configure(state="disabled" if self.pending_operations else "normal")
        pairing_state = "disabled" if self.pending_operations or self.state.busy else "normal"
        self.pairing_entry.configure(state=pairing_state)
        self.paste_button.configure(state=pairing_state)
        if "submit" in self.pending_operations:
            self.progress_var.set("正在发送一次任务请求，等待 Windows 回执…")
        elif self.state.unresolved:
            self.progress_var.set("上次任务待核实，已暂停新任务。重连可恢复查看；查不到时请先到 Windows 核对。")
        elif not self.state.connected:
            self.progress_var.set("尚未连接" if not self.state.job_id else "连接已断开；Windows 任务状态待重新查看")
        elif self.state.view_job:
            job = self.state.view_job
            task = ACTION_LABELS.get(job.get("task"), "Windows 任务")
            self.progress_var.set(f"{task} · {STATUS_LABELS[job['status']]} · 企业 {len(self.state.companies)} 家 / 结果 {len(self.state.results)} 条")
        else:
            self.progress_var.set(f"已连接，等待操作 · 企业 {len(self.state.companies)} 家 / 结果 {len(self.state.results)} 条")

    def _render_tables(self) -> None:
        status_by_key = {(str(row.get("公司名称", "")).strip(), str(row.get("税款所属月", ""))): row.get("状态", "")
                         for row in self.state.results}
        for tree in (self.company_tree, self.result_tree):
            children = tree.get_children()
            if children:
                tree.delete(*children)
        for index, row in enumerate(self.state.companies):
            name, month = str(row.get("name", "")), str(row.get("month", ""))
            self.company_tree.insert("", "end", iid=f"company-{index}", values=(
                name, month, row.get("expected_people", ""), status_by_key.get((name.strip(), month), "待处理")))
        for index, row in enumerate(self.state.results):
            self.result_tree.insert("", "end", iid=f"result-{index}", values=tuple(
                row.get(key, "") for key in ("公司名称", "税款所属月", "状态", "原因", "证据")))

    def _log(self, value: str) -> None:
        # Do not echo connection codes or exception representations into logs.
        text = str(value).replace("\x00", "")
        if self.pairing is not None:
            text = text.replace(self.pairing.token, "[连接凭据已隐藏]").replace(self.pairing.encode(), "[连接码已隐藏]")
        self.console.configure(state="normal")
        self.console.insert("end", text + "\n")
        self.log_lines += text.count("\n") + 1
        if self.log_lines > 2000:
            self.console.delete("1.0", f"{self.log_lines - 1600 + 1}.0")
            self.log_lines = 1600
        self.console.see("end")
        self.console.configure(state="disabled")

    def _clear_log(self) -> None:
        self.console.configure(state="normal")
        self.console.delete("1.0", "end")
        self.console.configure(state="disabled")
        self.log_lines = 0

    def export_results(self) -> None:
        if not self.state.results:
            return
        target = filedialog.asksaveasfilename(parent=self.root, title="导出 Windows 申报结果",
                                            defaultextension=".csv", filetypes=(("CSV 文件", "*.csv"),),
                                            initialfile=f"个税申报结果_{datetime.now():%Y%m%d_%H%M%S}.csv")
        if not target:
            return
        try:
            write_results_csv(Path(target), self.state.results)
        except (OSError, ValueError, TypeError, csv.Error):
            messagebox.showerror("导出失败", "结果文件未能写入，请选择可写入的位置。", parent=self.root)
            return
        self._log(f"已导出 {len(self.state.results)} 条结果。")

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.generation += 1
        if self.after_id is not None:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        if self.worker is not None:
            self.worker.close()
        self.root.destroy()


def write_results_csv(path: Path, results: list[dict]) -> None:
    fields = list(RESULT_FIELDS)
    for row in results:
        for key in row:
            if key not in fields:
                fields.append(key)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)


def run_self_test(report_path: Path) -> int:
    """Construct the complete real Tk UI, with pairing and networking disabled."""
    root = None
    app = None
    report = {"status": "failed", "offline": True, "ui_created": False,
              "user_pairing_accessed": False, "checks": {}}
    try:
        root = tk.Tk()
        root.withdraw()
        app = MacApp(root, read_saved=False, enable_network=False)
        root.update_idletasks()
        root.update()
        report["ui_created"] = bool(root.winfo_exists() and app.company_tree.winfo_exists()
                                    and app.result_tree.winfo_exists() and app.console.winfo_exists())
        report["user_pairing_accessed"] = app.user_pairing_accessed
        report["checks"] = {
            "tkinter": bool(root.tk.call("info", "patchlevel")),
            "six_actions": tuple(app.action_buttons) == tuple(ACTION_LABELS),
            "submission_labels_explicit": all("实际" in app.action_buttons[key].cget("text") for key in SUBMITTING_TASKS),
            "actions_disabled_disconnected": all(button.instate(("disabled",)) for button in app.action_buttons.values()),
            "settings_disabled_disconnected": all(widget.instate(("disabled",)) for widget in app.setting_widgets),
            "stop_disabled_disconnected": app.stop_button.instate(("disabled",)),
            "export_disabled_without_results": app.export_button.instate(("disabled",)),
            "connection_controls_present": (app.connect_button.winfo_exists() and app.forget_button.winfo_exists()
                                            and app.paste_button.winfo_exists() and not app.paste_button.instate(("disabled",))),
            "empty_business_views": not app.company_tree.get_children() and not app.result_tree.get_children(),
            "no_network_worker": app.worker is None and app.client is None and not app.pending_operations,
            "no_saved_pairing_access": app.store is None and not app.user_pairing_accessed and not app.pairing_var.get(),
            "no_scheduled_network_poll": app.after_id is None,
        }
        if not report["ui_created"] or not all(report["checks"].values()):
            raise RuntimeError("离线界面自检未通过。")
        report["status"] = "passed"
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        if app is not None:
            app.close()
        elif root is not None:
            root.destroy()
    try:
        _atomic_json(Path(report_path), report)
    except OSError:
        return 3
    return 0 if report["status"] == "passed" else 2


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="个税一键申报 Mac 客户端：Mac 操作，Windows 执行。")
    parser.add_argument("--self-test", action="store_true", help="离线创建完整界面，不读连接码、不联网、不发送任务")
    parser.add_argument("--report", type=Path, help="离线自检 JSON 报告路径")
    arguments = parser.parse_args(argv)
    if arguments.self_test:
        if arguments.report is None:
            parser.error("--self-test 需要同时指定 --report <路径>")
        return run_self_test(arguments.report)
    if arguments.report is not None:
        parser.error("--report 仅用于 --self-test")
    root = tk.Tk()
    MacApp(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
