"""외부 API 응답용 SQLite TTL 캐시."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CacheEntry:
    value: Any
    is_fresh: bool
    age_seconds: float


class SQLiteTTLCache:
    def __init__(self, path: str | Path = ".cache/plan_b_api.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS api_cache (
                    cache_key TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                )
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def get(self, key: str, *, now: float | None = None) -> CacheEntry | None:
        current = time.time() if now is None else now
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT payload, created_at, expires_at
                FROM api_cache
                WHERE cache_key = ?
                """,
                (key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
        except json.JSONDecodeError:
            return None
        return CacheEntry(
            value=value,
            is_fresh=current <= float(row[2]),
            age_seconds=max(0.0, current - float(row[1])),
        )

    def set(
        self,
        key: str,
        value: Any,
        *,
        ttl_seconds: float,
        now: float | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("캐시 TTL은 0보다 커야 합니다.")
        current = time.time() if now is None else now
        payload = json.dumps(value, ensure_ascii=False, default=str)
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO api_cache(cache_key, payload, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    payload = excluded.payload,
                    created_at = excluded.created_at,
                    expires_at = excluded.expires_at
                """,
                (key, payload, current, current + ttl_seconds),
            )
            connection.commit()
        finally:
            connection.close()
