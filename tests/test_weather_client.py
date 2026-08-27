from __future__ import annotations

import json
import unittest
from datetime import datetime

from plan_b_api import (
    KMAClient,
    KMAConfig,
    calculate_weather_severity,
    grid_from_latlon,
    latest_forecast_base,
)


def forecast_items() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    values = {
        "TMP": "25",
        "POP": "90",
        "PTY": "1",
        "PCP": "30.0mm",
        "SKY": "4",
        "REH": "85",
        "WSD": "4.5",
    }
    for category, value in values.items():
        result.append({
            "fcstDate": "20260729",
            "fcstTime": "1200",
            "category": category,
            "fcstValue": value,
        })
    return result


class WeatherClientTests(unittest.TestCase):
    def test_seoul_coordinate_converts_to_kma_grid(self) -> None:
        self.assertEqual(grid_from_latlon(37.5760, 126.9767), (60, 127))

    def test_latest_base_waits_for_publication_delay(self) -> None:
        self.assertEqual(
            latest_forecast_base(datetime(2026, 7, 29, 14, 5)),
            ("20260729", "1100"),
        )
        self.assertEqual(
            latest_forecast_base(datetime(2026, 7, 29, 14, 20)),
            ("20260729", "1400"),
        )

    def test_heavy_rain_has_high_severity(self) -> None:
        severity = calculate_weather_severity(
            precipitation_probability=90,
            precipitation_type=1,
            precipitation_mm=30,
            temperature_c=25,
            wind_speed_mps=4,
        )
        self.assertGreaterEqual(severity, 0.8)

    def test_forecast_response_becomes_snapshot(self) -> None:
        payload = {
            "response": {
                "header": {"resultCode": "00", "resultMsg": "NORMAL_SERVICE"},
                "body": {"items": {"item": forecast_items()}},
            }
        }
        seen_urls: list[str] = []

        def transport(url: str, _: float) -> tuple[int, bytes]:
            seen_urls.append(url)
            return 200, json.dumps(payload).encode()

        client = KMAClient(
            KMAConfig(service_key="test", max_retries=0),
            transport=transport,
        )
        snapshot = client.village_forecast(
            latitude=37.5760,
            longitude=126.9767,
            target_time=datetime(2026, 7, 29, 11, 30),
            now=datetime(2026, 7, 29, 14, 20),
        )

        self.assertEqual(snapshot.forecast_time, datetime(2026, 7, 29, 12))
        self.assertEqual(snapshot.temperature_c, 25.0)
        self.assertEqual(snapshot.precipitation_mm, 30.0)
        self.assertGreaterEqual(snapshot.severity, 0.8)
        self.assertIn("nx=60", seen_urls[0])
        self.assertIn("ny=127", seen_urls[0])


if __name__ == "__main__":
    unittest.main()
