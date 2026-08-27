"""사용자 우선순위에 따라 동적으로 변하는 후보 점수 엔진."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


PRIORITY_FIELDS = (
    "weather_fit",
    "route_time",
    "crowd_avoidance",
    "child_fit",
    "walking_fit",
)

RANK_WEIGHTS: dict[int, float] = {
    1: 32.0,
    2: 23.0,
    3: 17.0,
    4: 13.0,
    5: 10.0,
}

DATA_CONFIDENCE_WEIGHT = 5.0
BASE_WEIGHTS: dict[str, float] = {
    name: RANK_WEIGHTS[index]
    for index, name in enumerate(PRIORITY_FIELDS, start=1)
}
BASE_WEIGHTS["data_confidence"] = DATA_CONFIDENCE_WEIGHT

LABELS = {
    "weather_fit": "날씨 적합",
    "route_time": "이동시간",
    "crowd_avoidance": "혼잡 회피",
    "budget_fit": "예산 적합",
    "child_fit": "아동 적합",
    "walking_fit": "보행 부담",
    "data_confidence": "데이터 신뢰도",
}


@dataclass(frozen=True)
class UserPriorities:
    """선택한 항목만 1~n순위를 갖고 나머지는 동일 배점이다."""

    weather_fit: int | None = None
    route_time: int | None = None
    crowd_avoidance: int | None = None
    budget_fit: int | None = None
    child_fit: int | None = None
    walking_fit: int | None = None

    @classmethod
    def from_order(cls, *fields: str) -> "UserPriorities":
        """최우선 항목부터 필드명을 나열해 순위 객체를 만든다."""

        if not 1 <= len(fields) <= len(PRIORITY_FIELDS):
            raise ValueError("우선순위 항목은 1~5개를 입력해야 합니다.")
        unknown = set(fields) - set(PRIORITY_FIELDS)
        if unknown:
            raise ValueError(f"알 수 없는 우선순위 항목입니다: {sorted(unknown)}")
        if len(set(fields)) != len(fields):
            raise ValueError("같은 우선순위 항목을 중복 입력할 수 없습니다.")
        ranks = {field: rank for rank, field in enumerate(fields, start=1)}
        return cls(**ranks)

    def as_dict(self) -> dict[str, int | None]:
        values = {
            "weather_fit": self.weather_fit,
            "route_time": self.route_time,
            "crowd_avoidance": self.crowd_avoidance,
            "child_fit": self.child_fit,
            "walking_fit": self.walking_fit,
        }
        invalid = {
            name: value
            for name, value in values.items()
            if value is not None and not 1 <= value <= 5
        }
        if invalid:
            raise ValueError(f"우선순위는 1~5여야 합니다: {invalid}")
        selected = [value for value in values.values() if value is not None]
        if len(selected) != len(set(selected)):
            raise ValueError("선택한 우선순위는 중복될 수 없습니다.")
        if sorted(selected) != list(range(1, len(selected) + 1)):
            raise ValueError("선택한 우선순위는 1부터 연속되어야 합니다.")
        return values


@dataclass(frozen=True)
class CandidateSignals:
    """각 항목은 0.0~1.0 사이의 적합도다."""

    weather_fit: float
    route_time: float
    crowd_avoidance: float
    budget_fit: float
    child_fit: float
    walking_fit: float
    data_confidence: float

    def as_dict(self) -> dict[str, float]:
        values = {
            field: float(getattr(self, field))
            for field in BASE_WEIGHTS
        }
        invalid = {name: value for name, value in values.items() if not 0 <= value <= 1}
        if invalid:
            raise ValueError(f"후보 적합도는 0.0~1.0이어야 합니다: {invalid}")
        return values


@dataclass(frozen=True)
class CandidateConstraints:
    """점수보다 먼저 적용하는 필수조건."""

    weather_safe: bool = True
    reaches_locked_stop: bool = True
    onward_route_feasible: bool = True
    accessibility_ok: bool = True
    within_walking_time: bool = True
    within_transport_time: bool = True
    open_now: bool | None = True
    within_budget: bool | None = True
    budget_is_hard: bool = False


@dataclass(frozen=True)
class ScoreResult:
    eligible: bool
    total_score: float
    weights: dict[str, float]
    contributions: dict[str, float]
    reasons: tuple[str, ...]
    rejection_reasons: tuple[str, ...]


def dynamic_weights(priorities: UserPriorities) -> dict[str, float]:
    """선택 순위에 배점하고 미선택 일반 항목에는 남은 점수를 배분한다."""

    priority_values = priorities.as_dict()
    # 아동 적합은 사용자가 직접 선택한 경우에만 점수에 포함한다.
    inactive = (
        {"child_fit"} if priority_values["child_fit"] is None else set()
    )
    weights = {
        name: RANK_WEIGHTS[rank]
        for name in PRIORITY_FIELDS
        if (rank := priority_values[name]) is not None
    }
    unselected = [
        name
        for name in PRIORITY_FIELDS
        if priority_values[name] is None and name not in inactive
    ]
    if unselected:
        remaining = (
            100.0
            - DATA_CONFIDENCE_WEIGHT
            - sum(weights.values())
        )
        equal_weight = remaining / len(unselected)
        weights.update({name: equal_weight for name in unselected})
    weights.update({name: 0.0 for name in inactive})
    weights["data_confidence"] = DATA_CONFIDENCE_WEIGHT
    return weights


def _hard_rejections(constraints: CandidateConstraints) -> list[str]:
    reasons: list[str] = []
    if not constraints.weather_safe:
        reasons.append("안전한 날씨 조건을 충족하지 않음")
    if not constraints.reaches_locked_stop:
        reasons.append("다음 고정 일정에 제시간 도착 불가")
    if not constraints.onward_route_feasible:
        reasons.append("후보에서 다음 일정까지 허용 이동수단으로 연결 불가")
    if not constraints.accessibility_ok:
        reasons.append("필수 접근성 조건을 충족하지 않음")
    if not constraints.within_walking_time:
        reasons.append("도보 이동시간이 최대 허용시간을 초과함")
    if not constraints.within_transport_time:
        reasons.append("차량·대중교통 이동시간이 30분 한도를 초과함")
    if constraints.open_now is False:
        reasons.append("도착 시 운영하지 않음")
    if constraints.budget_is_hard and constraints.within_budget is False:
        reasons.append("필수 예산 한도를 초과함")
    return reasons


def score_candidate(
    signals: CandidateSignals,
    priorities: UserPriorities,
    constraints: CandidateConstraints | None = None,
) -> ScoreResult:
    """필수조건 통과 후 개인별 동적 가중치로 후보를 평가한다."""

    constraints = constraints or CandidateConstraints()
    rejections = _hard_rejections(constraints)
    weights = dynamic_weights(priorities)
    if rejections:
        return ScoreResult(
            eligible=False,
            total_score=0.0,
            weights=weights,
            contributions={name: 0.0 for name in BASE_WEIGHTS},
            reasons=(),
            rejection_reasons=tuple(rejections),
        )

    values = signals.as_dict()
    contributions = {
        name: round(weights[name] * value, 3)
        for name, value in values.items()
    }
    total = round(sum(contributions.values()), 2)

    ranked = sorted(
        contributions,
        key=lambda name: contributions[name],
        reverse=True,
    )
    reasons = tuple(
        f"{LABELS[name]} {values[name] * 100:.0f}%"
        for name in ranked[:3]
    )
    return ScoreResult(
        eligible=True,
        total_score=total,
        weights=weights,
        contributions=contributions,
        reasons=reasons,
        rejection_reasons=(),
    )


def rank_candidates(
    candidates: Mapping[
        str,
        tuple[CandidateSignals, CandidateConstraints],
    ],
    priorities: UserPriorities,
) -> list[tuple[str, ScoreResult]]:
    """후보 ID와 신호를 받아 적격 후보를 점수순으로 정렬한다."""

    scored = [
        (
            candidate_id,
            score_candidate(signals, priorities, constraints),
        )
        for candidate_id, (signals, constraints) in candidates.items()
    ]
    return sorted(
        scored,
        key=lambda item: (item[1].eligible, item[1].total_score),
        reverse=True,
    )
