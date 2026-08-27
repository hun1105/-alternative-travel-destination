from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime

from plan_b_api import (
    CandidateFacts,
    NormalizedPlace,
    TripContext,
    UserPriorities,
    build_candidate_signals,
    evaluate_place_candidate,
    normalize_place,
)


def make_place() -> NormalizedPlace:
    return normalize_place(
        {
            "content_id": "126508",
            "content_type_id": "12",
            "common": [{
                "title": "경복궁",
                "mapx": "126.97",
                "mapy": "37.57",
                "firstimage": "https://example.com/image.jpg",
            }],
            "intro": [{
                "restdate": "매주 화요일",
                "usetime": "[1월~12월]09:00~18:00",
                "parking": "가능",
            }],
            "info": [
                {"infoname": "입장료", "infotext": "개인 대인 3,000원"},
                {"infoname": "화장실", "infotext": "있음"},
            ],
            "images": [],
        }
    )


def facts(**changes: object) -> CandidateFacts:
    values = {
        "indoor_ratio": 0.7,
        "route_minutes": 10.0,
        "walking_meters": 800.0,
        "crowd_level": 0.3,
        "child_suitability": 0.9,
        "visit_minutes": 60.0,
    }
    values.update(changes)
    return CandidateFacts(**values)


class SignalBuilderTests(unittest.TestCase):
    def test_calculates_group_cost_and_signals(self) -> None:
        result = build_candidate_signals(
            make_place(),
            facts(),
            TripContext(
                arrival_time=datetime(2026, 7, 29, 10),
                party_size=3,
                remaining_budget_krw=20_000,
            ),
        )

        self.assertEqual(result.estimated_group_cost_krw, 9_000)
        self.assertTrue(result.constraints.within_budget)
        self.assertAlmostEqual(result.signals.budget_fit, 0.775)
        self.assertTrue(result.open_at_arrival)

    def test_closed_day_is_hard_rejection(self) -> None:
        result = evaluate_place_candidate(
            make_place(),
            facts(),
            TripContext(arrival_time=datetime(2026, 7, 28, 10)),
            UserPriorities(),
        )

        self.assertFalse(result.score.eligible)
        self.assertIn("도착 시 운영하지 않음", result.score.rejection_reasons)

    def test_severe_weather_rejects_outdoor_candidate(self) -> None:
        result = evaluate_place_candidate(
            make_place(),
            facts(indoor_ratio=0.1),
            TripContext(
                arrival_time=datetime(2026, 7, 29, 10),
                weather_severity=0.9,
            ),
            UserPriorities(),
        )

        self.assertFalse(result.build.constraints.weather_safe)
        self.assertFalse(result.score.eligible)

    def test_locked_schedule_uses_route_visit_and_buffer(self) -> None:
        result = build_candidate_signals(
            make_place(),
            facts(route_minutes=30.0, visit_minutes=60.0, onward_minutes=20.0),
            TripContext(
                arrival_time=datetime(2026, 7, 29, 10),
                minutes_until_locked_stop=100.0,
                schedule_buffer_minutes=10.0,
            ),
        )

        self.assertFalse(result.constraints.reaches_locked_stop)

    def test_walking_over_fifteen_minutes_is_rejected(self) -> None:
        result = evaluate_place_candidate(
            make_place(),
            facts(route_minutes=15.1),
            TripContext(arrival_time=datetime(2026, 7, 29, 10)),
            UserPriorities(),
        )

        self.assertFalse(result.score.eligible)
        self.assertIn(
            "도보 이동시간이 최대 허용시간을 초과함",
            result.score.rejection_reasons,
        )

    def test_exactly_fifteen_minutes_walking_is_rejected(self) -> None:
        # 정확히 15분이면 도보를 유지하지 않고 버스 등 다른 수단으로 넘긴다.
        result = evaluate_place_candidate(
            make_place(),
            facts(route_minutes=15.0),
            TripContext(arrival_time=datetime(2026, 7, 29, 10)),
            UserPriorities(),
        )

        self.assertFalse(result.score.eligible)
        self.assertIn(
            "도보 이동시간이 최대 허용시간을 초과함",
            result.score.rejection_reasons,
        )

    def test_just_under_fifteen_minutes_walking_is_accepted(self) -> None:
        result = evaluate_place_candidate(
            make_place(),
            facts(route_minutes=14.9),
            TripContext(arrival_time=datetime(2026, 7, 29, 10)),
            UserPriorities(),
        )

        self.assertNotIn(
            "도보 이동시간이 최대 허용시간을 초과함",
            result.score.rejection_reasons,
        )

    def test_transit_over_thirty_minutes_is_rejected(self) -> None:
        result = evaluate_place_candidate(
            make_place(),
            facts(
                route_minutes=31,
                walking_minutes=6,
                transport_mode="transit",
            ),
            TripContext(arrival_time=datetime(2026, 7, 29, 10)),
            UserPriorities(),
        )

        self.assertFalse(result.score.eligible)
        self.assertIn(
            "차량·대중교통 이동시간이 30분 한도를 초과함",
            result.score.rejection_reasons,
        )

    def test_unknown_values_create_notes(self) -> None:
        place = replace(make_place(), adult_fee_krw=None)
        result = build_candidate_signals(
            place,
            facts(crowd_level=None, child_suitability=None),
            TripContext(
                arrival_time=datetime(2026, 7, 29, 10),
                children_count=1,
                party_size=2,
                remaining_budget_krw=10_000,
            ),
        )

        self.assertIsNone(result.estimated_group_cost_krw)
        self.assertEqual(result.signals.crowd_avoidance, 0.5)
        self.assertGreaterEqual(len(result.notes), 3)


if __name__ == "__main__":
    unittest.main()
