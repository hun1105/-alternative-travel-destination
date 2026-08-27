"""공사 OpenAPI 연결 확인용 명령행 도구."""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .kto_client import KTOApiError, KTOClient, KTOResponse
from .normalizer import normalize_place
from .place_search_client import TMapPlaceSearchClient, TMapPlaceSearchError
from .api_cache import SQLiteTTLCache
from .car_route_client import TMapCarApiError, TMapCarClient
from .crowd_client import (
    SKCrowdApiError,
    SKCrowdClient,
    match_crowd_place,
)
from .optimized_recommender import (
    ApiOptimizationStats,
    OptimizedRecommendationResult,
    cached_crowd_places,
    cached_car_route,
    cached_realtime_crowd,
    cached_seoul_crowd,
    cached_seoul_transit_route,
    cached_weather_forecast,
    enrich_seoul_transit_walk_geometry,
    recommend_nearby_optimized,
)
from .priority_prompt import prompt_user_priorities
from .scoring import LABELS
from .recommender import (
    NearbyRecommendationResult,
    build_candidate_evidence,
    recommend_nearby,
)
from .signal_builder import TripContext
from .schedule_feasibility import NextScheduleConstraint
from .route_client import TMapApiError, TMapPedestrianClient
from .seoul_crowd_client import (
    SEOUL_AREA_CROWD_INFLUENCE,
    SEOUL_AREA_CROWD_NOTICE,
    SeoulCrowdApiError,
    SeoulCrowdClient,
    find_seoul_crowd_area,
    find_seoul_crowd_area_by_name,
)
from .seoul_transit_client import (
    SeoulTransitApiError,
    SeoulTransitClient,
)
from .weather_client import KMAApiError, KMAClient, WeatherSnapshot
from .trip_plan import validate_trip_plan
from .trip_store import TripNotFoundError
from .api_service import PlanBApiService


def _print_result(result: KTOResponse | dict[str, Any]) -> None:
    data = asdict(result) if isinstance(result, KTOResponse) else result
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _print_recommendations(result: NearbyRecommendationResult) -> None:
    if not result.candidates:
        print("평가 가능한 실제 후보가 없습니다.")
    for rank, candidate in enumerate(result.candidates, start=1):
        score = candidate.evaluation.score
        build = candidate.evaluation.build
        distance = (
            f"{candidate.distance_meters:.0f}m"
            if candidate.distance_meters is not None
            else "정보 없음"
        )
        cost = (
            f"{build.estimated_group_cost_krw:,}원"
            if build.estimated_group_cost_krw is not None
            else "정보 없음"
        )
        print(f"\n[{rank}위] {candidate.title}")
        print(f"콘텐츠 ID/타입: {candidate.content_id}/{candidate.content_type_id}")
        print(f"직선거리: {distance}")
        if candidate.facts.transport_mode == "transit":
            walk = (
                f", 환승도보 {candidate.facts.walking_minutes:.1f}분"
                if candidate.facts.walking_minutes is not None
                else ""
            )
            print(
                f"서울시 대중교통 경로: {candidate.facts.route_minutes:.1f}분{walk}"
            )
        elif candidate.facts.transport_mode == "car":
            print(f"TMAP 자동차 경로: {candidate.facts.route_minutes:.1f}분")
        elif candidate.route_source != "직선거리 추정":
            print(
                f"실제 도보 경로: {candidate.facts.walking_meters:.0f}m"
                f" / {candidate.facts.route_minutes:.1f}분"
            )
        schedule = candidate.schedule_feasibility
        if schedule is not None:
            print(
                f"다음 일정: {schedule.next_schedule_title} / "
                f"마감 {schedule.deadline:%Y-%m-%d %H:%M}"
            )
            print(
                f"다음 일정 경로: {schedule.onward_mode} "
                f"{schedule.onward_minutes:.1f}분 / {schedule.onward_source}"
            )
            print(
                f"예상 도착: {schedule.estimated_next_arrival:%Y-%m-%d %H:%M} "
                f"/ 여유 {schedule.slack_minutes:.1f}분"
            )
        print(f"예상 총비용: {cost}")
        if candidate.place.event_start_date:
            period = str(candidate.place.event_start_date)
            if candidate.place.event_end_date:
                period += f"~{candidate.place.event_end_date}"
            print(f"행사 기간: {period}")
        if candidate.place.session_times:
            print(f"운영 회차: {', '.join(candidate.place.session_times)}")
        if candidate.place.reservation_required is not None:
            reservation = (
                "필요" if candidate.place.reservation_required else "불필요"
            )
            print(f"사전예약: {reservation}")
        print(f"적격 여부: {score.eligible}")
        if candidate.base_score is not None:
            print(f"기본 점수: {candidate.base_score}")
            print(f"신뢰도 감점: -{candidate.confidence_penalty}")
        print(f"최종 점수: {score.total_score}")
        evidence = build_candidate_evidence(candidate)
        print(
            f"판단 신뢰도: {evidence.confidence_percent}%"
            f" ({evidence.confidence_level})"
        )
        print(f"운영 확인: {evidence.operation_status}")
        print(f"상세정보 출처: {evidence.detail_source}")
        print(f"경로 출처: {evidence.route_source}")
        print(f"혼잡도 출처: {evidence.crowd_source}")
        if candidate.crowd_scope == "영역":
            print(f"주변 지역 혼잡도: {candidate.crowd_label}")
            print(
                "점수 반영: 영역 혼잡도 "
                f"{SEOUL_AREA_CROWD_INFLUENCE:.0%} + 중립값 "
                f"{1 - SEOUL_AREA_CROWD_INFLUENCE:.0%}"
            )
            print(f"혼잡도 주의: {SEOUL_AREA_CROWD_NOTICE}")
        elif candidate.facts.crowd_level is not None:
            crowd_level = round(candidate.facts.crowd_level * 3) + 1
            crowd_label = {1: "여유", 2: "보통", 3: "혼잡", 4: "매우 혼잡"}[
                crowd_level
            ]
            print(f"장소 혼잡도: {crowd_level}단계 ({crowd_label})")
        print(f"실제 데이터: {', '.join(evidence.actual_data)}")
        print(f"추정 데이터: {', '.join(evidence.estimated_data)}")
        if evidence.neutral_data:
            print(f"중립 처리: {', '.join(evidence.neutral_data)}")
        if evidence.excluded_data:
            print(f"점수 제외: {', '.join(evidence.excluded_data)}")
        if candidate.place.warnings:
            print(f"정규화 경고: {'; '.join(candidate.place.warnings)}")
        if score.reasons:
            print(f"주요 이유: {', '.join(score.reasons)}")
        contributions = sorted(
            score.contributions.items(),
            key=lambda item: item[1],
            reverse=True,
        )
        contribution_text = [
            f"{LABELS[name]} {value:.2f}점"
            for name, value in contributions
            if score.weights.get(name, 0) > 0
        ]
        if contribution_text:
            print(f"기본 점수 기여: {', '.join(contribution_text)}")
        if score.rejection_reasons:
            print(f"제외 이유: {', '.join(score.rejection_reasons)}")
        notes = candidate.estimation_notes + build.notes
        if notes:
            print(f"추정·누락: {'; '.join(notes)}")
    if result.skipped:
        print("\n[조회 제외]")
        for reason in result.skipped:
            print(f"- {reason}")


def _print_weather(weather: WeatherSnapshot) -> None:
    print("\n[자동 날씨]")
    print(f"예보 시각: {weather.forecast_time}")
    print(f"상태: {weather.summary}")
    print(f"기온: {weather.temperature_c}℃")
    print(f"강수확률: {weather.precipitation_probability}%")
    print(f"예상 강수량: {weather.precipitation_mm}mm")
    print(f"습도: {weather.humidity_percent}%")
    print(f"풍속: {weather.wind_speed_mps}m/s")
    print(f"날씨 위험도: {weather.severity}")


def _print_optimized(result: OptimizedRecommendationResult) -> None:
    _print_recommendations(
        NearbyRecommendationResult(
            candidates=result.recommendations,
            skipped=result.skipped,
        )
    )
    if result.excluded:
        print("\n[필수조건 제외 후보]")
        for candidate in result.excluded:
            reasons = ", ".join(candidate.evaluation.score.rejection_reasons)
            print(f"- {candidate.title}: {reasons}")
    stats = result.stats
    print("\n[API 최적화 통계]")
    print(f"실제 API 호출: {stats.total_api_calls}회")
    print(f"목록 API: {stats.list_api_calls}회")
    print(f"상세 API: {stats.detail_api_calls}회")
    print(f"날씨 API: {stats.weather_api_calls}회")
    print(f"보행 경로 API: {stats.route_api_calls}회")
    print(f"자동차 경로 API: {stats.car_route_api_calls}회")
    print(f"혼잡도 장소 목록 API: {stats.crowd_place_api_calls}회")
    print(f"실시간 혼잡도 API: {stats.crowd_realtime_api_calls}회")
    print(f"서울시 혼잡도 API: {stats.seoul_crowd_api_calls}회")
    print(f"서울시 환승경로 API: {stats.seoul_transit_api_calls}회")
    print(f"캐시 적중: {stats.cache_hits}회")
    print(f"상세 캐시 적중: {stats.detail_cache_hits}회")
    print(f"날씨 캐시 적중: {stats.weather_cache_hits}회")
    print(f"보행 경로 캐시 적중: {stats.route_cache_hits}회")
    print(f"자동차 경로 캐시 적중: {stats.car_route_cache_hits}회")
    print(f"혼잡도 장소 캐시 적중: {stats.crowd_place_cache_hits}회")
    print(f"실시간 혼잡도 캐시 적중: {stats.crowd_realtime_cache_hits}회")
    print(f"서울시 혼잡도 캐시 적중: {stats.seoul_crowd_cache_hits}회")
    print(f"서울시 환승경로 캐시 적중: {stats.seoul_transit_cache_hits}회")
    print(f"만료 캐시 복구: {stats.stale_cache_hits}회")
    print(f"상세 만료 캐시: {stats.detail_stale_cache_hits}회")
    print(f"날씨 만료 캐시: {stats.weather_stale_cache_hits}회")
    print(f"보행 경로 만료 캐시: {stats.route_stale_cache_hits}회")
    print(f"보행 경로 대체 처리: {stats.route_failures}회")
    print(f"자동차 경로 대체 처리: {stats.car_route_failures}회")
    print(f"혼잡도 대체 처리: {stats.crowd_failures}회")
    print(f"혼잡도 미지원 장소: {stats.crowd_unmatched}개")
    print(f"서울시 혼잡도 대체 처리: {stats.seoul_crowd_failures}회")
    print(f"서울시 미지원 구역: {stats.seoul_crowd_unmatched}개")
    print(f"서울시 환승경로 실패: {stats.seoul_transit_failures}회")
    print(f"평가 후보: {stats.candidates_considered}개")
    print(f"중복 생략: {stats.duplicates_skipped}개")
    print(f"실패: {stats.failures}개")


def load_env_file(path: str = ".env") -> None:
    """외부 패키지 없이 단순 KEY=VALUE 형식의 .env를 읽는다."""

    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Plan B 관광공사 API 호출 도구")
    subparsers = parser.add_subparsers(dest="command", required=True)

    area = subparsers.add_parser("areas", help="지역·시군구 코드 조회")
    area.add_argument("--area-code")

    keyword = subparsers.add_parser("keyword", help="관광지 키워드 검색")
    keyword.add_argument("query")
    keyword.add_argument("--area-code")
    keyword.add_argument("--content-type-id")
    keyword.add_argument("--rows", type=int, default=10)

    nearby = subparsers.add_parser("nearby", help="현재 위치 주변 관광지 검색")
    nearby.add_argument("--x", type=float, required=True, help="경도")
    nearby.add_argument("--y", type=float, required=True, help="위도")
    nearby.add_argument("--radius", type=int, default=2000)
    nearby.add_argument("--content-type-id")
    nearby.add_argument("--rows", type=int, default=10)

    festival = subparsers.add_parser("festivals", help="기간 내 축제 검색")
    festival.add_argument("--start", required=True, help="YYYYMMDD")
    festival.add_argument("--end", help="YYYYMMDD")
    festival.add_argument("--area-code")
    festival.add_argument("--rows", type=int, default=10)

    detail = subparsers.add_parser("detail", help="관광지 상세 묶음 조회")
    detail.add_argument("content_id")
    detail.add_argument("content_type_id")
    detail.add_argument("--without-info", action="store_true")
    detail.add_argument("--without-images", action="store_true")

    normalize = subparsers.add_parser(
        "normalize",
        help="관광지 상세정보를 복구 엔진용 데이터로 정규화",
    )
    normalize.add_argument("content_id")
    normalize.add_argument("content_type_id")

    place_search = subparsers.add_parser(
        "place-search",
        help="TMAP 지도 선택용 장소 검색",
    )
    place_search.add_argument("query")
    place_search.add_argument("--center-x", type=float)
    place_search.add_argument("--center-y", type=float)
    place_search.add_argument("--radius-km", type=int, default=20)
    place_search.add_argument("--count", type=int, default=10)

    validate_plan = subparsers.add_parser(
        "validate-trip-plan",
        help="여행 계획 JSON 파일 검증",
    )
    validate_plan.add_argument("json_file")

    create_plan = subparsers.add_parser(
        "create-trip-plan",
        help="여행 계획 JSON을 저장하고 trip_id 발급",
    )
    create_plan.add_argument("json_file")
    create_plan.add_argument("--cache-db", default=".cache/plan_b_api.sqlite3")

    get_plan = subparsers.add_parser(
        "get-trip-plan",
        help="trip_id로 저장된 여행 계획 조회",
    )
    get_plan.add_argument("--trip-id", required=True)
    get_plan.add_argument("--cache-db", default=".cache/plan_b_api.sqlite3")

    replace_schedule = subparsers.add_parser(
        "replace-trip-schedule",
        help="저장된 계획의 일정 하나를 새 후보로 교체",
    )
    replace_schedule.add_argument("--trip-id", required=True)
    replace_schedule.add_argument(
        "json_file", help="item_id·place·visit_minutes를 담은 JSON 파일"
    )
    replace_schedule.add_argument(
        "--cache-db", default=".cache/plan_b_api.sqlite3"
    )

    recommend = subparsers.add_parser(
        "recommend-nearby",
        help="실제 주변 관광지를 조회해 사용자 우선순위로 추천",
    )
    recommend.add_argument("--x", type=float, required=True, help="현재 경도")
    recommend.add_argument("--y", type=float, required=True, help="현재 위도")
    recommend.add_argument("--radius", type=int, default=3000)
    recommend.add_argument("--rows", type=int, default=3)
    recommend.add_argument("--content-type-id")
    recommend.add_argument("--arrival", help="도착시각 ISO 형식")
    recommend.add_argument(
        "--weather-severity",
        type=float,
        help="0~1 수동값. 생략하면 기상청 단기예보 자동 조회",
    )
    recommend.add_argument("--budget", type=int, help=argparse.SUPPRESS)
    recommend.add_argument("--party-size", type=int, default=1)
    recommend.add_argument("--children", type=int, default=0)
    recommend.add_argument(
        "--max-route-minutes", type=float, default=30.0,
        help="이동시간 점수 기준이며 필수 제한은 아님",
    )
    recommend.add_argument(
        "--max-walking-minutes", type=float, default=15.0,
        help="도보 강제 제한 시간(기본 15분)",
    )
    recommend.add_argument(
        "--max-transport-minutes", type=float, default=30.0,
        help="대중교통 이용 시 강제 제한 시간(기본 30분)",
    )
    recommend.add_argument(
        "--max-walking-meters", type=float, default=3000.0,
        help="보행 부담 점수 기준이며 필수 제한은 아님",
    )
    recommend.add_argument(
        "--budget-is-hard", action="store_true", help=argparse.SUPPRESS
    )
    recommend.add_argument(
        "--include-restaurants",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    optimized = subparsers.add_parser(
        "recommend-nearby-optimized",
        help="캐시와 점진 조회로 적격 주변 후보를 추천",
    )
    optimized.add_argument("--x", type=float, required=True, help="현재 경도")
    optimized.add_argument("--y", type=float, required=True, help="현재 위도")
    optimized.add_argument("--radius", type=int, default=3000)
    optimized.add_argument("--search-rows", type=int, default=20)
    optimized.add_argument("--eligible-count", type=int, default=3)
    optimized.add_argument("--max-detail-calls", type=int, default=12)
    optimized.add_argument(
        "--cache-db",
        default=".cache/plan_b_api.sqlite3",
    )
    optimized.add_argument("--content-type-id")
    optimized.add_argument("--arrival", help="추천 시작시각 ISO 형식")
    optimized.add_argument(
        "--weather-severity",
        type=float,
        help="0~1 수동값. 생략하면 캐시된 기상청 예보 사용",
    )
    optimized.add_argument("--budget", type=int, help=argparse.SUPPRESS)
    optimized.add_argument("--party-size", type=int, default=1)
    optimized.add_argument("--children", type=int, default=0)
    optimized.add_argument(
        "--max-route-minutes", type=float, default=30.0,
        help="이동시간 점수 기준이며 필수 제한은 아님",
    )
    optimized.add_argument(
        "--max-walking-minutes", type=float, default=15.0,
        help="도보 강제 제한 시간(기본 15분)",
    )
    optimized.add_argument(
        "--max-transport-minutes", type=float, default=30.0,
        help="서울시 대중교통 강제 제한 시간(기본 30분)",
    )
    optimized.add_argument(
        "--max-walking-meters", type=float, default=3000.0,
        help="보행 부담 점수 기준이며 필수 제한은 아님",
    )
    optimized.add_argument(
        "--budget-is-hard", action="store_true", help=argparse.SUPPRESS
    )
    optimized.add_argument(
        "--include-restaurants",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    optimized.add_argument(
        "--route-mode",
        choices=("auto", "tmap", "estimated"),
        default="auto",
        help="auto는 TMAP 키가 있으면 실제 보행 경로 사용",
    )
    optimized.add_argument(
        "--car-mode",
        choices=("auto", "tmap", "off"),
        default="auto",
        help="도보 15분 이상 시 TMAP 자동차 경로 사용",
    )
    optimized.add_argument(
        "--max-car-route-calls",
        type=int,
        default=5,
        help="추천 1회 자동차 경로 최대 호출 수(0~10)",
    )
    optimized.add_argument(
        "--crowd-mode",
        choices=("auto", "seoul", "off"),
        default="auto",
        help="auto는 서울시 영역 혼잡도를 사용하고 미지원 지역은 중립 처리",
    )
    optimized.add_argument(
        "--max-crowd-calls",
        type=int,
        default=3,
        help=argparse.SUPPRESS,
    )
    optimized.add_argument(
        "--max-seoul-crowd-calls",
        type=int,
        default=10,
        help="서울시 혼잡도 최대 호출 수(0~20)",
    )
    optimized.add_argument(
        "--transit-mode",
        choices=("auto", "seoul", "off"),
        default="auto",
        help="도보 15분 이상 시 서울시 버스·지하철 환승경로 사용",
    )
    optimized.add_argument(
        "--max-seoul-transit-calls",
        type=int,
        default=3,
        help="추천 1회 서울시 환승경로 최대 호출 수(0~10)",
    )
    optimized.add_argument("--next-x", type=float, help="다음 일정 경도")
    optimized.add_argument("--next-y", type=float, help="다음 일정 위도")
    optimized.add_argument(
        "--next-arrival", help="다음 일정 도착 마감시각 ISO 형식"
    )
    optimized.add_argument("--next-title", default="다음 일정")
    optimized.add_argument(
        "--visit-minutes", type=float, default=60.0,
        help="대체 후보 예상 체류시간(기본 60분)",
    )
    optimized.add_argument(
        "--schedule-buffer-minutes", type=float, default=10.0,
        help="다음 일정 도착 안전 여유시간(기본 10분)",
    )

    weather = subparsers.add_parser(
        "weather",
        help="기상청 단기예보와 자동 날씨 위험도 조회",
    )
    weather.add_argument("--x", type=float, required=True, help="경도")
    weather.add_argument("--y", type=float, required=True, help="위도")
    weather.add_argument("--at", help="예보 대상시각 ISO 형식")

    route = subparsers.add_parser(
        "walking-route",
        help="TMAP 실제 보행 거리와 소요시간 조회",
    )
    route.add_argument("--start-x", type=float, required=True)
    route.add_argument("--start-y", type=float, required=True)
    route.add_argument("--end-x", type=float, required=True)
    route.add_argument("--end-y", type=float, required=True)
    route.add_argument("--end-name", default="목적지")

    car_route = subparsers.add_parser(
        "car-route",
        help="TMAP 자동차 경로 조회",
    )
    car_route.add_argument("--start-x", type=float, required=True)
    car_route.add_argument("--start-y", type=float, required=True)
    car_route.add_argument("--end-x", type=float, required=True)
    car_route.add_argument("--end-y", type=float, required=True)
    car_route.add_argument("--end-name", default="목적지")
    car_route.add_argument("--cache-db", default=".cache/plan_b_api.sqlite3")

    crowd = subparsers.add_parser(
        "crowd",
        help="SK 실시간 장소 혼잡도 조회",
    )
    target = crowd.add_mutually_exclusive_group(required=True)
    target.add_argument("--poi-id")
    target.add_argument("--name", help="SK 지원 장소의 정확한 이름")
    crowd.add_argument("--cache-db", default=".cache/plan_b_api.sqlite3")

    seoul_crowd = subparsers.add_parser(
        "seoul-crowd",
        help="서울시 실시간·예측 구역 혼잡도 조회",
    )
    seoul_crowd.add_argument("--area", help="서울시 장소명 또는 AREA_CD")
    seoul_crowd.add_argument("--x", type=float, help="경도")
    seoul_crowd.add_argument("--y", type=float, help="위도")
    seoul_crowd.add_argument("--at", help="도착시각 ISO 형식")
    seoul_crowd.add_argument(
        "--cache-db", default=".cache/plan_b_api.sqlite3"
    )

    seoul_transit = subparsers.add_parser(
        "seoul-transit-route",
        help="서울시 버스·지하철 환승경로 조회",
    )
    seoul_transit.add_argument("--start-x", type=float, required=True)
    seoul_transit.add_argument("--start-y", type=float, required=True)
    seoul_transit.add_argument("--end-x", type=float, required=True)
    seoul_transit.add_argument("--end-y", type=float, required=True)
    seoul_transit.add_argument(
        "--cache-db", default=".cache/plan_b_api.sqlite3"
    )

    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    load_env_file()
    args = build_parser().parse_args()

    try:
        if args.command == "place-search":
            result = TMapPlaceSearchClient.from_env().search(
                args.query,
                count=args.count,
                center_x=args.center_x,
                center_y=args.center_y,
                radius_km=args.radius_km,
            )
            _print_result({
                "query": result.query,
                "total_count": result.total_count,
                "items": [item.selection_payload() for item in result.items],
            })
            return

        if args.command == "validate-trip-plan":
            path = Path(args.json_file)
            body = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(body, dict):
                raise ValueError("여행 계획 JSON은 객체여야 합니다.")
            _print_result({"valid": True, "plan": validate_trip_plan(body)})
            return

        if args.command == "create-trip-plan":
            path = Path(args.json_file)
            body = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(body, dict):
                raise ValueError("여행 계획 JSON은 객체여야 합니다.")
            service = PlanBApiService(args.cache_db)
            _print_result(service.create_trip_plan(body))
            return

        if args.command == "get-trip-plan":
            service = PlanBApiService(args.cache_db)
            try:
                _print_result(service.get_trip_plan(args.trip_id))
            except TripNotFoundError as exc:
                raise ValueError(f"저장된 여행 계획을 찾지 못했습니다: {exc}") from exc
            return

        if args.command == "replace-trip-schedule":
            path = Path(args.json_file)
            body = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(body, dict):
                raise ValueError("교체 요청 JSON은 객체여야 합니다.")
            service = PlanBApiService(args.cache_db)
            try:
                _print_result(service.replace_trip_schedule(args.trip_id, body))
            except TripNotFoundError as exc:
                raise ValueError(f"저장된 여행 계획을 찾지 못했습니다: {exc}") from exc
            return

        if args.command == "weather":
            target = (
                datetime.fromisoformat(args.at)
                if args.at
                else datetime.now()
            )
            with KMAClient.from_env() as weather_client:
                weather = weather_client.village_forecast(
                    latitude=args.y,
                    longitude=args.x,
                    target_time=target,
                )
            _print_weather(weather)
            return

        if args.command == "walking-route":
            route = TMapPedestrianClient.from_env().pedestrian_route(
                start_x=args.start_x,
                start_y=args.start_y,
                end_x=args.end_x,
                end_y=args.end_y,
                end_name=args.end_name,
            )
            _print_result({
                "distance_meters": route.distance_meters,
                "duration_seconds": route.duration_seconds,
                "duration_minutes": round(route.duration_minutes, 1),
                "geometry": route.geometry,
            })
            return

        if args.command == "crowd":
            crowd_client = SKCrowdClient.from_env()
            crowd_cache = SQLiteTTLCache(args.cache_db)
            crowd_stats = ApiOptimizationStats()
            poi_id = args.poi_id
            if not poi_id:
                places = cached_crowd_places(
                    crowd_client, crowd_cache, crowd_stats
                )
                matched = match_crowd_place(args.name, places)
                if matched is None:
                    raise ValueError(
                        "SK 지원 장소 목록에서 정확히 일치하는 이름을 찾지 못했습니다."
                    )
                poi_id = matched.poi_id
            snapshot, source = cached_realtime_crowd(
                crowd_client, crowd_cache, crowd_stats, poi_id=poi_id
            )
            _print_result({
                **asdict(snapshot),
                "label": snapshot.label,
                "normalized_level": snapshot.normalized_level,
                "source": source,
                "api_calls": crowd_stats.total_api_calls,
            })
            return

        if args.command == "seoul-crowd":
            if args.area:
                area = find_seoul_crowd_area_by_name(args.area)
            elif args.x is not None and args.y is not None:
                area = find_seoul_crowd_area(args.x, args.y)
            else:
                raise ValueError("--area 또는 --x와 --y를 입력하세요.")
            if area is None:
                raise ValueError("서울시 혼잡도 지원 구역을 찾지 못했습니다.")
            target_time = (
                datetime.fromisoformat(args.at) if args.at else datetime.now()
            )
            seoul_stats = ApiOptimizationStats()
            snapshot, source = cached_seoul_crowd(
                SeoulCrowdClient.from_env(),
                SQLiteTTLCache(args.cache_db),
                seoul_stats,
                area_code=area.area_code,
                target_time=target_time,
            )
            _print_result({
                **asdict(snapshot),
                "source": source,
                "data_scope": "서울시 영역",
                "score_influence": SEOUL_AREA_CROWD_INFLUENCE,
                "accuracy_notice": SEOUL_AREA_CROWD_NOTICE,
                "matched_area": asdict(area) | {"rings": "생략"},
                "api_calls": seoul_stats.total_api_calls,
            })
            return

        if args.command == "seoul-transit-route":
            transit_stats = ApiOptimizationStats()
            transit_cache = SQLiteTTLCache(args.cache_db)
            route, source = cached_seoul_transit_route(
                SeoulTransitClient.from_env(),
                transit_cache,
                transit_stats,
                start_x=args.start_x,
                start_y=args.start_y,
                end_x=args.end_x,
                end_y=args.end_y,
            )
            try:
                route = enrich_seoul_transit_walk_geometry(
                    route,
                    TMapPedestrianClient.from_env(),
                    transit_cache,
                    transit_stats,
                )
            except ValueError:
                pass  # TMAP_APP_KEY 미설정 시 도보 구간 직선 근사를 그대로 둔다.
            _print_result({
                **asdict(route),
                "source": source,
                "within_30_minutes": route.duration_minutes <= 30,
                "api_calls": transit_stats.total_api_calls,
            })
            return

        if args.command == "car-route":
            car_stats = ApiOptimizationStats()
            route, source = cached_car_route(
                TMapCarClient.from_env(),
                SQLiteTTLCache(args.cache_db),
                car_stats,
                start_x=args.start_x,
                start_y=args.start_y,
                end_x=args.end_x,
                end_y=args.end_y,
                end_name=args.end_name,
            )
            _print_result({
                **asdict(route),
                "duration_minutes": round(route.duration_minutes, 2),
                "source": source,
                "within_30_minutes": route.duration_minutes <= 30,
                "api_calls": car_stats.total_api_calls,
            })
            return

        recommendation_commands = {
            "recommend-nearby",
            "recommend-nearby-optimized",
        }
        priorities = (
            prompt_user_priorities()
            if args.command in recommendation_commands
            else None
        )
        cache = (
            SQLiteTTLCache(args.cache_db)
            if args.command == "recommend-nearby-optimized"
            else None
        )
        optimization_stats = (
            ApiOptimizationStats()
            if args.command == "recommend-nearby-optimized"
            else None
        )
        route_client: TMapPedestrianClient | None = None
        car_route_client: TMapCarClient | None = None
        crowd_client: SKCrowdClient | None = None
        seoul_crowd_client: SeoulCrowdClient | None = None
        seoul_transit_client: SeoulTransitClient | None = None
        if args.command == "recommend-nearby-optimized":
            if args.route_mode != "estimated":
                try:
                    route_client = TMapPedestrianClient.from_env()
                except ValueError:
                    if args.route_mode == "tmap":
                        raise
            if args.car_mode != "off":
                try:
                    car_route_client = TMapCarClient.from_env()
                except ValueError:
                    if args.car_mode == "tmap":
                        raise
            if args.crowd_mode in {"auto", "seoul"}:
                try:
                    seoul_crowd_client = SeoulCrowdClient.from_env()
                except ValueError:
                    if args.crowd_mode == "seoul":
                        raise
            if args.transit_mode in {"auto", "seoul"}:
                try:
                    seoul_transit_client = SeoulTransitClient.from_env()
                except ValueError:
                    if args.transit_mode == "seoul":
                        raise
        automatic_weather: WeatherSnapshot | None = None
        weather_severity = getattr(args, "weather_severity", None)
        if args.command in recommendation_commands and weather_severity is None:
            arrival = (
                datetime.fromisoformat(args.arrival)
                if args.arrival
                else datetime.now()
            )
            with KMAClient.from_env() as weather_client:
                if cache is not None and optimization_stats is not None:
                    automatic_weather = cached_weather_forecast(
                        weather_client,
                        cache,
                        optimization_stats,
                        latitude=args.y,
                        longitude=args.x,
                        target_time=arrival,
                    )
                else:
                    automatic_weather = weather_client.village_forecast(
                        latitude=args.y,
                        longitude=args.x,
                        target_time=arrival,
                    )
            weather_severity = automatic_weather.severity
            _print_weather(automatic_weather)

        with KTOClient.from_env() as client:
            if args.command == "areas":
                result = client.area_codes(area_code=args.area_code)
            elif args.command == "keyword":
                result = client.keyword_search(
                    args.query,
                    area_code=args.area_code,
                    content_type_id=args.content_type_id,
                    num_of_rows=args.rows,
                )
            elif args.command == "nearby":
                result = client.location_based_list(
                    map_x=args.x,
                    map_y=args.y,
                    radius=args.radius,
                    content_type_id=args.content_type_id,
                    num_of_rows=args.rows,
                )
            elif args.command == "festivals":
                result = client.festival_search(
                    event_start_date=args.start,
                    event_end_date=args.end,
                    area_code=args.area_code,
                    num_of_rows=args.rows,
                )
            elif args.command == "detail":
                result = client.detail_bundle(
                    args.content_id,
                    args.content_type_id,
                    include_info=not args.without_info,
                    include_images=not args.without_images,
                )
            elif args.command == "normalize":
                bundle = client.detail_bundle(
                    args.content_id,
                    args.content_type_id,
                )
                result = asdict(normalize_place(bundle))
            elif args.command == "recommend-nearby":
                arrival = (
                    datetime.fromisoformat(args.arrival)
                    if args.arrival
                    else datetime.now()
                )
                trip = TripContext(
                    arrival_time=arrival,
                    party_size=args.party_size,
                    children_count=args.children,
                    remaining_budget_krw=args.budget,
                    weather_severity=weather_severity,
                    max_route_minutes=args.max_route_minutes,
                    max_walking_minutes=args.max_walking_minutes,
                    max_transport_minutes=args.max_transport_minutes,
                    max_walking_meters=args.max_walking_meters,
                    budget_is_hard=False,
                )
                result = recommend_nearby(
                    client,
                    map_x=args.x,
                    map_y=args.y,
                    radius=args.radius,
                    rows=args.rows,
                    trip=trip,
                    priorities=priorities,
                    content_type_id=args.content_type_id,
                    include_restaurants=args.include_restaurants,
                )
            elif args.command == "recommend-nearby-optimized":
                arrival = (
                    datetime.fromisoformat(args.arrival)
                    if args.arrival
                    else datetime.now()
                )
                trip = TripContext(
                    arrival_time=arrival,
                    party_size=args.party_size,
                    children_count=args.children,
                    remaining_budget_krw=args.budget,
                    weather_severity=weather_severity,
                    max_route_minutes=args.max_route_minutes,
                    max_walking_minutes=args.max_walking_minutes,
                    max_transport_minutes=args.max_transport_minutes,
                    max_walking_meters=args.max_walking_meters,
                    budget_is_hard=False,
                )
                next_values = (args.next_x, args.next_y, args.next_arrival)
                if any(value is not None for value in next_values) and not all(
                    value is not None for value in next_values
                ):
                    raise ValueError(
                        "--next-x, --next-y, --next-arrival을 함께 입력해야 합니다."
                    )
                next_schedule = (
                    NextScheduleConstraint(
                        longitude=args.next_x,
                        latitude=args.next_y,
                        arrival_deadline=datetime.fromisoformat(
                            args.next_arrival
                        ),
                        visit_minutes=args.visit_minutes,
                        buffer_minutes=args.schedule_buffer_minutes,
                        title=args.next_title,
                    )
                    if args.next_arrival is not None
                    else None
                )
                result = recommend_nearby_optimized(
                    client,
                    cache,
                    map_x=args.x,
                    map_y=args.y,
                    radius=args.radius,
                    search_rows=args.search_rows,
                    eligible_count=args.eligible_count,
                    max_detail_calls=args.max_detail_calls,
                    trip=trip,
                    priorities=priorities,
                    stats=optimization_stats,
                    content_type_id=args.content_type_id,
                    route_client=route_client,
                    car_route_client=car_route_client,
                    crowd_client=crowd_client,
                    max_crowd_calls=args.max_crowd_calls,
                    seoul_crowd_client=seoul_crowd_client,
                    max_seoul_crowd_calls=args.max_seoul_crowd_calls,
                    seoul_transit_client=seoul_transit_client,
                    max_seoul_transit_calls=args.max_seoul_transit_calls,
                    max_car_route_calls=args.max_car_route_calls,
                    next_schedule=next_schedule,
                    include_restaurants=args.include_restaurants,
                )
            else:
                raise AssertionError(f"지원하지 않는 명령: {args.command}")

        if isinstance(result, OptimizedRecommendationResult):
            _print_optimized(result)
        elif isinstance(result, NearbyRecommendationResult):
            _print_recommendations(result)
        else:
            _print_result(result)
    except (
        KMAApiError,
        KTOApiError,
        TMapApiError,
        SKCrowdApiError,
        SeoulCrowdApiError,
        SeoulTransitApiError,
        TMapCarApiError,
        TMapPlaceSearchError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise SystemExit(f"오류: {exc}") from exc


if __name__ == "__main__":
    main()
