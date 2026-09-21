"""Map domain errors to HTTP responses with one consistent error envelope."""

from __future__ import annotations

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from kvstore.core.exceptions import (
    ClusterError,
    CommandError,
    CrossShardError,
    KeyNotFoundError,
    KVStoreError,
    NodeUnavailableError,
    ProtocolError,
)
from kvstore.schemas.common import ErrorBody, ErrorResponse

_STATUS_BY_ERROR: dict[type[KVStoreError], int] = {
    KeyNotFoundError: status.HTTP_404_NOT_FOUND,
    CommandError: status.HTTP_400_BAD_REQUEST,
    ProtocolError: status.HTTP_400_BAD_REQUEST,
    CrossShardError: status.HTTP_400_BAD_REQUEST,
    NodeUnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
    ClusterError: status.HTTP_502_BAD_GATEWAY,
}


def status_for(error: KVStoreError) -> int:
    for cls in type(error).__mro__:
        if cls in _STATUS_BY_ERROR:
            return _STATUS_BY_ERROR[cls]
    return status.HTTP_500_INTERNAL_SERVER_ERROR


def error_response(status_code: int, body: ErrorBody) -> JSONResponse:
    content = ErrorResponse(error=body).model_dump(exclude_none=True)
    return JSONResponse(status_code=status_code, content=content)


async def _handle_kvstore_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, KVStoreError)
    return error_response(status_for(exc), ErrorBody(code=exc.code, message=exc.message))


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    body = ErrorBody(
        code="VALIDATION_ERROR",
        message="request validation failed",
        details=jsonable_encoder(exc.errors()),
    )
    return error_response(422, body)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(KVStoreError, _handle_kvstore_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
