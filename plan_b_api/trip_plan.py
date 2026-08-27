"""지도 선택 기반 여행 계획 JSON 규격과 검증."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time
from typing import Any, Mapping

from .schedule_feasibility import NextScheduleConstraint


SCHEMA_VERSION = "1.0"


def _required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name}이 필요합니다.")
    return text


def _parse_date(value: Any) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError("trip_date는 YYYY-MM-DD 형식이어야 합니다.") from exc


def _parse_time(value: Any, name: str) -> time | None:
    if value in (None, ""):
        return None
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{name}은 HH:MM 형식이어야 합니다.") from exc


@dataclass(frozen=True)
class SelectedPlace:
    """지도 검색 결과에서 자동으로 채워지는 장소정보."""

    provider: str
    place_id: str
    name: str
    longitude: float
    latitude: float
    address: str = ""
    category: str = ""
    phone: str = ""
    cat1: str = ""
    cat2: str = ""
    cat3: str = ""
    lcls1: str = ""
    lcls2: str = ""
    lcls3: str = ""
    # 관광공사 콘텐츠 ID. TMAP 좌표와 관광공사 등록 좌표가 수백 미터씩
    # 어긋나는 경우가 있어, 대체 추천에서 "자기 자신 제외"를 정확히
    # 하려면 거리뿐 아니라 이 ID로도 걸러야 한다.
    kto_content_id: str = ""
    # 관광공사 업종 대분류(예: 12 관광지, 39 음식점). "같은 업종만
    # 검색" 필터에 쓴다 — cat2/lcls2보다 항상 더 잘 채워진다.
    kto_content_type_id: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SelectedPlace":
        try:
            longitude = float(value["longitude"])
            latitude = float(value["latitude"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("선택 장소의 longitude와 latitude가 필요합니다.") from exc
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("선택 장소 좌표 범위가 올바르지 않습니다.")
        return cls(
            provider=_required_text(value.get("provider"), "장소 제공자"),
            place_id=_required_text(value.get("place_id"), "장소 ID"),
            name=_required_text(value.get("name"), "장소명"),
            longitude=longitude,
            latitude=latitude,
            address=str(value.get("address") or "").strip(),
            category=str(value.get("category") or "").strip(),
            phone=str(value.get("phone") or "").strip(),
            cat1=str(value.get("cat1") or "").strip(),
            cat2=str(value.get("cat2") or "").strip(),
            cat3=str(value.get("cat3") or "").strip(),
            lcls1=str(value.get("lcls1") or "").strip(),
            lcls2=str(value.get("lcls2") or "").strip(),
            lcls3=str(value.get("lcls3") or "").strip(),
            kto_content_id=str(value.get("kto_content_id") or "").strip(),
            kto_content_type_id=str(value.get("kto_content_type_id") or "").strip(),
        )


@dataclass(frozen=True)
class TripScheduleItem:
    """사용자는 체류시간만 입력하고 장소정보는 지도 선택으로 채운다."""

    item_id: str
    place: SelectedPlace
    visit_minutes: int
    fixed_arrival_time: time | None = None
    locked: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TripScheduleItem":
        try:
            visit_minutes = int(value["visit_minutes"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("visit_minutes는 정수여야 합니다.") from exc
        if not 5 <= visit_minutes <= 720:
            raise ValueError("visit_minutes는 5~720분이어야 합니다.")
        place_value = value.get("place")
        if not isinstance(place_value, Mapping):
            raise ValueError("지도에서 선택한 place 객체가 필요합니다.")
        return cls(
            item_id=_required_text(value.get("item_id"), "일정 ID"),
            place=SelectedPlace.from_mapping(place_value),
            visit_minutes=visit_minutes,
            fixed_arrival_time=_parse_time(
                value.get("fixed_arrival_time"), "fixed_arrival_time"
            ),
            locked=bool(value.get("locked", False)),
        )


@dataclass(frozen=True)
class TripPlan:
    """MVP 여행 계획 교환 규격."""

    title: str
    trip_date: date
    start_time: time | None = None
    schedules: tuple[TripScheduleItem, ...] = field(default_factory=tuple)
    schema_version: str = SCHEMA_VERSION
    walking_to_bus_threshold_minutes: int = 15

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TripPlan":
        raw_schedules = value.get("schedules", [])
        if not isinstance(raw_schedules, list):
            raise ValueError("schedules는 배열이어야 합니다.")
        schedules = tuple(
            TripScheduleItem.from_mapping(item)
            for item in raw_schedules
            if isinstance(item, Mapping)
        )
        if len(schedules) != len(raw_schedules):
            raise ValueError("모든 schedules 항목은 객체여야 합니다.")
        ids = [item.item_id for item in schedules]
        if len(ids) != len(set(ids)):
            raise ValueError("일정 ID는 중복될 수 없습니다.")
        threshold = int(value.get("walking_to_bus_threshold_minutes", 15))
        if not 1 <= threshold <= 60:
            raise ValueError("도보→버스 기준은 1~60분이어야 합니다.")
        return cls(
            title=_required_text(value.get("title"), "여행 제목"),
            trip_date=_parse_date(value.get("trip_date")),
            start_time=_parse_time(value.get("start_time"), "start_time"),
            schedules=schedules,
            schema_version=str(value.get("schema_version") or SCHEMA_VERSION),
            walking_to_bus_threshold_minutes=threshold,
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["trip_date"] = self.trip_date.isoformat()
        result["start_time"] = (
            self.start_time.isoformat(timespec="minutes")
            if self.start_time is not None
            else None
        )
        for index, item in enumerate(self.schedules):
            fixed = item.fixed_arrival_time
            result["schedules"][index]["fixed_arrival_time"] = (
                fixed.isoformat(timespec="minutes") if fixed is not None else None
            )
        return result


def validate_trip_plan(value: Mapping[str, Any]) -> dict[str, Any]:
    return TripPlan.from_mapping(value).to_dict()


def replace_schedule_item(
    plan: TripPlan,
    item_id: str,
    *,
    place: SelectedPlace,
    visit_minutes: int | None = None,
    fixed_arrival_time: time | None = None,
    locked: bool | None = None,
) -> TripPlan:
    """문제가 생긴 일정 하나를 사용자가 선택한 새 후보로 교체한다."""

    updated: list[TripScheduleItem] = []
    matched = False
    for item in plan.schedules:
        if item.item_id != item_id:
            updated.append(item)
            continue
        matched = True
        updated.append(replace(
            item,
            place=place,
            visit_minutes=(
                visit_minutes if visit_minutes is not None else item.visit_minutes
            ),
            fixed_arrival_time=(
                fixed_arrival_time
                if fixed_arrival_time is not None
                else item.fixed_arrival_time
            ),
            locked=locked if locked is not None else item.locked,
        ))
    if not matched:
        raise ValueError(f"일정 ID를 찾을 수 없습니다: {item_id}")
    return replace(plan, schedules=tuple(updated))


def next_schedule_item(
    plan: TripPlan, item_id: str
) -> TripScheduleItem | None:
    """주어진 일정 바로 다음 항목을 반환한다. 마지막 일정이면 None이다."""

    ids = [item.item_id for item in plan.schedules]
    try:
        index = ids.index(item_id)
    except ValueError:
        raise ValueError(f"일정 ID를 찾을 수 없습니다: {item_id}") from None
    if index + 1 >= len(plan.schedules):
        return None
    return plan.schedules[index + 1]


def build_next_schedule_constraint(
    plan: TripPlan,
    next_item: TripScheduleItem,
    *,
    buffer_minutes: float = 10.0,
) -> NextScheduleConstraint | None:
    """다음 일정에 고정 도착시각이 있을 때만 추천 엔진 제약으로 변환한다."""

    if next_item.fixed_arrival_time is None:
        return None
    deadline = datetime.combine(plan.trip_date, next_item.fixed_arrival_time)
    return NextScheduleConstraint(
        longitude=next_item.place.longitude,
        latitude=next_item.place.latitude,
        arrival_deadline=deadline,
        visit_minutes=float(next_item.visit_minutes),
        buffer_minutes=buffer_minutes,
        title=next_item.place.name,
    )
