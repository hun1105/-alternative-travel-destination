from __future__ import annotations

import unittest
from datetime import datetime

from plan_b_api import NextScheduleConstraint, evaluate_schedule_feasibility


class ScheduleFeasibilityTests(unittest.TestCase):
    def test_candidate_reaches_next_schedule_with_buffer(self) -> None:
        result = evaluate_schedule_feasibility(
            start_time=datetime(2026, 8, 4, 14, 0),
            constraint=NextScheduleConstraint(
                longitude=127.0,
                latitude=37.5,
                arrival_deadline=datetime(2026, 8, 4, 16, 0),
                visit_minutes=60,
                buffer_minutes=10,
            ),
            inbound_minutes=15,
            onward_minutes=20,
            onward_mode="walking",
            onward_source="테스트 경로",
        )

        self.assertTrue(result.feasible)
        self.assertEqual(result.required_minutes, 105)
        self.assertEqual(result.slack_minutes, 15)
        self.assertEqual(
            result.estimated_next_arrival,
            datetime(2026, 8, 4, 15, 35),
        )

    def test_late_candidate_is_rejected(self) -> None:
        result = evaluate_schedule_feasibility(
            start_time=datetime(2026, 8, 4, 14, 0),
            constraint=NextScheduleConstraint(
                longitude=127.0,
                latitude=37.5,
                arrival_deadline=datetime(2026, 8, 4, 15, 30),
            ),
            inbound_minutes=20,
            onward_minutes=20,
            onward_mode="transit",
            onward_source="테스트 경로",
        )

        self.assertFalse(result.feasible)
        self.assertEqual(result.slack_minutes, -20)


if __name__ == "__main__":
    unittest.main()
