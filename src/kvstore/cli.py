"""Interactive command-line client for the TCP data plane.

    python -m kvstore.cli --port 7000                 # REPL against the router
    python -m kvstore.cli --port 6379 SET greeting hi # one-shot

Arguments are strings; wrap JSON in single quotes to send objects or arrays:
    SET user:1 '{"name": "tejas", "age": 22}'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import sys
from typing import Any

from kvstore.core.exceptions import KVStoreError
from kvstore.protocol.client import KVClient


def parse_token(token: str) -> Any:
    """JSON objects, arrays and quoted strings are decoded; everything else stays a string."""
    if token[:1] in '{["':
        try:
            return json.loads(token)
        except ValueError:
            pass
    return token


def format_result(result: Any) -> str:
    if result is None:
        return "(nil)"
    if isinstance(result, int) and not isinstance(result, bool):
        return f"(integer) {result}"
    if isinstance(result, str):
        return result
    return json.dumps(result, indent=2)


async def run_command(client: KVClient, tokens: list[str]) -> tuple[bool, str]:
    command, *args = tokens
    try:
        result = await client.execute(command, *(parse_token(arg) for arg in args))
    except KVStoreError as exc:
        return False, f"(error) {exc.code}: {exc.message}"
    return True, format_result(result)


async def _repl(client: KVClient) -> None:
    prompt = f"{client.address}> "
    while True:
        try:
            line = await asyncio.to_thread(input, prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if line.strip().lower() in {"quit", "exit"}:
            return
        try:
            tokens = shlex.split(line)
        except ValueError as exc:
            print(f"(error) {exc}")
            continue
        if tokens:
            print((await run_command(client, tokens))[1])


async def _main(args: argparse.Namespace) -> int:
    async with KVClient(args.host, args.port, timeout_s=args.timeout) as client:
        if not args.command:
            await _repl(client)
            return 0
        ok, output = await run_command(client, args.command)
        print(output)
        return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kvstore-cli", description="kvstore command-line client")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6379)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run once")
    return asyncio.run(_main(parser.parse_args(argv)))


if __name__ == "__main__":
    sys.exit(main())
