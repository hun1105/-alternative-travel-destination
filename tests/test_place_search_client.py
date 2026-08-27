from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlparse

from plan_b_api.place_search_client import (
    TMapPlaceSearchClient,
    TMapPlaceSearchConfig,
)


class PlaceSearchClientTests(unittest.TestCase):
    def test_search_returns_map_selection_payload(self) -> None:
        captured: dict[str, object] = {}

        def transport(url, headers, timeout):
            captured.update({"url": url, "headers": headers, "timeout": timeout})
            response = {
                "searchPoiInfo": {
                    "totalCount": "1",
                    "pois": {"poi": [{
                        "id": "1001",
                        "name": "경복궁",
                        "frontLon": "126.9767",
                        "frontLat": "37.5760",
                        "upperAddrName": "서울",
                        "middleAddrName": "종로구",
                        "roadName": "사직로",
                        "firstBuildNo": "161",
                        "lowerBizName": "고궁",
                        "telNo": "02-0000-0000",
                    }]},
                }
            }
            return 200, json.dumps(response).encode("utf-8")

        result = TMapPlaceSearchClient(
            TMapPlaceSearchConfig("secret"), transport=transport
        ).search("경복궁", center_x=126.97, center_y=37.57)

        self.assertEqual(result.items[0].name, "경복궁")
        self.assertEqual(result.items[0].selection_payload()["provider"], "tmap")
        self.assertEqual(result.items[0].longitude, 126.9767)
        query = parse_qs(urlparse(str(captured["url"])).query)
        self.assertEqual(query["searchKeyword"], ["경복궁"])
        self.assertEqual(captured["headers"]["appKey"], "secret")

    def test_category_strips_stray_backslash_before_slash(self) -> None:
        def transport(url, headers, timeout):
            response = {
                "searchPoiInfo": {
                    "totalCount": "1",
                    "pois": {"poi": [{
                        "id": "1002",
                        "name": "국립중앙박물관",
                        "frontLon": "126.9803",
                        "frontLat": "37.5240",
                        "lowerBizName": "박물관\\/기념관",
                    }]},
                }
            }
            return 200, json.dumps(response).encode("utf-8")

        result = TMapPlaceSearchClient(
            TMapPlaceSearchConfig("secret"), transport=transport
        ).search("국립중앙박물관", center_x=126.98, center_y=37.52)

        self.assertEqual(result.items[0].category, "박물관/기념관")

    def test_requires_both_center_coordinates(self) -> None:
        client = TMapPlaceSearchClient(
            TMapPlaceSearchConfig("secret"),
            transport=lambda *_: (200, b"{}"),
        )
        with self.assertRaisesRegex(ValueError, "함께"):
            client.search("경복궁", center_x=126.97)
