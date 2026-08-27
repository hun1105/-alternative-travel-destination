"""TMAP 보행자 경로 조회 클라이언트."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TMAP_PEDESTRIAN_URL = "https://apis.openapi.sk.com/tmap/routes/pedestrian"
Transport = Callable[
    [str, dict[str, str], bytes, float],
    tuple[int, bytes],
]


class TMapApiError(RuntimeError):
    """TMAP 호출 또는 응답 오류."""


@dataclass(frozen=True)
class TMapConfig:
    app_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = TMAP_PEDESTRIAN_URL

    @classmethod
    def from_env(cls) -> "TMapConfig":
        app_key = os.getenv("TMAP_APP_KEY", "").strip()
        if not app_key:
            raise ValueError(
                "환경변수 TMAP_APP_KEY가 필요합니다. "
                "SK open API에서 TMAP 보행자 경로 안내 상품을 신청하세요."
            )
        return cls(
            app_key=app_key,
            timeout_seconds=float(os.getenv("TMAP_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("TMAP_MAX_RETRIES", "2")),
            base_url=os.getenv("TMAP_PEDESTRIAN_URL", TMAP_PEDESTRIAN_URL)
            .strip()
            or TMAP_PEDESTRIAN_URL,
        )


@dataclass(frozen=True)
class WalkingStep:
    instruction: str
    turn_type: int | None
    distance_meters: float | None
    longitude: float
    latitude: float


@dataclass(frozen=True)
class WalkingRoute:
    distance_meters: float
    duration_seconds: int
    geometry: dict[str, Any] | None = None
    steps: tuple[WalkingStep, ...] = ()

    @property
    def duration_minutes(self) -> float:
        return self.duration_seconds / 60.0


def _line_string_geometry(
    features: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """LineString 조각들을 하나의 GeoJSON 경로로 이어붙인다."""

    coordinates: list[list[float]] = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "LineString":
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


def _steps_from_features(
    features: list[dict[str, Any]],
) -> tuple[WalkingStep, ...]:
    """지점(Point) 안내문구마다 다음 지점까지의 거리를 이어붙인다."""

    steps: list[WalkingStep] = []
    pending: dict[str, Any] | None = None
    pending_distance = 0.0

    def flush() -> None:
        nonlocal pending, pending_distance
        if pending is not None:
            steps.append(WalkingStep(
                instruction=pending["instruction"],
                turn_type=pending["turn_type"],
                distance_meters=pending_distance or None,
                longitude=pending["longitude"],
                latitude=pending["latitude"],
            ))
        pending = None
        pending_distance = 0.0

    for feature in features:
        geometry = feature.get("geometry") or {}
        properties: dict[str, Any] = feature.get("properties") or {}
        geometry_type = geometry.get("type")
        if geometry_type == "Point":
            flush()
            instruction = str(properties.get("description") or "").strip()
            coordinates = geometry.get("coordinates") or [None, None]
            try:
                lon, lat = float(coordinates[0]), float(coordinates[1])
            except (TypeError, ValueError, IndexError):
                continue
            if not instruction:
                continue
            turn_type_raw = properties.get("turnType")
            pending = {
                "instruction": instruction,
                "turn_type": (
                    int(turn_type_raw)
                    if isinstance(turn_type_raw, (int, float))
                    else None
                ),
                "longitude": lon,
                "latitude": lat,
            }
        elif geometry_type == "LineString" and pending is not None:
            try:
                pending_distance += float(properties.get("distance") or 0)
            except (TypeError, ValueError):
                pass
    flush()
    return tuple(steps)


def _default_transport(
    url: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


class TMapPedestrianClient:
    def __init__(
        self,
        config: TMapConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "TMapPedestrianClient":
        return cls(TMapConfig.from_env())

    def pedestrian_route(
        self,
        *,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        start_name: str = "현재 위치",
        end_name: str = "목적지",
    ) -> WalkingRoute:
        payload = {
            "startX": start_x,
            "startY": start_y,
            "endX": end_x,
            "endY": end_y,
            "reqCoordType": "WGS84GEO",
            "resCoordType": "WGS84GEO",
            "startName": start_name,
            "endName": end_name,
            "searchOption": "0",
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "appKey": self.config.app_key,
            "User-Agent": "PlanB-API/0.1",
        }
        url = f"{self.config.base_url}?version=1"
        status, response_body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, response_body = self._transport(
                    url,
                    headers,
                    body,
                    self.config.timeout_seconds,
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise TMapApiError(
                        f"TMAP 보행 경로 네트워크 오류: {exc}"
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))

        if status >= 400:
            detail = response_body.decode(
                "utf-8-sig", errors="replace"
            )[:300]
            raise TMapApiError(
                f"TMAP 보행 경로 HTTP 오류 {status}: {detail}"
            )
        try:
            response = json.loads(response_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TMapApiError(
                "TMAP 보행 경로 API가 JSON을 반환하지 않았습니다."
            ) from exc

        features = response.get("features") or []
        geometry = _line_string_geometry(features)
        steps = _steps_from_features(features)
        for feature in features:
            properties: dict[str, Any] = feature.get("properties") or {}
            distance = properties.get("totalDistance")
            duration = properties.get("totalTime")
            if distance is not None and duration is not None:
                try:
                    return WalkingRoute(
                        distance_meters=float(distance),
                        duration_seconds=int(float(duration)),
                        geometry=geometry,
                        steps=steps,
                    )
                except (TypeError, ValueError):
                    break
        message = (
            response.get("error", {}).get("message")
            if isinstance(response.get("error"), dict)
            else response.get("message")
        )
        raise TMapApiError(
            f"TMAP 보행 경로 결과가 없습니다: {message or '응답 형식 불일치'}"
        )
