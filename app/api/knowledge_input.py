from collections.abc import Awaitable, Callable

from fastapi import Request, UploadFile
from fastapi.routing import APIRoute
from starlette.responses import Response

from app.core.constants import (
    ERR_KB_FILE_TOO_LARGE,
    ERR_KB_REQUEST_TOO_LARGE,
    KNOWLEDGE_FILE_MAX_BYTES,
    KNOWLEDGE_FILE_READ_CHUNK_BYTES,
    KNOWLEDGE_REQUEST_MAX_BYTES,
)
from app.core.exceptions import ParameterException


class KnowledgeInputRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original_handler = super().get_route_handler()

        async def bounded_handler(request: Request) -> Response:
            if request.method == "POST":
                body = bytearray()
                async for chunk in request.stream():
                    if len(body) + len(chunk) > KNOWLEDGE_REQUEST_MAX_BYTES:
                        raise ParameterException(
                            ERR_KB_REQUEST_TOO_LARGE,
                            code=413,
                            max_bytes=KNOWLEDGE_REQUEST_MAX_BYTES,
                        )
                    body.extend(chunk)
                request._body = bytes(body)

            return await original_handler(request)

        return bounded_handler


async def read_knowledge_file(file: UploadFile) -> bytes:
    content = bytearray()
    while chunk := await file.read(KNOWLEDGE_FILE_READ_CHUNK_BYTES):
        if len(content) + len(chunk) > KNOWLEDGE_FILE_MAX_BYTES:
            raise ParameterException(
                ERR_KB_FILE_TOO_LARGE,
                code=413,
                max_bytes=KNOWLEDGE_FILE_MAX_BYTES,
            )
        content.extend(chunk)
    return bytes(content)
