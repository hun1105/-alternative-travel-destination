"""TourAPI 실제 주변 후보를 정규화하고 추천 점수로 정렬한다."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .kto_client import KTOApiError, KTOClient
from .normalizer import NormalizedPlace, normalize_place
from .scoring import UserPriorities
from .signal_builder import (
    CandidateEvaluation,
    CandidateFacts,
    TripContext,
    evaluate_place_candidate,
)
from .schedule_feasibility import ScheduleFeasibility


INDOOR_CONTENT_TYPES = {"14": 0.9, "32": 0.9, "38": 0.75, "39": 0.9}
OUTDOOR_CONTENT_TYPES = {"15": 0.3, "25": 0.2, "28": 0.3}
RECOMMENDATION_CONTENT_TYPES = {"12", "14", "15", "25", "28", "38", "39"}
MAX_CONFIDENCE_PENALTY = 15.0
INDOOR_WORDS = ("박물관", "미술관", "전시", "아쿠아리움", "실내", "공연장")
OUTDOOR_WORDS = ("궁", "공원", "산", "해변", "수목원", "거리", "마을", "숲")


@dataclass(frozen=True)
class RankedTourCandidate:
    title: str
    content_id: str
    content_type_id: str
    distance_meters: float | None
    place: NormalizedPlace
    facts: CandidateFacts
    evaluation: CandidateEvaluation
    estimation_notes: tuple[str, ...]
    detail_source: str = "실시간 API"
    route_source: str = "직선거리 추정"
    inbound_route_geometry: dict[str, Any] | None = None
    onward_route_geometry: dict[str, Any] | None = None
    crowd_source: str = "미연결"
    crowd_label: str | None = None
    crowd_raw_level: float | None = None
    crowd_scope: str = "미확인"
    base_score: float | None = None
    confidence_penalty: float = 0.0
    schedule_feasibility: ScheduleFeasibility | None = None
    cat1: str = ""
    cat2: str = ""
    cat3: str = ""
    # 관광공사 신규 분류체계(lclsSystm1/2/3). 오래된 콘텐츠는 cat1/2/3이
    # 비어 있어도 이쪽엔 값이 채워진 경우가 많아 대체 판단에 쓴다.
    lcls1: str = ""
    lcls2: str = ""
    lcls3: str = ""


@dataclass(frozen=True)
class CandidateEvidence:
    confidence_percent: int
    confidence_level: str
    operation_status: str
    actual_data: tuple[str, ...]
    estimated_data: tuple[str, ...]
    neutral_data: tuple[str, ...]
    excluded_data: tuple[str, ...]
    detail_source: str
    route_source: str
    crowd_source: str


@dataclass(frozen=True)
class NearbyRecommendationResult:
    candidates: tuple[RankedTourCandidate, ...]
    skipped: tuple[str, ...]


def build_candidate_evidence(
    candidate: RankedTourCandidate,
) -> CandidateEvidence:
    """추천 판단에 사용한 실제·추정·중립 데이터를 구분한다."""

    place = candidate.place
    facts = candidate.facts
    build = candidate.evaluation.build
    actual = ["관광지 기본정보"]
    estimated = ["실내 비율"]
    neutral: list[str] = []
    excluded: list[str] = []
    known_score = 0.0

    if candidate.distance_meters is not None:
        actual.append("직선거리")
        known_score += 0.5
    if candidate.route_source == "직선거리 추정":
        estimated.extend(("이동시간", "보행 부담"))
    else:
        actual.append("실제 도보 경로")
        known_score += 0.5
    if candidate.evaluation.score.weights.get("budget_fit", 0) == 0:
        excluded.append("예산 적합")
    elif place.adult_fee_krw is not None:
        actual.append("입장료")
        known_score += 1
    else:
        neutral.append("예산 적합")
    if build.open_at_arrival is True:
        operation_status = "도착 시 운영 확인"
        actual.append("운영시간·휴무")
        known_score += 1
    elif build.open_at_arrival is False:
        operation_status = "도착 시 미운영 확인"
        actual.append("운영시간·휴무")
        known_score += 1
    else:
        operation_status = "운영 여부 미확인"
        neutral.append("운영 여부")
    if facts.crowd_level is not None:
        if candidate.crowd_scope == "영역":
            actual.append("주변 지역 혼잡도(영역 단위)")
            known_score += 0.6
        else:
            actual.append("혼잡도")
            known_score += 1
    else:
        neutral.append("혼잡 회피")
    if candidate.evaluation.score.weights["child_fit"] == 0:
        excluded.append("아동 적합")
    elif facts.child_suitability is None:
        estimated.append("아동 적합")
    else:
        actual.append("아동 적합")

    confidence = round(
        100 * (
            0.6 * place.normalization_confidence
            + 0.4 * (known_score / 3)
        )
    )
    confidence = max(0, min(100, confidence))
    if confidence >= 80:
        level = "높음"
    elif confidence >= 60:
        level = "보통"
    else:
        level = "낮음"

    return CandidateEvidence(
        confidence_percent=confidence,
        confidence_level=level,
        operation_status=operation_status,
        actual_data=tuple(actual),
        estimated_data=tuple(estimated),
        neutral_data=tuple(neutral),
        excluded_data=tuple(excluded),
        detail_source=candidate.detail_source,
        route_source=candidate.route_source,
        crowd_source=candidate.crowd_source,
    )


def apply_confidence_penalty(
    candidate: RankedTourCandidate,
) -> RankedTourCandidate:
    """낮은 근거 신뢰도에 최대 15점 감점을 적용한다."""

    base_score = candidate.evaluation.score.total_score
    if not candidate.evaluation.score.eligible:
        return replace(candidate, base_score=base_score)
    confidence = build_candidate_evidence(candidate).confidence_percent / 100
    penalty = round((1 - confidence) * MAX_CONFIDENCE_PENALTY, 2)
    adjusted = max(0.0, round(base_score - penalty, 2))
    score = replace(candidate.evaluation.score, total_score=adjusted)
    evaluation = replace(candidate.evaluation, score=score)
    return replace(
        candidate,
        evaluation=evaluation,
        base_score=base_score,
        confidence_penalty=penalty,
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def estimate_indoor_ratio(place: NormalizedPlace) -> float:
    """콘텐츠 유형과 명칭으로 실내 비율을 보수적으로 추정한다."""

    # 명칭은 긴 소개문보다 장소의 실제 성격을 강하게 나타낸다.
    if any(word in place.title for word in OUTDOOR_WORDS):
        return 0.15
    if any(word in place.title for word in INDOOR_WORDS):
        return 0.95
    if place.content_type_id in INDOOR_CONTENT_TYPES:
        return INDOOR_CONTENT_TYPES[place.content_type_id]
    if place.content_type_id in OUTDOOR_CONTENT_TYPES:
        return OUTDOOR_CONTENT_TYPES[place.content_type_id]
    overview_indoor = any(word in place.overview for word in INDOOR_WORDS)
    overview_outdoor = any(word in place.overview for word in OUTDOOR_WORDS)
    if overview_indoor and not overview_outdoor:
        return 0.8
    if overview_outdoor and not overview_indoor:
        return 0.25
    return 0.5


def facts_from_tourapi(
    search_item: Mapping[str, Any],
    place: NormalizedPlace,
) -> tuple[CandidateFacts, tuple[str, ...]]:
    """TourAPI 값만으로 만들 수 없는 신호에는 추정치·중립값을 적용한다."""

    distance = _optional_float(search_item.get("dist"))
    if distance is None:
        distance = 1500.0
        distance_note = "거리 정보 누락: 1,500m 중립 추정"
    else:
        distance_note = "직선거리 기반 이동시간 추정"

    indoor_ratio = estimate_indoor_ratio(place)
    facts = CandidateFacts(
        indoor_ratio=indoor_ratio,
        route_minutes=max(1.0, distance / 75.0),
        walking_meters=distance,
        transport_mode="walking",
        walking_minutes=max(1.0, distance / 75.0),
        crowd_level=None,
        child_suitability=None,
        accessibility_suitability=None,
        visit_minutes=60.0,
    )
    notes = (
        distance_note,
        "실내 비율은 콘텐츠 유형·명칭 기반 추정",
    )
    return facts, notes


RESTAURANT_CONTENT_TYPE = "39"


def is_recommendation_content_type(
    content_type_id: str,
    *,
    include_restaurants: bool = False,
) -> bool:
    if content_type_id not in RECOMMENDATION_CONTENT_TYPES:
        return False
    if content_type_id == RESTAURANT_CONTENT_TYPE:
        return include_restaurants
    return True


def recommend_nearby(
    client: KTOClient,
    *,
    map_x: float,
    map_y: float,
    radius: int,
    rows: int,
    trip: TripContext,
    priorities: UserPriorities,
    content_type_id: str | None = None,
    include_restaurants: bool = False,
) -> NearbyRecommendationResult:
    """실제 주변 목록과 상세정보를 조회해 점수순으로 반환한다."""

    if not 1 <= rows <= 10:
        raise ValueError("실제 추천 후보 수는 1~10개여야 합니다.")

    response = client.location_based_list(
        map_x=map_x,
        map_y=map_y,
        radius=radius,
        content_type_id=content_type_id,
        arrange="E",
        num_of_rows=rows,
    )
    candidates: list[RankedTourCandidate] = []
    skipped: list[str] = []

    for item in response.items:
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

        try:
            bundle = client.detail_bundle(
                content_id,
                type_id,
                include_images=False,
            )
            place = normalize_place(bundle)
            facts, notes = facts_from_tourapi(item, place)
            evaluation = evaluate_place_candidate(
                place, facts, trip, priorities
            )
        except (KTOApiError, ValueError) as exc:
            skipped.append(f"{title}: {exc}")
            continue

        candidates.append(
            apply_confidence_penalty(RankedTourCandidate(
                title=place.title or title,
                content_id=content_id,
                content_type_id=type_id,
                distance_meters=_optional_float(item.get("dist")),
                place=place,
                facts=facts,
                evaluation=evaluation,
                estimation_notes=notes,
            ))
        )

    candidates.sort(
        key=lambda candidate: (
            candidate.evaluation.score.eligible,
            candidate.evaluation.score.total_score,
        ),
        reverse=True,
    )
    return NearbyRecommendationResult(
        candidates=tuple(candidates),
        skipped=tuple(skipped),
    )
