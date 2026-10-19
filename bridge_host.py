"""Authenticated LAN bridge for the existing Windows desktop worker.

This process stays in the signed-in user's desktop.  It does not expose Python,
shell commands, selectors, passwords, or an unattended service interface.
"""
from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from bridge_protocol import (BridgeError, Pairing, ensure_server_identity,
                             local_ipv4_addresses, parse_pairing)
from core import RESULT_FIELDS, _atomic_write


TASKS = frozenset({"read", "check_client", "prepare", "run", "test_company", "test_partial"})
SETTING_KEYS = ("url", "work_month", "tax_month", "expected_people", "generate_zero",
                "allow_missing_insurance", "writeback")
JOB_KEYS = ("id", "request_id", "task", "status", "created_at", "finished_at", "error")
JOB_STATES = frozenset({"queued", "running", "completed", "failed", "interrupted"})
MAX_BODY = 64 * 1024
BRIDGE_PORT = 8766
_LAN_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                      ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_FIXED_TEST_SETTINGS = {
    "url": "http://127.0.0.1:9/", "work_month": "2025-02", "tax_month": "2025-01",
    "expected_people": 1, "generate_zero": False, "allow_missing_insurance": False,
    "writeback": False,
}


class HostError(BridgeError):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code


def _now():
    return datetime.now(timezone.utc).isoformat()


def is_lan_address(address):
    """Use explicit RFC1918 ranges, not ipaddress's broader 'private' label."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and any(ip in network for network in _LAN_NETWORKS)


def interactive_desktop_ready():
    """Fail closed for services, disconnected sessions, and a locked desktop.

    Only reads session/desktop information; never switches or unlocks a desktop.
    The actual tax-client check remains the original check_client operation.
    """
    if os.name != "nt":
        return False
    from ctypes import wintypes
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user = ctypes.WinDLL("user32", use_last_error=True)
        wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
        kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        kernel.ProcessIdToSessionId.restype = wintypes.BOOL
        kernel.GetCurrentThreadId.restype = wintypes.DWORD
        session = wintypes.DWORD()
        if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)) or session.value == 0:
            return False
        wts.WTSQuerySessionInformationW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                   ctypes.c_int, ctypes.POINTER(ctypes.c_void_p),
                                                   ctypes.POINTER(wintypes.DWORD)]
        wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        buffer, size = ctypes.c_void_p(), wintypes.DWORD()
        if not wts.WTSQuerySessionInformationW(None, session.value, 8, ctypes.byref(buffer), ctypes.byref(size)):
            return False
        try:
            if size.value < ctypes.sizeof(ctypes.c_int) or ctypes.cast(buffer, ctypes.POINTER(ctypes.c_int))[0] != 0:
                return False  # WTSActive == 0
        finally:
            wts.WTSFreeMemory(buffer)
        user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        user.OpenInputDesktop.restype = wintypes.HANDLE
        user.GetThreadDesktop.argtypes = [wintypes.DWORD]
        user.GetThreadDesktop.restype = wintypes.HANDLE
        user.CloseDesktop.argtypes = [wintypes.HANDLE]
        user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                  wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        user.GetUserObjectInformationW.restype = wintypes.BOOL

        def desktop_name(handle):
            name = ctypes.create_unicode_buffer(256)
            needed = wintypes.DWORD()
            if not handle or not user.GetUserObjectInformationW(handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)):
                return ""
            return name.value

        handle = user.OpenInputDesktop(0, False, 0x0001 | 0x0100)
        if not handle:
            return False
        try:
            return (desktop_name(handle).casefold() == "default" and
                    desktop_name(user.GetThreadDesktop(kernel.GetCurrentThreadId())).casefold() == "default")
        finally:
            user.CloseDesktop(handle)
    except (OSError, AttributeError, ValueError):
        return False


def _request_uuid(value):
    if not isinstance(value, str) or len(value) != 36:
        raise HostError(400, "invalid_request_id", "request_id 必须是 UUID。")
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise HostError(400, "invalid_request_id", "request_id 必须是 UUID。") from exc


def _validate_settings(values):
    if not isinstance(values, dict) or set(values) - set(SETTING_KEYS):
        raise HostError(400, "invalid_settings", "只允许修改工作台地址、月份、人数和三个勾选项。")
    result = copy.deepcopy(values)
    for key, value in result.items():
        if key == "url":
            if not isinstance(value, str) or not value or len(value) > 2048 or any(c in value for c in "\r\n\x00"):
                raise HostError(400, "invalid_settings", "工作台地址格式错误。")
            try:
                parts = urlsplit(value.strip())
                if (parts.scheme not in ("http", "https") or not parts.hostname or
                        parts.username is not None or parts.password is not None or parts.port == 0):
                    raise ValueError()
            except ValueError as exc:
                raise HostError(400, "invalid_settings", "工作台地址须为不含账号密码的 HTTP 或 HTTPS 地址。") from exc
            result[key] = value.strip()
        elif key in ("work_month", "tax_month"):
            if not isinstance(value, str) or not re.fullmatch(r"[1-9]\d{3}-(0[1-9]|1[0-2])", value):
                raise HostError(400, "invalid_settings", "月份须为 YYYY-MM。")
        elif key == "expected_people":
            if type(value) is not int or not 1 <= value <= 2147483647:
                raise HostError(400, "invalid_settings", "预期人数须为正整数。")
        elif type(value) is not bool:
            raise HostError(400, "invalid_settings", "勾选项须为布尔值。")
    return result


def _public_settings(settings):
    # Old configuration files can contain credentials.  Return only editable
    # fields, and never return URL userinfo, query tokens, or fragments.
    result = {}
    for key in SETTING_KEYS:
        value = settings.get(key, _FIXED_TEST_SETTINGS[key])
        expected_type = str if key in ("url", "work_month", "tax_month") else int if key == "expected_people" else bool
        result[key] = value if type(value) is expected_type else _FIXED_TEST_SETTINGS[key]
    try:
        parts = urlsplit(str(result["url"]))
        hostname = parts.hostname or ""
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            host += ":" + str(parts.port)
        result["url"] = urlunsplit((parts.scheme, host, parts.path, "", ""))
    except ValueError:
        result["url"] = ""
    return result


class BridgeManager:
    """Own task reception; the original Controller still owns all business work.

    The controller factory receives emit(kind, value).  Its controller must
    expose jobs (Queue), stop (Event), execute(task, settings), and optionally
    worker (Thread).  Tests can inject a controller without a worker thread.
    """
    def __init__(self, base, *, server_id, hostname=None, controller_factory=None,
                 desktop_ready=None, settings_loader=None, settings_saver=None):
        if controller_factory is None or settings_loader is None or settings_saver is None:
            from app import Controller, load_settings, save_settings
            controller_factory = controller_factory or Controller
            settings_loader = settings_loader or load_settings
            settings_saver = settings_saver or save_settings
        self.base = Path(base)
        self.server_id = str(server_id)
        self.hostname = hostname or socket.gethostname()
        self._desktop_ready = desktop_ready or interactive_desktop_ready
        self._settings_loader, self._settings_saver = settings_loader, settings_saver
        self._lock = threading.RLock()
        self._path = self.base / "data" / "mac_bridge" / "jobs.json"
        self._records = {}
        self._active_id = None
        self._stop_requested = False
        self._job_error = ""
        self._storage_fault = False
        self._closing = False
        self._close_queued = False
        self._companies, self._results = [], []
        self._events = deque(maxlen=1000)
        self._seq = 0
        self._load_records()
        self._settings = copy.deepcopy(self._settings_loader())
        if not isinstance(self._settings, dict):
            raise HostError(503, "settings_unavailable", "本地设置格式错误，请在 Windows 端修正。")
        self.controller = controller_factory(self.emit)
        self._execute = self.controller.execute
        # The existing loop obtains RunLock and clears its stop Event before
        # calling execute.  Wrap that boundary so a stop received while queued
        # cannot be cleared accidentally, and desktop state is checked again.
        self.controller.execute = self._execute_received

    def _load_records(self):
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("jobs"), dict):
                raise ValueError()
            records = payload["jobs"]
            for request_id, record in records.items():
                if (not isinstance(record, dict) or set(record) != {"payload_sha256", "job"} or
                        not re.fullmatch(r"[a-f0-9]{64}", str(record.get("payload_sha256", "")))):
                    raise ValueError()
                job = record["job"]
                if (not isinstance(job, dict) or set(job) != set(JOB_KEYS) or
                        _request_uuid(request_id) != request_id or job.get("request_id") != request_id or
                        _request_uuid(job.get("id")) != job.get("id") or job.get("task") not in TASKS or
                        job.get("status") not in JOB_STATES or not isinstance(job.get("created_at"), str) or
                        not isinstance(job.get("error"), str) or
                        (job.get("finished_at") is not None and not isinstance(job.get("finished_at"), str))):
                    raise ValueError()
            self._records = records
            changed = False
            for record in records.values():
                job = record["job"]
                if job["status"] in ("queued", "running"):
                    job.update(status="interrupted", finished_at=_now(),
                               error="上次连接端意外中断。任务不会自动重跑；请先核对原申报结果。")
                    changed = True
            if changed:
                self._write_records(records)
        except (OSError, ValueError, TypeError, HostError) as exc:
            raise HostError(503, "journal_unavailable", "任务接收记录不可读或无法恢复，已禁止执行。请保留 jobs.json 并检查。") from exc

    def _write_records(self, records):
        _atomic_write(self._path, json.dumps({"version": 1, "jobs": records}, ensure_ascii=False, indent=2))

    def _record_job(self, job):
        updated = copy.deepcopy(self._records)
        updated[job["request_id"]]["job"] = copy.deepcopy(job)
        self._write_records(updated)
        self._records = updated

    def _active(self):
        return self._records[self._active_id]["job"] if self._active_id else None

    def _event(self, kind, value):
        self._seq += 1
        self._events.append({"seq": self._seq, "kind": kind, "value": copy.deepcopy(value)})

    @property
    def busy(self):
        with self._lock:
            return self._active_id is not None

    def status(self, after=0):
        if type(after) is not int or after < 0:
            raise HostError(400, "invalid_cursor", "after 须为非负整数。")
        with self._lock:
            recent = [record["job"] for record in list(self._records.values())[-20:]][::-1]
            return copy.deepcopy({
                "protocol": 1, "server_id": self.server_id, "hostname": self.hostname,
                "settings": _public_settings(self._settings), "active_job": self._active(),
                "last_job": recent[0] if recent else None, "recent_jobs": recent,
                "companies": self._companies, "results": self._results,
                "events": [event for event in self._events if event["seq"] > after],
                "last_event_seq": self._seq,
            })

    def _ready(self):
        try:
            return self._desktop_ready() is True
        except Exception:
            return False

    def submit(self, request_id, task, settings):
        request_id = _request_uuid(request_id)
        if not isinstance(task, str) or task not in TASKS:
            raise HostError(400, "invalid_task", "不支持该任务。")
        changes = _validate_settings(settings)
        digest = hashlib.sha256(json.dumps({"task": task, "settings": changes}, sort_keys=True,
                                           ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        with self._lock:
            existing = self._records.get(request_id)
            if existing:
                if not hmac.compare_digest(existing["payload_sha256"], digest):
                    raise HostError(409, "request_conflict", "同一 request_id 已用于其他任务或设置，未重复执行。")
                return copy.deepcopy(existing["job"])
            if self._closing:
                raise HostError(409, "closing", "Windows 连接端正在关闭。")
            if self._storage_fault:
                raise HostError(503, "journal_unavailable", "任务记录写入失败，已禁止新任务。")
            if self._active_id:
                raise HostError(409, "busy", "已有任务正在执行，请等待或明确停止。")
            if not self._ready():
                raise HostError(409, "desktop_unavailable", "Windows 桌面不可用。请在 Windows 登录、解除锁屏并保持桌面开启。")
            try:
                merged = copy.deepcopy(self._settings_loader())
                if not isinstance(merged, dict):
                    raise ValueError()
                merged.update(changes)
                _validate_settings({key: merged[key] for key in SETTING_KEYS})
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise HostError(503, "settings_unavailable", "本地设置不可读或格式错误，请在 Windows 端修正。") from exc
            job = {"id": str(uuid.uuid4()), "request_id": request_id, "task": task,
                   "status": "queued", "created_at": _now(), "finished_at": None, "error": ""}
            updated = copy.deepcopy(self._records)
            updated[request_id] = {"payload_sha256": digest, "job": job}
            try:
                self._write_records(updated)
            except OSError as exc:
                self._storage_fault = True
                raise HostError(503, "journal_unavailable", "任务接收记录保存失败，任务未排队。") from exc
            self._records = updated
            self._active_id = request_id
            self._stop_requested, self._job_error = False, ""
            self._companies, self._results = [], []
            self._event("job", job)
            try:
                self._settings_saver(merged)
                self._settings = merged
                self.controller.jobs.put((task, merged))
            except Exception:
                self._job_error = "任务设置或队列保存失败，任务未开始。"
                self._finish()
            return copy.deepcopy(self._records[request_id]["job"])

    def _execute_received(self, task, settings):
        with self._lock:
            job = self._active()
            if job is None or task != job["task"]:
                raise HostError(409, "unknown_job", "执行队列与已接收记录不一致，已禁止执行。")
            if self._stop_requested or self._closing:
                self.controller.stop.set()
                return
            if not self._ready():
                self._job_error = "Windows 桌面在开始前已不可用，任务未执行。"
                self._event("error", self._job_error)
                return
            running = dict(job, status="running")
            try:
                self._record_job(running)
            except OSError:
                self._storage_fault = True
                self._job_error = "执行状态无法持久化，任务未执行。"
                self._event("error", self._job_error)
                return
            self._event("job", running)
        try:
            return self._execute(task, settings)
        except Exception as exc:
            # Preserve failure state even if the original worker's error
            # formatter itself cannot import a damaged desktop dependency.
            with self._lock:
                self._job_error = "执行失败：" + type(exc).__name__
            raise

    def emit(self, kind, value):
        with self._lock:
            if kind in ("log", "error"):
                text = str(value)[:8192]
                if kind == "error" and self._active_id:
                    self._job_error = text
                self._event(kind, text)
            elif kind == "companies":
                self._companies = [{"key": str(c.key), "name": str(c.name), "month": str(c.month),
                                    "expected_people": c.expected_people} for c in value]
                self._event(kind, self._companies)
            elif kind == "result":
                self._results = [{key: str(row.get(key, "")) for key in RESULT_FIELDS} for row in value]
                self._event(kind, self._results)
            elif kind == "idle":
                if self._active_id:
                    self._finish()
                self._event("idle", "")

    def _finish(self):
        job = copy.deepcopy(self._active())
        if job is None:
            return
        if self._stop_requested:
            state = "interrupted"
            error = "已按请求停止。已发送的申报请先核验，不会自动重新执行。"
        elif self._job_error:
            state, error = "failed", self._job_error
        elif job["status"] != "running":
            state, error = "failed", "执行线程在任务开始前退出，请在 Windows 检查客户端。"
        elif any(row.get("状态") not in ("SUCCESS", "PREPARED", "VERIFIED") for row in self._results):
            state = "failed"
            error = "部分企业尚未完成，请查看原始结果；已发送的申报请先核验，勿重复申报。"
        else:
            state, error = "completed", ""
        job.update(status=state, error=error, finished_at=_now())
        try:
            self._record_job(job)
        except OSError:
            self._storage_fault = True
            job.update(status="failed", error="任务已退出，但记录保存失败。请核对原申报结果；已禁止新任务。")
            self._records[job["request_id"]]["job"] = job
        self._active_id = None
        self._event("job", job)

    def stop(self, job_id=None):
        if job_id is not None and (not isinstance(job_id, str) or len(job_id) > 64):
            raise HostError(400, "invalid_job_id", "job_id 格式错误。")
        with self._lock:
            job = self._active()
            if job_id is not None and (job is None or job["id"] != job_id):
                old = next((record["job"] for record in self._records.values() if record["job"]["id"] == job_id), None)
                if old is not None:
                    return copy.deepcopy(old)  # A stale stop must not stop a later job.
                raise HostError(404, "job_not_found", "未找到该任务，未停止其他任务。")
            if job is None:
                return None
            if not self._stop_requested:
                self._stop_requested = True
                self.controller.stop.set()
                self._event("log", "已收到停止请求，等待当前安全步骤结束。")
            return copy.deepcopy(job)

    def request_close(self):
        """Request graceful shutdown; True means the worker has really exited."""
        with self._lock:
            self._closing = True
            if self._active_id:
                self.stop()
                return False
            if not self._close_queued:
                self.controller.jobs.put(("close", None))
                self._close_queued = True
            worker = getattr(self.controller, "worker", None)
            return worker is None or not worker.is_alive()


class _RateLimiter:
    def __init__(self):
        self._lock = threading.Lock()
        self._peers = {}

    def allow(self, address):
        with self._lock:
            now = time.monotonic()
            tokens, previous = self._peers.get(address, (40.0, now))
            tokens = min(40.0, tokens + (now - previous) * 5)
            if len(self._peers) >= 512 and address not in self._peers:
                self._peers = {key: value for key, value in self._peers.items() if now - value[1] < 60}
                if len(self._peers) >= 512:
                    return False
            self._peers[address] = (max(0.0, tokens - 1), now)
            return tokens >= 1


class _BridgeServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 16
    allow_reuse_address = False

    def __init__(self, address, context, token, manager):
        self.context, self.token, self.manager = context, token.encode("utf-8"), manager
        self.limiter = _RateLimiter()
        self.last_authenticated_at = 0.0
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, _BridgeHandler)

    def verify_request(self, request, client_address):
        return is_lan_address(client_address[0])

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            # TLS handshakes run in bounded handler threads, never in the
            # accept loop; untrusted/public peers are rejected before TLS.
            request.settimeout(8)
            with self.context.wrap_socket(request, server_side=True) as secure:
                self.finish_request(secure, client_address)
        except (OSError, ssl.SSLError):
            pass
        finally:
            self.shutdown_request(request)
            self._slots.release()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


class _BridgeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def version_string(self):
        return "TaxZeroBridge/1"

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def _common(self):
        if not self.server.limiter.allow(self.client_address[0]):
            raise HostError(429, "rate_limited", "请求过于频繁，请稍后重试。")
        if len(self.path) > 2048:
            raise HostError(414, "invalid_path", "请求路径过长。")
        if self.headers.get("Origin") is not None or self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise HostError(403, "origin_rejected", "仅接受已配对的本机客户端，不接受网页跨站请求。")
        headers = self.headers.get_all("Authorization", [])
        supplied = headers[0][7:] if len(headers) == 1 and headers[0].startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), self.server.token):
            raise HostError(401, "unauthorized", "连接码认证失败，请重新配对。")
        self.server.last_authenticated_at = time.monotonic()
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or parts.fragment:
            raise HostError(400, "invalid_path", "请求路径格式错误。")
        return parts

    def _body(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise HostError(400, "invalid_body", "不接受分块请求。")
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or not re.fullmatch(r"\d{1,10}", lengths[0].strip()):
            raise HostError(411, "length_required", "须提供 Content-Length。")
        length = int(lengths[0])
        if length > MAX_BODY:
            raise HostError(413, "body_too_large", "请求体不得超过 64 KiB。")
        types = self.headers.get_all("Content-Type", [])
        if len(types) != 1 or types[0].split(";", 1)[0].strip().lower() != "application/json":
            raise HostError(415, "json_required", "仅接受 application/json。")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HostError(400, "invalid_body", "请求体不完整。")
        try:
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise HostError(400, "invalid_json", "JSON 格式错误。") from exc
        if not isinstance(value, dict):
            raise HostError(400, "invalid_json", "JSON 顶层须为对象。")
        return value

    def _dispatch(self, method):
        try:
            parts = self._common()
            if method not in ("GET", "POST"):
                raise HostError(405, "method_not_allowed", "不支持该请求方法。")
            if method == "GET" and parts.path == "/v1/status":
                parameters = parse_qs(parts.query, keep_blank_values=True)
                values = parameters.get("after", ["0"])
                if set(parameters) - {"after"} or len(values) != 1 or not re.fullmatch(r"\d{1,18}", values[0]):
                    raise HostError(400, "invalid_cursor", "after 须为非负整数。")
                self._send(200, self.server.manager.status(int(values[0])))
                return
            if method == "POST" and parts.path in ("/v1/jobs", "/v1/stop") and not parts.query:
                body = self._body()
                if parts.path == "/v1/jobs":
                    if set(body) != {"request_id", "task", "settings"}:
                        raise HostError(400, "invalid_body", "须提供 request_id、task 和 settings。")
                    job = self.server.manager.submit(body["request_id"], body["task"], body["settings"])
                else:
                    if set(body) != {"job_id"}:
                        raise HostError(400, "invalid_body", "须提供 job_id（或 null）。")
                    job = self.server.manager.stop(body["job_id"])
                self._send(200, {"job": job})
                return
            raise HostError(404, "not_found", "接口不存在。")
        except HostError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (ConnectionError, TimeoutError, ssl.SSLError):
            self.close_connection = True
        except Exception:
            self._send(503, {"error": {"code": "internal_error", "message": "连接端暂时无法处理请求，请查看 Windows 窗口。"}})

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._dispatch("OPTIONS")

    do_HEAD = do_OPTIONS
    do_PUT = do_OPTIONS
    do_DELETE = do_OPTIONS
    do_PATCH = do_OPTIONS


def create_server(address, identity, manager):
    """Return an unstarted HTTPS server; callers own serve_forever/shutdown."""
    host, port = address
    if host != "0.0.0.0" and not is_lan_address(host):
        raise HostError(400, "invalid_bind", "连接端只允许监听本地或私网 IPv4 地址。")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(identity["cert_path"]), str(identity["key_path"]))
    if not isinstance(identity.get("token"), str) or len(identity["token"]) < 32:
        raise HostError(503, "invalid_identity", "连接凭据格式错误。")
    return _BridgeServer((host, port), context, identity["token"], manager)


class _BridgeMutex:
    def __enter__(self):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, "Local\\TaxZeroMacBridge_v1")
        if not self.handle:
            raise HostError(503, "mutex_unavailable", "无法创建 Mac 连接端运行锁。")
        if ctypes.get_last_error() == 183:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise HostError(409, "already_open", "Mac 连接端已经打开，请使用现有窗口中的连接码。")
        return self

    def __exit__(self, *_):
        if self.handle:
            self.kernel.CloseHandle(self.handle)


class BridgeWindow:
    """Small Windows pairing/status window; business controls live on the Mac."""
    def __init__(self, root, manager, server, pairing):
        import tkinter as tk
        from tkinter import ttk
        self.root, self.manager, self.server = root, manager, server
        self.closing = False
        self._last_log = ""
        self._last_seq = 0
        root.title("个税一键申报 · Mac 连接端")
        root.geometry("800x480")
        root.minsize(700, 430)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        frame = ttk.Frame(root, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="在 Mac 操作，在这台 Windows 执行", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="保持 Windows 已登录且不锁屏，扣缴端与需要的微信保持可用。\n在 Mac 的“个税一键申报”粘贴下方连接码，两台电脑须连接同一局域网。",
                  wraplength=740).pack(anchor="w", pady=(10, 14))
        ttk.Label(frame, text="连接码（包含配对凭据，仅交给你自己的 Mac）").pack(anchor="w")
        self.code = pairing.encode()
        box = tk.Text(frame, height=5, wrap="char", font=("Consolas", 10))
        box.insert("1.0", self.code)
        box.configure(state="disabled")
        box.pack(fill="x", pady=(6, 8))
        tools = ttk.Frame(frame)
        tools.pack(fill="x")
        ttk.Button(tools, text="复制连接码", command=self.copy_code).pack(side="left")
        self.mapping_button = ttk.Button(tools, text="微信群与Excel配对", command=self.open_mapping)
        self.mapping_button.pack(side="left", padx=10)
        ttk.Label(tools, text=f"HTTPS 端口：{server.server_address[1]}").pack(side="right")
        self.status_text = tk.StringVar(value="连接已开启，等待 Mac 连接。")
        ttk.Label(frame, textvariable=self.status_text, font=("Microsoft YaHei UI", 11, "bold"),
                  wraplength=730).pack(anchor="w", pady=(20, 8))
        self.detail_text = tk.StringVar(value="任务只有在 Mac 上点击操作后才会开始。")
        ttk.Label(frame, textvariable=self.detail_text, wraplength=730).pack(anchor="w", pady=(0, 10))
        self.stop_button = ttk.Button(frame, text="停止当前任务（Esc）", command=self.stop_active, state="disabled")
        self.stop_button.pack(anchor="e", side="bottom")
        root.bind("<Escape>", lambda _: self.stop_active())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(150, self.poll)

    def copy_code(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.code)

    def open_mapping(self):
        from tkinter import messagebox
        if self.manager.busy or self.closing:
            messagebox.showinfo("正在办理", "请在当前任务结束后维护微信群配对。", parent=self.root)
            return
        try:
            from app import launch_wechat_mapping
            launch_wechat_mapping()
        except Exception as exc:
            messagebox.showerror("微信群与Excel配对", str(exc), parent=self.root)

    def stop_active(self):
        self.manager.stop()
        if self.manager.busy:
            self.status_text.set("已请求停止，正在等待当前安全步骤结束。")

    def close(self):
        self.closing = True
        self.status_text.set("正在安全关闭：已请求停止，等待执行线程退出。")
        self.stop_button.configure(state="disabled")
        if self.manager.request_close():
            self.root.destroy()

    def poll(self):
        if self.closing:
            if self.manager.request_close():
                self.root.destroy()
                return
        else:
            status = self.manager.status(self._last_seq)
            self._last_seq = status["last_event_seq"]
            for event in status["events"]:
                if event["kind"] in ("log", "error"):
                    self._last_log = event["value"]
            job = status["active_job"]
            labels = {"read": "读取网页名单", "check_client": "检查扣缴端", "prepare": "准备并检查",
                      "run": "一键申报", "test_company": "测试指定公司", "test_partial": "测试部分0申报"}
            if job:
                self.status_text.set("任务进行中：" + labels[job["task"]] + "。关闭窗口会先停止并等待。")
            elif self.server.last_authenticated_at and time.monotonic() - self.server.last_authenticated_at < 20:
                self.status_text.set("Mac 已连接，等待下一次操作。")
            else:
                self.status_text.set("连接已开启，等待 Mac 连接。")
            self.stop_button.configure(state="normal" if job else "disabled")
            self.mapping_button.configure(state="disabled" if job else "normal")
            if self._last_log:
                self.detail_text.set(self._last_log[-600:])
        self.root.after(150, self.poll)


def main(base):
    if os.name != "nt":
        return 2
    import tkinter as tk
    from tkinter import messagebox
    manager = server = server_thread = root = mutex = None
    try:
        mutex = _BridgeMutex().__enter__()
        root = tk.Tk()
        root.withdraw()
        # The process mutex is held before identity creation or binding.
        identity = ensure_server_identity(Path(base) / "data" / "mac_bridge")
        manager = BridgeManager(base, server_id=identity["server_id"])
        try:
            server = create_server(("0.0.0.0", BRIDGE_PORT), identity, manager)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) == 10048:
                raise HostError(409, "port_in_use", "连接端口 8766 已被占用。请使用已打开的连接端，或关闭占用该端口的程序后重新打开。") from exc
            raise HostError(503, "listen_failed", "无法开启局域网连接端口 8766，请检查 Windows 网络权限后重新打开。") from exc
        pairing = Pairing(hosts=local_ipv4_addresses(), port=server.server_address[1],
                          token=identity["token"], fingerprint=identity["fingerprint"])
        BridgeWindow(root, manager, server, pairing)
        server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        server_thread.start()
        root.deiconify()
        root.mainloop()
        return 0
    except Exception as exc:
        if root is None:
            root = tk.Tk()
            root.withdraw()
        message = str(exc) if isinstance(exc, HostError) else "Mac 连接端启动失败：" + type(exc).__name__
        try:
            messagebox.showerror("Mac 连接端", message, parent=root)
        except tk.TclError:
            pass
        return 1
    finally:
        if manager is not None:
            while not manager.request_close():
                time.sleep(0.1)
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
            server.server_close()
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass
        if mutex is not None:
            mutex.__exit__(None, None, None)


def run_self_test(report_path):
    """Exercise frozen TLS/crypto/Tk imports with no real Controller or data."""
    import tempfile
    checks, errors = {}, []
    server = server_thread = root = None
    report_path = Path(report_path)

    class OfflineController:
        def __init__(self, emit):
            self.emit, self.jobs, self.stop = emit, queue.Queue(), threading.Event()
            self.executed = []

        def execute(self, task, settings):
            self.executed.append(task)
            raise AssertionError("Offline controller must never execute a business task")

    try:
        import cryptography
        checks["cryptography_import"] = bool(cryptography.__version__)
        with tempfile.TemporaryDirectory(prefix="taxzero-bridge-self-test-") as directory:
            base = Path(directory)
            identity = ensure_server_identity(base / "identity")
            same = ensure_server_identity(base / "identity")
            checks["temporary_identity"] = all(identity.get(key) for key in ("cert_path", "key_path", "token", "fingerprint", "server_id")) is True
            checks["identity_persisted"] = identity["token"] == same["token"] and identity["fingerprint"] == same["fingerprint"]
            manager = BridgeManager(base, server_id=identity["server_id"], hostname="offline-self-test",
                                    controller_factory=OfflineController, desktop_ready=lambda: True,
                                    settings_loader=lambda: dict(_FIXED_TEST_SETTINGS),
                                    settings_saver=lambda _: None)
            server = create_server(("127.0.0.1", 0), identity, manager)
            server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
            server_thread.start()
            context = ssl.create_default_context(cafile=str(identity["cert_path"]))
            context.check_hostname = False

            def get_status(token):
                connection = http.client.HTTPSConnection("127.0.0.1", server.server_address[1], timeout=5, context=context)
                try:
                    connection.connect()
                    observed = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
                    fingerprint = str(identity["fingerprint"]).lower().replace("sha256:", "").replace(":", "")
                    if not hmac.compare_digest(observed, fingerprint):
                        raise AssertionError("TLS certificate fingerprint mismatch")
                    checks["certificate_fingerprint"] = True
                    connection.request("GET", "/v1/status?after=0", headers={"Authorization": "Bearer " + token})
                    response = connection.getresponse()
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()

            status, payload = get_status(identity["token"])
            checks["tls_status"] = status == 200 and payload["protocol"] == 1 and payload["server_id"] == identity["server_id"]
            checks["empty_isolated_status"] = payload["settings"] == _FIXED_TEST_SETTINGS and payload["companies"] == [] and payload["results"] == [] and payload["active_job"] is None
            status, payload = get_status("incorrect-offline-token")
            checks["authentication_required"] = status == 401 and payload["error"]["code"] == "unauthorized"
            checks["lan_clients_only"] = is_lan_address("127.0.0.1") and not is_lan_address("8.8.8.8") and not is_lan_address("100.64.0.1")
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            pairing = Pairing(hosts=("127.0.0.1",), port=server.server_address[1],
                              token=identity["token"], fingerprint=identity["fingerprint"])
            checks["pairing_roundtrip"] = parse_pairing(pairing.encode()) == pairing
            window = BridgeWindow(root, manager, server, pairing)
            root.update_idletasks()
            root.update()
            checks["tk_connection_window"] = root.winfo_exists() == 1 and bool(window.code)
            checks["no_business_queue"] = manager.controller.jobs.empty() and manager.controller.executed == []
            checks["no_business_files"] = not (base / "settings.json").exists() and not (base / "data" / "state.json").exists()
            window.close()
            root = None
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)
            server = None
    except Exception as exc:
        errors.append({"type": type(exc).__name__, "message": str(exc)[:1200]})
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        if server is not None:
            if server_thread is not None and server_thread.is_alive():
                server.shutdown()
            server.server_close()
    passed = bool(checks) and all(checks.values()) and not errors
    report = {"status": "passed" if passed else "failed", "offline": True,
              "real_business_executed": False, "checks": checks, "errors": errors}
    try:
        _atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    except OSError:
        return 1
    return 0 if passed else 1

