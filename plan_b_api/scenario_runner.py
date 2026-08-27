"""대회 시연용 Plan B 핵심 시나리오 10개 자동 검증."""

from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .api_cache import SQLiteTTLCache
from .normalizer import NormalizedPlace, normalize_place
from .optimized_recommender import ApiOptimizationStats, cached_walking_route
from .route_client import TMapApiError, WalkingRoute
from .scoring import UserPriorities
from .signal_builder import CandidateFacts, TripContext, evaluate_place_candidate


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str


def _place(
    title: str,
    *,
    content_type_id: str = "12",
    hours: str = "[1월~12월]09:00~18:00",
    rest: str = "연중무휴",
    fee: str | None = "대인 3,000원",
    parking: bool = True,
    toilet: bool = True,
    event_start: str | None = None,
    event_end: str | None = None,
) -> NormalizedPlace:
    intro: dict[str, str] = {
        "usetime": hours,
        "restdate": rest,
        "parking": "가능" if parking else "불가",
    }
    if event_start:
        intro["eventstartdate"] = event_start
    if event_end:
        intro["eventenddate"] = event_end
    info: list[dict[str, str]] = []
    if fee is not None:
        info.append({"infoname": "입장료", "infotext": fee})
    if toilet:
        info.append({"infoname": "화장실", "infotext": "있음"})
    return normalize_place({
        "content_id": title,
        "content_type_id": content_type_id,
        "common": [{
            "contentid": title,
            "contenttypeid": content_type_id,
            "title": title,
            "mapx": "126.97",
            "mapy": "37.57",
            "firstimage": "https://example.com/image.jpg",
        }],
        "intro": [intro],
        "info": info,
    })


def _facts(
    *,
    indoor: float,
    route: float = 10,
    walking: float = 500,
) -> CandidateFacts:
    return CandidateFacts(
        indoor_ratio=indoor,
        route_minutes=route,
        walking_meters=walking,
    )


def _priorities(*fields: str) -> UserPriorities:
    return UserPriorities.from_order(*fields)


def _scenario_heat() -> ScenarioResult:
    result = evaluate_place_candidate(
        _place("야외 공원"),
        _facts(indoor=0.15),
        TripContext(datetime(2026, 8, 3, 14), weather_severity=0.9),
        _priorities("weather_fit"),
    )
    passed = not result.score.eligible and any(
        "날씨" in reason for reason in result.score.rejection_reasons
    )
    return ScenarioResult("폭염 시 야외 후보 제외", passed, str(result.score.rejection_reasons))


def _scenario_rain() -> ScenarioResult:
    trip = TripContext(datetime(2026, 8, 3, 14), weather_severity=0.7)
    outdoor = evaluate_place_candidate(
        _place("야외 정원"), _facts(indoor=0.15), trip, _priorities("weather_fit")
    )
    indoor = evaluate_place_candidate(
        _place("실내 박물관", content_type_id="14"),
        _facts(indoor=0.95),
        trip,
        _priorities("weather_fit"),
    )
    passed = indoor.score.total_score > outdoor.score.total_score
    detail = f"실내 {indoor.score.total_score}점 > 야외 {outdoor.score.total_score}점"
    return ScenarioResult("비 오는 날 실내 우선", passed, detail)


def _scenario_night() -> ScenarioResult:
    result = evaluate_place_candidate(
        _place("주간 박물관"),
        _facts(indoor=0.95),
        TripContext(datetime(2026, 8, 3, 21)),
        _priorities("route_time"),
    )
    return ScenarioResult(
        "야간 운영 종료 후보 제외",
        not result.score.eligible,
        str(result.score.rejection_reasons),
    )


def _scenario_closed_day() -> ScenarioResult:
    result = evaluate_place_candidate(
        _place("화요일 휴무관", rest="매주 화요일"),
        _facts(indoor=0.95),
        TripContext(datetime(2026, 8, 4, 12)),
        _priorities("route_time"),
    )
    return ScenarioResult(
        "정기 휴무일 후보 제외",
        not result.score.eligible,
        str(result.score.rejection_reasons),
    )


def _scenario_expired_event() -> ScenarioResult:
    result = evaluate_place_candidate(
        _place(
            "종료 행사",
            content_type_id="15",
            event_start="20251001",
            event_end="20251031",
        ),
        _facts(indoor=0.5),
        TripContext(datetime(2026, 8, 3, 12)),
        _priorities("route_time"),
    )
    return ScenarioResult(
        "종료된 행사 제외",
        not result.score.eligible,
        str(result.score.rejection_reasons),
    )


def _scenario_walking_time_limit() -> ScenarioResult:
    result = evaluate_place_candidate(
        _place("도보 초과 장소"),
        _facts(indoor=0.8, route=16),
        TripContext(datetime(2026, 8, 3, 12)),
        _priorities("walking_fit"),
    )
    return ScenarioResult(
        "도보 15분 초과 후보 제외",
        not result.score.eligible,
        str(result.score.rejection_reasons),
    )


def _scenario_children() -> ScenarioResult:
    trip = TripContext(datetime(2026, 8, 3, 12), party_size=2, children_count=1)
    equipped = evaluate_place_candidate(
        _place("편의시설 장소", parking=True, toilet=True),
        _facts(indoor=0.8),
        trip,
        _priorities("child_fit"),
    )
    unequipped = evaluate_place_candidate(
        _place("편의시설 부족", parking=False, toilet=False),
        _facts(indoor=0.8),
        trip,
        _priorities("child_fit"),
    )
    left = equipped.build.signals.child_fit
    right = unequipped.build.signals.child_fit
    return ScenarioResult("아동 동반 편의시설 반영", left > right, f"{left:.1f} > {right:.1f}")


def _scenario_walking() -> ScenarioResult:
    trip = TripContext(datetime(2026, 8, 3, 12), max_walking_meters=1000)
    near = evaluate_place_candidate(
        _place("가까운 장소"),
        _facts(indoor=0.8, walking=300),
        trip,
        _priorities("walking_fit"),
    )
    far = evaluate_place_candidate(
        _place("먼 장소"),
        _facts(indoor=0.8, walking=3000),
        trip,
        _priorities("walking_fit"),
    )
    passed = near.score.total_score > far.score.total_score
    detail = f"가까움 {near.score.total_score}점 > 멂 {far.score.total_score}점"
    return ScenarioResult("도보 제한 점수 반영", passed, detail)


class _FailingRouteClient:
    def pedestrian_route(self, **_: object) -> WalkingRoute:
        raise TMapApiError("의도한 장애")


class _WorkingRouteClient:
    def __init__(self) -> None:
        self.calls = 0

    def pedestrian_route(self, **_: object) -> WalkingRoute:
        self.calls += 1
        return WalkingRoute(800, 600)


def _route_arguments() -> dict[str, object]:
    return {
        "start_x": 126.97,
        "start_y": 37.57,
        "end_x": 126.98,
        "end_y": 37.58,
        "end_name": "테스트 장소",
    }


def _scenario_api_failure() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as directory:
        cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
        key = "walking:126.97000:37.57000:126.98000:37.58000"
        cache.set(
            key,
            {"distance_meters": 900, "duration_seconds": 720},
            ttl_seconds=1,
            now=0,
        )
        stats = ApiOptimizationStats()
        route, source = cached_walking_route(
            _FailingRouteClient(),  # type: ignore[arg-type]
            cache,
            stats,
            **_route_arguments(),  # type: ignore[arg-type]
        )
        passed = stats.route_stale_cache_hits == 1 and route.distance_meters == 900
        return ScenarioResult("API 장애 시 만료 캐시 복구", passed, source)


def _scenario_cache() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as directory:
        cache = SQLiteTTLCache(Path(directory) / "cache.sqlite3")
        client = _WorkingRouteClient()
        stats = ApiOptimizationStats()
        cached_walking_route(
            client, cache, stats, **_route_arguments()  # type: ignore[arg-type]
        )
        _, source = cached_walking_route(
            client, cache, stats, **_route_arguments()  # type: ignore[arg-type]
        )
        passed = client.calls == 1 and stats.route_cache_hits == 1
        return ScenarioResult("반복 호출 캐시 사용", passed, source)


SCENARIOS: tuple[Callable[[], ScenarioResult], ...] = (
    _scenario_heat,
    _scenario_rain,
    _scenario_night,
    _scenario_closed_day,
    _scenario_expired_event,
    _scenario_walking_time_limit,
    _scenario_children,
    _scenario_walking,
    _scenario_api_failure,
    _scenario_cache,
)


def run_scenarios() -> tuple[ScenarioResult, ...]:
    return tuple(scenario() for scenario in SCENARIOS)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    results = run_scenarios()
    for index, result in enumerate(results, start=1):
        state = "통과" if result.passed else "실패"
        print(f"[{state}] {index}. {result.name}")
        print(f"       {result.detail}")
    passed = sum(result.passed for result in results)
    print(f"\n결과: {passed}/{len(results)}개 통과")
    if passed != len(results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
