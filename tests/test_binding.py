"""Source-IP binding, proven on real loopback sockets.

Linux routes all of 127.0.0.0/8 to loopback, so 127.0.0.2 / 127.0.0.3 stand in for
the VPS's IP_A / IP_B: the server reports which source address each client used.
"""
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import websocket

from viper.net import binding


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self):
        body = self.client_address[0].encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture()
def http_server():
    srv = HTTPServer(("127.0.0.1", 0), _Echo)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _loopback_alias_ok(ip: str) -> bool:
    s = socket.socket()
    try:
        s.bind((ip, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


needs_alias = pytest.mark.skipif(not _loopback_alias_ok("127.0.0.2"), reason="127.0.0.2 not bindable on this OS")


@needs_alias
def test_session_adapter_binds_source(http_server):
    a = binding.make_session("127.0.0.2")
    b = binding.make_session("127.0.0.3")
    assert a.get(http_server).text == "127.0.0.2"
    assert b.get(http_server).text == "127.0.0.3"
    # unbound session uses the OS route
    assert binding.make_session(None).get(http_server).text == "127.0.0.1"


@needs_alias
def test_thread_local_scope_binds_plain_sockets(http_server):
    import requests

    binding.install_hooks()
    with binding.bound_to("127.0.0.3"):
        assert requests.get(http_server).text == "127.0.0.3"
    assert requests.get(http_server).text == "127.0.0.1"


@needs_alias
def test_websocket_routes_by_url_path():
    """The Fyers SDK builds WebSocketApp itself; the run_forever hook must bind by path."""
    binding.install_hooks()
    binding.set_ws_route("/hsm/", "127.0.0.3")
    binding.set_ws_route("/trade/", "127.0.0.2")
    seen = []
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def accept():
        for _ in range(2):
            c, addr = srv.accept()
            seen.append(addr[0])
            c.close()  # handshake fails, that's fine: we only need the source address

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    for path in ("/hsm/v1-5/prod", "/trade/v3"):
        app = websocket.WebSocketApp(f"ws://127.0.0.1:{port}{path}")
        app.run_forever()
    t.join(5)
    srv.close()
    binding.set_ws_route("/hsm/", None)
    binding.set_ws_route("/trade/", None)
    assert seen == ["127.0.0.3", "127.0.0.2"]


def test_verify_rejects_foreign_ip():
    with pytest.raises(binding.BindingError):
        binding.verify_local_ip("203.0.113.77")  # TEST-NET-3, never local
    with pytest.raises(binding.BindingError):
        binding.verify_local_ip("not-an-ip")


def test_configure_disabled_is_noop():
    assert binding.configure(False, "", "") == {"trading": None, "data": None}


def test_configure_requires_ips_when_enabled():
    with pytest.raises(binding.BindingError):
        binding.configure(True, "", "127.0.0.1")
