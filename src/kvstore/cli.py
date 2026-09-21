"""Interactive command-line client, redis-cli style (speaks RESP, so it works against Redis too).

    python -m kvstore.cli --port 7000                  # REPL against the router
    python -m kvstore.cli --port 6379 SET greeting hi  # one-shot

Quote arguments containing spaces: SET user:1 '{"name": "tejas"}'
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
from kvstore.protocol.resp import SimpleString


def format_result(result: Any, indent: int = 0) -> str:
    if isinstance(result, KVStoreError):
        return f"(error) {result.to_resp()}"
    if result is None:
        return "(nil)"
    if isinstance(result, SimpleString):
        return str(result)
    if isinstance(result, int):
        return f"(integer) {result}"
    if isinstance(result, str):
        return json.dumps(result, ensure_ascii=False)
    if isinstance(result, list):
        if not result:
            return "(empty array)"
        width = len(str(len(result)))
        lines = []
        for i, item in enumerate(result, 1):
            prefix = f"{i:>{width}}) "
            body = format_result(item, indent + len(prefix))
            lines.append((" " * indent if i > 1 else "") + prefix + body)
        return "\n".join(lines)
    return str(result)


async def run_command(client: KVClient, tokens: list[str]) -> tuple[bool, str]:
    try:
        result = await client.execute(*tokens)
    except KVStoreError as exc:
        return False, format_result(exc)
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
