from __future__ import annotations

import json
import unittest
from datetime import datetime

from plan_b_api import (
    SeoulCrowdClient,
    SeoulCrowdConfig,
    find_seoul_crowd_area,
    load_seoul_crowd_areas,
    normalized_congestion,
    scoring_area_crowd_level,
)


class SeoulCrowdClientTests(unittest.TestCase):
    def test_area_crowd_is_blended_with_neutral_value(self) -> None:
        self.assertEqual(scoring_area_crowd_level(0.0), 0.2)
        self.assertEqual(scoring_area_crowd_level(0.5), 0.5)
        self.assertEqual(scoring_area_crowd_level(1.0), 0.8)

    def test_official_area_file_matches_gyeongbokgung_coordinate(self) -> None:
        areas = load_seoul_crowd_areas()
        area = find_seoul_crowd_area(126.9767, 37.5760, areas)

        self.assertEqual(len(areas), 121)
        self.assertIsNotNone(area)
        self.assertEqual(area.area_code, "POI008")
        self.assertEqual(area.name, "경복궁")

    def test_forecast_nearest_to_arrival_is_selected(self) -> None:
        payload = {
            "SeoulRtd.citydata_ppltn": {
                "RESULT": {"CODE": "INFO-000", "MESSAGE": "정상 처리되었습니다"},
                "row": [{
                    "AREA_NM": "경복궁",
                    "AREA_CD": "POI008",
                    "AREA_CONGEST_LVL": "여유",
                    "AREA_PPLTN_MIN": "1000",
                    "AREA_PPLTN_MAX": "1200",
                    "PPLTN_TIME": "2026-08-04 14:00",
                    "FCST_PPLTN": [
                        {
                            "FCST_TIME": "2026-08-04 15:00",
                            "FCST_CONGEST_LVL": "약간 붐빔",
                            "FCST_PPLTN_MIN": "2000",
                            "FCST_PPLTN_MAX": "2400",
                        }
                    ],
                }],
            }
        }

        def transport(_: str, __: float) -> tuple[int, bytes]:
            return 200, json.dumps(payload, ensure_ascii=False).encode()

        client = SeoulCrowdClient(
            SeoulCrowdConfig("secret", max_retries=0), transport=transport
        )
        result = client.crowd(
            "POI008", target_time=datetime(2026, 8, 4, 15, 10)
        )

        self.assertTrue(result.is_forecast)
        self.assertEqual(result.congestion_label, "약간 붐빔")
        self.assertEqual(result.population_min, 2000)
        self.assertAlmostEqual(result.normalized_level, 2 / 3)

    def test_congestion_labels_are_normalized(self) -> None:
        self.assertEqual(normalized_congestion("여유"), 0)
        self.assertEqual(normalized_congestion("붐빔"), 1)


if __name__ == "__main__":
    unittest.main()
