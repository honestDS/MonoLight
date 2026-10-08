import asyncio

import aiohttp
import pytest
from aiohttp.client_proto import ResponseHandler
from aiohttp.connector import TCPConnector

from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs


class _ExpectedTlsUpgradeError(RuntimeError):
    pass


class _MemoryTransport(asyncio.Transport):
    def __init__(self, loop: asyncio.AbstractEventLoop, protocol: ResponseHandler) -> None:
        self._loop = loop
        self._protocol = protocol
        self._closed = False
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        self._loop.call_soon(
            self._protocol.data_received,
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK",
        )

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, name: str, default: object = None) -> object:
        return default

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._protocol.connection_lost(None)

    def abort(self) -> None:
        self.close()


class _MemoryConnector(TCPConnector):
    def __init__(self) -> None:
        super().__init__()
        self.transports: list[_MemoryTransport] = []

    async def _create_direct_connection(self, req, traces, timeout, *, client_error=None) -> tuple[asyncio.Transport, ResponseHandler]:
        protocol = ResponseHandler(loop=self._loop)
        transport = _MemoryTransport(self._loop, protocol)
        protocol.connection_made(transport)
        self.transports.append(transport)
        return transport, protocol

    async def _start_tls_connection(self, underlying_transport, req, timeout, client_error=None):
        underlying_transport.close()
        raise _ExpectedTlsUpgradeError()


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", ("http", "https"))
@pytest.mark.parametrize(
    "proxy",
    (
        None,
        "http://127.0.0.1:8080",
        "http://user:pass@127.0.0.1:8080",
        "http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.COM:8080/",
        "http://user:pass@[2001:db8::1]:8080",
    ),
)
async def test_http_proxy_transport_serializes_requests(scheme: str, proxy: str | None) -> None:
    target = f"{scheme}://origin.example.test/resource?query=1"
    connector = _MemoryConnector()
    proxy_auth_headers = {
        "http://user:pass@127.0.0.1:8080": b"Proxy-Authorization: Basic dXNlcjpwYXNz\r\n",
        "http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.COM:8080/": b"Proxy-Authorization: Basic dXNlckBuYW1lOnBhc3N3b3JkOndpdGgvc2xhc2g=\r\n",
        "http://user:pass@[2001:db8::1]:8080": b"Proxy-Authorization: Basic dXNlcjpwYXNz\r\n",
    }

    async with aiohttp.ClientSession(
        connector=connector,
        timeout=aiohttp.ClientTimeout(total=2),
    ) as session:
        if scheme == "https" and proxy is not None:
            with pytest.raises(_ExpectedTlsUpgradeError):
                await session.get(
                    target,
                    headers={"Authorization": "Bearer provider-key"},
                    **build_aiohttp_proxy_kwargs(proxy),
                )
        else:
            response = await session.get(
                target,
                headers={"Authorization": "Bearer provider-key"},
                **build_aiohttp_proxy_kwargs(proxy),
            )
            assert await response.read() == b"OK"

    wire = b"".join(connector.transports[0].writes)

    expected_proxy_auth = proxy_auth_headers.get(proxy)
    if expected_proxy_auth is None:
        assert b"Proxy-Authorization:" not in wire
    else:
        assert expected_proxy_auth in wire

    if proxy is None:
        assert wire.startswith(b"GET /resource?query=1 HTTP/1.1\r\n")
        assert b"Authorization: Bearer provider-key\r\n" in wire
    elif scheme == "http":
        assert wire.startswith(b"GET http://origin.example.test/resource?query=1 HTTP/1.1\r\n")
        assert b"Authorization: Bearer provider-key\r\n" in wire
    else:
        assert wire.startswith(b"CONNECT origin.example.test:443 HTTP/1.1\r\n")
        assert b"Authorization: Bearer provider-key\r\n" not in wire
