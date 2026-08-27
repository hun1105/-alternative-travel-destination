from __future__ import annotations

import unittest
from datetime import datetime

from plan_b_api.trip_plan import (
    SelectedPlace,
    TripPlan,
    build_next_schedule_constraint,
    next_schedule_item,
    replace_schedule_item,
    validate_trip_plan,
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
                "address": "서울 종로구 사직로 161",
            },
        }],
    }


def two_stop_plan() -> dict[str, object]:
    data = sample_plan()
    data["schedules"].append({  # type: ignore[union-attr]
        "item_id": "stop-2",
        "visit_minutes": 90,
        "fixed_arrival_time": "17:00",
        "locked": True,
        "place": {
            "provider": "tmap",
            "place_id": "2002",
            "name": "동대문디자인플라자",
            "longitude": 127.0095,
            "latitude": 37.5665,
        },
    })
    return data


class TripPlanTests(unittest.TestCase):
    def test_map_selected_place_becomes_valid_plan(self) -> None:
        plan = TripPlan.from_mapping(sample_plan())

        self.assertEqual(plan.title, "서울 역사 여행")
        self.assertEqual(plan.schedules[0].place.name, "경복궁")
        self.assertEqual(plan.schedules[0].visit_minutes, 60)
        self.assertEqual(plan.to_dict()["trip_date"], "2026-08-07")

    def test_rejects_missing_coordinates(self) -> None:
        data = sample_plan()
        del data["schedules"][0]["place"]["longitude"]  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "longitude"):
            validate_trip_plan(data)

    def test_rejects_duplicate_item_ids(self) -> None:
        data = sample_plan()
        data["schedules"].append(data["schedules"][0].copy())  # type: ignore[union-attr,index]

        with self.assertRaisesRegex(ValueError, "중복"):
            validate_trip_plan(data)

    def test_replace_schedule_item_swaps_place_and_keeps_others(self) -> None:
        plan = TripPlan.from_mapping(two_stop_plan())
        replacement = SelectedPlace(
            provider="tmap",
            place_id="3003",
            name="대한민국역사박물관",
            longitude=126.9770,
            latitude=37.5738,
        )

        updated = replace_schedule_item(
            plan, "stop-1", place=replacement, visit_minutes=45
        )

        self.assertEqual(updated.schedules[0].place.name, "대한민국역사박물관")
        self.assertEqual(updated.schedules[0].visit_minutes, 45)
        # 다른 일정은 그대로 유지된다.
        self.assertEqual(updated.schedules[1].place.name, "동대문디자인플라자")

    def test_replace_schedule_item_unknown_id_raises(self) -> None:
        plan = TripPlan.from_mapping(sample_plan())
        replacement = SelectedPlace(
            provider="tmap", place_id="3003", name="대체 장소",
            longitude=126.9, latitude=37.5,
        )

        with self.assertRaisesRegex(ValueError, "찾을 수 없습니다"):
            replace_schedule_item(plan, "missing", place=replacement)

    def test_next_schedule_item_returns_following_stop(self) -> None:
        plan = TripPlan.from_mapping(two_stop_plan())

        following = next_schedule_item(plan, "stop-1")

        self.assertIsNotNone(following)
        self.assertEqual(following.item_id, "stop-2")

    def test_next_schedule_item_returns_none_for_last_stop(self) -> None:
        plan = TripPlan.from_mapping(two_stop_plan())

        self.assertIsNone(next_schedule_item(plan, "stop-2"))

    def test_build_next_schedule_constraint_from_locked_item(self) -> None:
        plan = TripPlan.from_mapping(two_stop_plan())
        following = next_schedule_item(plan, "stop-1")

        constraint = build_next_schedule_constraint(plan, following)

        self.assertIsNotNone(constraint)
        self.assertEqual(constraint.title, "동대문디자인플라자")
        self.assertEqual(
            constraint.arrival_deadline, datetime(2026, 8, 7, 17, 0)
        )
        self.assertEqual(constraint.visit_minutes, 90.0)

    def test_build_next_schedule_constraint_none_without_fixed_time(self) -> None:
        plan = TripPlan.from_mapping(two_stop_plan())
        # stop-1은 fixed_arrival_time이 없다.
        first = plan.schedules[0]

        self.assertIsNone(build_next_schedule_constraint(plan, first))
