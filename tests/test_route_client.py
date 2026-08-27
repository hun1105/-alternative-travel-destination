from __future__ import annotations

import json
import unittest

from plan_b_api import (
    TMapApiError,
    TMapConfig,
    TMapPedestrianClient,
)


class RouteClientTests(unittest.TestCase):
    def test_parses_pedestrian_route_summary(self) -> None:
        captured: dict[str, object] = {}

        def transport(
            url: str,
            headers: dict[str, str],
            body: bytes,
            timeout: float,
        ) -> tuple[int, bytes]:
            captured.update({
                "url": url,
                "headers": headers,
                "body": json.loads(body.decode("utf-8")),
                "timeout": timeout,
            })
            response = {
                "features": [
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[126.97, 37.57], [126.975, 37.575]],
                        },
                    },
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[126.975, 37.575], [126.98, 37.58]],
                        },
                    },
                    {
                        "properties": {
                            "totalDistance": 842,
                            "totalTime": 720,
                        },
                    },
                ],
            }
            return 200, json.dumps(response).encode("utf-8")

        client = TMapPedestrianClient(
            TMapConfig("secret"),
            transport=transport,
        )
        route = client.pedestrian_route(
            start_x=126.97,
            start_y=37.57,
            end_x=126.98,
            end_y=37.58,
            end_name="박물관",
        )

        self.assertEqual(route.distance_meters, 842)
        self.assertEqual(route.duration_minutes, 12)
        self.assertEqual(captured["headers"]["appKey"], "secret")
        self.assertEqual(captured["body"]["reqCoordType"], "WGS84GEO")
        self.assertEqual(route.geometry["type"], "LineString")
        self.assertEqual(
            route.geometry["coordinates"],
            [[126.97, 37.57], [126.975, 37.575], [126.98, 37.58]],
        )

    def test_parses_turn_by_turn_steps(self) -> None:
        def transport(url, headers, body, timeout):
            response = {
                "features": [
                    {
                        "geometry": {"type": "Point", "coordinates": [126.97, 37.57]},
                        "properties": {
                            "turnType": 200,
                            "description": "보행자도로를 따라 51m 이동",
                        },
                    },
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[126.97, 37.57], [126.971, 37.571]],
                        },
                        "properties": {"distance": 51},
                    },
                    {
                        "geometry": {"type": "Point", "coordinates": [126.971, 37.571]},
                        "properties": {
                            "turnType": 12,
                            "description": "좌회전 후 보행자도로를 따라 34m 이동",
                        },
                    },
                    {
                        "geometry": {
                            "type": "LineString",
                            "coordinates": [[126.971, 37.571], [126.972, 37.572]],
                        },
                        "properties": {"distance": 34},
                    },
                    {
                        "geometry": {"type": "Point", "coordinates": [126.972, 37.572]},
                        "properties": {"turnType": 201, "description": "도착"},
                    },
                    {
                        "properties": {"totalDistance": 85, "totalTime": 60},
                    },
                ],
            }
            return 200, json.dumps(response, ensure_ascii=False).encode("utf-8")

        client = TMapPedestrianClient(
            TMapConfig("secret"),
            transport=transport,
        )
        route = client.pedestrian_route(
            start_x=126.97, start_y=37.57, end_x=126.972, end_y=37.572,
        )

        self.assertEqual(len(route.steps), 3)
        self.assertEqual(route.steps[0].instruction, "보행자도로를 따라 51m 이동")
        self.assertEqual(route.steps[0].turn_type, 200)
        self.assertEqual(route.steps[0].distance_meters, 51)
        self.assertEqual(route.steps[1].instruction, "좌회전 후 보행자도로를 따라 34m 이동")
        self.assertEqual(route.steps[1].distance_meters, 34)
        self.assertEqual(route.steps[2].instruction, "도착")
        self.assertIsNone(route.steps[2].distance_meters)

    def test_reports_http_error(self) -> None:
        client = TMapPedestrianClient(
            TMapConfig("secret", max_retries=0),
            transport=lambda *_: (401, b"Unauthorized"),
        )
        with self.assertRaisesRegex(TMapApiError, "401"):
            client.pedestrian_route(
                start_x=126.97,
                start_y=37.57,
                end_x=126.98,
                end_y=37.58,
            )


if __name__ == "__main__":
    unittest.main()
