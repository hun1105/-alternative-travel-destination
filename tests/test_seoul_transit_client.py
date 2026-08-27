from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from plan_b_api.seoul_transit_client import (
    SeoulTransitClient,
    SeoulTransitConfig,
    parse_seoul_transit_response,
)


def _odsay_payload(**info_overrides: object) -> dict[str, object]:
    info = {
        "totalTime": 24,
        "totalDistance": 6500,
        "totalWalkTime": 7,
        "busTransitCount": 0,
        "subwayTransitCount": 1,
    }
    info.update(info_overrides)
    return {
        "result": {
            "path": [{
                "pathType": 1,
                "info": info,
                "subPath": [
                    {
                        "trafficType": 3,
                        "distance": 300,
                        "sectionTime": 4,
                    },
                    {
                        "trafficType": 1,
                        "distance": 2000,
                        "sectionTime": 9,
                        "lane": {"name": "수도권 2호선", "subwaycode": 2},
                        "passStopList": {"stations": [
                            {"stationName": "당산", "x": 126.9027, "y": 37.5349},
                            {"stationName": "합정", "x": 126.9145, "y": 37.5499},
                        ]},
                    },
                ],
            }],
        },
    }


class SeoulTransitClientTests(unittest.TestCase):
    def test_parses_json_route(self) -> None:
        route = parse_seoul_transit_response(
            json.dumps(_odsay_payload()).encode()
        )
        self.assertEqual(route.duration_minutes, 24)
        self.assertEqual(route.walking_minutes, 7)
        self.assertEqual(route.transfer_count, 1)
        self.assertEqual(route.route_type, "수도권 2호선")
        self.assertEqual(
            route.geometry,
            {
                "type": "LineString",
                "coordinates": [[126.9027, 37.5349], [126.9145, 37.5499]],
            },
        )

    def test_builds_legs_with_transfer_and_exit_numbers(self) -> None:
        payload = {
            "result": {
                "path": [{
                    "info": {
                        "totalTime": 35,
                        "totalDistance": 8000,
                        "totalWalkTime": 10,
                        "busTransitCount": 1,
                        "subwayTransitCount": 1,
                    },
                    "subPath": [
                        {
                            "trafficType": 3,
                            "distance": 200,
                            "sectionTime": 3,
                        },
                        {
                            "trafficType": 2,
                            "distance": 3000,
                            "sectionTime": 12,
                            "stationCount": 6,
                            "lane": {"busNo": "272"},
                            "startName": "종로구청",
                            "endName": "광화문역",
                        },
                        {
                            "trafficType": 1,
                            "distance": 4000,
                            "sectionTime": 15,
                            "stationCount": 4,
                            "lane": {"name": "수도권 2호선"},
                            "startName": "시청",
                            "endName": "합정",
                            "startExitNo": "3",
                            "endExitNo": "7",
                        },
                        {
                            "trafficType": 3,
                            "distance": 300,
                            "sectionTime": 5,
                        },
                    ],
                }],
            },
        }
        route = parse_seoul_transit_response(json.dumps(payload).encode())

        self.assertEqual(len(route.legs), 4)
        walk1, bus, subway, walk2 = route.legs

        self.assertEqual(walk1.mode, "도보")
        self.assertIn("200m", walk1.instruction)

        self.assertEqual(bus.mode, "버스")
        self.assertEqual(bus.lane_name, "272")
        self.assertIn("종로구청", bus.instruction)
        self.assertIn("광화문역", bus.instruction)
        self.assertIn("6개 정류장", bus.instruction)

        self.assertEqual(subway.mode, "지하철")
        self.assertEqual(subway.start_entrance_no, "3")
        self.assertEqual(subway.end_exit_no, "7")
        self.assertIn("3번 입구", subway.instruction)
        self.assertIn("수도권 2호선", subway.instruction)
        self.assertIn("7번 출구", subway.instruction)

        self.assertEqual(walk2.mode, "도보")

    def test_route_fills_walk_endpoints_and_stitches_geometry(self) -> None:
        payload = {
            "result": {
                "path": [{
                    "info": {
                        "totalTime": 30, "totalDistance": 5000,
                        "totalWalkTime": 8, "busTransitCount": 0,
                        "subwayTransitCount": 1,
                    },
                    "subPath": [
                        {"trafficType": 3, "distance": 200, "sectionTime": 3},
                        {
                            "trafficType": 1,
                            "distance": 2000,
                            "sectionTime": 9,
                            "lane": {"name": "수도권 2호선"},
                            "startX": 126.9027, "startY": 37.5349,
                            "endX": 126.9145, "endY": 37.5499,
                            "passStopList": {"stations": [
                                {"stationName": "당산", "x": 126.9027, "y": 37.5349},
                                {"stationName": "합정", "x": 126.9145, "y": 37.5499},
                            ]},
                        },
                        {"trafficType": 3, "distance": 150, "sectionTime": 2},
                    ],
                }],
            },
        }
        client = SeoulTransitClient(
            SeoulTransitConfig("test-api-key", max_retries=0),
            transport=lambda url, timeout: (200, json.dumps(payload).encode()),
        )
        route = client.route(
            start_x=126.90, start_y=37.53,
            end_x=126.92, end_y=37.56,
        )
        walk1, subway, walk2 = route.legs

        self.assertEqual((walk1.start_longitude, walk1.start_latitude), (126.90, 37.53))
        self.assertEqual((walk1.end_longitude, walk1.end_latitude), (126.9027, 37.5349))
        self.assertEqual((walk2.start_longitude, walk2.start_latitude), (126.9145, 37.5499))
        self.assertEqual((walk2.end_longitude, walk2.end_latitude), (126.92, 37.56))

        coords = route.geometry["coordinates"]
        self.assertEqual(coords[0], [126.90, 37.53])
        self.assertEqual(coords[-1], [126.92, 37.56])
        self.assertIn([126.9027, 37.5349], coords)
        self.assertIn([126.9145, 37.5499], coords)

    def test_raises_on_error_response(self) -> None:
        payload = {"error": {"code": -98, "msg": "출, 도착지가 700m이내입니다."}}
        with self.assertRaises(Exception):
            parse_seoul_transit_response(json.dumps(payload).encode())

    def test_raises_on_list_shaped_auth_error(self) -> None:
        # 실제 ODsay 인증 실패 응답은 error가 배열 형태로 내려온다.
        payload = {
            "error": [{
                "code": "500",
                "message": "[ApiKeyAuthFailed] ApiKey authentication failed.",
            }]
        }
        with self.assertRaisesRegex(Exception, "ApiKeyAuthFailed"):
            parse_seoul_transit_response(json.dumps(payload).encode())

    def test_builds_coordinate_request(self) -> None:
        captured: dict[str, object] = {}

        def transport(url: str, timeout: float):
            captured["url"] = url
            captured["timeout"] = timeout
            return 200, json.dumps(_odsay_payload(totalTime=20)).encode()

        client = SeoulTransitClient(
            SeoulTransitConfig("test-api-key", max_retries=0),
            transport=transport,
        )
        route = client.route(
            start_x=126.97, start_y=37.57,
            end_x=127.01, end_y=37.59,
        )
        query = parse_qs(urlparse(str(captured["url"])).query)
        self.assertEqual(route.duration_minutes, 20)
        self.assertEqual(query["apiKey"], ["test-api-key"])
        self.assertEqual(query["SX"], ["126.9700000"])


if __name__ == "__main__":
    unittest.main()
