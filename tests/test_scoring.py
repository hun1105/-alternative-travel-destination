from __future__ import annotations

import unittest

from plan_b_api import (
    CandidateConstraints,
    CandidateSignals,
    UserPriorities,
    score_candidate,
)


def signals(*, budget: float, crowd: float, route: float = 0.8) -> CandidateSignals:
    return CandidateSignals(
        weather_fit=0.8,
        route_time=route,
        crowd_avoidance=crowd,
        budget_fit=budget,
        child_fit=0.8,
        walking_fit=0.8,
        data_confidence=0.9,
    )


class DynamicScoringTests(unittest.TestCase):
    def test_priority_changes_candidate_order(self) -> None:
        near = signals(budget=1.0, crowd=0.2, route=1.0)
        quiet = signals(budget=0.3, crowd=1.0, route=0.2)

        route_user = UserPriorities.from_order(
            "route_time", "weather_fit", "walking_fit",
            "child_fit", "crowd_avoidance",
        )
        quiet_user = UserPriorities.from_order(
            "crowd_avoidance", "weather_fit", "route_time",
            "child_fit", "walking_fit",
        )

        self.assertGreater(
            score_candidate(near, route_user).total_score,
            score_candidate(quiet, route_user).total_score,
        )
        self.assertGreater(
            score_candidate(quiet, quiet_user).total_score,
            score_candidate(near, quiet_user).total_score,
        )

    def test_budget_is_excluded_from_score(self) -> None:
        low = score_candidate(signals(budget=0.0, crowd=0.5), UserPriorities())
        high = score_candidate(signals(budget=1.0, crowd=0.5), UserPriorities())
        self.assertEqual(low.total_score, high.total_score)
        self.assertNotIn("budget_fit", low.weights)

    def test_hard_constraint_cannot_be_overridden_by_priority(self) -> None:
        result = score_candidate(
            signals(budget=1.0, crowd=1.0),
            UserPriorities(),
            CandidateConstraints(open_now=False),
        )

        self.assertFalse(result.eligible)
        self.assertEqual(result.total_score, 0.0)
        self.assertIn("도착 시 운영하지 않음", result.rejection_reasons)

    def test_weights_always_sum_to_one_hundred(self) -> None:
        result = score_candidate(
            signals(budget=0.7, crowd=0.7),
            UserPriorities(),
        )

        self.assertAlmostEqual(sum(result.weights.values()), 100.0, places=2)

    def test_first_rank_always_has_more_weight_than_second(self) -> None:
        priorities = UserPriorities.from_order(
            "walking_fit", "weather_fit", "route_time",
            "crowd_avoidance", "child_fit",
        )
        result = score_candidate(
            signals(budget=0.7, crowd=0.7),
            priorities,
        )

        self.assertEqual(result.weights["walking_fit"], 32.0)
        self.assertEqual(result.weights["weather_fit"], 23.0)

    def test_duplicate_ranks_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "중복"):
            UserPriorities(weather_fit=1, route_time=1).as_dict()

    def test_unselected_priorities_receive_equal_weight(self) -> None:
        priorities = UserPriorities.from_order("walking_fit", "weather_fit")
        result = score_candidate(
            signals(budget=0.7, crowd=0.7),
            priorities,
        )

        self.assertEqual(result.weights["walking_fit"], 32.0)
        self.assertEqual(result.weights["weather_fit"], 23.0)
        self.assertEqual(result.weights["route_time"], 20.0)
        self.assertEqual(result.weights["crowd_avoidance"], 20.0)
        self.assertEqual(result.weights["child_fit"], 0.0)
        self.assertAlmostEqual(sum(result.weights.values()), 100.0)

    def test_child_fit_is_used_only_when_selected(self) -> None:
        unselected = score_candidate(
            signals(budget=0.7, crowd=0.7),
            UserPriorities.from_order("route_time"),
        )
        selected = score_candidate(
            signals(budget=0.7, crowd=0.7),
            UserPriorities.from_order("child_fit", "route_time"),
        )

        self.assertEqual(unselected.weights["child_fit"], 0.0)
        self.assertEqual(selected.weights["child_fit"], 32.0)


if __name__ == "__main__":
    unittest.main()
