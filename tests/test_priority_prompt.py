from __future__ import annotations

import unittest

from plan_b_api import dynamic_weights, prompt_user_priorities


class PriorityPromptTests(unittest.TestCase):
    def test_accepts_partial_order(self) -> None:
        outputs: list[str] = []
        priorities = prompt_user_priorities(
            input_func=lambda _: "5, 1, 3",
            output_func=outputs.append,
        )
        weights = dynamic_weights(priorities)

        self.assertEqual(priorities.walking_fit, 1)
        self.assertEqual(priorities.weather_fit, 2)
        self.assertEqual(priorities.crowd_avoidance, 3)
        self.assertEqual(weights["walking_fit"], 32.0)
        self.assertEqual(weights["weather_fit"], 23.0)
        self.assertEqual(weights["crowd_avoidance"], 17.0)
        self.assertNotIn("budget_fit", weights)
        self.assertEqual(weights["child_fit"], 0.0)
        self.assertIn("선택 가능한 개수는 1~5개입니다.", outputs[0])

    def test_retries_after_duplicate_selection(self) -> None:
        answers = iter(("1,1", "1"))
        outputs: list[str] = []

        priorities = prompt_user_priorities(
            input_func=lambda _: next(answers),
            output_func=outputs.append,
        )

        self.assertEqual(priorities.weather_fit, 1)
        self.assertTrue(any("중복" in output for output in outputs))


if __name__ == "__main__":
    unittest.main()
