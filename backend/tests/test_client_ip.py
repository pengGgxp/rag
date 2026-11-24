import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from pydantic import ValidationError
from starlette.requests import Request

from app.infra.client_ip import payment_client_ip
from app.infra.config import Settings


def request(peer, headers=()):
    return Request({"type": "http", "client": (peer, 1234) if peer else None,
                    "headers": [(key.encode(), value.encode()) for key, value in headers]})


@pytest.mark.parametrize("peer,trusted,xff,expected", [
    ("10.0.0.1", "10.0.0.1", "198.51.100.10", "198.51.100.10"),
    ("10.0.0.1", "", "198.51.100.10", "10.0.0.1"),
    ("198.51.100.20", "10.0.0.1", "198.51.100.10", "198.51.100.20"),
    ("10.0.0.1", "10.0.0.1,10.1.0.0/24", "198.51.100.10, 10.1.0.2", "198.51.100.10"),
    ("10.0.0.1", "10.0.0.1", "192.0.2.99, 198.51.100.10", "198.51.100.10"),
    ("10.0.0.1", "10.0.0.1", "garbage, 198.51.100.10", "198.51.100.10"),
    ("10.0.0.1", "10.0.0.1", "198.51.100.10, 10.1.0.2", "10.1.0.2"),
    ("10.0.0.1", "10.0.0.0/24", "10.0.0.2, 10.0.0.3", "10.0.0.2"),
    ("10.0.0.1", "10.0.0.1", "", "10.0.0.1"),
    ("10.0.0.1", "10.0.0.1", "198.51.100.10, invalid", "10.0.0.1"),
    ("10.0.0.1", "10.0.0.1", "unknown", "10.0.0.1"),
    ("10.0.0.1", "10.0.0.1", "198.51.100.10:123", "10.0.0.1"),
    ("10.0.0.1", "10.0.0.1", "198.51.100.10,", "10.0.0.1"),
    ("10.0.0.1", "10.0.0.1", "198.51.100.010", "10.0.0.1"),
    ("10.0.0.1", "10.0.0.1", "2001:db8::10", "2001:db8::10"),
    ("fd00::1", "fd00::/64", "2001:db8::10, fd00::2", "2001:db8::10"),
    ("fd00::1", "fd00::1", "[2001:db8::10]:443", "fd00::1"),
    ("fd00::1", "fd00::1", "fe80::1%eth0", "fd00::1"),
    ("2001:db8::20", "fd00::/64", "2001:db8::10", "2001:db8::20"),
    ("::ffff:10.0.0.1", "10.0.0.1", "::ffff:198.51.100.10", "198.51.100.10"),
    (None, "10.0.0.1", "198.51.100.10", ""),
    ("not-an-ip", "10.0.0.1", "198.51.100.10", ""),
])
def test_payment_source(peer, trusted, xff, expected):
    assert payment_client_ip(request(peer, [("x-forwarded-for", xff)]), trusted) == expected


def test_duplicate_headers_preserve_order_and_alternative_headers_are_ignored():
    headers = [("x-forwarded-for", "192.0.2.99"), ("x-forwarded-for", "198.51.100.10, 10.1.0.2"),
               ("x-real-ip", "192.0.2.88"), ("forwarded", "for=192.0.2.77")]
    assert payment_client_ip(request("10.0.0.1", headers), "10.0.0.1,10.1.0.2") == "198.51.100.10"
    assert payment_client_ip(request("10.0.0.1", headers[2:]), "10.0.0.1") == "10.0.0.1"


@pytest.mark.parametrize("trusted", ["*", "0.0.0.0/0", "::/0", "10.0.0.1,", "proxy.example", "10.0.0.1/99"])
def test_bad_trust_config_fails_at_startup(trusted):
    with pytest.raises(ValidationError, match="payment trusted proxies"):
        Settings(_env_file=None, payment_trusted_proxy_ips=trusted)


@pytest.mark.parametrize("trusted,expected", [("127.0.0.1", "198.51.100.10"), ("", "127.0.0.1")])
def test_real_uvicorn_connection_cannot_rewrite_transport_peer(trusted, expected):
    async def app(scope, receive, send):
        source = payment_client_ip(Request(scope), trusted)
        body = json.dumps({"peer": scope["client"][0], "source": source}).encode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, proxy_headers=False, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            response = httpx.get(f"http://127.0.0.1:{sock.getsockname()[1]}", headers={
                "X-Forwarded-For": "192.0.2.99, 198.51.100.10", "X-Real-IP": "192.0.2.88"})
            assert response.json() == {"peer": "127.0.0.1", "source": expected}
        finally:
            server.should_exit = True
            thread.join(timeout=10)
        assert not thread.is_alive()
