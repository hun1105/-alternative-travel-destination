from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from plan_b_api import KTOApiError, KTOClient, KTOConfig


def make_client(handler):
    config = KTOConfig(
        service_key="abc%2B123%3D%3D",
        max_retries=0,
    )
    return KTOClient(config, transport=handler)


class KTOClientTests(unittest.TestCase):
    def test_keyword_search_normalizes_single_item_and_service_key(self) -> None:
        def handler(url: str, _: float) -> tuple[int, bytes]:
            parsed = urlsplit(url)
            params = parse_qs(parsed.query)
            self.assertTrue(parsed.path.endswith("/searchKeyword2"))
            self.assertEqual(params["keyword"], ["경복궁"])
            self.assertEqual(params["serviceKey"], ["abc+123=="])
            payload = {
                "response": {
                    "header": {"resultCode": "0000", "resultMsg": "OK"},
                    "body": {
                        "items": {"item": {"contentid": "126508"}},
                        "numOfRows": 10,
                        "pageNo": 1,
                        "totalCount": 1,
                    },
                }
            }
            return 200, json.dumps(payload).encode()

        with make_client(handler) as client:
            result = client.keyword_search("경복궁", num_of_rows=10)

        self.assertEqual(result.total_count, 1)
        self.assertEqual(result.items, [{"contentid": "126508"}])

    def test_empty_items_becomes_empty_list(self) -> None:
        def handler(_: str, __: float) -> tuple[int, bytes]:
            payload = {
                "response": {
                    "header": {"resultCode": "0000", "resultMsg": "OK"},
                    "body": {
                        "items": "",
                        "numOfRows": 10,
                        "pageNo": 1,
                        "totalCount": 0,
                    },
                }
            }
            return 200, json.dumps(payload).encode()

        with make_client(handler) as client:
            result = client.area_codes()

        self.assertEqual(result.items, [])
        self.assertEqual(result.total_count, 0)

    def test_api_error_raises_structured_exception(self) -> None:
        def handler(_: str, __: float) -> tuple[int, bytes]:
            payload = {
                "response": {
                    "header": {
                        "resultCode": "30",
                        "resultMsg": "SERVICE KEY IS NOT REGISTERED ERROR.",
                    },
                    "body": {},
                }
            }
            return 200, json.dumps(payload).encode()

        with make_client(handler) as client:
            with self.assertRaises(KTOApiError) as error:
                client.area_codes()

        self.assertEqual(error.exception.result_code, "30")
        self.assertEqual(error.exception.operation, "areaCode2")

    def test_nearby_radius_validation(self) -> None:
        def unused(_: str, __: float) -> tuple[int, bytes]:
            return 200, b"{}"

        with make_client(unused) as client:
            with self.assertRaises(ValueError):
                client.location_based_list(
                    map_x=126.9,
                    map_y=37.5,
                    radius=20_001,
                )

    def test_http_error_exposes_message_but_redacts_service_key(self) -> None:
        def handler(_: str, __: float) -> tuple[int, bytes]:
            return (
                500,
                b"<OpenAPI_ServiceResponse><cmmMsgHeader>"
                b"<returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR"
                b" abc+123==</returnAuthMsg></cmmMsgHeader>"
                b"</OpenAPI_ServiceResponse>",
            )

        with make_client(handler) as client:
            with self.assertRaises(KTOApiError) as error:
                client.area_codes()

        message = str(error.exception)
        self.assertIn("SERVICE_KEY_IS_NOT_REGISTERED_ERROR", message)
        self.assertNotIn("abc+123==", message)

    def test_detail_common_uses_only_current_parameters(self) -> None:
        banned = {
            "defaultYN",
            "firstImageYN",
            "areacodeYN",
            "catcodeYN",
            "addrinfoYN",
            "mapinfoYN",
            "overviewYN",
        }

        def handler(url: str, _: float) -> tuple[int, bytes]:
            parsed = urlsplit(url)
            params = parse_qs(parsed.query)
            self.assertTrue(parsed.path.endswith("/detailCommon2"))
            self.assertEqual(params["contentId"], ["126508"])
            self.assertTrue(banned.isdisjoint(params))
            payload = {
                "response": {
                    "header": {"resultCode": "0000", "resultMsg": "OK"},
                    "body": {
                        "items": {"item": {"contentid": "126508"}},
                        "numOfRows": 10,
                        "pageNo": 1,
                        "totalCount": 1,
                    },
                }
            }
            return 200, json.dumps(payload).encode()

        with make_client(handler) as client:
            result = client.detail_common("126508")

        self.assertEqual(result.items[0]["contentid"], "126508")

    def test_top_level_api_error_is_reported(self) -> None:
        def handler(_: str, __: float) -> tuple[int, bytes]:
            payload = {
                "responseTime": "2026-07-28T12:00:00",
                "resultCode": "10",
                "resultMsg": "INVALID_REQUEST_PARAMETER_ERROR(addrinfoYN)",
            }
            return 200, json.dumps(payload).encode()

        with make_client(handler) as client:
            with self.assertRaises(KTOApiError) as error:
                client.detail_common("126508")

        self.assertEqual(error.exception.result_code, "10")
        self.assertIn("addrinfoYN", str(error.exception))


if __name__ == "__main__":
    unittest.main()
