from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from technocore_orchestrator.errors import StateError, StorageError
from technocore_orchestrator.runtime import (
    _control_paths,
    request_active_cancellation,
    run_with_control,
)


def test_kernel_lease_prevents_a_second_supervisor_and_releases_cleanly(tmp_path: Path) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def hold_lease() -> None:
            started.set()
            await release.wait()

        first = asyncio.create_task(
            run_with_control(hold_lease(), storage_root=tmp_path, run_id="run_locktest")
        )
        await started.wait()

        async def should_not_start() -> None:
            raise AssertionError("the second operation must not start")

        with pytest.raises(StateError, match="another supervisor owns this run"):
            await run_with_control(should_not_start(), storage_root=tmp_path, run_id="run_locktest")

        release.set()
        assert await first is None

        assert (
            await run_with_control(
                should_not_start_after_release(),
                storage_root=tmp_path,
                run_id="run_locktest",
            )
            is None
        )

    async def should_not_start_after_release() -> None:
        return None

    asyncio.run(exercise())


def test_cancellation_targets_only_an_active_supervisor(tmp_path: Path) -> None:
    assert request_active_cancellation(tmp_path, "run_canceltest") is False

    async def exercise() -> None:
        started = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            await asyncio.Event().wait()

        controlled = asyncio.create_task(
            run_with_control(wait_forever(), storage_root=tmp_path, run_id="run_canceltest")
        )
        await started.wait()
        assert request_active_cancellation(tmp_path, "run_canceltest") is True
        with pytest.raises(asyncio.CancelledError):
            await controlled

    asyncio.run(exercise())
    assert request_active_cancellation(tmp_path, "run_canceltest") is False


def test_stale_cancellation_marker_is_removed_before_an_operation(tmp_path: Path) -> None:
    paths = _control_paths(tmp_path, "run_stalecancel")
    paths.cancel.write_text("stale", encoding="utf-8")

    async def inspect_marker() -> None:
        assert not paths.cancel.exists()

    asyncio.run(run_with_control(inspect_marker(), storage_root=tmp_path, run_id="run_stalecancel"))
    assert not paths.cancel.exists()


def test_invalid_cancellation_marker_fails_closed(tmp_path: Path) -> None:
    paths = _control_paths(tmp_path, "run_badcancel")

    async def exercise() -> None:
        started = asyncio.Event()

        async def wait_forever() -> None:
            started.set()
            await asyncio.Event().wait()

        controlled = asyncio.create_task(
            run_with_control(wait_forever(), storage_root=tmp_path, run_id="run_badcancel")
        )
        await started.wait()
        paths.cancel.mkdir()
        with pytest.raises(StorageError, match="unable to inspect the cancellation request"):
            await controlled

    asyncio.run(exercise())
