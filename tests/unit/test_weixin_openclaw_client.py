import asyncio

import aiohttp
import pytest
from aiohttp.client_proto import ResponseHandler

from app.adapters.weixin_openclaw.client import WeixinOpenClawClient
from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig


class BlackholeTransport(asyncio.Transport):
    def __init__(self, loop, protocol):
        super().__init__()
        self._loop = loop
        self._protocol = protocol
        self._closed = False

    def write(self, _data):
        pass

    def writelines(self, _data):
        pass

    def is_closing(self):
        return self._closed

    def get_write_buffer_size(self):
        return 0

    def get_extra_info(self, _name, default=None):
        return default

    def close(self):
        if not self._closed:
            self._closed = True
            self._loop.call_soon(self._protocol.connection_lost, None)

    def abort(self):
        self.close()


@pytest.mark.asyncio
async def test_getupdates_connect_timeout_is_connection_timeout(monkeypatch):
    client = WeixinOpenClawClient(
        WeixinOpenClawConfig(
            token="test-token",
            base_url="https://unused.invalid",
            api_timeout_ms=20,
        )
    )

    try:
        await client.ensure_session()

        async def never_connect(*_args, **_kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(client.session.connector, "_create_connection", never_connect)

        with pytest.raises(aiohttp.ConnectionTimeoutError) as exc_info:
            await asyncio.wait_for(
                client.request_json(
                    "POST",
                    "ilink/bot/getupdates",
                    payload={},
                    timeout_ms=100,
                ),
                timeout=1,
            )

        assert type(exc_info.value) is aiohttp.ConnectionTimeoutError
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "expected_exception"),
    [
        ("ilink/bot/getupdates", aiohttp.SocketTimeoutError),
        ("ilink/bot/sendmessage", TimeoutError),
    ],
    ids=["getupdates-sock-read", "sendmessage-total"],
)
async def test_response_timeout_type_depends_on_endpoint(monkeypatch, endpoint, expected_exception):
    client = WeixinOpenClawClient(
        WeixinOpenClawConfig(
            token="test-token",
            base_url="https://unused.invalid",
            api_timeout_ms=200,
        )
    )

    try:
        await client.ensure_session()
        loop = asyncio.get_running_loop()
        protocol = ResponseHandler(loop)
        transport = BlackholeTransport(loop, protocol)
        protocol.connection_made(transport)

        async def return_protocol(*_args, **_kwargs):
            return protocol

        monkeypatch.setattr(client.session.connector, "_create_connection", return_protocol)

        with pytest.raises(TimeoutError) as exc_info:
            await asyncio.wait_for(
                client.request_json(
                    "POST",
                    endpoint,
                    payload={},
                    timeout_ms=20,
                ),
                timeout=1,
            )

        assert type(exc_info.value) is expected_exception
    finally:
        await client.close()
