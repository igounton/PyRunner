"""F130: DNS rebinding — the address connected to is the address validated.

A name that answers a public IP to the validation lookup and 127.0.0.1 to the
connect lookup used to reach a loopback server (the Brain URL intake, GitHub
import/sync and the plugin completion client all checked only the first answer).
"""

import http.server
import socket
import threading
from unittest.mock import patch

import requests
from django.test import SimpleTestCase, override_settings

from core.api.exceptions import ApiError
from core.brain.intake import fetch_text
from core.netguard import UnsafeAddressError, guarded_create_connection
from core.vectors.budget import BudgetSession

_real_getaddrinfo = socket.getaddrinfo


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"internal secret page"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _answer(ip, port):
    family = socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, port or 0))]


@override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=False)
class RebindingTests(SimpleTestCase):
    def setUp(self):
        self.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.calls = 0

    def rebinding(self, host, port, *args, **kwargs):
        if host != "rebind.test":
            return _real_getaddrinfo(host, port, *args, **kwargs)
        self.calls += 1
        # First lookup (validation) answers a public address, later ones loopback.
        return _answer("93.184.216.34" if self.calls == 1 else "127.0.0.1", port)

    def test_brain_url_intake_does_not_follow_a_rebound_name(self):
        with patch("socket.getaddrinfo", self.rebinding):
            with self.assertRaises(ApiError) as caught:
                fetch_text(f"http://rebind.test:{self.port}/")
        self.assertEqual(caught.exception.code, "fetch_failed")

    def test_guarded_session_refuses_a_loopback_answer(self):
        with patch("socket.getaddrinfo", self.rebinding):
            self.calls = 1  # every lookup answers loopback
            with BudgetSession(10**12, guard_addresses=True) as session:
                with self.assertRaises(requests.RequestException):
                    session.get(f"http://rebind.test:{self.port}/")

    def test_guarded_connection_checks_every_answer(self):
        with patch("socket.getaddrinfo", return_value=_answer("169.254.169.254", 80)):
            with self.assertRaises(UnsafeAddressError):
                guarded_create_connection(("meta.test", 80), 1)

    @override_settings(PYRUNNER_ALLOW_PRIVATE_NETWORK=True)
    def test_private_flag_still_allows_a_sibling(self):
        sock = guarded_create_connection(("127.0.0.1", self.port), 2)
        sock.close()
