from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

from plan_b_api.api_cache import SQLiteTTLCache
from plan_b_api.optimized_recommender import (
    ApiOptimizationStats,
    _adjust_snapped_route,
    cached_seoul_transit_route_with_snap,
    enrich_seoul_transit_walk_geometry,
    find_nearest_transit_stops,
)
from plan_b_api.place_search_client import (
    PlaceSearchResponse,
    PlaceSearchResult,
    TMapPlaceSearchClient,
)
from plan_b_api.route_client import (
    TMapPedestrianClient,
    WalkingRoute,
    WalkingStep,
)
from plan_b_api.seoul_transit_client import (
    SeoulTransitApiError,
    SeoulTransitClient,
    SeoulTransitConfig,
    SeoulTransitLeg,
    SeoulTransitRoute,
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
            payload = {
                "msgHeader": {"headerCd": "0", "headerMsg": ""},
                "msgBody": {
                    "itemList": [{
                        "time": "20",
                        "distance": "5000",
                        "pathList": [{
                            "routeNm": "273",
                            "fname": "광화문",
                            "fx": "126.97",
                            "fy": "37.57",
                            "tname": "혜화",
                            "tx": "127.01",
                            "ty": "37.59",
                            "railLinkList": None,
                        }],
                    }],
                },
            }
            return 200, json.dumps(payload).encode()

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
        self.assertEqual(query["serviceKey"], ["test-api-key"])
        self.assertEqual(query["startX"], ["126.9700000"])
        self.assertEqual(query["resultType"], ["json"])

    def test_builds_odsay_coordinate_request(self) -> None:
        captured: dict[str, object] = {}

        def transport(url: str, timeout: float):
            captured["url"] = url
            captured["timeout"] = timeout
            return 200, json.dumps(_odsay_payload(totalTime=20)).encode()

        client = SeoulTransitClient(
            SeoulTransitConfig(
                "test-api-key",
                max_retries=0,
                base_url="https://api.odsay.com/v1/api/searchPubTransPathT",
            ),
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

    def test_parses_public_data_portal_bus_and_subway(self) -> None:
        payload = {
            "msgHeader": {"headerCd": "0", "headerMsg": "정상"},
            "msgBody": {
                "itemList": [{
                    "time": "35",
                    "distance": "8500",
                    "pathList": [
                        {
                            "routeNm": "9호선",
                            "fname": "여의도역",
                            "fx": "126.9242",
                            "fy": "37.5215",
                            "tname": "석촌",
                            "tx": "127.1067",
                            "ty": "37.5049",
                            "railLinkList": [{"railLinkId": "1"}, {"railLinkId": "2"}],
                        },
                        {
                            "routeNm": "320",
                            "fname": "석촌호수",
                            "fx": "127.1058",
                            "fy": "37.5067",
                            "tname": "잠실역",
                            "tx": "127.1005",
                            "ty": "37.5126",
                            "railLinkList": None,
                        },
                    ],
                }],
            },
        }
        route = parse_seoul_transit_response(json.dumps(payload).encode())
        self.assertEqual(route.duration_minutes, 35)
        self.assertEqual(route.distance_meters, 8500)
        self.assertEqual(route.transfer_count, 1)
        self.assertEqual(route.route_type, "9호선 → 320")
        self.assertEqual(len(route.legs), 5)  # walk1, subway, transfer_walk, bus, walk2
        self.assertEqual(route.legs[1].mode, "지하철")
        self.assertEqual(route.legs[1].station_count, 2)
        self.assertEqual(route.legs[2].mode, "도보")
        self.assertIn("환승 이동", route.legs[2].instruction)
        self.assertEqual(route.legs[3].mode, "버스")

    def test_selects_fastest_route_when_multiple(self) -> None:
        payload = {
            "msgHeader": {"headerCd": "0", "headerMsg": "정상"},
            "msgBody": {
                "itemList": [
                    {
                        "time": "40",
                        "distance": "9000",
                        "pathList": [{"routeNm": "100", "fname": "A", "fx": "126.9", "fy": "37.5", "tname": "B", "tx": "127.0", "ty": "37.6"}],
                    },
                    {
                        "time": "22",  # fastest!
                        "distance": "7000",
                        "pathList": [{"routeNm": "200", "fname": "A", "fx": "126.9", "fy": "37.5", "tname": "B", "tx": "127.0", "ty": "37.6"}],
                    },
                    {
                        "time": "30",
                        "distance": "8000",
                        "pathList": [{"routeNm": "300", "fname": "A", "fx": "126.9", "fy": "37.5", "tname": "B", "tx": "127.0", "ty": "37.6"}],
                    },
                ],
            },
        }
        route = parse_seoul_transit_response(json.dumps(payload).encode())
        self.assertEqual(route.duration_minutes, 22)
        self.assertEqual(route.route_type, "200")

    def test_selects_least_transfers_route_when_requested(self) -> None:
        payload = {
            "msgHeader": {"headerCd": "0", "headerMsg": "정상"},
            "msgBody": {
                "itemList": [
                    {
                        "time": "20",  # faster, but 1 transfer (2 paths)
                        "distance": "5000",
                        "pathList": [
                            {"routeNm": "100", "fname": "A", "fx": "126.9", "fy": "37.5", "tname": "B", "tx": "127.0", "ty": "37.6"},
                            {"routeNm": "200", "fname": "B", "fx": "127.0", "fy": "37.6", "tname": "C", "tx": "127.1", "ty": "37.7"},
                        ],
                    },
                    {
                        "time": "26",  # slower, but direct (1 path)
                        "distance": "6000",
                        "pathList": [
                            {"routeNm": "DirectBus", "fname": "A", "fx": "126.9", "fy": "37.5", "tname": "C", "tx": "127.1", "ty": "37.7"},
                        ],
                    },
                ],
            },
        }
        body = json.dumps(payload).encode()
        fastest_route = parse_seoul_transit_response(body, routing_preference="fastest")
        self.assertEqual(fastest_route.duration_minutes, 20)
        self.assertEqual(fastest_route.route_type, "100 → 200")

        direct_route = parse_seoul_transit_response(body, routing_preference="least_transfers")
        self.assertEqual(direct_route.duration_minutes, 26)
        self.assertEqual(direct_route.route_type, "DirectBus")

    def test_raises_on_public_data_portal_error(self) -> None:
        payload = {
            "msgHeader": {"headerCd": "1", "headerMsg": "XML Parsing Error"},
            "msgBody": {"itemList": None},
        }
        with self.assertRaisesRegex(Exception, "XML Parsing Error"):
            parse_seoul_transit_response(json.dumps(payload).encode())


class TransitSnapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.cache = SQLiteTTLCache(self.tmp.name)
        self.stats = ApiOptimizationStats()

    def tearDown(self) -> None:
        if os.path.exists(self.tmp.name):
            os.remove(self.tmp.name)

    def test_find_nearest_transit_stops(self) -> None:
        place_client = MagicMock(spec=TMapPlaceSearchClient)
        place_client.search.return_value = PlaceSearchResponse(
            query="정류장",
            total_count=3,
            items=(
                PlaceSearchResult(
                    place_id="1",
                    name="영화사입구[정류장]",
                    longitude=127.0941,
                    latitude=37.5541,
                    category="버스정류장",
                ),
                PlaceSearchResult(
                    place_id="2",
                    name="아차산역 5호선",
                    longitude=127.0850,
                    latitude=37.5450,
                    category="지하철역",
                ),
                PlaceSearchResult(
                    place_id="3",
                    name="일반식당",
                    longitude=127.0900,
                    latitude=37.5500,
                    category="음식점",
                ),
            ),
        )
        stops = find_nearest_transit_stops(
            place_client, 127.0987, 37.5510, radius_km=1, count=5
        )
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0].name, "영화사입구[정류장]")
        self.assertEqual(stops[1].name, "아차산역 5호선")

    def test_adjust_snapped_route_departure(self) -> None:
        base_route = SeoulTransitRoute(
            duration_minutes=15.0,
            legs=(
                SeoulTransitLeg(
                    mode="도보",
                    instruction="정류장 이동",
                    start_longitude=127.0941,
                    start_latitude=37.5541,
                    end_longitude=127.0940,
                    end_latitude=37.5540,
                ),
                SeoulTransitLeg(
                    mode="버스",
                    instruction="광진03 승차",
                    lane_name="광진03",
                    start_longitude=127.0940,
                    start_latitude=37.5540,
                    end_longitude=127.0855,
                    end_latitude=37.5455,
                ),
            ),
        )
        adjusted = _adjust_snapped_route(
            base_route,
            original_start_x=127.0987,
            original_start_y=37.5510,
            original_end_x=127.0850,
            original_end_y=37.5450,
            snapped_start=True,
            snapped_end=False,
            start_name="아차산생태공원",
            end_name="아차산역",
        )
        self.assertEqual(adjusted.legs[0].mode, "도보")
        self.assertEqual(adjusted.legs[0].start_longitude, 127.0987)
        self.assertEqual(adjusted.legs[0].start_latitude, 37.5510)

    def test_adjust_snapped_route_arrival(self) -> None:
        base_route = SeoulTransitRoute(
            duration_minutes=15.0,
            legs=(
                SeoulTransitLeg(
                    mode="버스",
                    instruction="광진03 승차",
                    lane_name="광진03",
                    start_longitude=127.0855,
                    start_latitude=37.5455,
                    end_longitude=127.0940,
                    end_latitude=37.5540,
                    end_name="영화사입구",
                ),
            ),
        )
        adjusted = _adjust_snapped_route(
            base_route,
            original_start_x=127.0850,
            original_start_y=37.5450,
            original_end_x=127.0987,
            original_end_y=37.5510,
            snapped_start=False,
            snapped_end=True,
            start_name="아차산역",
            end_name="아차산생태공원",
        )
        self.assertEqual(len(adjusted.legs), 2)
        self.assertEqual(adjusted.legs[1].mode, "도보")
        self.assertEqual(adjusted.legs[1].end_longitude, 127.0987)
        self.assertEqual(adjusted.legs[1].end_latitude, 37.5510)

    def test_cached_seoul_transit_route_with_snap_fallback(self) -> None:
        transit_client = MagicMock(spec=SeoulTransitClient)

        def transit_route_side_effect(**kwargs: object) -> SeoulTransitRoute:
            start_x = kwargs.get("start_x")
            if start_x == 127.0987:
                raise SeoulTransitApiError("XML Parsing Error")
            return SeoulTransitRoute(
                duration_minutes=10.0,
                legs=(
                    SeoulTransitLeg(
                        mode="도보",
                        instruction="도보",
                        start_longitude=float(start_x),
                        start_latitude=float(kwargs["start_y"]),
                        end_longitude=127.0940,
                        end_latitude=37.5540,
                    ),
                    SeoulTransitLeg(
                        mode="버스",
                        instruction="광진03",
                        lane_name="광진03",
                        start_longitude=127.0940,
                        start_latitude=37.5540,
                        end_longitude=127.0850,
                        end_latitude=37.5450,
                    ),
                ),
            )

        transit_client.route.side_effect = transit_route_side_effect

        place_client = MagicMock(spec=TMapPlaceSearchClient)
        place_client.search.return_value = PlaceSearchResponse(
            query="정류장",
            total_count=1,
            items=(
                PlaceSearchResult(
                    place_id="1",
                    name="영화사입구[정류장]",
                    longitude=127.0941,
                    latitude=37.5541,
                    category="버스정류장",
                ),
            ),
        )

        route, source = cached_seoul_transit_route_with_snap(
            transit_client,
            place_client,
            self.cache,
            self.stats,
            start_x=127.0987,
            start_y=37.5510,
            end_x=127.0850,
            end_y=37.5450,
            start_name="아차산생태공원",
            end_name="아차산역",
        )
        self.assertIn("영화사입구", source)
        self.assertEqual(route.legs[0].start_longitude, 127.0987)
        self.assertEqual(route.legs[0].start_latitude, 37.5510)

    def test_enrich_seoul_transit_walk_geometry(self) -> None:
        walking_client = MagicMock(spec=TMapPedestrianClient)
        walking_client.pedestrian_route.return_value = WalkingRoute(
            distance_meters=350.0,
            duration_seconds=300,
            geometry={
                "type": "LineString",
                "coordinates": [
                    [127.0987, 37.5510],
                    [127.0950, 37.5520],
                    [127.0941, 37.5541],
                ],
            },
            steps=(WalkingStep("도보 이동", 0, 350.0, 127.0987, 37.5510),),
        )
        base_route = SeoulTransitRoute(
            duration_minutes=15.0,
            walking_minutes=1.0,
            legs=(
                SeoulTransitLeg(
                    mode="도보",
                    instruction="도보 이동",
                    start_longitude=127.0987,
                    start_latitude=37.5510,
                    end_longitude=127.0941,
                    end_latitude=37.5541,
                ),
                SeoulTransitLeg(
                    mode="버스",
                    instruction="광진03 승차",
                    lane_name="광진03",
                    start_longitude=127.0941,
                    start_latitude=37.5541,
                    end_longitude=127.0850,
                    end_latitude=37.5450,
                    geometry={
                        "type": "LineString",
                        "coordinates": [[127.0941, 37.5541], [127.0850, 37.5450]],
                    },
                ),
            ),
        )
        enriched = enrich_seoul_transit_walk_geometry(
            base_route,
            walking_client,
            self.cache,
            self.stats,
        )
        self.assertEqual(len(enriched.legs[0].geometry["coordinates"]), 3)
        self.assertEqual(enriched.legs[0].distance_meters, 350.0)
        self.assertEqual(enriched.legs[0].duration_minutes, 5.0)
        self.assertEqual(enriched.walking_minutes, 5.0)
        self.assertEqual(enriched.duration_minutes, 19.0)
        self.assertEqual(len(enriched.geometry["coordinates"]), 4)


if __name__ == "__main__":
    unittest.main()
