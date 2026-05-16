from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


SCHEMA = """
CREATE TABLE IF NOT EXISTS lazarus_index (
    infohash TEXT PRIMARY KEY,
    name TEXT,
    size_bytes INTEGER,
    creation_date INTEGER,
    leechers INTEGER DEFAULT 0,
    is_recoverable INTEGER NOT NULL DEFAULT 0,
    seeds INTEGER DEFAULT 0,
    peers INTEGER DEFAULT 0,
    source TEXT,
    discovered_at INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    metadata_status TEXT NOT NULL DEFAULT 'pending',
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_lazarus_creation_date
ON lazarus_index (creation_date);

CREATE INDEX IF NOT EXISTS idx_lazarus_recoverable
ON lazarus_index (is_recoverable);

CREATE INDEX IF NOT EXISTS idx_lazarus_last_seen
ON lazarus_index (last_seen);
"""


@dataclass(slots=True)
class IndexRecord:
    infohash: str
    name: str | None = None
    size_bytes: int | None = None
    creation_date: int | None = None
    leechers: int | None = None
    is_recoverable: bool | None = None
    seeds: int | None = None
    peers: int | None = None
    source: str | None = None
    discovered_at: int | None = None
    last_seen: int | None = None
    metadata_status: str = "pending"
    error: str | None = None

    def normalized_infohash(self) -> str:
        cleaned = self.infohash.strip().lower()
        if len(cleaned) != 40:
            raise ValueError(f"infohash must be 40 hex characters, got {self.infohash!r}")
        int(cleaned, 16)
        return cleaned


class LazarusDatabase:
    def __init__(self, path: str | Path = "lazarus.db") -> None:
        self.path = Path(path)
        if self.path.parent != Path("."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LazarusDatabase":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def initialize(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def upsert_record(self, record: IndexRecord) -> None:
        now = int(time.time())
        infohash = record.normalized_infohash()
        discovered_at = record.discovered_at or now
        last_seen = record.last_seen or now
        is_recoverable = (
            int(record.is_recoverable)
            if record.is_recoverable is not None
            else int((record.seeds or 0) > 0)
        )

        self.conn.execute(
            """
            INSERT INTO lazarus_index (
                infohash, name, size_bytes, creation_date, leechers,
                is_recoverable, seeds, peers, source, discovered_at,
                last_seen, metadata_status, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(infohash) DO UPDATE SET
                name = COALESCE(excluded.name, lazarus_index.name),
                size_bytes = COALESCE(excluded.size_bytes, lazarus_index.size_bytes),
                creation_date = COALESCE(excluded.creation_date, lazarus_index.creation_date),
                leechers = COALESCE(excluded.leechers, lazarus_index.leechers),
                is_recoverable = CASE
                    WHEN excluded.is_recoverable = 1 OR lazarus_index.is_recoverable = 1 THEN 1
                    ELSE 0
                END,
                seeds = COALESCE(excluded.seeds, lazarus_index.seeds),
                peers = COALESCE(excluded.peers, lazarus_index.peers),
                source = COALESCE(excluded.source, lazarus_index.source),
                last_seen = excluded.last_seen,
                metadata_status = COALESCE(excluded.metadata_status, lazarus_index.metadata_status),
                error = excluded.error
            """,
            (
                infohash,
                record.name,
                record.size_bytes,
                record.creation_date,
                record.leechers,
                is_recoverable,
                record.seeds,
                record.peers,
                record.source,
                discovered_at,
                last_seen,
                record.metadata_status,
                record.error,
            ),
        )
        self.conn.commit()

    def upsert_many(self, records: Iterable[IndexRecord]) -> int:
        count = 0
        for record in records:
            self.upsert_record(record)
            count += 1
        return count

    def exists(self, infohash: str) -> bool:
        normalized = IndexRecord(infohash=infohash).normalized_infohash()
        row = self.conn.execute(
            "SELECT 1 FROM lazarus_index WHERE infohash = ?",
            (normalized,),
        ).fetchone()
        return row is not None

    def summary(self) -> dict[str, int]:
        row = self.conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN is_recoverable = 1 THEN 1 ELSE 0 END), 0) AS recoverable,
                COALESCE(SUM(CASE WHEN creation_date IS NOT NULL THEN 1 ELSE 0 END), 0) AS timestamped
            FROM lazarus_index
            """
        ).fetchone()
        return {
            "total": int(row["total"]),
            "recoverable": int(row["recoverable"]),
            "timestamped": int(row["timestamped"]),
        }
