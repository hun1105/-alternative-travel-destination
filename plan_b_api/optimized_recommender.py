"""호출량을 줄이는 별도 주변 관광지 추천 엔진."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from typing import Any

from .api_cache import SQLiteTTLCache
from .car_route_client import CarRoute, TMapCarApiError, TMapCarClient
from .crowd_client import (
    CrowdPlace,
    CrowdSnapshot,
    SKCrowdApiError,
    SKCrowdClient,
    match_crowd_place,
)
from .kto_client import KTOApiError, KTOClient
from .normalizer import normalize_place
from .recommender import (
    RankedTourCandidate,
    apply_confidence_penalty,
    facts_from_tourapi,
    is_recommendation_content_type,
)
from .route_client import (
    TMapApiError,
    TMapPedestrianClient,
    WalkingRoute,
    WalkingStep,
)
from .seoul_crowd_client import (
    SEOUL_AREA_CROWD_NOTICE,
    SeoulCrowdApiError,
    SeoulCrowdClient,
    SeoulCrowdSnapshot,
    find_seoul_crowd_area,
    load_seoul_crowd_areas,
    scoring_area_crowd_level,
)
from .scoring import UserPriorities
from .schedule_feasibility import (
    NextScheduleConstraint,
    ScheduleFeasibility,
    evaluate_schedule_feasibility,
)
from .seoul_transit_client import (
    SeoulTransitApiError,
    SeoulTransitClient,
    SeoulTransitLeg,
    SeoulTransitRoute,
    combine_leg_geometries,
)
from .signal_builder import TripContext, evaluate_place_candidate
from .weather_client import (
    KMAApiError,
    KMAClient,
    WeatherSnapshot,
    grid_from_latlon,
)


DETAIL_TTL_SECONDS = 6 * 60 * 60
FESTIVAL_TTL_SECONDS = 60 * 60
WEATHER_TTL_SECONDS = 20 * 60
ROUTE_TTL_SECONDS = 24 * 60 * 60
CROWD_PLACES_TTL_SECONDS = 24 * 60 * 60
CROWD_REALTIME_TTL_SECONDS = 10 * 60
SEOUL_CROWD_TTL_SECONDS = 5 * 60
SEOUL_TRANSIT_TTL_SECONDS = 30 * 60
CAR_ROUTE_TTL_SECONDS = 5 * 60
WALKING_LIMIT_REASON = "도보 이동시간이 최대 허용시간을 초과함"


def _straight_distance_meters(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> float:
    lat1, lat2 = radians(start_y), radians(end_y)
    dlat = lat2 - lat1
    dlon = radians(end_x - start_x)
    value = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 6_371_000 * 2 * atan2(sqrt(value), sqrt(1 - value))


def _estimated_walking_minutes(distance_meters: float) -> float:
    return max(1.0, distance_meters / 75.0)


@dataclass
class ApiOptimizationStats:
    list_api_calls: int = 0
    detail_api_calls: int = 0
    weather_api_calls: int = 0
    route_api_calls: int = 0
    car_route_api_calls: int = 0
    crowd_place_api_calls: int = 0
    crowd_realtime_api_calls: int = 0
    seoul_crowd_api_calls: int = 0
    seoul_transit_api_calls: int = 0
    cache_hits: int = 0
    stale_cache_hits: int = 0
    detail_cache_hits: int = 0
    weather_cache_hits: int = 0
    detail_stale_cache_hits: int = 0
    weather_stale_cache_hits: int = 0
    route_cache_hits: int = 0
    car_route_cache_hits: int = 0
    crowd_place_cache_hits: int = 0
    crowd_realtime_cache_hits: int = 0
    seoul_crowd_cache_hits: int = 0
    seoul_transit_cache_hits: int = 0
    route_stale_cache_hits: int = 0
    route_failures: int = 0
    car_route_failures: int = 0
    crowd_failures: int = 0
    crowd_unmatched: int = 0
    seoul_crowd_failures: int = 0
    seoul_crowd_unmatched: int = 0
    seoul_transit_failures: int = 0
    candidates_considered: int = 0
    duplicates_skipped: int = 0
    failures: int = 0

    @property
    def total_api_calls(self) -> int:
        return (
            self.list_api_calls
            + self.detail_api_calls
            + self.weather_api_calls
            + self.route_api_calls
            + self.car_route_api_calls
            + self.crowd_place_api_calls
            + self.crowd_realtime_api_calls
            + self.seoul_crowd_api_calls
            + self.seoul_transit_api_calls
        )


@dataclass(frozen=True)
class OptimizedRecommendationResult:
    recommendations: tuple[RankedTourCandidate, ...]
    excluded: tuple[RankedTourCandidate, ...]
    skipped: tuple[str, ...]
    stats: ApiOptimizationStats


def _eligible_or_only_walking_limit(evaluation: Any) -> bool:
    reasons = set(evaluation.score.rejection_reasons)
    return evaluation.score.eligible or reasons == {WALKING_LIMIT_REASON}


def _weather_from_dict(value: dict[str, Any]) -> WeatherSnapshot:
    return WeatherSnapshot(
        forecast_time=datetime.fromisoformat(str(value["forecast_time"])),
        grid_x=int(value["grid_x"]),
        grid_y=int(value["grid_y"]),
        temperature_c=value.get("temperature_c"),
        precipitation_probability=value.get("precipitation_probability"),
        precipitation_type=value.get("precipitation_type"),
        precipitation_mm=value.get("precipitation_mm"),
        sky_code=value.get("sky_code"),
        humidity_percent=value.get("humidity_percent"),
        wind_speed_mps=value.get("wind_speed_mps"),
        severity=float(value["severity"]),
        summary=str(value["summary"]),
    )


def cached_weather_forecast(
    client: KMAClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
    *,
    latitude: float,
    longitude: float,
    target_time: datetime,
) -> WeatherSnapshot:
    """20분 캐시를 우선하고 장애 시 만료 캐시를 사용한다."""

    nx, ny = grid_from_latlon(latitude, longitude)
    target_key = target_time.strftime("%Y%m%d%H")
    key = f"weather:{nx}:{ny}:{target_key}"
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.weather_cache_hits += 1
        return _weather_from_dict(entry.value)

    try:
        stats.weather_api_calls += 1
        weather = client.village_forecast(
            latitude=latitude,
            longitude=longitude,
            target_time=target_time,
        )
    except KMAApiError:
        if entry:
            stats.stale_cache_hits += 1
            stats.weather_stale_cache_hits += 1
            return _weather_from_dict(entry.value)
        raise

    cache.set(key, asdict(weather), ttl_seconds=WEATHER_TTL_SECONDS)
    return weather


def _walking_route_from_dict(value: dict[str, Any]) -> WalkingRoute:
    return WalkingRoute(
        distance_meters=float(value["distance_meters"]),
        duration_seconds=int(value["duration_seconds"]),
        geometry=value.get("geometry"),
        steps=tuple(
            WalkingStep(
                instruction=str(step["instruction"]),
                turn_type=step.get("turn_type"),
                distance_meters=step.get("distance_meters"),
                longitude=float(step["longitude"]),
                latitude=float(step["latitude"]),
            )
            for step in value.get("steps") or []
        ),
    )


def cached_walking_route(
    client: TMapPedestrianClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    end_name: str,
) -> tuple[WalkingRoute, str]:
    """24시간 캐시를 우선하고 장애 시 만료 경로를 사용한다."""

    key = (
        f"walking:{start_x:.5f}:{start_y:.5f}:"
        f"{end_x:.5f}:{end_y:.5f}"
    )
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.route_cache_hits += 1
        return _walking_route_from_dict(entry.value), "유효 TMAP 경로 캐시"
    try:
        stats.route_api_calls += 1
        route = client.pedestrian_route(
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            end_name=end_name,
        )
    except TMapApiError:
        if entry:
            stats.stale_cache_hits += 1
            stats.route_stale_cache_hits += 1
            return _walking_route_from_dict(entry.value), "만료 TMAP 경로 캐시"
        raise
    cache.set(key, asdict(route), ttl_seconds=ROUTE_TTL_SECONDS)
    return route, "실시간 TMAP 보행 경로"


def _car_route_from_dict(value: dict[str, Any]) -> CarRoute:
    return CarRoute(
        distance_meters=float(value["distance_meters"]),
        duration_seconds=int(value["duration_seconds"]),
        total_fare_krw=value.get("total_fare_krw"),
        taxi_fare_krw=value.get("taxi_fare_krw"),
        geometry=value.get("geometry"),
    )


def cached_car_route(
    client: TMapCarClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    end_name: str,
) -> tuple[CarRoute, str]:
    key = f"car:{start_x:.5f}:{start_y:.5f}:{end_x:.5f}:{end_y:.5f}"
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.car_route_cache_hits += 1
        return _car_route_from_dict(entry.value), "유효 TMAP 자동차 경로 캐시"
    try:
        stats.car_route_api_calls += 1
        route = client.car_route(
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
            end_name=end_name,
        )
    except TMapCarApiError:
        if entry:
            stats.stale_cache_hits += 1
            return _car_route_from_dict(entry.value), "만료 TMAP 자동차 경로 캐시"
        raise
    cache.set(key, asdict(route), ttl_seconds=CAR_ROUTE_TTL_SECONDS)
    return route, "실시간 TMAP 자동차 경로"


def _seoul_transit_leg_from_dict(value: dict[str, Any]) -> SeoulTransitLeg:
    data = dict(value)
    steps = data.pop("steps", None) or []
    return SeoulTransitLeg(
        **data,
        steps=tuple(
            WalkingStep(
                instruction=str(step["instruction"]),
                turn_type=step.get("turn_type"),
                distance_meters=step.get("distance_meters"),
                longitude=float(step["longitude"]),
                latitude=float(step["latitude"]),
            )
            for step in steps
        ),
    )


def _seoul_transit_from_dict(value: dict[str, Any]) -> SeoulTransitRoute:
    return SeoulTransitRoute(
        duration_minutes=float(value["duration_minutes"]),
        distance_meters=value.get("distance_meters"),
        walking_minutes=value.get("walking_minutes"),
        walking_distance_meters=value.get("walking_distance_meters"),
        transfer_count=value.get("transfer_count"),
        route_type=str(value.get("route_type") or "버스+지하철"),
        geometry=value.get("geometry"),
        legs=tuple(
            _seoul_transit_leg_from_dict(leg) for leg in value.get("legs") or []
        ),
    )


def cached_seoul_transit_route(
    client: SeoulTransitClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> tuple[SeoulTransitRoute, str]:
    key = (
        f"seoul-transit:{start_x:.5f}:{start_y:.5f}:"
        f"{end_x:.5f}:{end_y:.5f}"
    )
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.seoul_transit_cache_hits += 1
        return _seoul_transit_from_dict(entry.value), "유효 서울시 환승경로 캐시"
    try:
        stats.seoul_transit_api_calls += 1
        route = client.route(
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
        )
    except SeoulTransitApiError:
        if entry:
            stats.stale_cache_hits += 1
            return _seoul_transit_from_dict(entry.value), "만료 서울시 환승경로 캐시"
        raise
    cache.set(key, asdict(route), ttl_seconds=SEOUL_TRANSIT_TTL_SECONDS)
    return route, "서울시 버스·지하철 환승경로"


def enrich_seoul_transit_walk_geometry(
    route: SeoulTransitRoute,
    walking_client: TMapPedestrianClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
) -> SeoulTransitRoute:
    """ODsay는 도보 구간의 실제 경로선을 주지 않으므로 TMAP으로 채운다."""

    legs = list(route.legs)
    for index, leg in enumerate(legs):
        if leg.mode != "도보" or leg.start_longitude is None or leg.end_longitude is None:
            continue
        try:
            walk_route, _ = cached_walking_route(
                walking_client,
                cache,
                stats,
                start_x=leg.start_longitude,
                start_y=leg.start_latitude,
                end_x=leg.end_longitude,
                end_y=leg.end_latitude,
                end_name=leg.end_name or "환승 지점",
            )
        except TMapApiError:
            continue
        if walk_route.geometry:
            legs[index] = replace(
                leg, geometry=walk_route.geometry, steps=walk_route.steps
            )
    legs_tuple = tuple(legs)
    geometry = combine_leg_geometries(legs_tuple) or route.geometry
    return replace(route, legs=legs_tuple, geometry=geometry)


def cached_crowd_places(
    client: SKCrowdClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
) -> tuple[CrowdPlace, ...]:
    key = "sk-crowd:supported-places"
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.crowd_place_cache_hits += 1
        return tuple(CrowdPlace(**value) for value in entry.value)
    try:
        stats.crowd_place_api_calls += 1
        places = client.supported_places()
    except SKCrowdApiError:
        if entry:
            stats.stale_cache_hits += 1
            return tuple(CrowdPlace(**value) for value in entry.value)
        raise
    cache.set(
        key,
        [asdict(place) for place in places],
        ttl_seconds=CROWD_PLACES_TTL_SECONDS,
    )
    return places


def _crowd_from_dict(value: dict[str, Any]) -> CrowdSnapshot:
    return CrowdSnapshot(
        poi_id=str(value["poi_id"]),
        poi_name=str(value["poi_name"]),
        congestion=float(value["congestion"]),
        congestion_level=int(value["congestion_level"]),
        measured_at=str(value["measured_at"]),
    )


def cached_realtime_crowd(
    client: SKCrowdClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
    *,
    poi_id: str,
) -> tuple[CrowdSnapshot, str]:
    key = f"sk-crowd:realtime:{poi_id}"
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.crowd_realtime_cache_hits += 1
        return _crowd_from_dict(entry.value), "유효 SK 혼잡도 캐시"
    try:
        stats.crowd_realtime_api_calls += 1
        snapshot = client.realtime(poi_id)
    except SKCrowdApiError:
        if entry:
            stats.stale_cache_hits += 1
            return _crowd_from_dict(entry.value), "만료 SK 혼잡도 캐시"
        raise
    cache.set(key, asdict(snapshot), ttl_seconds=CROWD_REALTIME_TTL_SECONDS)
    return snapshot, "SK 실시간 장소 혼잡도"


def _seoul_crowd_from_dict(value: dict[str, Any]) -> SeoulCrowdSnapshot:
    return SeoulCrowdSnapshot(
        area_code=str(value["area_code"]),
        area_name=str(value["area_name"]),
        congestion_label=str(value["congestion_label"]),
        normalized_level=float(value["normalized_level"]),
        population_min=value.get("population_min"),
        population_max=value.get("population_max"),
        measured_at=str(value.get("measured_at") or ""),
        forecast_time=value.get("forecast_time"),
        is_forecast=bool(value.get("is_forecast", False)),
        message=str(value.get("message") or ""),
    )


def cached_seoul_crowd(
    client: SeoulCrowdClient,
    cache: SQLiteTTLCache,
    stats: ApiOptimizationStats,
    *,
    area_code: str,
    target_time: datetime,
) -> tuple[SeoulCrowdSnapshot, str]:
    target_key = target_time.strftime("%Y%m%d%H")
    key = f"seoul-crowd:{area_code}:{target_key}"
    entry = cache.get(key)
    if entry and entry.is_fresh:
        stats.cache_hits += 1
        stats.seoul_crowd_cache_hits += 1
        snapshot = _seoul_crowd_from_dict(entry.value)
        return snapshot, "유효 서울시 주변 지역 혼잡도 캐시"
    try:
        stats.seoul_crowd_api_calls += 1
        snapshot = client.crowd(area_code, target_time=target_time)
    except SeoulCrowdApiError:
        if entry:
            stats.stale_cache_hits += 1
            return (
                _seoul_crowd_from_dict(entry.value),
                "만료 서울시 주변 지역 혼잡도 캐시",
            )
        raise
    cache.set(key, asdict(snapshot), ttl_seconds=SEOUL_CROWD_TTL_SECONDS)
    source = (
        "서울시 예측 주변 지역 혼잡도"
        if snapshot.is_forecast
        else "서울시 실시간 주변 지역 혼잡도"
    )
    return snapshot, source


def recommend_nearby_optimized(
    client: KTOClient,
    cache: SQLiteTTLCache,
    *,
    map_x: float,
    map_y: float,
    radius: int,
    search_rows: int,
    eligible_count: int,
    max_detail_calls: int,
    trip: TripContext,
    priorities: UserPriorities,
    stats: ApiOptimizationStats | None = None,
    content_type_id: str | None = None,
    route_client: TMapPedestrianClient | None = None,
    car_route_client: TMapCarClient | None = None,
    crowd_client: SKCrowdClient | None = None,
    max_crowd_calls: int = 3,
    seoul_crowd_client: SeoulCrowdClient | None = None,
    max_seoul_crowd_calls: int = 10,
    seoul_transit_client: SeoulTransitClient | None = None,
    max_seoul_transit_calls: int = 3,
    max_car_route_calls: int = 5,
    next_schedule: NextScheduleConstraint | None = None,
    include_restaurants: bool = False,
    preferred_cat2: str | None = None,
    preferred_lcls2: str | None = None,
    exclude_longitude: float | None = None,
    exclude_latitude: float | None = None,
    exclude_radius_meters: float = 50.0,
    exclude_content_id: str | None = None,
) -> OptimizedRecommendationResult:
    """가까운 후보부터 평가하고 적격 후보 수가 채워지면 중단한다."""

    if not 1 <= eligible_count <= 10:
        raise ValueError("적격 후보 수는 1~10개여야 합니다.")
    if not eligible_count <= search_rows <= 50:
        raise ValueError("검색 후보 수는 적격 후보 수 이상, 최대 50개여야 합니다.")
    if max_detail_calls < eligible_count:
        raise ValueError("최대 상세 호출 수는 적격 후보 수 이상이어야 합니다.")
    if not 0 <= max_crowd_calls <= 3:
        raise ValueError("실시간 혼잡도 호출 수는 무료 한도에 맞춰 0~3이어야 합니다.")
    if not 0 <= max_seoul_crowd_calls <= 20:
        raise ValueError("서울시 혼잡도 최대 호출 수는 0~20이어야 합니다.")
    if not 0 <= max_seoul_transit_calls <= 10:
        raise ValueError("서울시 환승경로 최대 호출 수는 0~10이어야 합니다.")
    if not 0 <= max_car_route_calls <= 10:
        raise ValueError("TMAP 자동차 경로 최대 호출 수는 0~10이어야 합니다.")
    if next_schedule is not None:
        next_schedule.validate(trip.arrival_time)
        available = (
            next_schedule.arrival_deadline - trip.arrival_time
        ).total_seconds() / 60
        trip = replace(
            trip,
            minutes_until_locked_stop=available,
            schedule_buffer_minutes=next_schedule.buffer_minutes,
        )

    stats = stats or ApiOptimizationStats()
    stats.list_api_calls += 1
    response = client.location_based_list(
        map_x=map_x,
        map_y=map_y,
        radius=radius,
        content_type_id=content_type_id,
        arrange="E",
        num_of_rows=search_rows,
    )

    recommendations: list[RankedTourCandidate] = []
    excluded: list[RankedTourCandidate] = []
    skipped: list[str] = []
    seen_ids: set[str] = set()
    crowd_places: tuple[CrowdPlace, ...] | None = None
    crowd_disabled = crowd_client is None or max_crowd_calls == 0
    seoul_crowd_disabled = (
        seoul_crowd_client is None or max_seoul_crowd_calls == 0
    )
    seoul_areas = (
        () if seoul_crowd_disabled else load_seoul_crowd_areas()
    )
    seoul_transit_disabled = (
        seoul_transit_client is None or max_seoul_transit_calls == 0
    )
    car_route_disabled = (
        car_route_client is None or max_car_route_calls == 0
    )

    for item in response.items:
        if len(recommendations) >= eligible_count:
            break
        content_id = str(item.get("contentid") or "").strip()
        type_id = str(item.get("contenttypeid") or "").strip()
        title = str(item.get("title") or content_id or "제목 없음").strip()
        if not content_id or not type_id:
            skipped.append(f"{title}: 콘텐츠 ID 또는 타입 누락")
            continue
        if not is_recommendation_content_type(
            type_id,
            include_restaurants=include_restaurants,
        ):
            skipped.append(f"{title}: 추천 대상이 아닌 업종({type_id})")
            continue
        if content_id in seen_ids:
            stats.duplicates_skipped += 1
            continue
        if exclude_content_id and content_id == exclude_content_id:
            skipped.append(f"{title}: 대체 대상 장소 자신이라 제외")
            continue
        if exclude_longitude is not None and exclude_latitude is not None:
            try:
                item_lon = float(item.get("mapx"))
                item_lat = float(item.get("mapy"))
            except (TypeError, ValueError):
                item_lon = item_lat = None
            if item_lon is not None and item_lat is not None:
                if (
                    _straight_distance_meters(
                        exclude_longitude, exclude_latitude, item_lon, item_lat
                    )
                    <= exclude_radius_meters
                ):
                    skipped.append(f"{title}: 대체 대상 장소와 같은 위치라 제외")
                    continue
        seen_ids.add(content_id)
        stats.candidates_considered += 1

        cache_key = f"kto-detail:{content_id}:{type_id}"
        entry = cache.get(cache_key)
        cache_note: tuple[str, ...] = ()
        detail_source = "실시간 API"
        if entry and entry.is_fresh:
            stats.cache_hits += 1
            stats.detail_cache_hits += 1
            bundle = entry.value
            detail_source = "유효 캐시"
        else:
            if stats.detail_api_calls >= max_detail_calls:
                if entry:
                    stats.stale_cache_hits += 1
                    stats.detail_stale_cache_hits += 1
                    bundle = entry.value
                    cache_note = ("상세 API 한도 도달: 만료 캐시 사용",)
                    detail_source = "만료 캐시"
                else:
                    skipped.append(f"{title}: 최대 상세 API 호출 수 도달")
                    continue
            else:
                try:
                    stats.detail_api_calls += 1
                    bundle = client.detail_bundle(
                        content_id,
                        type_id,
                        include_images=False,
                    )
                    ttl = (
                        FESTIVAL_TTL_SECONDS
                        if type_id == "15"
                        else DETAIL_TTL_SECONDS
                    )
                    cache.set(cache_key, bundle, ttl_seconds=ttl)
                except (KTOApiError, ValueError) as exc:
                    stats.failures += 1
                    if entry:
                        stats.stale_cache_hits += 1
                        stats.detail_stale_cache_hits += 1
                        bundle = entry.value
                        cache_note = (f"상세 API 실패: 만료 캐시 사용 ({exc})",)
                        detail_source = "만료 캐시"
                    else:
                        skipped.append(f"{title}: {exc}")
                        continue

        try:
            place = normalize_place(bundle)
            facts, notes = facts_from_tourapi(item, place)
            if next_schedule is not None:
                facts = replace(
                    facts, visit_minutes=next_schedule.visit_minutes
                )
            route_source = "직선거리 추정"
            inbound_geometry: dict[str, Any] | None = None
            onward_geometry: dict[str, Any] | None = None
            crowd_source = "미연결"
            crowd_label: str | None = None
            crowd_raw_level: float | None = None
            crowd_scope = "미확인"
            schedule_feasibility: ScheduleFeasibility | None = None
            evaluation = evaluate_place_candidate(
                place, facts, trip, priorities
            )
            if (
                route_client is not None
                and _eligible_or_only_walking_limit(evaluation)
                and place.longitude is not None
                and place.latitude is not None
            ):
                try:
                    route, route_source = cached_walking_route(
                        route_client,
                        cache,
                        stats,
                        start_x=map_x,
                        start_y=map_y,
                        end_x=place.longitude,
                        end_y=place.latitude,
                        end_name=place.title or title,
                    )
                    inbound_geometry = route.geometry
                    facts = replace(
                        facts,
                        route_minutes=route.duration_minutes,
                        walking_meters=route.distance_meters,
                        walking_minutes=route.duration_minutes,
                        transport_mode="walking",
                    )
                    notes = tuple(
                        note
                        for note in notes
                        if "직선거리 기반 이동시간" not in note
                    ) + ("TMAP 실제 도보 경로 적용",)
                except TMapApiError as exc:
                    stats.route_failures += 1
                    notes += (f"TMAP 실패로 직선거리 추정 유지 ({exc})",)
                evaluation = evaluate_place_candidate(
                    place, facts, trip, priorities
                )
            elif next_schedule is not None and (
                place.longitude is None or place.latitude is None
            ):
                facts = replace(facts, onward_route_feasible=False)
                notes += ("후보 좌표 누락으로 다음 일정 경로 계산 불가",)
                evaluation = evaluate_place_candidate(
                    place, facts, trip, priorities
                )
            if (
                not car_route_disabled
                and set(evaluation.score.rejection_reasons)
                == {WALKING_LIMIT_REASON}
                and place.longitude is not None
                and place.latitude is not None
                and stats.car_route_api_calls < max_car_route_calls
            ):
                try:
                    car_route, car_source = cached_car_route(
                        car_route_client,
                        cache,
                        stats,
                        start_x=map_x,
                        start_y=map_y,
                        end_x=place.longitude,
                        end_y=place.latitude,
                        end_name=place.title or title,
                    )
                    if car_route.duration_minutes <= trip.max_transport_minutes:
                        route_source = car_source
                        inbound_geometry = car_route.geometry
                        facts = replace(
                            facts,
                            route_minutes=car_route.duration_minutes,
                            walking_meters=0.0,
                            walking_minutes=None,
                            transport_mode="car",
                        )
                        notes += ("도보 15분 이상으로 TMAP 자동차 경로 적용",)
                    else:
                        notes += (
                            "TMAP 자동차 경로가 30분 한도를 초과해 미적용",
                        )
                except TMapCarApiError as exc:
                    stats.car_route_failures += 1
                    notes += (f"TMAP 자동차 경로 조회 실패 ({exc})",)
                evaluation = evaluate_place_candidate(
                    place, facts, trip, priorities
                )
            if (
                not seoul_transit_disabled
                and set(evaluation.score.rejection_reasons)
                == {WALKING_LIMIT_REASON}
                and place.longitude is not None
                and place.latitude is not None
                and stats.seoul_transit_api_calls < max_seoul_transit_calls
            ):
                try:
                    transit, route_source = cached_seoul_transit_route(
                        seoul_transit_client,
                        cache,
                        stats,
                        start_x=map_x,
                        start_y=map_y,
                        end_x=place.longitude,
                        end_y=place.latitude,
                    )
                    inbound_geometry = transit.geometry
                    facts = replace(
                        facts,
                        route_minutes=transit.duration_minutes,
                        walking_minutes=transit.walking_minutes,
                        walking_meters=(
                            transit.walking_distance_meters
                            if transit.walking_distance_meters is not None
                            else facts.walking_meters
                        ),
                        transport_mode="transit",
                    )
                    notes += (
                        "도보 15분 이상으로 서울시 대중교통 환승경로 적용",
                    )
                    if transit.walking_minutes is None:
                        notes += (
                            "환승경로 내 도보시간 미제공으로 도보 제한 판단 보류",
                        )
                except SeoulTransitApiError as exc:
                    stats.seoul_transit_failures += 1
                    notes += (f"서울시 환승경로 조회 실패 ({exc})",)
                evaluation = evaluate_place_candidate(
                    place, facts, trip, priorities
                )
            if (
                not seoul_crowd_disabled
                and evaluation.score.eligible
                and place.longitude is not None
                and place.latitude is not None
            ):
                area = find_seoul_crowd_area(
                    place.longitude, place.latitude, seoul_areas
                )
                if area is None:
                    stats.seoul_crowd_unmatched += 1
                    crowd_source = "서울시 혼잡도 미지원 구역"
                elif stats.seoul_crowd_api_calls >= max_seoul_crowd_calls:
                    target_key = trip.arrival_time.strftime("%Y%m%d%H")
                    cached = cache.get(
                        f"seoul-crowd:{area.area_code}:{target_key}"
                    )
                    if cached and cached.is_fresh:
                        stats.cache_hits += 1
                        stats.seoul_crowd_cache_hits += 1
                        snapshot = _seoul_crowd_from_dict(cached.value)
                        facts = replace(
                            facts,
                            crowd_level=scoring_area_crowd_level(
                                snapshot.normalized_level
                            ),
                        )
                        crowd_label = snapshot.congestion_label
                        crowd_raw_level = snapshot.normalized_level
                        crowd_scope = "영역"
                        crowd_source = "유효 서울시 주변 지역 혼잡도 캐시"
                        notes += (SEOUL_AREA_CROWD_NOTICE,)
                    else:
                        crowd_source = "서울시 혼잡도 호출 보호"
                else:
                    try:
                        snapshot, crowd_source = cached_seoul_crowd(
                            seoul_crowd_client,
                            cache,
                            stats,
                            area_code=area.area_code,
                            target_time=trip.arrival_time,
                        )
                        facts = replace(
                            facts,
                            crowd_level=scoring_area_crowd_level(
                                snapshot.normalized_level
                            ),
                        )
                        crowd_label = snapshot.congestion_label
                        crowd_raw_level = snapshot.normalized_level
                        crowd_scope = "영역"
                        notes += (SEOUL_AREA_CROWD_NOTICE,)
                    except SeoulCrowdApiError as exc:
                        stats.seoul_crowd_failures += 1
                        seoul_crowd_disabled = True
                        crowd_source = "서울시 주변 지역 혼잡도 실패·중립값"
                        notes += (f"서울시 혼잡도 조회 실패 ({exc})",)
                evaluation = evaluate_place_candidate(
                    place, facts, trip, priorities
                )
            if (
                facts.crowd_level is None
                and not crowd_disabled
                and evaluation.score.eligible
            ):
                try:
                    if crowd_places is None:
                        crowd_places = cached_crowd_places(
                            crowd_client, cache, stats
                        )
                    matched = match_crowd_place(place.title or title, crowd_places)
                    if matched is None:
                        stats.crowd_unmatched += 1
                        crowd_source = "SK 혼잡도 미지원 장소"
                    elif stats.crowd_realtime_api_calls >= max_crowd_calls:
                        cached = cache.get(f"sk-crowd:realtime:{matched.poi_id}")
                        if cached and cached.is_fresh:
                            stats.cache_hits += 1
                            stats.crowd_realtime_cache_hits += 1
                            snapshot = _crowd_from_dict(cached.value)
                            facts = replace(
                                facts, crowd_level=snapshot.normalized_level
                            )
                            crowd_source = "유효 SK 혼잡도 캐시"
                        else:
                            crowd_source = "SK 혼잡도 일일 호출 보호"
                    else:
                        snapshot, crowd_source = cached_realtime_crowd(
                            crowd_client,
                            cache,
                            stats,
                            poi_id=matched.poi_id,
                        )
                        facts = replace(
                            facts, crowd_level=snapshot.normalized_level
                        )
                    evaluation = evaluate_place_candidate(
                        place, facts, trip, priorities
                    )
                except SKCrowdApiError as exc:
                    stats.crowd_failures += 1
                    crowd_disabled = True
                    crowd_source = "SK 혼잡도 실패·중립값"
                    notes += (f"SK 혼잡도 조회 실패 ({exc})",)

            if (
                next_schedule is not None
                and evaluation.score.eligible
                and place.longitude is not None
                and place.latitude is not None
            ):
                onward_distance = _straight_distance_meters(
                    place.longitude,
                    place.latitude,
                    next_schedule.longitude,
                    next_schedule.latitude,
                )
                onward_minutes = _estimated_walking_minutes(onward_distance)
                onward_mode = "walking"
                onward_source = "직선거리 기반 다음 일정 도보 추정"
                onward_geometry = {
                    "type": "LineString",
                    "coordinates": [
                        [place.longitude, place.latitude],
                        [next_schedule.longitude, next_schedule.latitude],
                    ],
                }
                route_policy_ok = onward_minutes < trip.max_walking_minutes

                if route_client is not None:
                    try:
                        onward_walk, onward_source = cached_walking_route(
                            route_client,
                            cache,
                            stats,
                            start_x=place.longitude,
                            start_y=place.latitude,
                            end_x=next_schedule.longitude,
                            end_y=next_schedule.latitude,
                            end_name=next_schedule.title,
                        )
                        onward_minutes = onward_walk.duration_minutes
                        onward_geometry = onward_walk.geometry
                        route_policy_ok = (
                            onward_minutes < trip.max_walking_minutes
                        )
                    except TMapApiError as exc:
                        stats.route_failures += 1
                        notes += (
                            f"다음 일정 TMAP 실패로 직선거리 추정 유지 ({exc})",
                        )

                if (
                    not route_policy_ok
                    and not car_route_disabled
                    and stats.car_route_api_calls < max_car_route_calls
                ):
                    try:
                        onward_car, onward_car_source = cached_car_route(
                            car_route_client,
                            cache,
                            stats,
                            start_x=place.longitude,
                            start_y=place.latitude,
                            end_x=next_schedule.longitude,
                            end_y=next_schedule.latitude,
                            end_name=next_schedule.title,
                        )
                        if onward_car.duration_minutes <= trip.max_transport_minutes:
                            onward_minutes = onward_car.duration_minutes
                            onward_mode = "car"
                            onward_source = onward_car_source
                            onward_geometry = onward_car.geometry
                            route_policy_ok = True
                        else:
                            notes += (
                                "다음 일정 자동차 경로가 30분 한도를 초과함",
                            )
                    except TMapCarApiError as exc:
                        stats.car_route_failures += 1
                        notes += (f"다음 일정 자동차 경로 실패 ({exc})",)

                if (
                    not route_policy_ok
                    and not seoul_transit_disabled
                    and stats.seoul_transit_api_calls < max_seoul_transit_calls
                ):
                    try:
                        onward_transit, onward_source = cached_seoul_transit_route(
                            seoul_transit_client,
                            cache,
                            stats,
                            start_x=place.longitude,
                            start_y=place.latitude,
                            end_x=next_schedule.longitude,
                            end_y=next_schedule.latitude,
                        )
                        onward_minutes = onward_transit.duration_minutes
                        onward_mode = "transit"
                        onward_geometry = onward_transit.geometry
                        route_policy_ok = (
                            onward_minutes <= trip.max_transport_minutes
                            and (
                                onward_transit.walking_minutes is None
                                or onward_transit.walking_minutes
                                < trip.max_walking_minutes
                            )
                        )
                    except SeoulTransitApiError as exc:
                        stats.seoul_transit_failures += 1
                        notes += (f"다음 일정 서울시 환승경로 실패 ({exc})",)

                facts = replace(
                    facts,
                    visit_minutes=next_schedule.visit_minutes,
                    onward_minutes=onward_minutes,
                    onward_route_feasible=route_policy_ok,
                )
                schedule_feasibility = evaluate_schedule_feasibility(
                    start_time=trip.arrival_time,
                    constraint=next_schedule,
                    inbound_minutes=facts.route_minutes,
                    onward_minutes=onward_minutes,
                    onward_mode=onward_mode,
                    onward_source=onward_source,
                    route_policy_ok=route_policy_ok,
                )
                notes += (
                    "후보 방문 후 다음 고정 일정 도착 가능성 계산",
                )
                evaluation = evaluate_place_candidate(
                    place, facts, trip, priorities
                )
        except ValueError as exc:
            stats.failures += 1
            skipped.append(f"{title}: {exc}")
            continue

        distance = item.get("dist")
        try:
            distance_meters = float(distance)
        except (TypeError, ValueError):
            distance_meters = None
        candidate = apply_confidence_penalty(RankedTourCandidate(
            title=place.title or title,
            content_id=content_id,
            content_type_id=type_id,
            distance_meters=distance_meters,
            place=place,
            facts=facts,
            evaluation=evaluation,
            estimation_notes=notes + cache_note,
            detail_source=detail_source,
            route_source=route_source,
            inbound_route_geometry=inbound_geometry,
            onward_route_geometry=onward_geometry,
            crowd_source=crowd_source,
            crowd_label=crowd_label,
            crowd_raw_level=crowd_raw_level,
            crowd_scope=crowd_scope,
            schedule_feasibility=schedule_feasibility,
            cat1=str(item.get("cat1") or ""),
            cat2=str(item.get("cat2") or ""),
            cat3=str(item.get("cat3") or ""),
            lcls1=str(item.get("lclsSystm1") or ""),
            lcls2=str(item.get("lclsSystm2") or ""),
            lcls3=str(item.get("lclsSystm3") or ""),
        ))
        if evaluation.score.eligible:
            recommendations.append(candidate)
        else:
            excluded.append(candidate)

    def _sort_key(candidate: RankedTourCandidate) -> tuple[bool, float]:
        # cat2(구 분류체계)가 없는 콘텐츠도 lclsSystm2(신규 분류체계)는
        # 채워진 경우가 많아서, 둘 중 하나라도 일치하면 우선한다.
        matches_category = (
            bool(preferred_cat2) and candidate.cat2 == preferred_cat2
        ) or (
            bool(preferred_lcls2) and candidate.lcls2 == preferred_lcls2
        )
        return (matches_category, candidate.evaluation.score.total_score)

    recommendations.sort(key=_sort_key, reverse=True)
    return OptimizedRecommendationResult(
        recommendations=tuple(recommendations),
        excluded=tuple(excluded),
        skipped=tuple(skipped),
        stats=stats,
    )
