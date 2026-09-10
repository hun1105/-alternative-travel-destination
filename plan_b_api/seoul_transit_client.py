"""공공데이터포털 및 ODsay 기반 대중교통(버스+지하철) 환승경로 조회 클라이언트."""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, replace
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .route_client import WalkingStep


SEOUL_TRANSIT_BUS_N_SUB_URL = (
    "http://ws.bus.go.kr/api/rest/pathinfo/getPathInfoByBusNSub"
)
ODSAY_PATH_URL = "https://api.odsay.com/v1/api/searchPubTransPathT"
TRAFFIC_TYPE_SUBWAY = 1
TRAFFIC_TYPE_BUS = 2
TRAFFIC_TYPE_WALK = 3
Transport = Callable[[str, float], tuple[int, bytes]]


class SeoulTransitApiError(RuntimeError):
    """대중교통 경로 호출 또는 응답 오류."""


@dataclass(frozen=True)
class SeoulTransitConfig:
    api_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = SEOUL_TRANSIT_BUS_N_SUB_URL

    @classmethod
    def from_env(cls) -> "SeoulTransitConfig":
        api_key = (
            os.getenv("SEOUL_TRANSIT_SERVICE_KEY", "").strip()
            or os.getenv("KTO_SERVICE_KEY", "").strip()
            or os.getenv("ODSAY_API_KEY", "").strip()
        )
        if not api_key:
            raise ValueError(
                "대중교통 환승경로를 위한 API 키(SEOUL_TRANSIT_SERVICE_KEY 또는 KTO_SERVICE_KEY)가 필요합니다."
            )
        base_url = (
            os.getenv("SEOUL_TRANSIT_PATH_URL", "").strip()
            or os.getenv("ODSAY_PATH_URL", "").strip()
            or SEOUL_TRANSIT_BUS_N_SUB_URL
        )
        return cls(
            api_key=api_key,
            timeout_seconds=float(
                os.getenv("SEOUL_TRANSIT_TIMEOUT_SECONDS")
                or os.getenv("ODSAY_TIMEOUT_SECONDS")
                or "10"
            ),
            max_retries=int(
                os.getenv("SEOUL_TRANSIT_MAX_RETRIES")
                or os.getenv("ODSAY_MAX_RETRIES")
                or "2"
            ),
            base_url=base_url,
        )


def _haversine_distance_meters(
    lon1: float, lat1: float, lon2: float, lat2: float
) -> float:
    r = 6371000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return r * c


def _is_subway_route(route_nm: str | None, has_rail_links: bool) -> bool:
    if has_rail_links:
        return True
    if not route_nm:
        return False
    nm = route_nm.strip()
    subway_keywords = (
        "호선",
        "경의중앙",
        "수인분당",
        "신분당",
        "공항철도",
        "우이신설",
        "신림",
        "서해선",
        "경춘",
        "경강",
        "김포골드",
        "에버라인",
        "의정부경전철",
        "인천1호선",
        "인천2호선",
        "GTX",
    )
    if any(k in nm for k in subway_keywords):
        return True
    if nm.endswith("선"):
        return True
    return False


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
    """도보 구간의 시작·끝 좌표와 누락된 거리/시간을 앞뒤 구간에서 채운다."""

    filled: list[SeoulTransitLeg] = list(legs)
    for i, leg in enumerate(filled):
        if leg.mode != "도보":
            continue
        if i > 0 and filled[i - 1].end_longitude is not None:
            walk_start = (
                filled[i - 1].end_longitude,
                filled[i - 1].end_latitude,
            )
        elif leg.start_longitude is not None and leg.start_latitude is not None:
            walk_start = (leg.start_longitude, leg.start_latitude)
        else:
            walk_start = (start_x, start_y)

        if i + 1 < len(filled) and filled[i + 1].start_longitude is not None:
            walk_end = (
                filled[i + 1].start_longitude,
                filled[i + 1].start_latitude,
            )
        elif leg.end_longitude is not None and leg.end_latitude is not None:
            walk_end = (leg.end_longitude, leg.end_latitude)
        else:
            walk_end = (end_x, end_y)

        dist = leg.distance_meters
        if dist is None and walk_start and walk_end:
            dist = round(
                _haversine_distance_meters(
                    walk_start[0], walk_start[1], walk_end[0], walk_end[1]
                ),
                1,
            )

        dur = leg.duration_minutes
        if dur is None and dist is not None:
            dur = max(1.0, round(dist / 75.0, 1)) if dist >= 5.0 else 0.0

        instruction = leg.instruction
        if (
            not instruction
            or instruction == "도보 이동"
            or instruction.startswith("도보로")
        ):
            parts = []
            if dist is not None and dist >= 5.0:
                parts.append(f"{int(dist)}m")
            if dur is not None and dur > 0:
                parts.append(f"{round(dur, 1)}분")
            instruction = (
                "도보로 " + " · ".join(parts) + " 이동" if parts else "도보 이동"
            )

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
            distance_meters=dist,
            duration_minutes=dur,
            instruction=instruction,
            geometry=geometry,
        )
    return tuple(filled)


def _parse_public_data_portal_transit(
    payload: Mapping[str, Any],
    routing_preference: str = "fastest",
) -> SeoulTransitRoute:
    header = payload.get("msgHeader")
    if isinstance(header, Mapping):
        cd = str(header.get("headerCd", ""))
        msg = str(header.get("headerMsg") or "").strip()
        if cd != "0":
            if "XML Parsing Error" in msg or cd == "1":
                raise SeoulTransitApiError(
                    f"대중교통 환승 노선이 없습니다. ({msg or 'XML Parsing Error'})"
                )
            raise SeoulTransitApiError(
                f"서울시 대중교통 경로 오류 ({cd}): {msg or '경로를 찾을 수 없습니다.'}"
            )

    msg_body = payload.get("msgBody")
    items = (
        (msg_body or {}).get("itemList")
        if isinstance(msg_body, Mapping)
        else None
    )
    if isinstance(items, Mapping):
        items = [items]
    elif not isinstance(items, list):
        items = []

    valid_items = [
        it
        for it in items
        if isinstance(it, Mapping)
        and _optional_float(it.get("time")) is not None
    ]
    if not valid_items:
        raise SeoulTransitApiError(
            "대중교통 환승 노선이 없습니다."
        )

    # 경로 정렬 기준 (fastest: 최단시간 우선 / least_transfers: 최소환승 우선)
    if routing_preference == "least_transfers":
        def _sort_key(it: Mapping[str, Any]) -> tuple[int, float, float]:
            p = it.get("pathList")
            p_len = len(p) if isinstance(p, list) else (1 if p else 0)
            t = _optional_float(it.get("time")) or float("inf")
            d = _optional_float(it.get("distance")) or float("inf")
            return (p_len, t, d)
    else:
        def _sort_key(it: Mapping[str, Any]) -> tuple[float, int, float]:
            t = _optional_float(it.get("time")) or float("inf")
            p = it.get("pathList")
            p_len = len(p) if isinstance(p, list) else (1 if p else 0)
            d = _optional_float(it.get("distance")) or float("inf")
            return (t, p_len, d)

    best = min(valid_items, key=_sort_key)
    duration = _optional_float(best.get("time"))
    if duration is None:
        raise SeoulTransitApiError(
            "서울시 대중교통 응답에 소요시간(time)이 없습니다."
        )

    distance = _optional_float(best.get("distance"))
    raw_path_list = best.get("pathList")
    if isinstance(raw_path_list, Mapping):
        path_list = [raw_path_list]
    elif isinstance(raw_path_list, list):
        path_list = [p for p in raw_path_list if isinstance(p, Mapping)]
    else:
        path_list = []

    if not path_list:
        raise SeoulTransitApiError(
            "서울시 대중교통 경로 구간 정보가 없습니다."
        )

    legs: list[SeoulTransitLeg] = []

    # 1. 첫 도보 구간 (출발지 -> 첫 대중교통 승차 정류장)
    first_p = path_list[0]
    first_fx = _optional_float(first_p.get("fx"))
    first_fy = _optional_float(first_p.get("fy"))
    first_name = _clean_text(first_p.get("fname"))
    legs.append(
        SeoulTransitLeg(
            mode="도보",
            instruction=f"{first_name}까지 도보 이동"
            if first_name
            else "도보 이동",
            end_name=first_name,
            end_longitude=first_fx,
            end_latitude=first_fy,
        )
    )

    route_names: list[str] = []
    for i, p in enumerate(path_list):
        route_nm = _clean_text(p.get("routeNm"))
        fname = _clean_text(p.get("fname"))
        tname = _clean_text(p.get("tname"))
        fx = _optional_float(p.get("fx"))
        fy = _optional_float(p.get("fy"))
        tx = _optional_float(p.get("tx"))
        ty = _optional_float(p.get("ty"))

        rail_links = p.get("railLinkList")
        has_rail = bool(rail_links)
        is_subway = _is_subway_route(route_nm, has_rail)
        mode = "지하철" if is_subway else "버스"
        unit = "정거장" if is_subway else "정류장"

        station_count = None
        if isinstance(rail_links, list):
            station_count = len(rail_links)
        elif isinstance(rail_links, Mapping):
            station_count = 1

        if route_nm:
            route_names.append(route_nm)

        board_desc = f"{route_nm or mode} 승차" + (
            f" ({fname})" if fname else ""
        )
        alight_desc = f"{tname or '하차지점'}에서 하차"
        if station_count:
            alight_desc += f" ({station_count}개 {unit} 이동)"
        instruction = f"{board_desc} → {alight_desc}"

        geom = None
        if (
            fx is not None
            and fy is not None
            and tx is not None
            and ty is not None
        ):
            geom = {"type": "LineString", "coordinates": [[fx, fy], [tx, ty]]}

        legs.append(
            SeoulTransitLeg(
                mode=mode,
                instruction=instruction,
                lane_name=route_nm,
                start_name=fname,
                end_name=tname,
                station_count=station_count,
                start_longitude=fx,
                start_latitude=fy,
                end_longitude=tx,
                end_latitude=ty,
                geometry=geom,
            )
        )

        # 환승 도보 구간
        if i + 1 < len(path_list):
            next_p = path_list[i + 1]
            next_fx = _optional_float(next_p.get("fx"))
            next_fy = _optional_float(next_p.get("fy"))
            next_fname = _clean_text(next_p.get("fname"))
            next_route_nm = _clean_text(next_p.get("routeNm"))

            transfer_geom = None
            if (
                tx is not None
                and ty is not None
                and next_fx is not None
                and next_fy is not None
            ):
                transfer_geom = {
                    "type": "LineString",
                    "coordinates": [[tx, ty], [next_fx, next_fy]],
                }

            if tname and next_fname:
                if tname == next_fname:
                    transfer_desc = (
                        f"{tname}에서 {next_route_nm or '대중교통'}으로 환승"
                    )
                else:
                    transfer_desc = f"{tname}에서 {next_fname} ({next_route_nm or '환승'})까지 환승 이동"
            else:
                transfer_desc = "환승 이동"

            legs.append(
                SeoulTransitLeg(
                    mode="도보",
                    instruction=transfer_desc,
                    start_name=tname,
                    end_name=next_fname,
                    start_longitude=tx,
                    start_latitude=ty,
                    end_longitude=next_fx,
                    end_latitude=next_fy,
                    geometry=transfer_geom,
                )
            )

    # 3. 마지막 도보 구간 (마지막 하차 정류장 -> 목적지)
    last_p = path_list[-1]
    last_tx = _optional_float(last_p.get("tx"))
    last_ty = _optional_float(last_p.get("ty"))
    last_tname = _clean_text(last_p.get("tname"))
    legs.append(
        SeoulTransitLeg(
            mode="도보",
            instruction=f"{last_tname}에서 목적지까지 도보 이동"
            if last_tname
            else "도보 이동",
            start_name=last_tname,
            start_longitude=last_tx,
            start_latitude=last_ty,
        )
    )

    transfer_count = max(0, len(path_list) - 1)
    route_type = " → ".join(route_names) if route_names else "버스+지하철"
    legs_tuple = tuple(legs)

    return SeoulTransitRoute(
        duration_minutes=round(duration, 2),
        distance_meters=distance,
        walking_minutes=None,
        walking_distance_meters=None,
        transfer_count=transfer_count,
        route_type=route_type,
        geometry=combine_leg_geometries(legs_tuple),
        legs=legs_tuple,
    )


def _parse_odsay_transit(
    payload: Mapping[str, Any],
    routing_preference: str = "fastest",
) -> SeoulTransitRoute:
    result = payload.get("result")
    paths = (result or {}).get("path") if isinstance(result, Mapping) else None
    if not paths:
        raise SeoulTransitApiError("ODsay 대중교통 경로 결과가 없습니다.")

    if routing_preference == "least_transfers":
        def _odsay_sort_key(p: Mapping[str, Any]) -> tuple[int, float]:
            info = p.get("info") or {}
            transfers = (_optional_int(info.get("busTransitCount") or 0) or 0) + (
                _optional_int(info.get("subwayTransitCount") or 0) or 0
            )
            total_time = _optional_float(info.get("totalTime")) or float("inf")
            return (transfers, total_time)
    else:
        def _odsay_sort_key(p: Mapping[str, Any]) -> tuple[float, int]:
            info = p.get("info") or {}
            transfers = (_optional_int(info.get("busTransitCount") or 0) or 0) + (
                _optional_int(info.get("subwayTransitCount") or 0) or 0
            )
            total_time = _optional_float(info.get("totalTime")) or float("inf")
            return (total_time, transfers)

    best = min(
        (
            p
            for p in paths
            if isinstance(p, Mapping)
            and (p.get("info") or {}).get("totalTime")
        ),
        key=_odsay_sort_key,
        default=None,
    )
    if best is None:
        raise SeoulTransitApiError("ODsay 대중교통 경로 결과가 없습니다.")

    info = best.get("info") or {}
    duration = _optional_float(info.get("totalTime"))
    if duration is None:
        raise SeoulTransitApiError("ODsay 응답에 totalTime이 없습니다.")

    sub_paths = [
        sp for sp in (best.get("subPath") or []) if isinstance(sp, Mapping)
    ]
    walk_segments = [
        sp for sp in sub_paths if sp.get("trafficType") == TRAFFIC_TYPE_WALK
    ]
    info_walk_minutes = _optional_float(info.get("totalWalkTime"))
    walking_minutes = (
        info_walk_minutes
        if info_walk_minutes is not None and info_walk_minutes >= 0
        else (
            sum(
                _optional_float(sp.get("sectionTime")) or 0
                for sp in walk_segments
            )
            or None
        )
    )
    walking_distance = (
        sum(_optional_float(sp.get("distance")) or 0 for sp in walk_segments)
        or None
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


def parse_seoul_transit_response(
    body: bytes,
    routing_preference: str = "fastest",
) -> SeoulTransitRoute:
    text = body.decode("utf-8-sig", errors="replace").strip()
    try:
        payload: Any = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SeoulTransitApiError(
            "대중교통 경로 API가 JSON을 반환하지 않았습니다."
        ) from exc

    if not isinstance(payload, Mapping):
        raise SeoulTransitApiError(
            "대중교통 경로 응답 형식이 올바르지 않습니다."
        )

    error = payload.get("error")
    if isinstance(error, list) and error:
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

    if (
        "msgHeader" in payload
        or "msgBody" in payload
        or "comMsgHeader" in payload
    ):
        return _parse_public_data_portal_transit(
            payload, routing_preference=routing_preference
        )

    if "result" in payload:
        return _parse_odsay_transit(
            payload, routing_preference=routing_preference
        )

    raise SeoulTransitApiError(
        "인식할 수 없는 대중교통 경로 응답 형식입니다."
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
        routing_preference: str = "fastest",
    ) -> SeoulTransitRoute:
        if "odsay.com" in self.config.base_url:
            query = urlencode({
                "apiKey": self.config.api_key,
                "SX": f"{start_x:.7f}",
                "SY": f"{start_y:.7f}",
                "EX": f"{end_x:.7f}",
                "EY": f"{end_y:.7f}",
            })
        else:
            query = urlencode({
                "serviceKey": self.config.api_key,
                "startX": f"{start_x:.7f}",
                "startY": f"{start_y:.7f}",
                "endX": f"{end_x:.7f}",
                "endY": f"{end_y:.7f}",
                "resultType": "json",
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
                        f"대중교통 경로 네트워크 오류: {exc}"
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
                f"대중교통 경로 HTTP 오류 {status}: {detail}"
            )
        route = parse_seoul_transit_response(
            response_body, routing_preference=routing_preference
        )
        legs = fill_walk_leg_endpoints(
            route.legs,
            start_x=start_x,
            start_y=start_y,
            end_x=end_x,
            end_y=end_y,
        )
        walk_minutes = route.walking_minutes
        walk_dist = route.walking_distance_meters
        if walk_minutes is None:
            tot_mins = sum(
                leg.duration_minutes or 0
                for leg in legs
                if leg.mode == "도보" and (leg.distance_meters or 0) >= 5.0
            )
            walk_minutes = round(tot_mins, 1) if tot_mins > 0 else 0.0
        if walk_dist is None:
            tot_dist = sum(
                leg.distance_meters or 0
                for leg in legs
                if leg.mode == "도보" and (leg.distance_meters or 0) >= 5.0
            )
            walk_dist = round(tot_dist, 1) if tot_dist > 0 else 0.0

        geometry = combine_leg_geometries(legs) or {
            "type": "LineString",
            "coordinates": [[start_x, start_y], [end_x, end_y]],
        }
        return replace(
            route,
            legs=legs,
            geometry=geometry,
            walking_minutes=walk_minutes,
            walking_distance_meters=walk_dist,
        )
