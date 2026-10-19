"""Private-network pairing and certificate-pinned HTTPS for the Mac companion.

The exact server certificate is trusted by the user copying a connection code.
Its SHA256 is checked before any HTTP headers (including the token) are sent.
Mutation requests are sent once; transport failures never retry a tax job.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROTOCOL_VERSION = 1
PAIRING_PREFIX = "GESHUIMAC1:"
TASKS = frozenset(("read", "check_client", "prepare", "run", "test_company", "test_partial"))
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_NETWORKS = tuple(ipaddress.ip_network(value) for value in
                  ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))


class BridgeError(RuntimeError):
    def __init__(self, message, *, status=None, code="connection_error", uncertain=False):
        super().__init__(message)
        self.status, self.code, self.uncertain = status, code, uncertain


def allowed_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
        return isinstance(address, ipaddress.IPv4Address) and any(address in net for net in _NETWORKS)
    except ValueError:
        return False


@dataclass(frozen=True)
class Pairing:
    hosts: tuple[str, ...]
    port: int
    token: str = field(repr=False)
    fingerprint: str

    def __post_init__(self):
        if (not isinstance(self.hosts, (list, tuple)) or not 1 <= len(self.hosts) <= 8
                or any(not isinstance(h, str) or not allowed_address(h) for h in self.hosts)
                or type(self.port) is not int or not 1 <= self.port <= 65535
                or not isinstance(self.token, str) or not _HEX64.fullmatch(self.token)
                or not isinstance(self.fingerprint, str) or not _HEX64.fullmatch(self.fingerprint)):
            raise BridgeError("连接码格式不正确，请从 Windows 连接窗口重新复制。", code="invalid_pairing")
        object.__setattr__(self, "hosts", tuple(dict.fromkeys(self.hosts)))

    def encode(self) -> str:
        payload = {"v": PROTOCOL_VERSION, "hosts": self.hosts, "port": self.port,
                   "token": self.token, "fingerprint": self.fingerprint}
        data = json.dumps(payload, separators=(",", ":")).encode("ascii")
        return PAIRING_PREFIX + base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def parse_pairing(code: str) -> Pairing:
    try:
        if not isinstance(code, str) or len(code) > 8192:
            raise ValueError()
        value = "".join(code.split())
        if not value.startswith(PAIRING_PREFIX):
            raise ValueError()
        value = value[len(PAIRING_PREFIX):]
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if (not isinstance(payload, dict) or set(payload) != {"v", "hosts", "port", "token", "fingerprint"}
                or type(payload["v"]) is not int or payload["v"] != PROTOCOL_VERSION):
            raise ValueError()
        return Pairing(payload["hosts"], payload["port"], payload["token"], payload["fingerprint"])
    except (TypeError, ValueError, KeyError, UnicodeError, BridgeError) as exc:
        raise BridgeError("连接码格式不正确，请完整复制 Windows 窗口里的连接码。",
                          code="invalid_pairing") from None


def local_ipv4_addresses() -> tuple[str, ...]:
    candidates = set()
    try:
        for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            address = item[4][0]
            if allowed_address(address) and not address.startswith("127."):
                candidates.add(address)
    except OSError:
        pass
    ordered = sorted(candidates, key=lambda a: (not a.startswith("192.168."), not a.startswith("10."), a))
    return tuple(ordered[:7] + ["127.0.0.1"])


def _atomic_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        handle, name = tempfile.mkstemp(prefix=path.name + "-", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def ensure_server_identity(directory: str | Path) -> dict:
    """Create a persistent local identity; private material is never packaged.

    Only the Windows server calls this function. The Mac client uses stdlib SSL
    and the pinned fingerprint, so its .app does not require cryptography.
    """
    directory = Path(directory)
    metadata = directory / "identity.json"
    cert_path, key_path = directory / "server-cert.pem", directory / "server-key.pem"
    if metadata.exists():
        try:
            stored = json.loads(metadata.read_text(encoding="utf-8"))
            token, server_id = stored["token"], stored["server_id"]
            if not isinstance(token, str) or not _HEX64.fullmatch(token):
                raise ValueError()
            uuid.UUID(server_id)
            certificate = ssl.PEM_cert_to_DER_cert(cert_path.read_text(encoding="ascii"))
            fingerprint = hashlib.sha256(certificate).hexdigest()
            if not hmac.compare_digest(fingerprint, stored["fingerprint"]) or not key_path.is_file():
                raise ValueError()
            return {"cert_path": cert_path, "key_path": key_path, "token": token,
                    "fingerprint": fingerprint, "server_id": server_id}
        except (OSError, ValueError, KeyError, TypeError, ssl.SSLError):
            raise BridgeError("Windows 连接身份文件不完整，请保留申报记录并重新配置连接端。",
                              code="identity_invalid") from None

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "TaxZero private companion")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .sign(key, hashes.SHA256()))
    certificate_bytes = certificate.public_bytes(serialization.Encoding.PEM)
    private_bytes = key.private_bytes(serialization.Encoding.PEM,
                                     serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    token, server_id = secrets.token_hex(32), str(uuid.uuid4())
    fingerprint = hashlib.sha256(certificate.public_bytes(serialization.Encoding.DER)).hexdigest()
    _atomic_private(key_path, private_bytes)
    _atomic_private(cert_path, certificate_bytes)
    _atomic_private(metadata, json.dumps({"token": token, "fingerprint": fingerprint,
                                        "server_id": server_id}, separators=(",", ":")).encode("ascii"))
    return {"cert_path": cert_path, "key_path": key_path, "token": token,
            "fingerprint": fingerprint, "server_id": server_id}


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, fingerprint, timeout):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # Trust is the exact out-of-band certificate fingerprint, not a public CA.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(host, port, timeout=timeout, context=context)
        self._fingerprint = fingerprint

    def connect(self):
        super().connect()
        actual = hashlib.sha256(self.sock.getpeercert(binary_form=True)).hexdigest()
        if not hmac.compare_digest(actual, self._fingerprint):
            self.close()
            raise BridgeError("连接电脑的身份与连接码不一致，请重新复制 Windows 连接码。",
                              code="certificate_mismatch")


class BridgeClient:
    def __init__(self, pairing: Pairing, timeout: float = 5):
        if not isinstance(pairing, Pairing):
            raise BridgeError("请先输入 Windows 连接码。", code="invalid_pairing")
        self.pairing, self.timeout = pairing, timeout
        self._preferred_host = None

    def _request_one(self, host, method, path, payload=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False,
                                                       separators=(",", ":")).encode("utf-8")
        if body is not None and len(body) > MAX_REQUEST_BYTES:
            raise BridgeError("设置内容过大，未发送。", code="request_too_large")
        connection = _PinnedHTTPSConnection(host, self.pairing.port, self.pairing.fingerprint, self.timeout)
        headers = {"Authorization": "Bearer " + self.pairing.token, "Accept": "application/json",
                   "Connection": "close"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        try:
            # connect() verifies the certificate before HTTPConnection sends any header.
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise BridgeError("Windows 返回内容过大，请到 Windows 查看结果。",
                                  code="response_too_large", uncertain=method == "POST")
            if response.getheader("Content-Type", "").split(";", 1)[0].strip() != "application/json":
                raise BridgeError("Windows 连接端返回格式不正确。", code="invalid_response",
                                  uncertain=method == "POST")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BridgeError("Windows 返回内容未完整收到，请重新连接查看任务状态。",
                                  code="invalid_response", uncertain=method == "POST") from None
            if not isinstance(value, dict):
                raise BridgeError("Windows 返回格式不正确。", code="invalid_response", uncertain=method == "POST")
            if not 200 <= response.status < 300:
                error = value.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                code = error.get("code") if isinstance(error, dict) else "server_error"
                if not isinstance(message, str) or len(message) > 1200:
                    message = "Windows 未接受本次请求，请重新连接查看状态。"
                raise BridgeError(message, status=response.status, code=code,
                                  uncertain=method == "POST" and response.status >= 500)
            self._preferred_host = host
            return value
        except BridgeError:
            raise
        except (OSError, ssl.SSLError, http.client.HTTPException):
            message = ("连接中断，本次请求可能已到达 Windows。请重新连接查看同一任务，勿重复申报。"
                       if method == "POST" else "暂时无法连接 Windows，请确认连接端已打开、电脑未休眠且两台在同一局域网。")
            raise BridgeError(message, code="transport_error", uncertain=method == "POST") from None
        finally:
            connection.close()

    def _request(self, method, path, payload=None):
        if method != "GET":
            # Never retry a mutating request, including against an alternate IP.
            return self._request_one(self._preferred_host or self.pairing.hosts[0], method, path, payload)
        candidates = tuple(dict.fromkeys(((self._preferred_host,) if self._preferred_host else ()) + self.pairing.hosts))
        failure = None
        for host in candidates:
            try:
                return self._request_one(host, method, path, payload)
            except BridgeError as exc:
                if exc.status is not None:
                    raise
                failure = exc
        raise failure or BridgeError("无法连接 Windows 电脑。")

    def status(self, after: int = 0) -> dict:
        if type(after) is not int or after < 0:
            raise BridgeError("进度位置不正确。", code="invalid_cursor")
        value = self._request("GET", "/v1/status?after=" + str(after))
        if value.get("protocol") != PROTOCOL_VERSION:
            raise BridgeError("Mac 和 Windows 连接端版本不一致，请使用同一套安装包。", code="protocol_mismatch")
        return value

    def submit(self, task: str, settings: dict, request_id: str) -> dict:
        if task not in TASKS or not isinstance(settings, dict):
            raise BridgeError("操作或设置无效，未发送。", code="invalid_task")
        try:
            uuid.UUID(request_id)
        except (ValueError, TypeError, AttributeError):
            raise BridgeError("任务编号无效，未发送。", code="invalid_request_id") from None
        return self._request("POST", "/v1/jobs", {"request_id": request_id, "task": task, "settings": settings})

    def stop(self, job_id: str | None = None) -> dict:
        if job_id is not None and not isinstance(job_id, str):
            raise BridgeError("任务编号无效。", code="invalid_job_id")
        return self._request("POST", "/v1/stop", {"job_id": job_id})
