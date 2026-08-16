from __future__ import annotations

import base64
import json
import random
from typing import Any
from urllib.parse import quote

import aiohttp

from app.adapters.weixin_openclaw.config import WeixinOpenClawConfig
from app.core.constants import (
    ERR_MESSAGE_PLATFORM_TOKEN_REQUIRED,
    ERR_WEIXIN_OPENCLAW_CDN_DOWNLOAD_FAILED,
    ERR_WEIXIN_OPENCLAW_CDN_UPLOAD_FAILED,
    ERR_WEIXIN_OPENCLAW_CDN_UPLOAD_PARAM_MISSING,
    ERR_WEIXIN_OPENCLAW_REQUEST_FAILED,
    ERR_WEIXIN_OPENCLAW_SESSION_UNINITIALIZED,
)
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger

logger = get_logger(__name__)


class WeixinOpenClawClient:
    def __init__(self, config: WeixinOpenClawConfig) -> None:
        self.config = config
        self.base_url = config.base_url.rstrip("/")
        self.cdn_base_url = config.cdn_base_url.rstrip("/")
        self.token = config.token.strip()
        self.session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self.session is not None:
            await self.session.close()
            self.session = None

    async def ensure_session(self) -> None:
        if self.session is None or self.session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.api_timeout_ms / 1000)
            self.session = aiohttp.ClientSession(timeout=timeout)

    def build_headers(self, *, token_required: bool) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode("utf-8")).decode("utf-8"),
        }
        if token_required:
            if not self.token:
                raise BaseBusinessException(message=ERR_MESSAGE_PLATFORM_TOKEN_REQUIRED)
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def request_json(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        token_required: bool = True,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_session()
        if self.session is None:
            raise RuntimeError(t(ERR_WEIXIN_OPENCLAW_SESSION_UNINITIALIZED))
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        timeout = aiohttp.ClientTimeout(total=(timeout_ms or self.config.api_timeout_ms) / 1000)
        request_headers = self.build_headers(token_required=token_required)
        if headers:
            request_headers.update(headers)
        async with self.session.request(method, url, params=params, json=payload, headers=request_headers, timeout=timeout) as response:
            text = await response.text()
            if response.status >= 400:
                raise RuntimeError(
                    t(
                        ERR_WEIXIN_OPENCLAW_REQUEST_FAILED,
                        method=method,
                        endpoint=endpoint,
                        status=response.status,
                        detail=text,
                    )
                )
            if not text:
                return {}
            result = json.loads(text)
            if isinstance(result, dict) and any(result.get(key) not in (None, "", 0, "0") for key in ("ret", "errcode")):
                logger.bind(method=method, endpoint=endpoint, status=response.status).error(text)
                raise RuntimeError(
                    t(
                        ERR_WEIXIN_OPENCLAW_REQUEST_FAILED,
                        method=method,
                        endpoint=endpoint,
                        status=response.status,
                        detail=text,
                    )
                )
            return result

    async def download_cdn_bytes(self, encrypted_query_param: str) -> bytes:
        await self.ensure_session()
        if self.session is None:
            raise RuntimeError(t(ERR_WEIXIN_OPENCLAW_SESSION_UNINITIALIZED))
        timeout = aiohttp.ClientTimeout(total=self.config.api_timeout_ms / 1000)
        async with self.session.get(self.build_cdn_download_url(encrypted_query_param), timeout=timeout) as response:
            if response.status >= 400:
                detail = await response.text()
                raise RuntimeError(
                    t(
                        ERR_WEIXIN_OPENCLAW_CDN_DOWNLOAD_FAILED,
                        status=response.status,
                        detail=detail,
                    )
                )
            return await response.read()

    async def upload_cdn_bytes(self, upload_url: str, encrypted: bytes) -> str:
        await self.ensure_session()
        if self.session is None:
            raise RuntimeError(t(ERR_WEIXIN_OPENCLAW_SESSION_UNINITIALIZED))

        timeout = aiohttp.ClientTimeout(total=self.config.api_timeout_ms / 1000)
        async with self.session.post(
            upload_url,
            data=encrypted,
            headers={"Content-Type": "application/octet-stream"},
            timeout=timeout,
        ) as response:
            detail = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    t(
                        ERR_WEIXIN_OPENCLAW_CDN_UPLOAD_FAILED,
                        status=response.status,
                        detail=detail,
                    )
                )
            download_param = response.headers.get("x-encrypted-param")
            if not download_param:
                raise RuntimeError(t(ERR_WEIXIN_OPENCLAW_CDN_UPLOAD_PARAM_MISSING))
            return download_param

    def build_cdn_upload_url(self, upload_param: str, file_key: str) -> str:
        return f"{self.cdn_base_url}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"

    def build_cdn_download_url(self, encrypted_query_param: str) -> str:
        return f"{self.cdn_base_url}/download?encrypted_query_param={quote(encrypted_query_param)}"

    def resolve_cdn_upload_url(self, payload: dict[str, Any], file_key: str) -> str:
        upload_full_url = str(payload.get("upload_full_url") or payload.get("uploadFullUrl") or "").strip()
        if upload_full_url:
            return upload_full_url

        upload_param = str(payload.get("upload_param") or payload.get("uploadParam") or "").strip()
        if upload_param:
            return self.build_cdn_upload_url(upload_param, file_key)

        nested = payload.get("data")
        if isinstance(nested, dict):
            return self.resolve_cdn_upload_url(nested, file_key)
        return ""
