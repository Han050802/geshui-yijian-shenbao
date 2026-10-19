"""Real loopback TLS checks; never use a filing controller or business files."""
import contextlib
import json
import os
import ssl
import sys
import tempfile
import threading
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bridge_protocol import (BridgeClient, BridgeError, MAX_RESPONSE_BYTES,
                             Pairing, ensure_server_identity, parse_pairing)


@contextlib.contextmanager
def server_fixture(mode="normal"):
    with tempfile.TemporaryDirectory(prefix="bridge-protocol-test-") as temporary:
        identity = ensure_server_identity(Path(temporary) / "identity")
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, value):
                data = json.dumps(value).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer " + identity["token"]:
                    return self.reply(401, {"error": {"code": "unauthorized", "message": "连接码不匹配"}})
                if mode == "redirect":
                    return self.reply(302, {"error": {"code": "redirect", "message": "拒绝跳转"}})
                if mode == "oversized":
                    return self.reply(200, {"protocol": 1, "text": "x" * MAX_RESPONSE_BYTES})
                return self.reply(200, {"protocol": 1, "server_id": identity["server_id"], "busy": False})

            def do_POST(self):
                requests.append(("POST", self.path, self.headers.get("Authorization")))
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                if mode == "drop":
                    self.close_connection = True
                    return
                return self.reply(200, {"job": {"job_id": "offline-test", "status": "queued"}})

        class TestServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # Pin rejection intentionally closes TLS before an HTTP request.
                if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                    return
                super().handle_error(request, client_address)

        server = TestServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(identity["cert_path"], identity["key_path"])
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        pairing = Pairing(("127.0.0.1",), server.server_port, identity["token"], identity["fingerprint"])
        try:
            yield BridgeClient(pairing, timeout=2), requests, identity
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


class PairingTests(unittest.TestCase):
    def test_pairing_round_trip_and_secret_not_in_repr(self):
        pair = Pairing(("192.168.1.4", "127.0.0.1"), 8766, "a" * 64, "b" * 64)
        self.assertEqual(parse_pairing("\n" + pair.encode() + "\n"), pair)
        self.assertNotIn(pair.token, repr(pair))

    def test_public_dns_and_unexpected_addresses_cannot_be_paired(self):
        for host in ("8.8.8.8", "example.test", "169.254.1.1", "::1", "192.168.1.4/path"):
            with self.subTest(host=host), self.assertRaises(BridgeError):
                Pairing((host,), 8766, "a" * 64, "b" * 64)

    def test_corrupt_codes_do_not_echo_pasted_secrets(self):
        for value in ("secret-token", "GESHUIMAC1:???secret-token", "x" * 9000):
            with self.assertRaises(BridgeError) as raised:
                parse_pairing(value)
            self.assertNotIn(value, str(raised.exception))

    def test_identity_is_persistent_and_corruption_does_not_reissue(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            first = ensure_server_identity(directory)
            self.assertEqual(first, ensure_server_identity(directory))
            cert = first["cert_path"].read_bytes()
            if os.name != "nt":
                self.assertEqual(first["key_path"].stat().st_mode & 0o777, 0o600)
            (directory / "identity.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(BridgeError):
                ensure_server_identity(directory)
            self.assertEqual(cert, first["cert_path"].read_bytes())


class TransportTests(unittest.TestCase):
    def test_real_tls_pinned_status_then_single_job(self):
        with server_fixture() as (client, requests, identity):
            self.assertEqual(client.status()["server_id"], identity["server_id"])
            response = client.submit("read", {}, str(uuid.uuid4()))
            self.assertEqual(response["job"]["status"], "queued")
            self.assertEqual([request[0] for request in requests], ["GET", "POST"])

    def test_wrong_certificate_pin_sends_no_http_headers_or_token(self):
        with server_fixture() as (client, requests, _identity):
            wrong = Pairing(client.pairing.hosts, client.pairing.port, client.pairing.token, "0" * 64)
            with self.assertRaises(BridgeError) as raised:
                BridgeClient(wrong, timeout=2).status()
            self.assertEqual(raised.exception.code, "certificate_mismatch")
            self.assertEqual(requests, [])

    def test_wrong_token_returns_401_without_retrying_other_hosts(self):
        with server_fixture() as (client, requests, _identity):
            wrong = Pairing(("127.0.0.1", "127.0.0.2"), client.pairing.port, "0" * 64, client.pairing.fingerprint)
            rejected = BridgeClient(wrong, timeout=2)
            with patch.object(rejected, "_request_one", wraps=rejected._request_one) as call:
                with self.assertRaises(BridgeError) as raised:
                    rejected.status()
                self.assertEqual(raised.exception.status, 401)
                self.assertEqual(call.call_count, 1)
            self.assertEqual(len(requests), 1)

    def test_lost_post_response_is_uncertain_and_never_retried(self):
        with server_fixture("drop") as (client, requests, _identity):
            pair = Pairing(("127.0.0.1", "127.0.0.2"), client.pairing.port,
                           client.pairing.token, client.pairing.fingerprint)
            client = BridgeClient(pair, timeout=2)
            with patch.object(client, "_request_one", wraps=client._request_one) as call:
                with self.assertRaises(BridgeError) as raised:
                    client.submit("run", {}, str(uuid.uuid4()))
                self.assertTrue(raised.exception.uncertain)
                self.assertEqual(call.call_count, 1)
            self.assertEqual([r[0] for r in requests], ["POST"])

    def test_get_fallback_selects_working_host_before_mutation(self):
        with server_fixture() as (client, requests, _identity):
            pair = Pairing(("127.0.0.2", "127.0.0.1"), client.pairing.port,
                           client.pairing.token, client.pairing.fingerprint)
            client = BridgeClient(pair, timeout=1)
            client.status()
            client.submit("read", {}, str(uuid.uuid4()))
            self.assertEqual([r[0] for r in requests], ["GET", "POST"])

    def test_redirect_is_not_followed(self):
        with server_fixture("redirect") as (client, requests, _identity):
            with self.assertRaises(BridgeError) as raised:
                client.status()
            self.assertEqual(raised.exception.status, 302)
            self.assertEqual(len(requests), 1)

    def test_response_size_is_bounded(self):
        with server_fixture("oversized") as (client, _requests, _identity):
            with self.assertRaises(BridgeError) as raised:
                client.status()
            self.assertEqual(raised.exception.code, "response_too_large")

    def test_non_whitelisted_operation_never_connects(self):
        pair = Pairing(("127.0.0.1",), 1, "a" * 64, "b" * 64)
        client = BridgeClient(pair)
        with patch.object(client, "_request_one") as request:
            with self.assertRaises(BridgeError):
                client.submit("shell", {"command": "unused"}, str(uuid.uuid4()))
            request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
