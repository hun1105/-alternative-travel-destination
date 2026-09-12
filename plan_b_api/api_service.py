"""웹·앱에서 호출할 Plan B JSON API 서비스 계층."""

from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime
from datetime import time as dt_time
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any, Mapping, Sequence

from .api_cache import SQLiteTTLCache
from .car_route_client import TMapCarClient
from .crowd_client import SKCrowdClient, match_crowd_place
from .kto_client import KTOApiError, KTOClient
from .normalizer import normalize_place
from .place_search_client import (
    PlaceSearchResult,
    TMapPlaceSearchClient,
    TMapPlaceSearchError,
)
from .optimized_recommender import (
    ApiOptimizationStats,
    DETAIL_TTL_SECONDS,
    cached_crowd_places,
    cached_car_route,
    cached_realtime_crowd,
    cached_seoul_crowd,
    cached_seoul_transit_route,
    cached_seoul_transit_route_with_snap,
    cached_walking_route,
    cached_weather_forecast,
    enrich_seoul_transit_walk_geometry,
    recommend_nearby_optimized,
)
from .place_search_client import TMapPlaceSearchClient, TMapPlaceSearchError
from .recommender import RankedTourCandidate, build_candidate_evidence
from .route_client import TMapApiError, TMapPedestrianClient
from .seoul_crowd_client import (
    SEOUL_AREA_CROWD_INFLUENCE,
    SEOUL_AREA_CROWD_NOTICE,
    SeoulCrowdClient,
    find_seoul_crowd_area,
    find_seoul_crowd_area_by_name,
)
from .seoul_transit_client import (
    SeoulTransitApiError,
    SeoulTransitClient,
    _haversine_distance_meters,
)
from .scoring import LABELS, PRIORITY_FIELDS, UserPriorities
from .signal_builder import TripContext
from .route_optimizer import optimize_schedule_order
from .schedule_feasibility import NextScheduleConstraint
from .weather_client import KMAClient, WeatherSnapshot
from .trip_plan import (
    SelectedPlace,
    TripPlan,
    replace_schedule_item,
    validate_trip_plan,
)
from .trip_store import TripStore


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _normalize_place_name(value: Any) -> str:
    return re.sub(r"[\s()]+", "", str(value or "")).lower()


def _distance_meters(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    lat1r, lat2r = radians(lat1), radians(lat2)
    dlat = lat2r - lat1r
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(lat1r) * cos(lat2r) * sin(dlon / 2) ** 2
    return 6_371_000 * 2 * atan2(sqrt(a), sqrt(1 - a))


def _find_tmap_description(
    client: TMapPlaceSearchClient,
    *,
    name: str,
    longitude: float,
    latitude: float,
) -> str:
    """추천 후보를 TMAP에서 이름+좌표로 찾아 짧은 소개 문구(desc)를 가져온다.

    관광공사 overview는 백과사전식으로 길고 딱딱한 경우가 많은데,
    TMAP의 desc는 방송 소개 위주로 짧고 후보 카드에 더 잘 맞는다.
    다만 모든 장소에 있는 건 아니라서(예: DDP) 못 찾으면 빈 문자열을
    반환하고, 호출한 쪽에서 관광공사 overview로 대체한다.
    """

    try:
        result = client.search(
            name, count=5, center_x=longitude, center_y=latitude, radius_km=5,
        )
    except TMapPlaceSearchError:
        return ""

    target = _normalize_place_name(name)
    best: PlaceSearchResult | None = None
    best_rank: tuple[int, float] | None = None
    for item in result.items:
        if not item.desc:
            continue
        title = _normalize_place_name(item.name)
        if not title:
            continue
        if title == target:
            exactness = 0
        elif target in title or title in target:
            exactness = 1
        else:
            continue
        distance = _distance_meters(longitude, latitude, item.longitude, item.latitude)
        if distance > 2000:
            continue
        rank = (exactness, distance)
        if best_rank is None or rank < best_rank:
            best, best_rank = item, rank
    return best.desc if best else ""


def _priority_fields(values: Sequence[Any] | None) -> tuple[str, ...]:
    if not values:
        return ("weather_fit", "route_time", "crowd_avoidance")
    if isinstance(values, (str, bytes)):
        raise ValueError("priorities는 번호 또는 필드명의 JSON 배열이어야 합니다.")
    fields: list[str] = []
    for value in values:
        if isinstance(value, int):
            if not 1 <= value <= len(PRIORITY_FIELDS):
                raise ValueError("우선순위 번호는 1~6이어야 합니다.")
            fields.append(PRIORITY_FIELDS[value - 1])
        else:
            fields.append(str(value).strip())
    return tuple(fields)


def _weather_dict(weather: WeatherSnapshot, source: str) -> dict[str, Any]:
    data = asdict(weather)
    data["forecast_time"] = weather.forecast_time.isoformat()
    data["source"] = source
    return data


def _candidate_dict(
    candidate: RankedTourCandidate,
    *,
    rank: int | None = None,
    tmap_desc: str | None = None,
) -> dict[str, Any]:
    score = candidate.evaluation.score
    build = candidate.evaluation.build
    evidence = build_candidate_evidence(candidate)
    place = candidate.place
    result: dict[str, Any] = {
        "rank": rank,
        "title": candidate.title,
        "content_id": candidate.content_id,
        "content_type_id": candidate.content_type_id,
        "cat1": candidate.cat1,
        "cat2": candidate.cat2,
        "cat3": candidate.cat3,
        "lcls1": candidate.lcls1,
        "lcls2": candidate.lcls2,
        "lcls3": candidate.lcls3,
        "overview": place.overview,
        "tmap_desc": tmap_desc or None,
        "address": place.address,
        "longitude": place.longitude,
        "latitude": place.latitude,
        "image_url": place.image_url,
        "homepage": place.homepage,
        "straight_distance_meters": candidate.distance_meters,
        "walking_distance_meters": candidate.facts.walking_meters,
        "route_minutes": round(candidate.facts.route_minutes, 1),
        "walking_minutes": candidate.facts.walking_minutes,
        "transport_mode": candidate.facts.transport_mode,
        "transport_walking_minutes": candidate.facts.walking_minutes,
        "estimated_group_cost_krw": build.estimated_group_cost_krw,
        "eligible": score.eligible,
        "score": score.total_score,
        "base_score": candidate.base_score,
        "confidence_penalty": candidate.confidence_penalty,
        "reasons": list(score.reasons),
        "rejection_reasons": list(score.rejection_reasons),
        "weights": score.weights,
        "contributions": score.contributions,
        "signals": asdict(build.signals),
        "confidence": {
            "percent": evidence.confidence_percent,
            "level": evidence.confidence_level,
        },
        "operation_status": evidence.operation_status,
        "sources": {
            "detail": evidence.detail_source,
            "route": evidence.route_source,
            "crowd": evidence.crowd_source,
        },
        "route_geometry": {
            "inbound": candidate.inbound_route_geometry,
            "onward": candidate.onward_route_geometry,
        },
        "crowd": {
            "scope": candidate.crowd_scope,
            "label": candidate.crowd_label,
            "raw_level": candidate.crowd_raw_level,
            "scoring_level": candidate.facts.crowd_level,
            "score_influence": (
                SEOUL_AREA_CROWD_INFLUENCE
                if candidate.crowd_scope == "영역"
                else None
            ),
            "accuracy_notice": (
                SEOUL_AREA_CROWD_NOTICE
                if candidate.crowd_scope == "영역"
                else None
            ),
        },
        "evidence": {
            "actual": list(evidence.actual_data),
            "estimated": list(evidence.estimated_data),
            "neutral": list(evidence.neutral_data),
            "excluded": list(evidence.excluded_data),
        },
        "warnings": list(place.warnings),
        "notes": list(candidate.estimation_notes + build.notes),
        "event": {
            "start_date": _iso(place.event_start_date),
            "end_date": _iso(place.event_end_date),
            "session_times": list(place.session_times),
            "reservation_required": place.reservation_required,
        },
        "next_schedule": (
            {
                **asdict(candidate.schedule_feasibility),
                "deadline": candidate.schedule_feasibility.deadline.isoformat(),
                "candidate_arrival": (
                    candidate.schedule_feasibility.candidate_arrival.isoformat()
                ),
                "candidate_departure": (
                    candidate.schedule_feasibility.candidate_departure.isoformat()
                ),
                "estimated_next_arrival": (
                    candidate.schedule_feasibility.estimated_next_arrival.isoformat()
                ),
            }
            if candidate.schedule_feasibility is not None
            else None
        ),
    }
    return result


_PLACE_SEARCH_CACHE: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
_PLACE_SEARCH_LOCK = threading.Lock()
_PLACE_SEARCH_CACHE_MAXSIZE = 256
_PLACE_SEARCH_TTL = 3600.0  # 1 hour


class PlanBApiService:
    def __init__(self, cache_path: str | Path = ".cache/plan_b_api.sqlite3") -> None:
        self.cache = SQLiteTTLCache(cache_path)
        self.trips = TripStore(cache_path)

    @staticmethod
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "Plan B 관광 추천 API",
            "integrations": {
                "tour_api": bool(os.getenv("KTO_SERVICE_KEY", "").strip()),
                "weather_api": bool(os.getenv("KMA_SERVICE_KEY", "").strip()),
                "walking_route_api": bool(os.getenv("TMAP_APP_KEY", "").strip()),
                "car_route_api": bool(os.getenv("TMAP_APP_KEY", "").strip()),
                "place_search_api": bool(os.getenv("TMAP_APP_KEY", "").strip()),
                "crowd_api": bool(
                    os.getenv("SK_CROWD_APP_KEY", "").strip()
                    or os.getenv("TMAP_APP_KEY", "").strip()
                ),
                "seoul_crowd_api": bool(
                    os.getenv("SEOUL_OPEN_API_KEY", "").strip()
                ),
                "seoul_transit_api": bool(
                    os.getenv("SEOUL_TRANSIT_SERVICE_KEY", "").strip()
                    or os.getenv("KTO_SERVICE_KEY", "").strip()
                    or os.getenv("ODSAY_API_KEY", "").strip()
                ),
            },
        }

    @staticmethod
    def priorities() -> dict[str, Any]:
        return {
            "minimum_selections": 1,
            "maximum_selections": 6,
            "items": [
                {"number": index, "field": field, "label": LABELS[field]}
                for index, field in enumerate(PRIORITY_FIELDS, start=1)
            ],
        }

    def weather(self, query: Mapping[str, Any]) -> dict[str, Any]:
        longitude = float(query["x"])
        latitude = float(query["y"])
        target = datetime.fromisoformat(
            str(query.get("at") or datetime.now().isoformat())
        )
        stats = ApiOptimizationStats()
        weather = cached_weather_forecast(
            KMAClient.from_env(),
            self.cache,
            stats,
            latitude=latitude,
            longitude=longitude,
            target_time=target,
        )
        source = "기상청 API" if stats.weather_api_calls else "날씨 캐시"
        return _weather_dict(weather, source)

    def place_search(self, query: Mapping[str, Any]) -> dict[str, Any]:
        keyword = str(query.get("q") or query.get("query") or "").strip()
        if not keyword:
            return {"query": "", "total_count": 0, "items": []}

        center_x = query.get("center_x")
        center_y = query.get("center_y")
        count = int(query.get("count", 10))
        page = int(query.get("page", 1))
        radius_km = int(query.get("radius_km", 20))

        cx_key = round(float(center_x), 3) if center_x is not None and str(center_x).strip() != "" else None
        cy_key = round(float(center_y), 3) if center_y is not None and str(center_y).strip() != "" else None
        cache_key = (keyword, cx_key, cy_key, count, page, radius_km)

        with _PLACE_SEARCH_LOCK:
            if cache_key in _PLACE_SEARCH_CACHE:
                cached_time, cached_payload = _PLACE_SEARCH_CACHE[cache_key]
                if time.time() - cached_time < _PLACE_SEARCH_TTL:
                    _PLACE_SEARCH_CACHE.move_to_end(cache_key)
                    return dict(cached_payload)
                del _PLACE_SEARCH_CACHE[cache_key]

        client = TMapPlaceSearchClient.from_env()

        if center_x is not None and center_y is not None:
            # 지역 엄격 모드: 전달받은 중심 좌표 반경 내 장소만 엄격히 검색
            result = client.search(
                keyword,
                count=count,
                page=page,
                center_x=float(center_x),
                center_y=float(center_y),
                radius_km=radius_km,
            )
            items = list(result.items)
        else:
            # 전국 검색 모드: 중심 좌표 없이 전국 단위 검색
            result = client.search(
                keyword,
                count=count,
                page=page,
                center_x=None,
                center_y=None,
                radius_km=radius_km,
            )
            items = list(result.items)

        total_count = result.total_count
        if total_count == 0 and items:
            total_count = len(items)

        payload = {
            "query": result.query or keyword,
            "total_count": total_count,
            "items": [item.selection_payload() for item in items],
        }

        with _PLACE_SEARCH_LOCK:
            _PLACE_SEARCH_CACHE[cache_key] = (time.time(), payload)
            if len(_PLACE_SEARCH_CACHE) > _PLACE_SEARCH_CACHE_MAXSIZE:
                _PLACE_SEARCH_CACHE.popitem(last=False)

        return payload

    @staticmethod
    def match_kto_category(query: Mapping[str, Any]) -> dict[str, Any]:
        """TMAP 검색으로 넣은 장소를 관광공사 데이터와 매칭해 cat1/cat2/cat3를 찾는다.

        키워드 검색은 이름은 정확히 찾지만 cat1/cat2/cat3를 채워주지 않고,
        위치기반 검색은 반경 안의 후보만 훑기 때문에 이름이 같아도 놓칠 때가
        있다(위치기반 색인과 실제 좌표가 어긋난 콘텐츠가 실제로 존재함).
        두 결과를 합쳐서 이름·거리로 가장 그럴듯한 하나를 고른다.
        """

        longitude = float(query["x"])
        latitude = float(query["y"])
        name = str(query.get("name") or "").strip()
        if not name:
            raise ValueError("name 쿼리가 필요합니다.")
        max_distance = float(query.get("max_distance", 1500))
        if not 0 < max_distance <= 20_000:
            raise ValueError("max_distance는 1~20000미터 범위여야 합니다.")

        candidates: list[Mapping[str, Any]] = []
        with KTOClient.from_env() as client:
            try:
                candidates.extend(
                    client.keyword_search(name, num_of_rows=10).items
                )
            except KTOApiError:
                pass
            try:
                candidates.extend(
                    client.location_based_list(
                        map_x=longitude,
                        map_y=latitude,
                        radius=int(max_distance),
                        arrange="E",
                        num_of_rows=20,
                    ).items
                )
            except KTOApiError:
                pass

        target = _normalize_place_name(name)
        best: Mapping[str, Any] | None = None
        best_rank: tuple[int, float] | None = None
        best_distance = 0.0
        seen_ids: set[str] = set()
        for item in candidates:
            content_id = str(item.get("contentid") or "")
            if content_id and content_id in seen_ids:
                continue
            title = _normalize_place_name(item.get("title"))
            if not title:
                continue
            if title == target:
                exactness = 0
            elif target in title or title in target:
                exactness = 1
            else:
                continue
            try:
                distance = _distance_meters(
                    longitude, latitude,
                    float(item["mapx"]), float(item["mapy"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            if distance > max_distance:
                continue
            if content_id:
                seen_ids.add(content_id)
            rank = (exactness, distance)
            if best_rank is None or rank < best_rank:
                best, best_rank, best_distance = item, rank, distance

        if best is None:
            return {"matched": False}
        return {
            "matched": True,
            "content_id": str(best.get("contentid") or ""),
            "content_type_id": str(best.get("contenttypeid") or ""),
            "title": best.get("title"),
            "cat1": str(best.get("cat1") or ""),
            "cat2": str(best.get("cat2") or ""),
            "cat3": str(best.get("cat3") or ""),
            "lcls1": str(best.get("lclsSystm1") or ""),
            "lcls2": str(best.get("lclsSystm2") or ""),
            "lcls3": str(best.get("lclsSystm3") or ""),
            "distance_meters": round(best_distance, 1),
        }

    @staticmethod
    def validate_trip_plan(body: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "valid": True,
            "plan": validate_trip_plan(body),
        }

    @staticmethod
    def optimize_schedule(body: Mapping[str, Any]) -> dict[str, Any]:
        raw_items = body.get("items")
        if not isinstance(raw_items, Sequence) or isinstance(raw_items, (str, bytes)):
            raise ValueError("items 배열이 필요합니다.")
        items = [dict(it) if isinstance(it, Mapping) else it for it in raw_items]
        keep_first = bool(body.get("keep_first", True))
        speed_raw = body.get("average_speed_kmh", 25.0)
        try:
            average_speed_kmh = float(speed_raw)
        except (ValueError, TypeError):
            average_speed_kmh = 25.0

        res = optimize_schedule_order(
            items,
            keep_first=keep_first,
            average_speed_kmh=average_speed_kmh,
        )
        return {
            "optimized_indices": res.optimized_indices,
            "original_distance_meters": res.original_distance_meters,
            "optimized_distance_meters": res.optimized_distance_meters,
            "saved_distance_meters": res.saved_distance_meters,
            "estimated_saved_minutes": res.estimated_saved_minutes,
            "is_improved": res.is_improved,
            "items": res.items,
        }

    def create_trip_plan(self, body: Mapping[str, Any]) -> dict[str, Any]:
        plan = TripPlan.from_mapping(body)
        trip_id = self.trips.create(plan)
        return {"trip_id": trip_id, "version": 1, "plan": plan.to_dict()}

    def get_trip_plan(self, trip_id: str) -> dict[str, Any]:
        plan, version = self.trips.get(trip_id)
        return {"trip_id": trip_id, "version": version, "plan": plan.to_dict()}

    def replace_trip_plan(
        self, trip_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        """일정 전체를 통째로 덮어쓴다(낙관적 동시성 제어 적용)."""

        plan_value = body.get("plan")
        if not isinstance(plan_value, Mapping):
            raise ValueError("plan 객체가 필요합니다.")
        if body.get("expected_version") is None:
            raise ValueError("expected_version이 필요합니다.")
        expected_version = int(body["expected_version"])
        plan = TripPlan.from_mapping(plan_value)
        new_version = self.trips.save(
            trip_id, plan, expected_version=expected_version
        )
        return {"trip_id": trip_id, "version": new_version, "plan": plan.to_dict()}

    def replace_trip_schedule(
        self, trip_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        item_id = str(body.get("item_id") or "").strip()
        if not item_id:
            raise ValueError("item_id가 필요합니다.")
        place_value = body.get("place")
        if not isinstance(place_value, Mapping):
            raise ValueError("교체할 place 객체가 필요합니다.")
        place = SelectedPlace.from_mapping(place_value)
        visit_minutes = (
            int(body["visit_minutes"])
            if body.get("visit_minutes") is not None
            else None
        )
        fixed_arrival_time = (
            dt_time.fromisoformat(str(body["fixed_arrival_time"]))
            if body.get("fixed_arrival_time") is not None
            else None
        )
        locked = (
            bool(body["locked"]) if body.get("locked") is not None else None
        )

        plan, _version = self.trips.get(trip_id)
        updated = replace_schedule_item(
            plan,
            item_id,
            place=place,
            visit_minutes=visit_minutes,
            fixed_arrival_time=fixed_arrival_time,
            locked=locked,
        )
        new_version = self.trips.save(trip_id, updated)
        return {"trip_id": trip_id, "version": new_version, "plan": updated.to_dict()}

    def walking_route(self, query: Mapping[str, Any]) -> dict[str, Any]:
        stats = ApiOptimizationStats()
        route, source = cached_walking_route(
            TMapPedestrianClient.from_env(),
            self.cache,
            stats,
            start_x=float(query["start_x"]),
            start_y=float(query["start_y"]),
            end_x=float(query["end_x"]),
            end_y=float(query["end_y"]),
            end_name=str(query.get("end_name") or "목적지"),
        )
        return {
            "distance_meters": route.distance_meters,
            "duration_seconds": route.duration_seconds,
            "duration_minutes": round(route.duration_minutes, 1),
            "geometry": route.geometry,
            "steps": [asdict(step) for step in route.steps],
            "source": source,
        }

    def crowd(self, query: Mapping[str, Any]) -> dict[str, Any]:
        client = SKCrowdClient.from_env()
        stats = ApiOptimizationStats()
        poi_id = str(query.get("poi_id") or "").strip()
        if not poi_id:
            name = str(query.get("name") or "").strip()
            if not name:
                raise ValueError("poi_id 또는 name 쿼리가 필요합니다.")
            places = cached_crowd_places(client, self.cache, stats)
            matched = match_crowd_place(name, places)
            if matched is None:
                raise ValueError("SK 지원 장소와 정확히 일치하는 이름이 없습니다.")
            poi_id = matched.poi_id
        snapshot, source = cached_realtime_crowd(
            client, self.cache, stats, poi_id=poi_id
        )
        return {
            **asdict(snapshot),
            "label": snapshot.label,
            "normalized_level": snapshot.normalized_level,
            "source": source,
        }

    def seoul_crowd(self, query: Mapping[str, Any]) -> dict[str, Any]:
        name = str(query.get("area") or "").strip()
        if name:
            area = find_seoul_crowd_area_by_name(name)
        elif query.get("x") is not None and query.get("y") is not None:
            area = find_seoul_crowd_area(
                float(query["x"]), float(query["y"])
            )
        else:
            raise ValueError("area 또는 x와 y 쿼리가 필요합니다.")
        if area is None:
            raise ValueError("서울시 혼잡도 지원 구역을 찾지 못했습니다.")
        target = datetime.fromisoformat(
            str(query.get("at") or datetime.now().isoformat())
        )
        stats = ApiOptimizationStats()
        snapshot, source = cached_seoul_crowd(
            SeoulCrowdClient.from_env(),
            self.cache,
            stats,
            area_code=area.area_code,
            target_time=target,
        )
        return {
            **asdict(snapshot),
            "source": source,
            "data_scope": "서울시 영역",
            "score_influence": SEOUL_AREA_CROWD_INFLUENCE,
            "accuracy_notice": SEOUL_AREA_CROWD_NOTICE,
            "matched_area": {
                "area_code": area.area_code,
                "name": area.name,
                "category": area.category,
            },
        }

    def place(self, content_id: str, content_type_id: str) -> dict[str, Any]:
        cache_key = f"kto-detail:{content_id}:{content_type_id}"
        entry = self.cache.get(cache_key)
        if entry and entry.is_fresh:
            bundle = entry.value
            source = "유효 캐시"
        else:
            with KTOClient.from_env() as client:
                bundle = client.detail_bundle(
                    content_id,
                    content_type_id,
                    include_images=False,
                )
            self.cache.set(cache_key, bundle, ttl_seconds=DETAIL_TTL_SECONDS)
            source = "실시간 API"
        data = asdict(normalize_place(bundle))
        data["source"] = source
        return data

    def seoul_transit_route(self, query: Mapping[str, Any]) -> dict[str, Any]:
        required = ("start_x", "start_y", "end_x", "end_y")
        if any(query.get(name) is None for name in required):
            raise ValueError("start_x, start_y, end_x, end_y가 필요합니다.")
        routing_preference = str(
            query.get("routing_preference")
            or query.get("preference")
            or "fastest"
        )
        if routing_preference not in {"fastest", "least_transfers"}:
            routing_preference = "fastest"
        stats = ApiOptimizationStats()
        start_x = float(query["start_x"])
        start_y = float(query["start_y"])
        end_x = float(query["end_x"])
        end_y = float(query["end_y"])

        dist_meters = _haversine_distance_meters(start_x, start_y, end_x, end_y)

        # 500m 이내 근거리인 경우만 도보 경로로 안내한다.
        is_short_distance = dist_meters <= 500.0

        if not is_short_distance:
            # 500m 초과인 경우 대중교통 API를 호출하며, 직호출 실패 시 인근 정류장 스냅 연계를 시도한다.
            try:
                place_client = None
                try:
                    place_client = TMapPlaceSearchClient.from_env()
                except (ValueError, TMapPlaceSearchError):
                    pass

                route, source = cached_seoul_transit_route_with_snap(
                    SeoulTransitClient.from_env(),
                    place_client,
                    self.cache,
                    stats,
                    start_x=start_x,
                    start_y=start_y,
                    end_x=end_x,
                    end_y=end_y,
                    start_name=str(query.get("start_name") or ""),
                    end_name=str(query.get("end_name") or ""),
                    routing_preference=routing_preference,
                )
                try:
                    route = enrich_seoul_transit_walk_geometry(
                        route, TMapPedestrianClient.from_env(), self.cache, stats
                    )
                except (ValueError, TMapApiError):
                    pass
                return {
                    **asdict(route),
                    "source": source,
                    "within_30_minutes": route.duration_minutes <= 30,
                    "is_walking_fallback": False,
                    "api_calls": stats.total_api_calls,
                }
            except SeoulTransitApiError as exc:
                clean_err = str(exc)
                if "XML Parsing Error" in clean_err:
                    clean_err = "해당 구간을 연결하는 대중교통(버스·지하철) 환승 노선이 없습니다."
                raise SeoulTransitApiError(clean_err) from exc

        # 500m 이내 근거리 구간 도보 안내
        try:
            walk_data = self.walking_route(query)
            walk_mins = float(walk_data["duration_minutes"])
            walk_dist = float(walk_data["distance_meters"])
            walk_geom = walk_data.get("geometry")
            walk_steps = walk_data.get("steps") or []
        except Exception:
            walk_dist = round(dist_meters * 1.3, 1)
            walk_mins = max(1.0, round(walk_dist / 75.0, 1))
            walk_geom = {
                "type": "LineString",
                "coordinates": [[start_x, start_y], [end_x, end_y]],
            }
            walk_steps = []

        fallback_leg = {
            "mode": "도보",
            "instruction": f"500m 이내 근거리 구간 ({int(walk_dist)}m) 도보 이동 권장",
            "lane_name": None,
            "start_name": str(query.get("start_name") or "출발지"),
            "end_name": str(query.get("end_name") or "도착지"),
            "start_entrance_no": None,
            "end_exit_no": None,
            "station_count": None,
            "distance_meters": walk_dist,
            "duration_minutes": walk_mins,
            "start_longitude": start_x,
            "start_latitude": start_y,
            "end_longitude": end_x,
            "end_latitude": end_y,
            "start_entrance_longitude": None,
            "start_entrance_latitude": None,
            "end_exit_longitude": None,
            "end_exit_latitude": None,
            "geometry": walk_geom,
            "steps": walk_steps,
        }

        return {
            "duration_minutes": walk_mins,
            "distance_meters": walk_dist,
            "walking_minutes": walk_mins,
            "walking_distance_meters": walk_dist,
            "transfer_count": 0,
            "route_type": "도보 권장 (500m 이내)",
            "geometry": walk_geom,
            "legs": [fallback_leg],
            "source": "도보 이동 권장 (500m 이내)",
            "within_30_minutes": walk_mins <= 30,
            "is_walking_fallback": True,
            "notice": f"출발지와 목적지가 500m 이내(직선 {int(dist_meters)}m, 도보 약 {round(walk_mins)}분)로 가까워 도보 경로를 안내합니다.",
            "api_calls": stats.total_api_calls,
        }

    def car_route(self, query: Mapping[str, Any]) -> dict[str, Any]:
        required = ("start_x", "start_y", "end_x", "end_y")
        if any(query.get(name) is None for name in required):
            raise ValueError("start_x, start_y, end_x, end_y가 필요합니다.")
        stats = ApiOptimizationStats()
        route, source = cached_car_route(
            TMapCarClient.from_env(),
            self.cache,
            stats,
            start_x=float(query["start_x"]),
            start_y=float(query["start_y"]),
            end_x=float(query["end_x"]),
            end_y=float(query["end_y"]),
            end_name=str(query.get("end_name") or "목적지"),
        )
        return {
            **asdict(route),
            "duration_minutes": round(route.duration_minutes, 2),
            "source": source,
            "within_30_minutes": route.duration_minutes <= 30,
            "api_calls": stats.total_api_calls,
        }

    def recommendations(self, body: Mapping[str, Any]) -> dict[str, Any]:
        longitude = float(body["longitude"])
        latitude = float(body["latitude"])
        arrival = datetime.fromisoformat(
            str(body.get("arrival_time") or datetime.now().isoformat())
        )
        priorities = UserPriorities.from_order(
            *_priority_fields(body.get("priorities"))
        )
        stats = ApiOptimizationStats()
        weather_data: dict[str, Any]
        manual_weather = body.get("weather_severity")
        if manual_weather is None:
            weather = cached_weather_forecast(
                KMAClient.from_env(),
                self.cache,
                stats,
                latitude=latitude,
                longitude=longitude,
                target_time=arrival,
            )
            weather_severity = weather.severity
            weather_source = (
                "기상청 API" if stats.weather_api_calls else "날씨 캐시"
            )
            weather_data = _weather_dict(weather, weather_source)
        else:
            weather_severity = float(manual_weather)
            weather_data = {
                "severity": weather_severity,
                "source": "사용자 입력",
            }

        route_mode = str(body.get("route_mode") or "auto")
        if route_mode not in {"auto", "tmap", "estimated"}:
            raise ValueError("route_mode은 auto, tmap, estimated 중 하나입니다.")
        route_client = None
        if route_mode != "estimated":
            try:
                route_client = TMapPedestrianClient.from_env()
            except ValueError:
                if route_mode == "tmap":
                    raise

        car_mode = str(body.get("car_mode") or "auto")
        if car_mode not in {"auto", "tmap", "off"}:
            raise ValueError("car_mode은 auto, tmap, off 중 하나입니다.")
        car_route_client = None
        if car_mode != "off":
            try:
                car_route_client = TMapCarClient.from_env()
            except ValueError:
                if car_mode == "tmap":
                    raise

        crowd_mode = str(body.get("crowd_mode") or "auto")
        if crowd_mode not in {"auto", "seoul", "off"}:
            raise ValueError("crowd_mode은 auto, seoul, off 중 하나입니다.")
        crowd_client = None
        seoul_crowd_client = None
        if crowd_mode in {"auto", "seoul"}:
            try:
                seoul_crowd_client = SeoulCrowdClient.from_env()
            except ValueError:
                if crowd_mode == "seoul":
                    raise

        transit_mode = str(body.get("transit_mode") or "auto")
        if transit_mode not in {"auto", "seoul", "off"}:
            raise ValueError("transit_mode은 auto, seoul, off 중 하나입니다.")
        seoul_transit_client = None
        if transit_mode in {"auto", "seoul"}:
            try:
                seoul_transit_client = SeoulTransitClient.from_env()
            except ValueError:
                if transit_mode == "seoul":
                    raise

        trip = TripContext(
            arrival_time=arrival,
            party_size=int(body.get("party_size", 1)),
            children_count=int(body.get("children", 0)),
            remaining_budget_krw=(
                int(body["budget"]) if body.get("budget") is not None else None
            ),
            weather_severity=weather_severity,
            max_route_minutes=float(body.get("max_route_minutes", 30)),
            max_walking_minutes=float(body.get("max_walking_minutes", 15)),
            max_transport_minutes=float(body.get("max_transport_minutes", 30)),
            max_walking_meters=float(body.get("max_walking_meters", 3000)),
            budget_is_hard=False,
        )
        next_values = (
            body.get("next_longitude"),
            body.get("next_latitude"),
            body.get("next_arrival_time"),
        )
        if any(value is not None for value in next_values) and not all(
            value is not None for value in next_values
        ):
            raise ValueError(
                "next_longitude, next_latitude, next_arrival_time이 모두 필요합니다."
            )
        next_schedule = (
            NextScheduleConstraint(
                longitude=float(body["next_longitude"]),
                latitude=float(body["next_latitude"]),
                arrival_deadline=datetime.fromisoformat(
                    str(body["next_arrival_time"])
                ),
                visit_minutes=float(body.get("visit_minutes", 60)),
                buffer_minutes=float(body.get("schedule_buffer_minutes", 10)),
                title=str(body.get("next_title") or "다음 일정"),
            )
            if body.get("next_arrival_time") is not None
            else None
        )
        with KTOClient.from_env() as client:
            result = recommend_nearby_optimized(
                client,
                self.cache,
                map_x=longitude,
                map_y=latitude,
                radius=int(body.get("radius", 3000)),
                search_rows=int(body.get("search_rows", 20)),
                eligible_count=int(body.get("eligible_count", 3)),
                max_detail_calls=int(body.get("max_detail_calls", 12)),
                trip=trip,
                priorities=priorities,
                stats=stats,
                content_type_id=(
                    str(body["content_type_id"])
                    if body.get("content_type_id") is not None
                    else None
                ),
                route_client=route_client,
                car_route_client=car_route_client,
                crowd_client=crowd_client,
                max_crowd_calls=int(body.get("max_crowd_calls", 3)),
                seoul_crowd_client=seoul_crowd_client,
                max_seoul_crowd_calls=int(
                    body.get("max_seoul_crowd_calls", 10)
                ),
                seoul_transit_client=seoul_transit_client,
                max_seoul_transit_calls=int(
                    body.get("max_seoul_transit_calls", 3)
                ),
                transit_preference=str(
                    body.get("transit_preference")
                    or body.get("routing_preference")
                    or "fastest"
                ),
                max_car_route_calls=int(body.get("max_car_route_calls", 5)),
                next_schedule=next_schedule,
                include_restaurants=bool(body.get("include_restaurants", False)),
                preferred_cat2=(
                    str(body["preferred_cat2"]).strip()
                    if body.get("preferred_cat2")
                    else None
                ),
                preferred_lcls2=(
                    str(body["preferred_lcls2"]).strip()
                    if body.get("preferred_lcls2")
                    else None
                ),
                exclude_longitude=(
                    float(body["exclude_longitude"])
                    if body.get("exclude_longitude") is not None
                    else None
                ),
                exclude_latitude=(
                    float(body["exclude_latitude"])
                    if body.get("exclude_latitude") is not None
                    else None
                ),
                exclude_content_id=(
                    str(body["exclude_content_id"]).strip()
                    if body.get("exclude_content_id")
                    else None
                ),
            )

        # 최종 추천 후보(보통 3개)만 TMAP에서 짧은 소개문을 찾아본다 —
        # 제외된 후보까지 조회하면 호출량만 늘고 화면에 쓰이지 않는다.
        tmap_descriptions: dict[str, str] = {}
        try:
            desc_client = TMapPlaceSearchClient.from_env()
        except ValueError:
            desc_client = None
        if desc_client is not None:
            for candidate in result.recommendations:
                if candidate.place.longitude is None or candidate.place.latitude is None:
                    continue
                desc = _find_tmap_description(
                    desc_client,
                    name=candidate.title,
                    longitude=candidate.place.longitude,
                    latitude=candidate.place.latitude,
                )
                if desc:
                    tmap_descriptions[candidate.content_id] = desc

        stats_data = asdict(result.stats)
        stats_data["total_api_calls"] = result.stats.total_api_calls
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "weather": weather_data,
            "priorities": list(_priority_fields(body.get("priorities"))),
            "recommendations": [
                _candidate_dict(
                    candidate, rank=index,
                    tmap_desc=tmap_descriptions.get(candidate.content_id),
                )
                for index, candidate in enumerate(result.recommendations, start=1)
            ],
            "excluded": [
                _candidate_dict(candidate) for candidate in result.excluded
            ],
            "skipped": list(result.skipped),
            "optimization": stats_data,
        }
