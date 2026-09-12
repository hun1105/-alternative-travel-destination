from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from urllib.parse import quote

from plan_b_api.api_service import PlanBApiService, _priority_fields
from plan_b_api.server import PlanBRequestHandler, create_server


class FakeService:
    @staticmethod
    def health() -> dict[str, object]:
        return {"status": "ok"}

    @staticmethod
    def priorities() -> dict[str, object]:
        return {"items": []}

    @staticmethod
    def place_search(query: dict[str, object]) -> dict[str, object]:
        return {"query": query.get("q"), "items": []}

    @staticmethod
    def validate_trip_plan(body: dict[str, object]) -> dict[str, object]:
        return {"valid": True, "plan": body}

    @staticmethod
    def recommendations(body: dict[str, object]) -> dict[str, object]:
        return {"received": body, "recommendations": []}

    @staticmethod
    def optimize_schedule(body: dict[str, object]) -> dict[str, object]:
        return {"is_improved": True, "optimized_indices": [0, 1]}



class ApiServiceTests(unittest.TestCase):
    def test_priority_numbers_are_converted_to_fields(self) -> None:
        self.assertEqual(
            _priority_fields([2, 3, 5]),
            ("route_time", "crowd_avoidance", "walking_fit"),
        )
        with self.assertRaises(ValueError):
            _priority_fields("2,4,6")

    def test_priority_metadata_contains_five_items(self) -> None:
        data = PlanBApiService.priorities()
        self.assertEqual(len(data["items"]), 5)
        self.assertEqual(data["items"][0]["label"], "날씨 적합")

    def test_http_health_and_recommendation_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                "127.0.0.1",
                0,
                str(Path(directory) / "cache.sqlite3"),
            )
            PlanBRequestHandler.service = FakeService()  # type: ignore[assignment]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(f"{base}/health", timeout=2) as response:
                    health = json.loads(response.read().decode("utf-8"))
                self.assertEqual(health["status"], "ok")

                with urlopen(
                    f"{base}/place-search?q={quote('경복궁')}", timeout=2
                ) as response:
                    places = json.loads(response.read().decode("utf-8"))
                self.assertEqual(places["query"], "경복궁")

                body = json.dumps({"priorities": [2, 3, 5]}).encode("utf-8")
                request = Request(
                    f"{base}/recommendations",
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result["received"]["priorities"], [2, 3, 5])

                plan_body = json.dumps({"title": "서울 여행"}).encode("utf-8")
                request = Request(
                    f"{base}/trip-plans/validate",
                    data=plan_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    validated = json.loads(response.read().decode("utf-8"))
                self.assertTrue(validated["valid"])

                opt_body = json.dumps({"items": []}).encode("utf-8")
                request = Request(
                    f"{base}/trip-plans/optimize-route",
                    data=opt_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    opt_res = json.loads(response.read().decode("utf-8"))
                self.assertTrue(opt_res["is_improved"])

                with self.assertRaises(HTTPError) as context:
                    urlopen(f"{base}/missing", timeout=2)
                self.assertEqual(context.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_trip_plan_create_get_and_replace_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                "127.0.0.1",
                0,
                str(Path(directory) / "cache.sqlite3"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                plan_body = json.dumps({
                    "title": "서울 역사 여행",
                    "trip_date": "2026-08-07",
                    "schedules": [{
                        "item_id": "stop-1",
                        "visit_minutes": 60,
                        "place": {
                            "provider": "tmap",
                            "place_id": "1001",
                            "name": "경복궁",
                            "longitude": 126.9767,
                            "latitude": 37.5760,
                        },
                    }],
                }).encode("utf-8")
                request = Request(
                    f"{base}/trip-plans",
                    data=plan_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    created = json.loads(response.read().decode("utf-8"))
                trip_id = created["trip_id"]
                self.assertEqual(
                    created["plan"]["schedules"][0]["place"]["name"], "경복궁"
                )

                with urlopen(f"{base}/trip-plans/{trip_id}", timeout=2) as response:
                    fetched = json.loads(response.read().decode("utf-8"))
                self.assertEqual(fetched["plan"]["title"], "서울 역사 여행")

                replace_body = json.dumps({
                    "item_id": "stop-1",
                    "place": {
                        "provider": "tmap",
                        "place_id": "2002",
                        "name": "대한민국역사박물관",
                        "longitude": 126.9770,
                        "latitude": 37.5738,
                    },
                    "visit_minutes": 45,
                }).encode("utf-8")
                request = Request(
                    f"{base}/trip-plans/{trip_id}/replace-schedule",
                    data=replace_body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    replaced = json.loads(response.read().decode("utf-8"))
                self.assertEqual(
                    replaced["plan"]["schedules"][0]["place"]["name"],
                    "대한민국역사박물관",
                )
                self.assertEqual(
                    replaced["plan"]["schedules"][0]["visit_minutes"], 45
                )

                with urlopen(f"{base}/trip-plans/{trip_id}", timeout=2) as response:
                    refetched = json.loads(response.read().decode("utf-8"))
                self.assertEqual(
                    refetched["plan"]["schedules"][0]["place"]["name"],
                    "대한민국역사박물관",
                )

                with self.assertRaises(HTTPError) as context:
                    urlopen(f"{base}/trip-plans/missing-id", timeout=2)
                self.assertEqual(context.exception.code, 404)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_http_full_trip_plan_save_with_optimistic_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = create_server(
                "127.0.0.1",
                0,
                str(Path(directory) / "cache.sqlite3"),
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            def make_plan(title: str) -> dict[str, object]:
                return {
                    "title": title,
                    "trip_date": "2026-08-07",
                    "schedules": [{
                        "item_id": "stop-1",
                        "visit_minutes": 60,
                        "place": {
                            "provider": "tmap",
                            "place_id": "1001",
                            "name": "경복궁",
                            "longitude": 126.9767,
                            "latitude": 37.5760,
                        },
                    }],
                }

            try:
                request = Request(
                    f"{base}/trip-plans",
                    data=json.dumps(make_plan("원본")).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urlopen(request, timeout=2) as response:
                    created = json.loads(response.read().decode("utf-8"))
                trip_id = created["trip_id"]
                self.assertEqual(created["version"], 1)

                put_body = json.dumps({
                    "plan": make_plan("전체 재저장"),
                    "expected_version": 1,
                }).encode("utf-8")
                request = Request(
                    f"{base}/trip-plans/{trip_id}",
                    data=put_body,
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with urlopen(request, timeout=2) as response:
                    saved = json.loads(response.read().decode("utf-8"))
                self.assertEqual(saved["version"], 2)
                self.assertEqual(saved["plan"]["title"], "전체 재저장")

                # 오래된 버전(1)으로 다시 저장을 시도하면 409로 거부되어야 한다.
                stale_body = json.dumps({
                    "plan": make_plan("뒤늦은 저장"),
                    "expected_version": 1,
                }).encode("utf-8")
                request = Request(
                    f"{base}/trip-plans/{trip_id}",
                    data=stale_body,
                    headers={"Content-Type": "application/json"},
                    method="PUT",
                )
                with self.assertRaises(HTTPError) as context:
                    urlopen(request, timeout=2)
                self.assertEqual(context.exception.code, 409)

                with urlopen(f"{base}/trip-plans/{trip_id}", timeout=2) as response:
                    refetched = json.loads(response.read().decode("utf-8"))
                self.assertEqual(refetched["plan"]["title"], "전체 재저장")
                self.assertEqual(refetched["version"], 2)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_seoul_transit_route_short_distance_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = PlanBApiService(
                str(Path(directory) / "cache.sqlite3")
            )
            # 230m 근거리 (도보권 구간)
            result = service.seoul_transit_route({
                "start_x": 126.9770,
                "start_y": 37.5796,
                "end_x": 126.9790,
                "end_y": 37.5810,
            })
            self.assertTrue(result["is_walking_fallback"])
            self.assertEqual(result["route_type"], "도보 권장 (500m 이내)")
            self.assertEqual(result["transfer_count"], 0)
            self.assertGreater(result["duration_minutes"], 0)
            self.assertEqual(len(result["legs"]), 1)
            self.assertEqual(result["legs"][0]["mode"], "도보")
            self.assertIn("도보 경로를 안내합니다", result["notice"])

    def test_place_search_lru_cache_and_empty_query(self) -> None:
        import time
        from plan_b_api.api_service import _PLACE_SEARCH_CACHE, _PLACE_SEARCH_LOCK
        service = PlanBApiService()
        
        # 빈 쿼리 처리
        empty = service.place_search({"q": "   "})
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["total_count"], 0)

        # 가짜 캐시 주입 후 캐시 히트 동작 검증
        test_key = ("테스트장소", 126.977, 37.579, 10, 1, 20)
        fake_payload = {"query": "테스트장소", "total_count": 1, "items": [{"name": "테스트장소", "place_id": "test-1"}]}
        with _PLACE_SEARCH_LOCK:
            _PLACE_SEARCH_CACHE[test_key] = (time.time(), fake_payload)
        
        cached_result = service.place_search({
            "q": "테스트장소",
            "center_x": 126.9771,
            "center_y": 37.5791,
            "count": 10,
            "page": 1,
            "radius_km": 20,
        })
        self.assertEqual(cached_result["items"][0]["place_id"], "test-1")
        self.assertEqual(cached_result["query"], "테스트장소")


if __name__ == "__main__":
    unittest.main()

