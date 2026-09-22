"""Start and stop the servers under test: kvstore shards, a kvstore cluster, Redis."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Self

_START_TIMEOUT_S = 30.0


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Where the load generator connects: a RESP port and (kvstore only) an HTTP port."""

    resp_port: int
    http_port: int | None = None
    host: str = "127.0.0.1"


def _file_size_limit(limit: int | None) -> Callable[[], None] | None:
    """A ``preexec_fn`` capping the size of any file the process writes (POSIX only).

    Past it, ``write()`` fails with EFBIG -- CPython ignores SIGXFSZ -- which
    the node handles exactly like a full disk (ENOSPC).
    """
    if limit is None:
        return None
    if sys.platform == "win32":
        raise RuntimeError("file size limits need a POSIX system (use Linux or WSL)")
    import resource

    def apply() -> None:
        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return apply


class _Process(AbstractContextManager["_Process"]):
    def __init__(
        self,
        name: str,
        argv: list[str],
        env: dict[str, str],
        log_path: Path,
        *,
        file_size_limit: int | None = None,
    ) -> None:
        self.name = name
        self._argv, self._env = argv, env
        self._log = log_path.open("wb")
        self._proc = subprocess.Popen(
            argv,
            env=env,
            stdout=self._log,
            stderr=subprocess.STDOUT,
            preexec_fn=_file_size_limit(file_size_limit),
        )

    @property
    def pid(self) -> int:
        return self._proc.pid

    def kill(self) -> None:
        """SIGKILL: no shutdown code runs, like a crash (``kill -9``)."""
        self._proc.kill()
        self._proc.wait()

    def restart(self) -> None:
        """Start again with the same command, environment and data directory (no limits)."""
        if self._proc.poll() is None:
            raise RuntimeError(f"{self.name} is still running")
        self._proc = subprocess.Popen(
            self._argv, env=self._env, stdout=self._log, stderr=subprocess.STDOUT
        )

    def terminate(self) -> None:
        """SIGTERM, and wait: a clean shutdown."""
        self._proc.terminate()
        self._proc.wait(timeout=15)

    def check_alive(self) -> None:
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"{self.name} exited with code {self._proc.returncode}; see {self._log.name}"
            )

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._log.close()


def _remove_tree(path: Path) -> None:
    """Delete ``path``, retrying briefly: on Windows a stopped process's files
    can stay locked for a moment after it exits."""
    for _ in range(40):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(0.05)


def _wait_until(ready: Callable[[], bool], proc: _Process, what: str) -> None:
    deadline = time.monotonic() + _START_TIMEOUT_S
    while time.monotonic() < deadline:
        proc.check_alive()
        if ready():
            return
        time.sleep(0.1)
    raise TimeoutError(f"{what} did not become ready in {_START_TIMEOUT_S:.0f} s")


def _http_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/ready", timeout=1) as response:
            return bool(response.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def _replica_synced(http_port: int) -> bool:
    try:
        url = f"http://127.0.0.1:{http_port}/v1/commands"
        body = b'{"command": "INFO", "args": ["replication"]}'
        request = urllib.request.Request(
            url, data=body, headers={"content-type": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=1) as response:
            return b"master_link_status:up" in response.read()
    except (urllib.error.URLError, OSError):
        return False


def _resp_ready(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as sock:
            sock.sendall(b"PING\r\n")
            return sock.recv(64).startswith(b"+PONG")
    except OSError:
        return False


class Deployment(AbstractContextManager["Deployment"]):
    """A set of server processes plus a scratch directory, torn down together."""

    def __init__(self, work_dir: Path | None = None) -> None:
        self._stack = ExitStack()
        self.dir = Path(tempfile.mkdtemp(prefix="kvbench-", dir=work_dir))
        self._stack.callback(_remove_tree, self.dir)
        self._nodes: dict[str, tuple[_Process, Callable[[], bool]]] = {}
        self.endpoints: dict[str, Endpoint] = {}

    def kill(self, name: str) -> None:
        self._nodes[name][0].kill()

    def pid(self, name: str) -> int:
        return self._nodes[name][0].pid

    def stop(self, name: str) -> None:
        self._nodes[name][0].terminate()

    def restart(self, name: str) -> None:
        proc, ready = self._nodes[name]
        proc.restart()
        _wait_until(ready, proc, name)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stack.close()

    # ------------------------------------------------------------- kvstore
    def kvstore_node(
        self,
        name: str,
        *,
        fsync: str = "everysec",
        env: Mapping[str, str] | None = None,
        file_size_limit: int | None = None,
    ) -> Endpoint:
        """Start one node; ``env`` adds or overrides ``KV_*`` settings."""
        env = dict(env or {})
        resp_port, http_port = free_port(), free_port()
        node_env = {
            **os.environ,
            "KV_NODE_ID": name,
            "KV_TCP_PORT": str(resp_port),
            "KV_HTTP_PORT": str(http_port),
            "KV_DATA_DIR": str(self.dir / name),
            "KV_AOF_FSYNC": fsync,
            "KV_LOG_LEVEL": "WARNING",
            "KV_ACCESS_LOG": "false",
            **env,
        }
        proc = self._stack.enter_context(
            _Process(
                name,
                [sys.executable, "-m", "kvstore"],
                node_env,
                self.dir / f"{name}.log",
                file_size_limit=file_size_limit,
            )
        )
        ready = self._ready_fn(http_port, env.get("KV_REPLICAOF") is not None)
        self._nodes[name] = (proc, ready)
        _wait_until(ready, proc, name)
        self.endpoints[name] = Endpoint(resp_port, http_port)
        return self.endpoints[name]

    @staticmethod
    def _ready_fn(http_port: int, replica: bool) -> Callable[[], bool]:
        if not replica:
            return lambda: _http_ready(http_port)
        # A replica is ready once its first sync is done.
        return lambda: _http_ready(http_port) and _replica_synced(http_port)

    def replicated_cluster(
        self, groups: int = 3, *, fsync: str = "everysec", **router_env: str
    ) -> tuple[Endpoint, dict[str, tuple[Endpoint, Endpoint]]]:
        """``groups`` shard groups of a primary and a replica, behind a router."""
        members: dict[str, tuple[Endpoint, Endpoint]] = {}
        spec = []
        for i in range(1, groups + 1):
            primary = self.kvstore_node(f"g{i}-primary", fsync=fsync)
            replica = self.kvstore_node(
                f"g{i}-replica",
                fsync=fsync,
                env={"KV_REPLICAOF": f"127.0.0.1:{primary.resp_port}"},
            )
            members[f"g{i}"] = (primary, replica)
            spec.append(f"g{i}=127.0.0.1:{primary.resp_port}+127.0.0.1:{replica.resp_port}")
        router = self.kvstore_node(
            "router", env={"KV_NODE_ROLE": "router", "KV_SHARDS": ",".join(spec), **router_env}
        )
        return router, members

    def kvstore_cluster(self, shards: int = 3, *, fsync: str = "everysec") -> Endpoint:
        addresses = []
        for i in range(shards):
            shard = self.kvstore_node(f"shard-{i + 1}", fsync=fsync)
            addresses.append(f"127.0.0.1:{shard.resp_port}")
        return self.kvstore_node(
            "router", env={"KV_NODE_ROLE": "router", "KV_SHARDS": ",".join(addresses)}
        )

    # --------------------------------------------------------------- Redis
    def redis(
        self,
        redis_server: str,
        *,
        fsync: str = "everysec",
        loglevel: str = "warning",
        extra_args: Sequence[str] = (),
    ) -> Endpoint:
        port = free_port()
        data = self.dir / "redis"
        data.mkdir()
        argv = [
            redis_server,
            "--port", str(port),
            "--bind", "127.0.0.1",
            "--dir", str(data),
            "--save", "",
            "--appendonly", "yes",
            "--appendfsync", fsync,
            "--daemonize", "no",
            "--loglevel", loglevel,
            *extra_args,
        ]  # fmt: skip
        proc = self._stack.enter_context(
            _Process("redis", argv, dict(os.environ), self.dir / "redis.log")
        )
        ready = partial(_resp_ready, port)  # PONG only once the data is loaded
        self._nodes["redis"] = (proc, ready)
        _wait_until(ready, proc, "redis-server")
        self.endpoints["redis"] = Endpoint(port)
        return self.endpoints["redis"]
