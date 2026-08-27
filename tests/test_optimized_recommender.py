from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from plan_b_api import (
    ApiOptimizationStats,
    CarRoute,
    CrowdPlace,
    CrowdSnapshot,
    SeoulCrowdSnapshot,
    SeoulTransitLeg,
    SeoulTransitRoute,
    KTOResponse,
    NextScheduleConstraint,
    SQLiteTTLCache,
    TripContext,
    UserPriorities,
    WalkingRoute,
    WalkingStep,
    enrich_seoul_transit_walk_geometry,
    recommend_nearby_optimized,
)


def detail_bundle(
    content_id: str,
    type_id: str,
    title: str,
) -> dict[str, object]:
    intro: dict[str, str] = {
        "restdate": "매주 화요일",
        "usetime": "09:00~18:00",
        "parking": "가능",
    }
    if type_id == "15":
        intro.update({
            "eventstartdate": "20251001",
            "eventenddate": "20251031",
        })
    return {
        "content_id": content_id,
        "content_type_id": type_id,
        "common": [{
            "title": title,
            "mapx": "126.9767",
            "mapy": "37.5760",
            "firstimage": "https://example.com/image.jpg",
        }],
        "intro": [intro],
        "info": [
            {"infoname": "입장료", "infotext": "대인 3,000원"},
            {"infoname": "화장실", "infotext": "있음"},
        ],
    }


class FakeOptimizedClient:
    def __init__(self) -> None:
        self.list_calls = 0
        self.detail_calls = 0

    def location_based_list(self, **_: object) -> KTOResponse:
        self.list_calls += 1
        items = [
            {"contentid": "0", "contenttypeid": "15", "title": "종료 행사", "dist": "10"},
            {"contentid": "1", "contenttypeid": "14", "title": "박물관 1", "dist": "100"},
            {"contentid": "1", "contenttypeid": "14", "title": "박물관 중복", "dist": "100"},
            {"contentid": "2", "contenttypeid": "14", "title": "박물관 2", "dist": "200"},
            {"contentid": "3", "contenttypeid": "14", "title": "박물관 3", "dist": "300"},
            {"contentid": "4", "contenttypeid": "14", "title": "조회 불필요", "dist": "400"},
        ]
        return KTOResponse("locationBasedList2", items, 6, 1, 20, {})

    def detail_bundle(
        self,
        content_id: str,
        content_type_id: str,
        *,
        include_images: bool,
    ) -> dict[str, object]:
        self.detail_calls += 1
        title = "종료 행사" if content_id == "0" else f"박물관 {content_id}"
        return detail_bundle(content_id, content_type_id, title)


class FakeRouteClient:
    def __init__(self) -> None:
        self.calls = 0

    def pedestrian_route(self, **_: object) -> WalkingRoute:
        self.calls += 1
        return WalkingRoute(
            distance_meters=500,
            duration_seconds=420,
            geometry={
                "type": "LineString",
                "coordinates": [[126.97, 37.57], [126.975, 37.573]],
            },
        )


class FakeLongWalkingRouteClient:
    def pedestrian_route(self, **_: object) -> WalkingRoute:
        return WalkingRoute(distance_meters=1600, duration_seconds=1200)


class FakeExactlyFifteenMinuteWalkingRouteClient:
    def pedestrian_route(self, **_: object) -> WalkingRoute:
        return WalkingRoute(distance_meters=1000, duration_seconds=900)


class FakeSeoulTransitClient:
    def __init__(self) -> None:
        self.calls = 0

    def route(self, **_: object) -> SeoulTransitRoute:
        self.calls += 1
        return SeoulTransitRoute(
            duration_minutes=25,
            distance_meters=6000,
            walking_minutes=6,
            walking_distance_meters=420,
            transfer_count=1,
        )


class FakeCarRouteClient:
    def __init__(self) -> None:
        self.calls = 0

    def car_route(self, **_: object) -> CarRoute:
        self.calls += 1
        return CarRoute(distance_meters=6500, duration_seconds=1080)


class FakeCrowdClient:
    def __init__(self) -> None:
        self.place_calls = 0
        self.realtime_calls = 0

    def supported_places(self) -> tuple[CrowdPlace, ...]:
        self.place_calls += 1
        return tuple(
            CrowdPlace(str(index), f"박물관 {index}")
            for index in range(1, 4)
        )

    def realtime(self, poi_id: str) -> CrowdSnapshot:
        self.realtime_calls += 1
        return CrowdSnapshot(
            poi_id=poi_id,
            poi_name=f"박물관 {poi_id}",
            congestion=0.03,
            congestion_level=int(poi_id),
            measured_at="20260803150000",
        )


class FakeSeoulCrowdClient:
    def __init__(self) -> None:
        self.calls = 0

    def crowd(self, area_name_or_code: str, **_: object) -> SeoulCrowdSnapshot:
        self.calls += 1
        return SeoulCrowdSnapshot(
            area_code=area_name_or_code,
            area_name="경복궁",
            congestion_label="여유",
            normalized_level=0.0,
            population_min=1000,
            population_max=1200,
            measured_at="2026-08-04 14:00",
        )

class OptimizedRecommenderTests(unittest.TestCase):
    def test_car_recovers_candidate_over_walking_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            car = FakeCarRouteClient()
            result = recommend_nearby_optimized(
                FakeOptimizedClient(),  # type: ignore[arg-type]
                SQLiteTTLCache(Path(directory) / "cache.sqlite3"),
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=1,
                max_detail_calls=5,
                trip=TripContext(arrival_time=datetime(2026, 8, 3, 14)),
                priorities=UserPriorities.from_order("route_time"),
                route_client=FakeLongWalkingRouteClient(),  # type: ignore[arg-type]
                car_route_client=car,  # type: ignore[arg-type]
            )

            candidate = result.recommendations[0]
            self.assertEqual(candidate.facts.transport_mode, "car")
            self.assertEqual(candidate.facts.route_minutes, 18)
            self.assertEqual(result.stats.car_route_api_calls, 1)
            self.assertEqual(car.calls, 1)

    def test_exactly_fifteen_minute_walk_falls_back_to_car(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            car = FakeCarRouteClient()
            result = recommend_nearby_optimized(
                FakeOptimizedClient(),  # type: ignore[arg-type]
                SQLiteTTLCache(Path(directory) / "cache.sqlite3"),
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=1,
                max_detail_calls=5,
                trip=TripContext(arrival_time=datetime(2026, 8, 3, 14)),
                priorities=UserPriorities.from_order("route_time"),
                route_client=FakeExactlyFifteenMinuteWalkingRouteClient(),  # type: ignore[arg-type]
                car_route_client=car,  # type: ignore[arg-type]
            )

            candidate = result.recommendations[0]
            self.assertEqual(candidate.facts.transport_mode, "car")
            self.assertEqual(car.calls, 1)

    def test_next_schedule_route_filters_late_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = recommend_nearby_optimized(
                FakeOptimizedClient(),  # type: ignore[arg-type]
                SQLiteTTLCache(Path(directory) / "cache.sqlite3"),
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=1,
                max_detail_calls=5,
                trip=TripContext(arrival_time=datetime(2026, 8, 3, 14)),
                priorities=UserPriorities.from_order("route_time"),
                route_client=FakeRouteClient(),  # type: ignore[arg-type]
                next_schedule=NextScheduleConstraint(
                    longitude=127.0,
                    latitude=37.5,
                    arrival_deadline=datetime(2026, 8, 3, 15, 20),
                    visit_minutes=60,
                    buffer_minutes=10,
                    title="예약 전시",
                ),
            )

            self.assertFalse(result.recommendations)
            candidate = next(
                item
                for item in result.excluded
                if item.schedule_feasibility is not None
            )
            self.assertFalse(candidate.schedule_feasibility.feasible)
            self.assertIn(
                "다음 고정 일정에 제시간 도착 불가",
                candidate.evaluation.score.rejection_reasons,
            )

    def test_transit_recovers_candidate_over_walking_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            transit = FakeSeoulTransitClient()
            result = recommend_nearby_optimized(
                FakeOptimizedClient(),  # type: ignore[arg-type]
                SQLiteTTLCache(Path(directory) / "cache.sqlite3"),
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=1,
                max_detail_calls=5,
                trip=TripContext(arrival_time=datetime(2026, 8, 3, 14)),
                priorities=UserPriorities.from_order("route_time"),
                route_client=FakeLongWalkingRouteClient(),  # type: ignore[arg-type]
                seoul_transit_client=transit,  # type: ignore[arg-type]
            )

            candidate = result.recommendations[0]
            self.assertEqual(candidate.facts.transport_mode, "transit")
            self.assertEqual(candidate.facts.route_minutes, 25)
            self.assertTrue(candidate.evaluation.score.eligible)
            self.assertEqual(result.stats.seoul_transit_api_calls, 1)
            self.assertEqual(transit.calls, 1)

    def test_onward_route_geometry_is_captured_for_next_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = recommend_nearby_optimized(
                FakeOptimizedClient(),  # type: ignore[arg-type]
                SQLiteTTLCache(Path(directory) / "cache.sqlite3"),
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=1,
                max_detail_calls=5,
                trip=TripContext(arrival_time=datetime(2026, 8, 3, 14)),
                priorities=UserPriorities.from_order("route_time"),
                route_client=FakeRouteClient(),  # type: ignore[arg-type]
                next_schedule=NextScheduleConstraint(
                    longitude=127.0,
                    latitude=37.5,
                    arrival_deadline=datetime(2026, 8, 3, 20, 0),
                    visit_minutes=60,
                    buffer_minutes=10,
                    title="예약 전시",
                ),
            )

            candidate = result.recommendations[0]
            self.assertEqual(
                candidate.inbound_route_geometry,
                {
                    "type": "LineString",
                    "coordinates": [[126.97, 37.57], [126.975, 37.573]],
                },
            )
            self.assertEqual(
                candidate.onward_route_geometry,
                {
                    "type": "LineString",
                    "coordinates": [[126.97, 37.57], [126.975, 37.573]],
                },
            )

    def test_sqlite_cache_distinguishes_fresh_and_stale(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            cache.set("a", {"value": 1}, ttl_seconds=10, now=100)

            self.assertTrue(cache.get("a", now=105).is_fresh)
            self.assertFalse(cache.get("a", now=111).is_fresh)

    def test_second_run_uses_cache_and_stops_after_three_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            client = FakeOptimizedClient()
            trip = TripContext(
                arrival_time=datetime(2026, 7, 29, 14),
                remaining_budget_krw=20_000,
            )
            priorities = UserPriorities.from_order("route_time")
            arguments = {
                "map_x": 126.97,
                "map_y": 37.57,
                "radius": 2000,
                "search_rows": 20,
                "eligible_count": 3,
                "max_detail_calls": 10,
                "trip": trip,
                "priorities": priorities,
            }

            first = recommend_nearby_optimized(
                client, cache, **arguments  # type: ignore[arg-type]
            )
            second = recommend_nearby_optimized(
                client, cache, **arguments  # type: ignore[arg-type]
            )

            self.assertEqual(len(first.recommendations), 3)
            self.assertEqual(len(first.excluded), 1)
            self.assertEqual(first.stats.detail_api_calls, 4)
            self.assertEqual(first.stats.duplicates_skipped, 1)
            self.assertEqual(second.stats.detail_api_calls, 0)
            self.assertEqual(second.stats.cache_hits, 4)
            self.assertEqual(second.stats.detail_cache_hits, 4)
            self.assertEqual(client.detail_calls, 4)
            self.assertEqual(
                first.recommendations[0].detail_source,
                "실시간 API",
            )
            self.assertEqual(
                second.recommendations[0].detail_source,
                "유효 캐시",
            )
            self.assertNotIn(
                "4",
                {candidate.content_id for candidate in first.recommendations},
            )

    def test_real_walking_routes_are_cached_for_eligible_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            client = FakeOptimizedClient()
            route_client = FakeRouteClient()
            arguments = {
                "map_x": 126.97,
                "map_y": 37.57,
                "radius": 2000,
                "search_rows": 20,
                "eligible_count": 3,
                "max_detail_calls": 10,
                "trip": TripContext(
                    arrival_time=datetime(2026, 7, 29, 14),
                ),
                "priorities": UserPriorities.from_order("route_time"),
                "route_client": route_client,
            }

            first = recommend_nearby_optimized(
                client, cache, **arguments  # type: ignore[arg-type]
            )
            second = recommend_nearby_optimized(
                client, cache, **arguments  # type: ignore[arg-type]
            )

            self.assertEqual(first.stats.route_api_calls, 1)
            self.assertEqual(first.stats.route_cache_hits, 2)
            self.assertEqual(second.stats.route_api_calls, 0)
            self.assertEqual(second.stats.route_cache_hits, 3)
            self.assertEqual(route_client.calls, 1)
            self.assertEqual(
                first.recommendations[0].route_source,
                "실시간 TMAP 보행 경로",
            )
            self.assertEqual(
                second.recommendations[0].route_source,
                "유효 TMAP 경로 캐시",
            )
            self.assertEqual(
                first.recommendations[0].inbound_route_geometry,
                {
                    "type": "LineString",
                    "coordinates": [[126.97, 37.57], [126.975, 37.573]],
                },
            )
            self.assertEqual(
                second.recommendations[0].inbound_route_geometry,
                first.recommendations[0].inbound_route_geometry,
            )

    def test_crowd_calls_are_limited_and_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            client = FakeOptimizedClient()
            crowd_client = FakeCrowdClient()
            arguments = {
                "map_x": 126.97,
                "map_y": 37.57,
                "radius": 2000,
                "search_rows": 20,
                "eligible_count": 3,
                "max_detail_calls": 10,
                "trip": TripContext(arrival_time=datetime(2026, 7, 29, 14)),
                "priorities": UserPriorities.from_order("crowd_avoidance"),
                "crowd_client": crowd_client,
                "max_crowd_calls": 3,
            }

            first = recommend_nearby_optimized(client, cache, **arguments)
            second = recommend_nearby_optimized(client, cache, **arguments)

            self.assertEqual(first.stats.crowd_place_api_calls, 1)
            self.assertEqual(first.stats.crowd_realtime_api_calls, 3)
            self.assertEqual(second.stats.crowd_realtime_api_calls, 0)
            self.assertEqual(second.stats.crowd_realtime_cache_hits, 3)
            self.assertEqual(crowd_client.realtime_calls, 3)
            self.assertEqual(first.recommendations[0].title, "박물관 1")
            self.assertEqual(
                first.recommendations[0].crowd_source,
                "SK 실시간 장소 혼잡도",
            )

    def test_seoul_crowd_is_primary_and_shared_by_same_area(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            client = FakeOptimizedClient()
            seoul_client = FakeSeoulCrowdClient()
            result = recommend_nearby_optimized(
                client,
                cache,
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=3,
                max_detail_calls=10,
                trip=TripContext(arrival_time=datetime(2026, 8, 3, 14)),
                priorities=UserPriorities.from_order("crowd_avoidance"),
                seoul_crowd_client=seoul_client,
            )

            self.assertEqual(result.stats.seoul_crowd_api_calls, 1)
            self.assertEqual(result.stats.seoul_crowd_cache_hits, 2)
            self.assertEqual(seoul_client.calls, 1)
            self.assertTrue(
                all(
                    candidate.facts.crowd_level == 0.2
                    for candidate in result.recommendations
                )
            )
            self.assertTrue(
                all(
                    candidate.crowd_scope == "영역"
                    and candidate.crowd_raw_level == 0
                    for candidate in result.recommendations
                )
            )


class FakeWalkingRouteClientForTransit:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def pedestrian_route(self, **kwargs: object) -> WalkingRoute:
        self.calls.append(kwargs)
        return WalkingRoute(
            distance_meters=250,
            duration_seconds=200,
            geometry={
                "type": "LineString",
                "coordinates": [
                    [kwargs["start_x"], kwargs["start_y"]],
                    [(kwargs["start_x"] + kwargs["end_x"]) / 2, (kwargs["start_y"] + kwargs["end_y"]) / 2],
                    [kwargs["end_x"], kwargs["end_y"]],
                ],
            },
            steps=(
                WalkingStep(
                    instruction="좌회전 후 보행자도로를 따라 100m 이동",
                    turn_type=12,
                    distance_meters=100,
                    longitude=kwargs["start_x"],
                    latitude=kwargs["start_y"],
                ),
            ),
        )


class SeoulTransitWalkGeometryTests(unittest.TestCase):
    def test_walk_legs_get_real_geometry_and_route_geometry_is_restitched(self) -> None:
        route = SeoulTransitRoute(
            duration_minutes=20,
            legs=(
                SeoulTransitLeg(
                    mode="도보", instruction="도보로 200m 이동",
                    start_longitude=126.90, start_latitude=37.53,
                    end_longitude=126.905, end_latitude=37.535,
                    geometry={"type": "LineString", "coordinates": [[126.90, 37.53], [126.905, 37.535]]},
                ),
                SeoulTransitLeg(
                    mode="지하철", instruction="2호선 승차 → 하차",
                    start_longitude=126.905, start_latitude=37.535,
                    end_longitude=126.92, end_latitude=37.55,
                    geometry={"type": "LineString", "coordinates": [[126.905, 37.535], [126.92, 37.55]]},
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            walking_client = FakeWalkingRouteClientForTransit()
            enriched = enrich_seoul_transit_walk_geometry(
                route, walking_client, cache, ApiOptimizationStats()
            )

        self.assertEqual(len(walking_client.calls), 1)
        walk_leg = enriched.legs[0]
        self.assertEqual(len(walk_leg.geometry["coordinates"]), 3)
        # 도보 구간에는 TMAP의 turn-by-turn 안내도 함께 담긴다.
        self.assertEqual(len(walk_leg.steps), 1)
        self.assertIn("좌회전", walk_leg.steps[0].instruction)
        # 지하철 구간 geometry는 그대로 유지된다.
        self.assertEqual(enriched.legs[1].geometry, route.legs[1].geometry)
        # 전체 경로는 도보 구간의 중간점까지 포함해 다시 이어붙여진다.
        self.assertEqual(len(enriched.geometry["coordinates"]), 4)


class FakeCategoryClient:
    def location_based_list(self, **_: object) -> KTOResponse:
        items = [
            {"contentid": "near", "contenttypeid": "12", "title": "가까운 곳",
             "dist": "100", "cat2": "A0101"},
            {"contentid": "match", "contenttypeid": "12", "title": "비슷한 카테고리",
             "dist": "300", "cat2": "A0207"},
            {"contentid": "far", "contenttypeid": "12", "title": "먼 곳",
             "dist": "500", "cat2": "A0101"},
        ]
        return KTOResponse("locationBasedList2", items, 3, 1, 20, {})

    def detail_bundle(
        self, content_id: str, content_type_id: str, *, include_images: bool
    ) -> dict[str, object]:
        titles = {"near": "가까운 곳", "match": "비슷한 카테고리", "far": "먼 곳"}
        return detail_bundle(content_id, content_type_id, titles[content_id])


class CategoryPreferenceTests(unittest.TestCase):
    def _run(self, preferred_cat2: str | None) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
            result = recommend_nearby_optimized(
                FakeCategoryClient(),  # type: ignore[arg-type]
                cache,
                map_x=126.97,
                map_y=37.57,
                radius=2000,
                search_rows=20,
                eligible_count=3,
                max_detail_calls=10,
                trip=TripContext(
                    arrival_time=datetime(2026, 7, 29, 14),
                    remaining_budget_krw=20_000,
                ),
                priorities=UserPriorities.from_order("route_time"),
                preferred_cat2=preferred_cat2,
            )
            return [candidate.content_id for candidate in result.recommendations]

    def test_without_preference_sorts_by_score_only(self) -> None:
        order = self._run(None)
        self.assertEqual(order, ["near", "match", "far"])

    def test_matching_category_is_ranked_first_even_if_farther(self) -> None:
        order = self._run("A0207")
        self.assertEqual(order[0], "match")

    def test_no_matching_category_falls_back_to_normal_order(self) -> None:
        order = self._run("Z9999")
        self.assertEqual(order, ["near", "match", "far"])


if __name__ == "__main__":
    unittest.main()
