"""TourAPI 상세 응답을 복구 엔진용 데이터로 정규화한다."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Mapping


WEEKDAY_NAMES = {
    "월요일": 0,
    "화요일": 1,
    "수요일": 2,
    "목요일": 3,
    "금요일": 4,
    "토요일": 5,
    "일요일": 6,
}


@dataclass(frozen=True)
class OperatingWindow:
    """월별 운영시간 구간."""

    months: tuple[int, ...]
    open_time: str
    close_time: str
    last_admission_time: str | None = None


@dataclass(frozen=True)
class NormalizedPlace:
    """추천·필터링에 사용하는 관광지 표준 모델."""

    content_id: str
    content_type_id: str
    title: str
    address: str
    longitude: float | None
    latitude: float | None
    overview: str
    homepage: str
    image_url: str
    copyright_code: str
    closed_weekdays: tuple[int, ...]
    operating_windows: tuple[OperatingWindow, ...]
    event_start_date: date | None
    event_end_date: date | None
    session_times: tuple[str, ...]
    reservation_required: bool | None
    adult_fee_krw: int | None
    is_free: bool | None
    parking_available: bool | None
    toilet_available: bool | None
    raw_rest_date: str
    raw_operating_hours: str
    raw_fee: str
    raw_booking: str
    normalization_confidence: float
    warnings: tuple[str, ...]


def _first(items: Any) -> dict[str, Any]:
    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    if isinstance(items, dict):
        return items
    return {}


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_months(expression: str) -> tuple[int, ...]:
    months: set[int] = set()
    for part in expression.split("/"):
        numbers = [int(value) for value in re.findall(r"(\d{1,2})월", part)]
        if len(numbers) >= 2:
            start, end = numbers[0], numbers[1]
            if 1 <= start <= 12 and 1 <= end <= 12:
                if start <= end:
                    months.update(range(start, end + 1))
                else:
                    months.update(range(start, 13))
                    months.update(range(1, end + 1))
        elif numbers and 1 <= numbers[0] <= 12:
            months.add(numbers[0])
    return tuple(sorted(months))


def parse_operating_windows(text: str) -> tuple[OperatingWindow, ...]:
    """월 구간과 운영·입장마감 시각을 추출한다."""

    if not text:
        return ()

    pattern = re.compile(
        r"\[([^\]]+)\]\s*"
        r"(\d{1,2}:\d{2})\s*[~\-]\s*(\d{1,2}:\d{2})"
        r"(?:\s*\(입장마감\s*(\d{1,2}:\d{2})\))?"
    )
    windows: list[OperatingWindow] = []
    for match in pattern.finditer(text):
        months = _parse_months(match.group(1))
        if not months:
            continue
        windows.append(
            OperatingWindow(
                months=months,
                open_time=match.group(2),
                close_time=match.group(3),
                last_admission_time=match.group(4),
            )
        )

    if windows:
        return tuple(windows)

    simple_matches = list(re.finditer(
        r"(\d{1,2}:\d{2})\s*[~\-]\s*(\d{1,2}:\d{2})"
        r"(?:\s*\(입장마감\s*(\d{1,2}:\d{2})\))?",
        text,
    ))
    if not simple_matches:
        return ()

    return tuple(
        OperatingWindow(
            months=tuple(range(1, 13)),
            open_time=match.group(1),
            close_time=match.group(2),
            last_admission_time=match.group(3),
        )
        for match in simple_matches
    )


def parse_closed_weekdays(text: str) -> tuple[int, ...]:
    """휴무 문구에서 요일을 추출한다. 월요일은 0이다."""

    if not text or "연중무휴" in text or "무휴" in text:
        return ()
    return tuple(
        index for name, index in WEEKDAY_NAMES.items() if name in text
    )


def parse_adult_fee(text: str) -> tuple[int | None, bool | None]:
    """입장료 문구에서 일반 성인 요금을 우선 추출한다."""

    if not text:
        return None, None

    adult_patterns = (
        r"개인\s*대인\s*([\d,]+)\s*원",
        r"성인\s*([\d,]+)\s*원",
        r"대인\s*([\d,]+)\s*원",
    )
    for pattern in adult_patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1).replace(",", "")), False

    thousand = re.search(r"(\d+(?:\.\d+)?)\s*천\s*원", text)
    if thousand:
        return int(float(thousand.group(1)) * 1000), False

    prices = [
        int(value.replace(",", ""))
        for value in re.findall(r"([\d,]+)\s*원", text)
        if value.replace(",", "").isdigit()
    ]
    if prices:
        return prices[0], False
    if "무료" in text:
        return 0, True
    return None, None


def parse_event_date(value: Any) -> date | None:
    """YYYYMMDD·YYYY-MM-DD·YYYY.MM.DD 형식을 날짜로 변환한다."""

    text = re.sub(r"\D", "", str(value or ""))
    if len(text) != 8:
        return None
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        return None


def parse_session_times(text: str) -> tuple[str, ...]:
    """회차 시작시각만 추출하고 시간 범위의 종료시각은 제외한다."""

    text = text or ""
    entries: list[tuple[int, str]] = []
    covered: list[tuple[int, int]] = []
    range_pattern = re.compile(
        r"(?<!\d)(\d{1,2}):(\d{2})\s*[~\-]\s*"
        r"(\d{1,2}):(\d{2})(?!\d)"
    )
    for match in range_pattern.finditer(text):
        hour, minute = match.group(1), match.group(2)
        if int(hour) <= 23 and int(minute) <= 59:
            entries.append((match.start(), f"{int(hour):02d}:{minute}"))
        covered.append(match.span())

    single_pattern = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
    for match in single_pattern.finditer(text):
        if any(start <= match.start() < end for start, end in covered):
            continue
        hour, minute = match.group(1), match.group(2)
        if int(hour) > 23 or int(minute) > 59:
            continue
        entries.append((match.start(), f"{int(hour):02d}:{minute}"))

    result: list[str] = []
    for _, value in sorted(entries):
        if value not in result:
            result.append(value)
    return tuple(result)


def parse_reservation_required(text: str) -> bool | None:
    """예약·예매 문구로 사전 예약 필요 여부를 판정한다."""

    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return None
    if any(word in compact for word in ("예약불필요", "예약없이", "현장접수")):
        return False
    if any(word in compact for word in ("사전예약", "예약필수", "예매", "예약제")):
        return True
    return None


def _parse_boolean_availability(text: str) -> bool | None:
    if not text:
        return None
    compact = re.sub(r"\s+", "", text)
    if any(word in compact for word in ("불가", "없음", "미운영")):
        return False
    if any(word in compact for word in ("가능", "있음", "운영")):
        return True
    return None


def _info_map(items: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if not isinstance(items, list):
        return result
    for item in items:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("infoname", "")).strip()
        text = str(item.get("infotext", "")).strip()
        if name:
            result[name] = text
    return result


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def normalize_place(bundle: Mapping[str, Any]) -> NormalizedPlace:
    """`detail_bundle` 결과를 `NormalizedPlace`로 변환한다."""

    common = _first(bundle.get("common"))
    intro = _first(bundle.get("intro"))
    info = _info_map(bundle.get("info"))
    images = bundle.get("images")
    first_image = _first(images)

    raw_hours = _first_nonempty(
        intro.get("usetime"),
        intro.get("playtime"),
        intro.get("opentimefood"),
        intro.get("opentime"),
        info.get("이용시간"),
        info.get("공연시간"),
    )
    raw_rest = _first_nonempty(
        intro.get("restdate"),
        intro.get("restdatefood"),
        intro.get("restdateshopping"),
    )
    raw_fee = _first_nonempty(
        info.get("입장료"),
        info.get("이용요금"),
        info.get("이용료"),
        info.get("행사요금"),
        intro.get("usetimefestival"),
        intro.get("usefee"),
    )
    raw_booking = _first_nonempty(
        intro.get("bookingplace"),
        intro.get("reservationfood"),
        info.get("예약안내"),
        info.get("예매안내"),
    )
    fee, is_free = parse_adult_fee(raw_fee)
    windows = parse_operating_windows(raw_hours)
    event_start = parse_event_date(intro.get("eventstartdate"))
    event_end = parse_event_date(intro.get("eventenddate"))
    sessions = parse_session_times(
        " ".join(
            filter(
                None,
                (
                    str(intro.get("playtime", "")).strip(),
                    info.get("공연시간", ""),
                    info.get("행사시간", ""),
                ),
            )
        )
    )
    reservation_required = parse_reservation_required(
        " ".join(
            filter(
                None,
                (
                    raw_booking,
                    raw_fee,
                    str(intro.get("playtime", "")).strip(),
                    str(common.get("overview", "")).strip(),
                ),
            )
        )
    )
    closed_weekdays = parse_closed_weekdays(raw_rest)

    parking_text = _first_nonempty(
        intro.get("parking"),
        intro.get("parkingfood"),
        intro.get("parkingshopping"),
    )
    toilet_text = info.get("화장실", "")
    longitude = _optional_float(common.get("mapx"))
    latitude = _optional_float(common.get("mapy"))

    warnings: list[str] = []
    if not windows and not sessions:
        warnings.append("운영시간 정규화 실패 또는 정보 없음")
    if not raw_rest and event_start is None:
        warnings.append("휴무 정보 없음")
    if fee is None:
        warnings.append("일반 성인 입장료 정보 없음")
    if longitude is None or latitude is None:
        warnings.append("좌표 정보 없음")

    checks = (
        bool(common.get("title")),
        longitude is not None and latitude is not None,
        bool(windows or sessions),
        bool(raw_rest or event_start),
        fee is not None,
        bool(common.get("firstimage") or first_image.get("originimgurl")),
    )
    confidence = round(sum(checks) / len(checks), 3)

    return NormalizedPlace(
        content_id=str(bundle.get("content_id") or common.get("contentid") or ""),
        content_type_id=str(
            bundle.get("content_type_id") or common.get("contenttypeid") or ""
        ),
        title=str(common.get("title", "")).strip(),
        address=" ".join(
            part
            for part in (
                str(common.get("addr1", "")).strip(),
                str(common.get("addr2", "")).strip(),
            )
            if part
        ),
        longitude=longitude,
        latitude=latitude,
        overview=str(common.get("overview", "")).strip(),
        homepage=str(common.get("homepage", "")).strip(),
        image_url=str(
            common.get("firstimage")
            or first_image.get("originimgurl")
            or ""
        ).strip(),
        copyright_code=str(
            common.get("cpyrhtDivCd")
            or first_image.get("cpyrhtDivCd")
            or ""
        ).strip(),
        closed_weekdays=closed_weekdays,
        operating_windows=windows,
        event_start_date=event_start,
        event_end_date=event_end,
        session_times=sessions,
        reservation_required=reservation_required,
        adult_fee_krw=fee,
        is_free=is_free,
        parking_available=_parse_boolean_availability(parking_text),
        toilet_available=_parse_boolean_availability(toilet_text),
        raw_rest_date=raw_rest,
        raw_operating_hours=raw_hours,
        raw_fee=raw_fee,
        raw_booking=raw_booking,
        normalization_confidence=confidence,
        warnings=tuple(warnings),
    )


def is_open_at(place: NormalizedPlace, when: datetime) -> bool | None:
    """정규화된 정기 운영정보 기준 영업 여부를 반환한다."""

    if place.event_start_date and when.date() < place.event_start_date:
        return False
    if place.event_end_date and when.date() > place.event_end_date:
        return False
    if when.weekday() in place.closed_weekdays:
        return False
    if not place.operating_windows:
        return None

    matching = [
        window for window in place.operating_windows if when.month in window.months
    ]
    if not matching:
        return None

    minute = when.hour * 60 + when.minute
    for window in matching:
        open_hour, open_minute = map(int, window.open_time.split(":"))
        close_hour, close_minute = map(int, window.close_time.split(":"))
        start = open_hour * 60 + open_minute
        end = close_hour * 60 + close_minute
        if window.last_admission_time:
            last_hour, last_minute = map(
                int, window.last_admission_time.split(":")
            )
            end = min(end, last_hour * 60 + last_minute)
        if start <= minute < end:
            return True
    return False
