from __future__ import annotations

import json
import unittest

from plan_b_api import TMapCarClient, TMapCarConfig


class CarRouteClientTests(unittest.TestCase):
    def test_parses_car_route_summary(self) -> None:
        captured: dict[str, object] = {}

        def transport(url, headers, body, timeout):
            captured.update({
                "url": url,
                "headers": headers,
                "body": json.loads(body.decode("utf-8")),
                "timeout": timeout,
            })
            response = {"features": [
                {
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[126.97, 37.57], [127.01, 37.56]],
                    },
                },
                {"properties": {
                    "totalDistance": 7200,
                    "totalTime": 1080,
                    "totalFare": 1400,
                    "taxiFare": 9200,
                }},
            ]}
            return 200, json.dumps(response).encode("utf-8")

        route = TMapCarClient(
            TMapCarConfig("secret"), transport=transport
        ).car_route(
            start_x=126.97,
            start_y=37.57,
            end_x=127.01,
            end_y=37.56,
        )

        self.assertEqual(route.duration_minutes, 18)
        self.assertEqual(route.distance_meters, 7200)
        self.assertEqual(route.total_fare_krw, 1400)
        self.assertEqual(route.taxi_fare_krw, 9200)
        self.assertIn("format=json", captured["url"])
        self.assertEqual(captured["headers"]["appKey"], "secret")
        self.assertEqual(
            route.geometry["coordinates"],
            [[126.97, 37.57], [127.01, 37.56]],
        )

