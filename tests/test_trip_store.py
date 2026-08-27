from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from plan_b_api.trip_plan import TripPlan
from plan_b_api.trip_store import (
    TripNotFoundError,
    TripStore,
    TripVersionConflictError,
)


def sample_plan() -> dict[str, object]:
    return {
        "title": "서울 역사 여행",
        "trip_date": "2026-08-07",
        "start_time": "10:00",
        "schedules": [{
            "item_id": "stop-1",
            "visit_minutes": 60,
            "place": {
                "provider": "tmap",
                "place_id": "1001",
                "name": "경복궁",
                "longitude": 126.9767,
                "latitude": 37.5760,
            },
        }],
    }


class TripStoreTests(unittest.TestCase):
    def test_create_then_get_round_trips_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "trips.sqlite3")
            plan = TripPlan.from_mapping(sample_plan())

            trip_id = store.create(plan)
            loaded, version = store.get(trip_id)

            self.assertEqual(loaded.title, plan.title)
            self.assertEqual(loaded.schedules[0].place.name, "경복궁")
            self.assertEqual(version, 1)

    def test_get_unknown_trip_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "trips.sqlite3")

            with self.assertRaises(TripNotFoundError):
                store.get("missing-id")

    def test_save_overwrites_existing_plan_and_bumps_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "trips.sqlite3")
            plan = TripPlan.from_mapping(sample_plan())
            trip_id = store.create(plan)

            renamed = TripPlan.from_mapping({**sample_plan(), "title": "수정된 제목"})
            new_version = store.save(trip_id, renamed)

            self.assertEqual(new_version, 2)
            loaded, version = store.get(trip_id)
            self.assertEqual(loaded.title, "수정된 제목")
            self.assertEqual(version, 2)

    def test_save_unknown_trip_id_raises(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "trips.sqlite3")
            plan = TripPlan.from_mapping(sample_plan())

            with self.assertRaises(TripNotFoundError):
                store.save("missing-id", plan)

    def test_save_with_matching_expected_version_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "trips.sqlite3")
            trip_id = store.create(TripPlan.from_mapping(sample_plan()))

            new_version = store.save(
                trip_id,
                TripPlan.from_mapping({**sample_plan(), "title": "A"}),
                expected_version=1,
            )

            self.assertEqual(new_version, 2)

    def test_save_with_stale_expected_version_raises_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = TripStore(Path(directory) / "trips.sqlite3")
            trip_id = store.create(TripPlan.from_mapping(sample_plan()))
            store.save(
                trip_id, TripPlan.from_mapping({**sample_plan(), "title": "먼저 저장됨"})
            )

            with self.assertRaises(TripVersionConflictError):
                store.save(
                    trip_id,
                    TripPlan.from_mapping({**sample_plan(), "title": "뒤늦은 저장"}),
                    expected_version=1,
                )

            loaded, version = store.get(trip_id)
            self.assertEqual(loaded.title, "먼저 저장됨")
            self.assertEqual(version, 2)


if __name__ == "__main__":
    unittest.main()
