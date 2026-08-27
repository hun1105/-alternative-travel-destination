"""서울시 실시간 인구데이터 장소 혼잡도 클라이언트."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


SEOUL_CROWD_BASE_URL = "http://openapi.seoul.go.kr:8088"
SEOUL_AREA_DATA = Path(__file__).with_name("data") / "seoul_crowd_areas.json"
SEOUL_AREA_CROWD_INFLUENCE = 0.6
SEOUL_AREA_CROWD_NOTICE = (
    "서울시 혼잡도는 영역별 데이터이므로 개별 장소·건물의 실제 "
    "혼잡도와 다를 수 있습니다."
)
Transport = Callable[[str, float], tuple[int, bytes]]


class SeoulCrowdApiError(RuntimeError):
    """서울시 실시간 인구데이터 호출 또는 응답 오류."""


@dataclass(frozen=True)
class SeoulCrowdConfig:
    service_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = SEOUL_CROWD_BASE_URL

    @classmethod
    def from_env(cls) -> "SeoulCrowdConfig":
        service_key = os.getenv("SEOUL_OPEN_API_KEY", "").strip()
        if not service_key:
            raise ValueError("환경변수 SEOUL_OPEN_API_KEY가 필요합니다.")
        return cls(
            service_key=service_key,
            timeout_seconds=float(os.getenv("SEOUL_CROWD_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("SEOUL_CROWD_MAX_RETRIES", "2")),
            base_url=os.getenv("SEOUL_CROWD_BASE_URL", SEOUL_CROWD_BASE_URL).strip()
            or SEOUL_CROWD_BASE_URL,
        )


@dataclass(frozen=True)
class SeoulCrowdArea:
    area_code: str
    category: str
    name: str
    rings: tuple[tuple[tuple[float, float], ...], ...]


@dataclass(frozen=True)
class SeoulCrowdSnapshot:
    area_code: str
    area_name: str
    congestion_label: str
    normalized_level: float
    population_min: int | None
    population_max: int | None
    measured_at: str
    forecast_time: str | None = None
    is_forecast: bool = False
    message: str = ""


def normalized_congestion(label: str) -> float:
    compact = " ".join(label.split())
    mapping = {
        "여유": 0.0,
        "보통": 1 / 3,
        "약간 붐빔": 2 / 3,
        "약간붐빔": 2 / 3,
        "혼잡": 2 / 3,
        "붐빔": 1.0,
        "매우 혼잡": 1.0,
    }
    if compact not in mapping:
        raise SeoulCrowdApiError(f"알 수 없는 서울시 혼잡도 단계: {label}")
    return mapping[compact]


def scoring_area_crowd_level(raw_level: float) -> float:
    """영역 혼잡도 60%와 중립 혼잡도 40%를 혼합한다."""

    bounded = max(0.0, min(1.0, raw_level))
    neutral_level = 0.5
    return round(
        SEOUL_AREA_CROWD_INFLUENCE * bounded
        + (1 - SEOUL_AREA_CROWD_INFLUENCE) * neutral_level,
        4,
    )


def _point_in_ring(x: float, y: float, ring: tuple[tuple[float, float], ...]) -> bool:
    inside = False
    if len(ring) < 3:
        return False
    previous_x, previous_y = ring[-1]
    for current_x, current_y in ring:
        crosses = (current_y > y) != (previous_y > y)
        if crosses:
            intersection_x = (
                (previous_x - current_x)
                * (y - current_y)
                / (previous_y - current_y)
                + current_x
            )
            if x < intersection_x:
                inside = not inside
        previous_x, previous_y = current_x, current_y
    return inside


def point_in_area(longitude: float, latitude: float, area: SeoulCrowdArea) -> bool:
    """SHP의 외곽·내부 링을 홀짝 규칙으로 판정한다."""

    return sum(
        _point_in_ring(longitude, latitude, ring) for ring in area.rings
    ) % 2 == 1


def load_seoul_crowd_areas(
    path: str | Path = SEOUL_AREA_DATA,
) -> tuple[SeoulCrowdArea, ...]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    return tuple(
        SeoulCrowdArea(
            area_code=str(value["area_code"]),
            category=str(value["category"]),
            name=str(value["name"]),
            rings=tuple(
                tuple((float(point[0]), float(point[1])) for point in ring)
                for ring in value["rings"]
            ),
        )
        for value in raw
    )


def find_seoul_crowd_area(
    longitude: float,
    latitude: float,
    areas: tuple[SeoulCrowdArea, ...] | None = None,
) -> SeoulCrowdArea | None:
    candidates = [
        area
        for area in (areas or load_seoul_crowd_areas())
        if point_in_area(longitude, latitude, area)
    ]
    if not candidates:
        return None
    return min(
        candidates,
        key=lambda area: sum(len(ring) for ring in area.rings),
    )


def find_seoul_crowd_area_by_name(
    name_or_code: str,
    areas: tuple[SeoulCrowdArea, ...] | None = None,
) -> SeoulCrowdArea | None:
    target = "".join(name_or_code.split()).casefold()
    matches = [
        area
        for area in (areas or load_seoul_crowd_areas())
        if target in {
            "".join(area.name.split()).casefold(),
            area.area_code.casefold(),
        }
    ]
    return matches[0] if len(matches) == 1 else None


def _default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = Request(url, headers={"User-Agent": "PlanB-API/0.1"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _walk_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d %H:%M", "%Y%m%d%H%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class SeoulCrowdClient:
    def __init__(
        self,
        config: SeoulCrowdConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "SeoulCrowdClient":
        return cls(SeoulCrowdConfig.from_env())

    def crowd(
        self,
        area_name_or_code: str,
        *,
        target_time: datetime | None = None,
    ) -> SeoulCrowdSnapshot:
        area_path = quote(area_name_or_code, safe="")
        key_path = quote(self.config.service_key, safe="")
        url = (
            f"{self.config.base_url.rstrip('/')}/{key_path}/json/"
            f"citydata_ppltn/1/5/{area_path}"
        )
        status, body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, body = self._transport(url, self.config.timeout_seconds)
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise SeoulCrowdApiError(
                        f"서울시 혼잡도 네트워크 오류: {exc}"
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))
        if status >= 400:
            detail = body.decode("utf-8-sig", errors="replace")[:300]
            raise SeoulCrowdApiError(
                f"서울시 혼잡도 HTTP 오류 {status}: {detail}"
            )
        try:
            response = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SeoulCrowdApiError(
                "서울시 혼잡도 API가 JSON을 반환하지 않았습니다."
            ) from exc

        result = next(
            (
                value["RESULT"]
                for value in _walk_dicts(response)
                if isinstance(value.get("RESULT"), dict)
            ),
            None,
        )
        if result and str(result.get("CODE", "INFO-000")) != "INFO-000":
            raise SeoulCrowdApiError(
                f"서울시 혼잡도 응답 오류 {result.get('CODE')}: "
                f"{result.get('MESSAGE')}"
            )

        current = next(
            (value for value in _walk_dicts(response) if value.get("AREA_CONGEST_LVL")),
            None,
        )
        if current is None:
            message = next(
                (
                    value.get("MESSAGE")
                    for value in _walk_dicts(response)
                    if value.get("MESSAGE")
                ),
                "혼잡도 데이터 없음",
            )
            raise SeoulCrowdApiError(f"서울시 혼잡도 결과가 없습니다: {message}")

        selected = current
        is_forecast = False
        forecast_time: str | None = None
        if target_time is not None:
            forecasts = [
                value
                for value in _walk_dicts(current.get("FCST_PPLTN") or [])
                if value.get("FCST_CONGEST_LVL") and _parse_time(value.get("FCST_TIME"))
            ]
            if forecasts:
                nearest = min(
                    forecasts,
                    key=lambda value: abs(
                        (_parse_time(value.get("FCST_TIME")) - target_time).total_seconds()
                    ),
                )
                nearest_time = _parse_time(nearest.get("FCST_TIME"))
                if nearest_time and abs((nearest_time - target_time).total_seconds()) <= 3600:
                    selected = {
                        **current,
                        "AREA_CONGEST_LVL": nearest["FCST_CONGEST_LVL"],
                        "AREA_PPLTN_MIN": nearest.get("FCST_PPLTN_MIN"),
                        "AREA_PPLTN_MAX": nearest.get("FCST_PPLTN_MAX"),
                    }
                    is_forecast = True
                    forecast_time = str(nearest.get("FCST_TIME") or "")

        label = str(selected["AREA_CONGEST_LVL"]).strip()
        return SeoulCrowdSnapshot(
            area_code=str(current.get("AREA_CD") or area_name_or_code),
            area_name=str(current.get("AREA_NM") or area_name_or_code),
            congestion_label=label,
            normalized_level=normalized_congestion(label),
            population_min=_optional_int(selected.get("AREA_PPLTN_MIN")),
            population_max=_optional_int(selected.get("AREA_PPLTN_MAX")),
            measured_at=str(current.get("PPLTN_TIME") or ""),
            forecast_time=forecast_time,
            is_forecast=is_forecast,
            message=str(current.get("AREA_CONGEST_MSG") or ""),
        )
