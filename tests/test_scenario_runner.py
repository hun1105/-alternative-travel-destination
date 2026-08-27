from __future__ import annotations

import unittest

from plan_b_api.scenario_runner import run_scenarios


class ScenarioRunnerTests(unittest.TestCase):
    def test_all_mvp_scenarios_pass(self) -> None:
        results = run_scenarios()
        self.assertEqual(len(results), 10)
        self.assertTrue(
            all(result.passed for result in results),
            [result.name for result in results if not result.passed],
        )


if __name__ == "__main__":
    unittest.main()
