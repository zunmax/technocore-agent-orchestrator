from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.storage.sqlite import LATEST_SCHEMA_VERSION


def test_concurrent_database_open_serializes_schema_migration(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite3"
    barrier = Barrier(2)

    def open_store() -> None:
        barrier.wait(timeout=5)
        with SQLiteStore.open(database):
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(lambda _index: open_store(), range(2)))

    with sqlite3.connect(database) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == LATEST_SCHEMA_VERSION
