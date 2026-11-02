"""Source-IP binding for the dual-IP VPS.

FYERS whitelists exactly one IP per app. The VPS has two public IPs, so each
app's traffic must leave from *its* IP:

    trading app  (orders, positions, funds, order socket)  -> VPS_TRADING_SOURCE_IP (IP_A)
    data app     (quotes, history, option chain, data socket) -> VPS_DATA_SOURCE_IP (IP_B)

Three layers enforce this:

1. ``SourceAddressAdapter`` - a ``requests`` adapter whose urllib3 pool binds every
   socket with ``source_address=(ip, 0)``. Used by our own REST clients.
2. ``bound_to(ip)`` - a thread-local scope honoured by a ``socket.socket.connect``
   hook. Anything run inside it (e.g. the SDK's internal ``requests.post`` symbol
   lookups) leaves from ``ip``.
3. A ``websocket.WebSocketApp.run_forever`` hook. The Fyers SDK creates its
   WebSocketApp and calls ``run_forever`` on a thread it owns, so there's no
   argument we can pass. The hook classifies the socket by URL path (``/hsm/`` is
   the market data socket, ``/trade/`` the order socket) and runs the whole
   ``run_forever`` loop, reconnects included, inside ``bound_to`` for that role's IP.

With ``ENABLE_IP_BINDING=false`` (local staging) all of this is a no-op and the OS
picks the route.
"""
from __future__ import annotations

import contextlib
import ipaddress
import logging
import socket
import threading
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger("viper.net")

_tls = threading.local()
_installed = False
_ws_routes: dict[str, str] = {}  # url substring -> source ip
_lock = threading.Lock()


class BindingError(RuntimeError):
    pass


def current_bind_ip() -> str | None:
    return getattr(_tls, "ip", None)


@contextlib.contextmanager
def bound_to(ip: str | None) -> Iterator[None]:
    """Bind every outbound IPv4 TCP connect made on this thread to ``ip``."""
    prev = getattr(_tls, "ip", None)
    _tls.ip = ip or None
    try:
        yield
    finally:
        _tls.ip = prev


def _patched_connect_factory(orig):
    def connect(self, address):  # type: ignore[no-untyped-def]
        ip = getattr(_tls, "ip", None)
        if ip and self.family == socket.AF_INET and self.type & socket.SOCK_STREAM:
            try:
                local = self.getsockname()
            except OSError:
                local = ("0.0.0.0", 0)
            if local[0] in ("0.0.0.0", "") and local[1] == 0:
                self.bind((ip, 0))
        return orig(self, address)

    return connect


def install_hooks() -> None:
    """Install the socket and websocket hooks once per process."""
    global _installed
    with _lock:
        if _installed:
            return
        socket.socket.connect = _patched_connect_factory(socket.socket.connect)  # type: ignore[method-assign]
        try:
            import websocket  # websocket-client, used by fyers_apiv3

            orig_run = websocket.WebSocketApp.run_forever

            def run_forever(self, *a, **kw):  # type: ignore[no-untyped-def]
                ip = route_for_ws(getattr(self, "url", ""))
                if ip:
                    log.info("ws %s bound to source %s", self.url, ip)
                with bound_to(ip):
                    return orig_run(self, *a, **kw)

            websocket.WebSocketApp.run_forever = run_forever  # type: ignore[method-assign]
        except ImportError:  # pragma: no cover - websocket-client is a hard dep
            log.warning("websocket-client missing; WS binding hook not installed")
        _installed = True


def set_ws_route(url_fragment: str, ip: str | None) -> None:
    if ip:
        _ws_routes[url_fragment] = ip
    else:
        _ws_routes.pop(url_fragment, None)


def route_for_ws(url: str) -> str | None:
    for frag, ip in _ws_routes.items():
        if frag in url:
            return ip
    return None


def verify_local_ip(ip: str) -> None:
    """Fail fast if ``ip`` is not a valid IPv4 address assigned to this host."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as e:
        raise BindingError(f"{ip!r} is not a valid IP address") from e
    if addr.version != 4:
        raise BindingError(f"{ip} is IPv6; Fyers whitelisting uses IPv4")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((ip, 0))
    except OSError as e:
        raise BindingError(
            f"cannot bind to {ip}: {e}. Is it assigned to an interface on this host? "
            "(check `ip -4 addr`) - or set ENABLE_IP_BINDING=false for staging."
        ) from e
    finally:
        s.close()


class SourceAddressAdapter(HTTPAdapter):
    """HTTPAdapter that pins the local end of every pooled connection."""

    def __init__(self, source_ip: str, **kw):
        self.source_ip = source_ip
        super().__init__(**kw)

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["source_address"] = (self.source_ip, 0)
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["source_address"] = (self.source_ip, 0)
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def make_session(source_ip: str | None, pool: int = 16) -> requests.Session:
    s = requests.Session()
    if source_ip:
        adapter: HTTPAdapter = SourceAddressAdapter(source_ip, pool_connections=pool, pool_maxsize=pool)
    else:
        adapter = HTTPAdapter(pool_connections=pool, pool_maxsize=pool)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def configure(enable: bool, trading_ip: str, data_ip: str) -> dict[str, str | None]:
    """Validate and register IPs. Returns the effective role -> IP map."""
    install_hooks()
    if not enable:
        set_ws_route("/hsm/", None)
        set_ws_route("/trade/", None)
        return {"trading": None, "data": None}
    for role, ip in (("trading", trading_ip), ("data", data_ip)):
        if not ip:
            raise BindingError(f"ENABLE_IP_BINDING=true but no source IP configured for the {role} app")
        verify_local_ip(ip)
    if trading_ip == data_ip:
        log.warning("trading and data source IPs are identical (%s); both apps share one IP", trading_ip)
    set_ws_route("/hsm/", data_ip)      # FyersDataSocket: wss://socket.fyers.in/hsm/v1-5/prod
    set_ws_route("/trade/", trading_ip)  # FyersOrderSocket: wss://socket.fyers.in/trade/v3
    return {"trading": trading_ip, "data": data_ip}
