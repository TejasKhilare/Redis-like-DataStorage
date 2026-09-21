"""Newline-delimited JSON wire protocol.

Request:  ``{"command": "SET", "args": ["user:1", {"name": "tejas"}]}``
Response: ``{"ok": true, "result": "OK"}``
          ``{"ok": false, "error": {"code": "WRONG_ARITY", "message": "..."}}``

Phase 2 replaces this with RESP so standard Redis tooling can connect.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from kvstore.core.exceptions import KVStoreError, ProtocolError


def encode_request(command: str, args: list[Any]) -> bytes:
    return _encode({"command": command, "args": args})


def decode_request(line: bytes) -> tuple[str, list[Any]]:
    try:
        request = json.loads(line)
    except ValueError:
        raise ProtocolError("request is not valid JSON") from None
    if not isinstance(request, dict):
        raise ProtocolError("request must be a JSON object")
    command, args = request.get("command"), request.get("args", [])
    if not isinstance(command, str) or not command:
        raise ProtocolError("'command' must be a non-empty string")
    if not isinstance(args, list):
        raise ProtocolError("'args' must be a list")
    return command, args


def encode_result(result: Any) -> bytes:
    return _encode({"ok": True, "result": result})


def encode_error(error: KVStoreError) -> bytes:
    return _encode({"ok": False, "error": {"code": error.code, "message": error.message}})


@dataclass(frozen=True, slots=True)
class Response:
    ok: bool
    result: Any = None
    error_code: str = ""
    error_message: str = ""

    def unwrap(self) -> Any:
        """Return the result, or raise the error the server reported."""
        if not self.ok:
            raise KVStoreError.from_code(self.error_code, self.error_message)
        return self.result


def decode_response(line: bytes) -> Response:
    try:
        response = json.loads(line)
    except ValueError:
        raise ProtocolError("response is not valid JSON") from None
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise ProtocolError("malformed response")
    if response["ok"]:
        return Response(ok=True, result=response.get("result"))
    error = response.get("error")
    if not isinstance(error, dict):
        raise ProtocolError("malformed error response")
    return Response(
        ok=False,
        error_code=str(error.get("code", KVStoreError.code)),
        error_message=str(error.get("message", "")),
    )


def _encode(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()
