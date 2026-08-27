"""여행 계획을 trip_id로 저장·조회하는 영구 저장소."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

from .trip_plan import TripPlan


class TripNotFoundError(KeyError):
    """저장된 여행 계획을 찾지 못했다."""


class TripVersionConflictError(RuntimeError):
    """다른 곳에서 먼저 저장되어 요청한 버전이 최신이 아니다."""


class TripStore:
    def __init__(self, path: str | Path = ".cache/plan_b_api.sqlite3") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS trip_plans (
                    trip_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(trip_plans)")
            }
            if "version" not in columns:
                connection.execute(
                    "ALTER TABLE trip_plans ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
                )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def create(self, plan: TripPlan, *, now: float | None = None) -> str:
        """새 trip_id를 발급해 계획을 저장하고(버전 1) 그 ID를 반환한다."""

        trip_id = uuid.uuid4().hex[:12]
        current = time.time() if now is None else now
        payload = json.dumps(plan.to_dict(), ensure_ascii=False)
        connection = self._connect()
        try:
            connection.execute(
                """
                INSERT INTO trip_plans(trip_id, payload, version, created_at, updated_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (trip_id, payload, current, current),
            )
            connection.commit()
        finally:
            connection.close()
        return trip_id

    def save(
        self,
        trip_id: str,
        plan: TripPlan,
        *,
        expected_version: int | None = None,
        now: float | None = None,
    ) -> int:
        """기존 trip_id의 계획을 덮어쓰고 새 버전 번호를 반환한다.

        expected_version을 주면 그 버전일 때만 저장한다(낙관적 동시성 제어).
        저장된 버전이 다르면 TripVersionConflictError, trip_id가 없으면
        TripNotFoundError를 낸다.
        """

        current = time.time() if now is None else now
        payload = json.dumps(plan.to_dict(), ensure_ascii=False)
        connection = self._connect()
        try:
            if expected_version is None:
                cursor = connection.execute(
                    """
                    UPDATE trip_plans
                    SET payload = ?, updated_at = ?, version = version + 1
                    WHERE trip_id = ?
                    """,
                    (payload, current, trip_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE trip_plans
                    SET payload = ?, updated_at = ?, version = version + 1
                    WHERE trip_id = ? AND version = ?
                    """,
                    (payload, current, trip_id, expected_version),
                )
            connection.commit()
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT version FROM trip_plans WHERE trip_id = ?", (trip_id,)
                ).fetchone()
                if row is None:
                    raise TripNotFoundError(trip_id)
                raise TripVersionConflictError(
                    f"trip_id={trip_id}의 현재 버전은 {row[0]}인데 "
                    f"요청한 버전은 {expected_version}입니다. 최신 내용을 "
                    "다시 불러온 뒤 저장하세요."
                )
            new_version = connection.execute(
                "SELECT version FROM trip_plans WHERE trip_id = ?", (trip_id,)
            ).fetchone()[0]
        finally:
            connection.close()
        return new_version

    def get(self, trip_id: str) -> tuple[TripPlan, int]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT payload, version FROM trip_plans WHERE trip_id = ?",
                (trip_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise TripNotFoundError(trip_id)
        return TripPlan.from_mapping(json.loads(row[0])), row[1]
