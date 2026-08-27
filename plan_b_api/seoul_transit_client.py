"""ODsay 기반 대중교통(버스+지하철) 환승경로 조회 클라이언트."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .route_client import WalkingStep


ODSAY_PATH_URL = "https://api.odsay.com/v1/api/searchPubTransPathT"
TRAFFIC_TYPE_SUBWAY = 1
TRAFFIC_TYPE_BUS = 2
TRAFFIC_TYPE_WALK = 3
Transport = Callable[[str, float], tuple[int, bytes]]


class SeoulTransitApiError(RuntimeError):
    """ODsay 대중교통 경로 호출 또는 응답 오류."""


@dataclass(frozen=True)
class SeoulTransitConfig:
    api_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = ODSAY_PATH_URL

    @classmethod
    def from_env(cls) -> "SeoulTransitConfig":
        api_key = os.getenv("ODSAY_API_KEY", "").strip()
        if not api_key:
            raise ValueError(
                "환경변수 ODSAY_API_KEY가 필요합니다. "
                "https://lab.odsay.com 에서 회원가입 후 API 키를 발급받으세요."
            )
        return cls(
            api_key=api_key,
            timeout_seconds=float(os.getenv("ODSAY_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("ODSAY_MAX_RETRIES", "2")),
            base_url=os.getenv("ODSAY_PATH_URL", ODSAY_PATH_URL).strip()
            or ODSAY_PATH_URL,
        )


@dataclass(frozen=True)
class SeoulTransitLeg:
    mode: str
    instruction: str
    lane_name: str | None = None
    start_name: str | None = None
    end_name: str | None = None
    start_entrance_no: str | None = None
    end_exit_no: str | None = None
    station_count: int | None = None
    distance_meters: float | None = None
    duration_minutes: float | None = None
    start_longitude: float | None = None
    start_latitude: float | None = None
    end_longitude: float | None = None
    end_latitude: float | None = None
    start_entrance_longitude: float | None = None
    start_entrance_latitude: float | None = None
    end_exit_longitude: float | None = None
    end_exit_latitude: float | None = None
    geometry: dict[str, Any] | None = None
    steps: tuple[WalkingStep, ...] = ()


@dataclass(frozen=True)
class SeoulTransitRoute:
    duration_minutes: float
    distance_meters: float | None = None
    walking_minutes: float | None = None
    walking_distance_meters: float | None = None
    transfer_count: int | None = None
    route_type: str = "버스+지하철"
    geometry: dict[str, Any] | None = None
    legs: tuple[SeoulTransitLeg, ...] = ()


def _default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "PlanB-API/0.1"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    number = _optional_float(value)
    return int(number) if number is not None else None


def _optional_coordinate(value: Any) -> float | None:
    """0.0은 ODsay가 출입구 좌표 없음을 나타내는 값이라 좌표로 취급하지 않는다."""

    number = _optional_float(value)
    return number if number else None


def _lane_name(sub_path: Mapping[str, Any]) -> str | None:
    lane = sub_path.get("lane")
    if isinstance(lane, list):
        lane = lane[0] if lane else None
    if isinstance(lane, Mapping):
        name = lane.get("name") or lane.get("busNo")
        return str(name) if name else None
    return None


def _route_type_summary(sub_paths: list[Mapping[str, Any]]) -> str:
    names: list[str] = []
    for sub_path in sub_paths:
        traffic_type = sub_path.get("trafficType")
        if traffic_type == TRAFFIC_TYPE_WALK:
            continue
        name = _lane_name(sub_path)
        if name and (not names or names[-1] != name):
            names.append(name)
    return " → ".join(names) if names else "버스+지하철"


def _clean_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _leg_geometry_from_stations(
    sub_path: Mapping[str, Any],
) -> dict[str, Any] | None:
    stations = ((sub_path.get("passStopList") or {}).get("stations")) or []
    coordinates: list[list[float]] = []
    for station in stations:
        try:
            lon, lat = float(station["x"]), float(station["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if coordinates and coordinates[-1] == [lon, lat]:
            continue
        coordinates.append([lon, lat])
    if len(coordinates) < 2:
        return None
    return {"type": "LineString", "coordinates": coordinates}


def _build_leg(sub_path: Mapping[str, Any]) -> SeoulTransitLeg:
    traffic_type = sub_path.get("trafficType")
    lane_name = _lane_name(sub_path)
    start_name = _clean_text(sub_path.get("startName"))
    end_name = _clean_text(sub_path.get("endName"))
    station_count = _optional_int(sub_path.get("stationCount"))
    distance = _optional_float(sub_path.get("distance"))
    duration = _optional_float(sub_path.get("sectionTime"))
    start_entrance_no = _clean_text(sub_path.get("startExitNo"))
    end_exit_no = _clean_text(sub_path.get("endExitNo"))

    if traffic_type == TRAFFIC_TYPE_WALK:
        mode = "도보"
        parts = []
        if distance:
            parts.append(f"{int(distance)}m")
        if duration:
            parts.append(f"{round(duration, 1)}분")
        instruction = (
            "도보로 " + " · ".join(parts) + " 이동" if parts else "도보 이동"
        )
    elif traffic_type in (TRAFFIC_TYPE_SUBWAY, TRAFFIC_TYPE_BUS):
        mode = "지하철" if traffic_type == TRAFFIC_TYPE_SUBWAY else "버스"
        unit = "정거장" if traffic_type == TRAFFIC_TYPE_SUBWAY else "정류장"
        segments = []
        if traffic_type == TRAFFIC_TYPE_SUBWAY and start_entrance_no:
            segments.append(f"{start_name or '승차역'} {start_entrance_no}번 입구로 진입")
        board = f"{lane_name or mode} 승차" + (f" ({start_name})" if start_name else "")
        alight = f"{end_name or '하차 정류장'}에서 하차"
        if station_count:
            alight += f" ({station_count}개 {unit} 이동)"
        segments.append(f"{board} → {alight}")
        if traffic_type == TRAFFIC_TYPE_SUBWAY and end_exit_no:
            segments.append(f"{end_exit_no}번 출구로 이동")
        instruction = " · ".join(segments)
    else:
        mode = "이동"
        instruction = "구간 이동"

    return SeoulTransitLeg(
        mode=mode,
        instruction=instruction,
        lane_name=lane_name,
        start_name=start_name,
        end_name=end_name,
        start_entrance_no=start_entrance_no,
        end_exit_no=end_exit_no,
        station_count=station_count,
        distance_meters=distance,
        duration_minutes=round(duration, 1) if duration is not None else None,
        start_longitude=_optional_float(sub_path.get("startX")),
        start_latitude=_optional_float(sub_path.get("startY")),
        end_longitude=_optional_float(sub_path.get("endX")),
        end_latitude=_optional_float(sub_path.get("endY")),
        start_entrance_longitude=_optional_coordinate(sub_path.get("startExitX")),
        start_entrance_latitude=_optional_coordinate(sub_path.get("startExitY")),
        end_exit_longitude=_optional_coordinate(sub_path.get("endExitX")),
        end_exit_latitude=_optional_coordinate(sub_path.get("endExitY")),
        geometry=_leg_geometry_from_stations(sub_path),
    )


def combine_leg_geometries(
    legs: tuple[SeoulTransitLeg, ...],
) -> dict[str, Any] | None:
    """구간별 geometry를 순서대로 이어붙여 하나의 경로선을 만든다."""

    coordinates: list[list[float]] = []
    for leg in legs:
        geometry = leg.geometry
        if not geometry or geometry.get("type") != "LineString":
            continue
        for point in geometry.get("coordinates") or []:
            try:
                lon, lat = float(point[0]), float(point[1])
            except (TypeError, ValueError, IndexError):
                continue
            if coordinates and coordinates[-1] == [lon, lat]:
                continue
            coordinates.append([lon, lat])
    if len(coordinates) < 2:
        return None
    return {"type": "LineString", "coordinates": coordinates}


def fill_walk_leg_endpoints(
    legs: tuple[SeoulTransitLeg, ...],
    *,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
) -> tuple[SeoulTransitLeg, ...]:
    """도보 구간은 좌표가 없으므로 앞뒤 구간(또는 전체 출발·도착지)에서 채운다."""

    filled: list[SeoulTransitLeg] = list(legs)
    for i, leg in enumerate(filled):
        if leg.mode != "도보":
            continue
        if i > 0 and filled[i - 1].end_longitude is not None:
            walk_start = (filled[i - 1].end_longitude, filled[i - 1].end_latitude)
        else:
            walk_start = (start_x, start_y)
        if i + 1 < len(filled) and filled[i + 1].start_longitude is not None:
            walk_end = (filled[i + 1].start_longitude, filled[i + 1].start_latitude)
        else:
            walk_end = (end_x, end_y)
        geometry = leg.geometry or {
            "type": "LineString",
            "coordinates": [list(walk_start), list(walk_end)],
        }
        filled[i] = replace(
            leg,
            start_longitude=walk_start[0],
            start_latitude=walk_start[1],
            end_longitude=walk_end[0],
            end_latitude=walk_end[1],
            geometry=geometry,
        )
    return tuple(filled)


def parse_seoul_transit_response(body: bytes) -> SeoulTransitRoute:
    text = body.decode("utf-8-sig", errors="replace").strip()
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SeoulTransitApiError(
            "ODsay 대중교통 경로 API가 JSON을 반환하지 않았습니다."
        ) from exc

    if not isinstance(payload, Mapping):
        raise SeoulTransitApiError("ODsay 대중교통 경로 응답 형식이 올바르지 않습니다.")

    error = payload.get("error")
    if isinstance(error, list) and error:
        # 인증·요청 오류(예: ApiKeyAuthFailed)는 배열 형태로 내려온다.
        first = error[0]
        message = (
            (first.get("message") or first.get("msg") or first)
            if isinstance(first, Mapping)
            else first
        )
        raise SeoulTransitApiError(f"ODsay 대중교통 경로 오류: {message}")
    if isinstance(error, Mapping):
        message = error.get("msg") or error.get("message") or error
        raise SeoulTransitApiError(f"ODsay 대중교통 경로 오류: {message}")

    result = payload.get("result")
    paths = (result or {}).get("path") if isinstance(result, Mapping) else None
    if not paths:
        raise SeoulTransitApiError("ODsay 대중교통 경로 결과가 없습니다.")

    best = min(
        (p for p in paths if isinstance(p, Mapping) and (p.get("info") or {}).get("totalTime")),
        key=lambda p: p["info"]["totalTime"],
        default=None,
    )
    if best is None:
        raise SeoulTransitApiError("ODsay 대중교통 경로 결과가 없습니다.")

    info = best.get("info") or {}
    duration = _optional_float(info.get("totalTime"))
    if duration is None:
        raise SeoulTransitApiError("ODsay 응답에 totalTime이 없습니다.")

    sub_paths = [sp for sp in (best.get("subPath") or []) if isinstance(sp, Mapping)]
    walk_segments = [sp for sp in sub_paths if sp.get("trafficType") == TRAFFIC_TYPE_WALK]
    info_walk_minutes = _optional_float(info.get("totalWalkTime"))
    walking_minutes = (
        info_walk_minutes
        if info_walk_minutes is not None and info_walk_minutes >= 0
        else (sum(_optional_float(sp.get("sectionTime")) or 0 for sp in walk_segments) or None)
    )
    walking_distance = (
        sum(_optional_float(sp.get("distance")) or 0 for sp in walk_segments) or None
    )
    transfer_count = _optional_int(info.get("busTransitCount") or 0) or 0
    transfer_count += _optional_int(info.get("subwayTransitCount") or 0) or 0

    legs = tuple(_build_leg(sp) for sp in sub_paths)
    return SeoulTransitRoute(
        duration_minutes=round(duration, 2),
        distance_meters=_optional_float(info.get("totalDistance")),
        walking_minutes=walking_minutes,
        walking_distance_meters=walking_distance,
        transfer_count=transfer_count,
        route_type=_route_type_summary(sub_paths),
        geometry=combine_leg_geometries(legs),
        legs=legs,
    )


class SeoulTransitClient:
    def __init__(
        self,
        config: SeoulTransitConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "SeoulTransitClient":
        return cls(SeoulTransitConfig.from_env())

    def route(
        self,
        *,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
    ) -> SeoulTransitRoute:
        query = urlencode({
            "apiKey": self.config.api_key,
            "SX": f"{start_x:.7f}",
            "SY": f"{start_y:.7f}",
            "EX": f"{end_x:.7f}",
            "EY": f"{end_y:.7f}",
        })
        url = f"{self.config.base_url}?{query}"
        status, response_body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, response_body = self._transport(
                    url, self.config.timeout_seconds
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise SeoulTransitApiError(
                        f"ODsay 대중교통 경로 네트워크 오류: {exc}"
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))
        if status >= 400:
            detail = response_body.decode("utf-8-sig", errors="replace")[:300]
            raise SeoulTransitApiError(
                f"ODsay 대중교통 경로 HTTP 오류 {status}: {detail}"
            )
        route = parse_seoul_transit_response(response_body)
        legs = fill_walk_leg_endpoints(
            route.legs,
            start_x=start_x, start_y=start_y,
            end_x=end_x, end_y=end_y,
        )
        geometry = combine_leg_geometries(legs) or {
            "type": "LineString",
            "coordinates": [[start_x, start_y], [end_x, end_y]],
        }
        return replace(route, legs=legs, geometry=geometry)
