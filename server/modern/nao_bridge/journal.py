import json
import sqlite3
from pathlib import Path
from typing import Any


class Journal:
    """Persist dispatch evidence before native effects; a restart never implies completion."""

    def __init__(self, directory: Path | None) -> None:
        if directory:
            directory.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            str(directory / "operations.sqlite3") if directory else ":memory:"
        )
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, record TEXT NOT NULL)"
        )

    def save(self, operation: dict[str, Any], owner: str, released: bool) -> None:
        record = {"operation": operation, "owner": owner, "released": released}
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO operations VALUES (?, ?)",
                (operation["id"], json.dumps(record, allow_nan=False)),
            )

    def load(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.db.execute("SELECT record FROM operations ORDER BY rowid")
        ]

    def delete(self, operation_id: str) -> None:
        with self.db:
            self.db.execute("DELETE FROM operations WHERE id = ?", (operation_id,))

    def close(self) -> None:
        self.db.close()
