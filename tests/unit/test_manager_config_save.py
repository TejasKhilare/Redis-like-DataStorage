"""The cluster manager saves its config without blocking the router's event loop."""

import asyncio
import time
from pathlib import Path

import pytest

from kvstore.cluster.manager import ClusterManager
from kvstore.cluster.topology import ClusterConfig

SLOW_DISK_S = 2.0  # generous: a slow CI machine still has to beat it


@pytest.fixture
def slow_disk(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Every save blocks its thread like an fsync behind heavy writeback; returns saved epochs."""
    real_save = ClusterConfig.save
    saved: list[int] = []

    def save(self: ClusterConfig, path: Path) -> None:
        time.sleep(SLOW_DISK_S)
        real_save(self, path)
        saved.append(self.epoch)

    monkeypatch.setattr(ClusterConfig, "save", save)
    return saved


async def longest_stall(seconds: float) -> float:
    """Tick every 10 ms for ``seconds``; the longest gap between ticks."""
    gaps, last = [], time.perf_counter()
    deadline = last + seconds
    while (now := time.perf_counter()) < deadline:
        gaps.append(now - last)
        last = now
        await asyncio.sleep(0.01)
    return max(gaps)


async def test_a_slow_disk_does_not_stall_the_router(tmp_path: Path, slow_disk: list[int]) -> None:
    path = tmp_path / "cluster.json"
    config = ClusterConfig.from_spec(["a=127.0.0.1:1+127.0.0.1:2"])
    routed: list[int] = []
    manager = ClusterManager(config, on_change=lambda c: routed.append(c.epoch), config_path=path)

    started = time.perf_counter()
    manager.set_config(config.next_epoch())
    assert time.perf_counter() - started < SLOW_DISK_S / 4
    assert routed == [1]  # the router follows the change at once
    assert await longest_stall(SLOW_DISK_S / 2) < SLOW_DISK_S / 4  # and keeps serving

    await manager.flush_config()
    saved = ClusterConfig.load(path)
    assert saved is not None and saved.epoch == 1


async def test_saves_are_in_order_and_end_with_the_newest(
    tmp_path: Path, slow_disk: list[int]
) -> None:
    path = tmp_path / "cluster.json"
    config = ClusterConfig.from_spec(["a=127.0.0.1:1+127.0.0.1:2"])
    manager = ClusterManager(config, on_change=lambda c: None, config_path=path)
    for _ in range(4):  # a burst, faster than the disk
        manager.set_config(manager.config.next_epoch())
        await asyncio.sleep(0)
    await manager.stop()  # stopping waits for the save
    assert slow_disk == sorted(slow_disk) and slow_disk[-1] == 4
    assert len(slow_disk) < 4  # changes made during a save share the next one
    saved = ClusterConfig.load(path)
    assert saved is not None and saved.epoch == 4


async def test_a_failed_save_is_retried_by_the_next_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cluster.json"
    config = ClusterConfig.from_spec(["a=127.0.0.1:1+127.0.0.1:2"])
    manager = ClusterManager(config, on_change=lambda c: None, config_path=path)
    real_save = ClusterConfig.save

    def full(self: ClusterConfig, path: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(ClusterConfig, "save", full)
    manager.set_config(config.next_epoch())
    await manager.flush_config()  # logged, not raised: routing goes on
    assert not path.exists()

    monkeypatch.setattr(ClusterConfig, "save", real_save)
    manager.set_config(manager.config.next_epoch())
    await manager.flush_config()
    saved = ClusterConfig.load(path)
    assert saved is not None and saved.epoch == 2
