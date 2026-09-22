"""A fault-injecting TCP proxy for chaos tests (a small Toxiproxy).

Put it between two nodes and it can:

* **delay** every chunk by a fixed latency, per direction, keeping the
  order and not throttling throughput (``tc netem delay`` without root);
* **partition**: connections still open, but nothing is forwarded either
  way -- not even a close -- so requests *time out*, as in a real network
  partition, rather than failing fast like a dead process;
* **heal**: forward again. Connections whose data was swallowed during the
  partition are closed, as a real peer would reset them; clients reconnect.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field


@dataclass(eq=False)
class _Link:
    client: asyncio.StreamWriter
    upstream: asyncio.StreamWriter
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    lost_data: bool = False

    def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.client.close()
        self.upstream.close()


class FaultProxy:
    def __init__(self, target_host: str, target_port: int, *, host: str = "127.0.0.1") -> None:
        self.target = (target_host, target_port)
        self.host = host
        self.latency_s = 0.0
        self.partitioned = False
        self._healed = asyncio.Event()
        self._healed.set()
        self._server: asyncio.Server | None = None
        self._links: set[_Link] = set()
        self.connections = 0

    @property
    def port(self) -> int:
        assert self._server is not None
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"

    async def start(self, port: int = 0) -> None:
        self._server = await asyncio.start_server(self._accept, self.host, port)

    async def stop(self) -> None:
        for link in list(self._links):
            link.close()
        self._links.clear()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._server.wait_closed(), 2)

    # ------------------------------------------------------------ faults
    def partition(self) -> None:
        self.partitioned = True
        self._healed.clear()

    def heal(self) -> None:
        self.partitioned = False
        self._healed.set()
        for link in [link for link in self._links if link.lost_data]:
            link.close()
            self._links.discard(link)

    def delay(self, seconds: float) -> None:
        self.latency_s = seconds

    # --------------------------------------------------------- plumbing
    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        try:
            up_reader, up_writer = await asyncio.open_connection(*self.target)
        except OSError:
            writer.close()
            return
        link = _Link(writer, up_writer)
        self._links.add(link)
        link.tasks = [
            asyncio.create_task(self._pump(reader, up_writer, link)),
            asyncio.create_task(self._pump(up_reader, writer, link)),
        ]
        await asyncio.gather(*link.tasks, return_exceptions=True)
        # A peer that closed during a partition: the other side must not learn
        # of it until the partition heals (a FIN would not get through either).
        await self._healed.wait()
        link.close()
        self._links.discard(link)

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, link: _Link
    ) -> None:
        queue: asyncio.Queue[tuple[float, bytes] | None] = asyncio.Queue()

        async def deliver() -> None:
            while (item := await queue.get()) is not None:
                due, data = item
                wait = due - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                writer.write(data)
                await writer.drain()

        sender = asyncio.create_task(deliver())
        try:
            while data := await reader.read(65536):
                if self.partitioned:
                    link.lost_data = True  # dropped on the floor
                    continue
                await queue.put((time.monotonic() + self.latency_s, data))
            await queue.put(None)
            await sender
        except (ConnectionError, OSError):
            pass
        finally:
            sender.cancel()
            if not self.partitioned:
                with contextlib.suppress(Exception):
                    writer.write_eof()
