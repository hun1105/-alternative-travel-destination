"""정규화 장소와 여행 상황을 추천 점수 입력으로 변환한다."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .normalizer import NormalizedPlace, is_open_at
from .scoring import (
    CandidateConstraints,
    CandidateSignals,
    ScoreResult,
    UserPriorities,
    score_candidate,
)


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _check_ratio(name: str, value: float | None) -> None:
    if value is not None and not 0.0 <= value <= 1.0:
        raise ValueError(f"{name}은 0.0~1.0이어야 합니다: {value}")


@dataclass(frozen=True)
class TripContext:
    """사용자·여행 전체에 공통인 현재 상황."""

    arrival_time: datetime
    party_size: int = 1
    paid_admission_count: int | None = None
    children_count: int = 0
    remaining_budget_krw: int | None = None
    weather_severity: float = 0.0
    max_route_minutes: float = 60.0
    max_walking_minutes: float = 15.0
    max_transport_minutes: float = 30.0
    max_walking_meters: float = 3000.0
    minutes_until_locked_stop: float | None = None
    budget_is_hard: bool = False
    requires_accessibility: bool = False
    schedule_buffer_minutes: float = 10.0

    def validate(self) -> None:
        _check_ratio("weather_severity", self.weather_severity)
        if self.party_size < 1:
            raise ValueError("party_size는 1 이상이어야 합니다.")
        if self.paid_admission_count is not None and self.paid_admission_count < 0:
            raise ValueError("paid_admission_count는 0 이상이어야 합니다.")
        if not 0 <= self.children_count <= self.party_size:
            raise ValueError("children_count는 0~party_size여야 합니다.")
        if self.remaining_budget_krw is not None and self.remaining_budget_krw < 0:
            raise ValueError("remaining_budget_krw는 0 이상이어야 합니다.")
        if (
            self.max_route_minutes <= 0
            or self.max_walking_minutes <= 0
            or self.max_transport_minutes <= 0
            or self.max_walking_meters <= 0
        ):
            raise ValueError("최대 이동시간과 도보거리는 0보다 커야 합니다.")
        if self.minutes_until_locked_stop is not None and self.minutes_until_locked_stop < 0:
            raise ValueError("minutes_until_locked_stop은 0 이상이어야 합니다.")
        if self.schedule_buffer_minutes < 0:
            raise ValueError("schedule_buffer_minutes는 0 이상이어야 합니다.")


@dataclass(frozen=True)
class CandidateFacts:
    """경로·날씨·선호 시스템이 제공하는 후보별 사실."""

    indoor_ratio: float
    route_minutes: float
    walking_meters: float
    crowd_level: float | None = None
    child_suitability: float | None = None
    accessibility_suitability: float | None = None
    visit_minutes: float = 60.0
    onward_minutes: float = 0.0
    onward_route_feasible: bool = True
    extra_transport_cost_krw: int = 0
    transport_mode: str = "walking"
    walking_minutes: float | None = None

    def validate(self) -> None:
        for name in (
            "indoor_ratio",
            "crowd_level",
            "child_suitability",
            "accessibility_suitability",
        ):
            _check_ratio(name, getattr(self, name))
        if min(
            self.route_minutes,
            self.walking_meters,
            self.visit_minutes,
            self.onward_minutes,
            self.extra_transport_cost_krw,
        ) < 0:
            raise ValueError("시간·거리·비용은 0 이상이어야 합니다.")
        if self.walking_minutes is not None and self.walking_minutes < 0:
            raise ValueError("도보시간은 0 이상이어야 합니다.")
        if self.transport_mode not in {"walking", "transit", "car"}:
            raise ValueError("이동수단은 walking, transit, car 중 하나여야 합니다.")


@dataclass(frozen=True)
class SignalBuildResult:
    signals: CandidateSignals
    constraints: CandidateConstraints
    estimated_group_cost_krw: int | None
    open_at_arrival: bool | None
    notes: tuple[str, ...]


@dataclass(frozen=True)
class CandidateEvaluation:
    build: SignalBuildResult
    score: ScoreResult


def _budget_signal(
    place: NormalizedPlace,
    facts: CandidateFacts,
    trip: TripContext,
    notes: list[str],
) -> tuple[float, int | None, bool | None]:
    if place.adult_fee_krw is None:
        notes.append("입장료 정보가 없어 예산 적합도를 중립값으로 처리")
        return 0.5, None, None

    paid_count = (
        trip.paid_admission_count
        if trip.paid_admission_count is not None
        else trip.party_size
    )
    cost = place.adult_fee_krw * paid_count + facts.extra_transport_cost_krw
    budget = trip.remaining_budget_krw
    if budget is None:
        notes.append("남은 예산이 없어 비용만 계산")
        return 0.7 if cost == 0 else 0.5, cost, None
    if budget == 0:
        return (1.0 if cost == 0 else 0.0), cost, cost == 0

    within = cost <= budget
    if within:
        fit = 1.0 - 0.5 * (cost / budget)
    else:
        fit = 1.0 - ((cost - budget) / budget)
    return _clamp(fit), cost, within


def _child_signal(
    place: NormalizedPlace,
    facts: CandidateFacts,
    trip: TripContext,
    notes: list[str],
) -> float:
    if trip.children_count == 0:
        return 0.8
    if facts.child_suitability is not None:
        return facts.child_suitability

    score = 0.4
    if place.toilet_available is True:
        score += 0.2
    if place.parking_available is True:
        score += 0.1
    notes.append("아동 적합도 정보가 없어 편의시설 기반으로 추정")
    return _clamp(score)


def build_candidate_signals(
    place: NormalizedPlace,
    facts: CandidateFacts,
    trip: TripContext,
) -> SignalBuildResult:
    """실제 후보 정보를 점수 신호와 필수조건으로 자동 변환한다."""

    facts.validate()
    trip.validate()
    notes: list[str] = []

    weather_fit = _clamp(
        1.0 - trip.weather_severity * (1.0 - facts.indoor_ratio)
    )
    weather_safe = not (
        trip.weather_severity >= 0.8 and facts.indoor_ratio <= 0.2
    )
    route_fit = _clamp(
        1.0 - facts.route_minutes / (trip.max_route_minutes * 1.5)
    )
    walking_fit = _clamp(
        1.0 - facts.walking_meters / (trip.max_walking_meters * 1.5)
    )

    if facts.crowd_level is None:
        crowd_fit = 0.5
        notes.append("혼잡도 정보가 없어 중립값으로 처리")
    else:
        crowd_fit = 1.0 - facts.crowd_level

    budget_fit, cost, within_budget = _budget_signal(
        place, facts, trip, notes
    )
    child_fit = _child_signal(place, facts, trip, notes)
    open_now = is_open_at(place, trip.arrival_time)
    if open_now is None:
        notes.append("운영시간을 판단할 수 없어 필수조건을 보류")

    reaches_locked_stop = True
    if trip.minutes_until_locked_stop is not None:
        required = (
            facts.route_minutes
            + facts.visit_minutes
            + facts.onward_minutes
            + trip.schedule_buffer_minutes
        )
        reaches_locked_stop = required <= trip.minutes_until_locked_stop

    accessibility_ok = True
    if trip.requires_accessibility:
        if facts.accessibility_suitability is None:
            notes.append("접근성 정보가 없어 필수조건을 보류")
        else:
            accessibility_ok = facts.accessibility_suitability >= 0.5

    signals = CandidateSignals(
        weather_fit=round(weather_fit, 4),
        route_time=round(route_fit, 4),
        crowd_avoidance=round(crowd_fit, 4),
        budget_fit=round(budget_fit, 4),
        child_fit=round(child_fit, 4),
        walking_fit=round(walking_fit, 4),
        data_confidence=round(place.normalization_confidence, 4),
    )
    walking_minutes = (
        facts.walking_minutes
        if facts.walking_minutes is not None
        else facts.route_minutes
        if facts.transport_mode == "walking"
        else None
    )
    constraints = CandidateConstraints(
        weather_safe=weather_safe,
        reaches_locked_stop=reaches_locked_stop,
        onward_route_feasible=facts.onward_route_feasible,
        accessibility_ok=accessibility_ok,
        within_walking_time=(
            # 정확히 최대 허용시간이면 도보로 보지 않고 대중교통·자동차로 넘긴다.
            walking_minutes < trip.max_walking_minutes
            if walking_minutes is not None
            else True
        ),
        within_transport_time=(
            facts.route_minutes <= trip.max_transport_minutes
            if facts.transport_mode in {"transit", "car"}
            else True
        ),
        open_now=open_now,
        within_budget=within_budget,
        budget_is_hard=trip.budget_is_hard,
    )
    return SignalBuildResult(
        signals=signals,
        constraints=constraints,
        estimated_group_cost_krw=cost,
        open_at_arrival=open_now,
        notes=tuple(notes),
    )


def evaluate_place_candidate(
    place: NormalizedPlace,
    facts: CandidateFacts,
    trip: TripContext,
    priorities: UserPriorities,
) -> CandidateEvaluation:
    """자동 신호 생성과 사용자별 점수 계산을 한 번에 실행한다."""

    build = build_candidate_signals(place, facts, trip)
    score = score_candidate(build.signals, priorities, build.constraints)
    return CandidateEvaluation(build=build, score=score)
