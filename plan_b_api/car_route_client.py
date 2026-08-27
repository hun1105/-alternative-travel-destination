"""TMAP 자동차 경로 조회 클라이언트."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


TMAP_CAR_URL = "https://apis.openapi.sk.com/tmap/routes"
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, bytes]]


class TMapCarApiError(RuntimeError):
    """TMAP 자동차 경로 호출 또는 응답 오류."""


@dataclass(frozen=True)
class TMapCarConfig:
    app_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = TMAP_CAR_URL

    @classmethod
    def from_env(cls) -> "TMapCarConfig":
        app_key = os.getenv("TMAP_APP_KEY", "").strip()
        if not app_key:
            raise ValueError("환경변수 TMAP_APP_KEY가 필요합니다.")
        return cls(
            app_key=app_key,
            timeout_seconds=float(os.getenv("TMAP_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("TMAP_MAX_RETRIES", "2")),
            base_url=os.getenv("TMAP_CAR_URL", TMAP_CAR_URL).strip()
            or TMAP_CAR_URL,
        )


@dataclass(frozen=True)
class CarRoute:
    distance_meters: float
    duration_seconds: int
    total_fare_krw: int | None = None
    taxi_fare_krw: int | None = None
    geometry: dict[str, Any] | None = None

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


def _optional_int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


class TMapCarClient:
    def __init__(
        self,
        config: TMapCarConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "TMapCarClient":
        return cls(TMapCarConfig.from_env())

    def car_route(
        self,
        *,
        start_x: float,
        start_y: float,
        end_x: float,
        end_y: float,
        start_name: str = "현재 위치",
        end_name: str = "목적지",
    ) -> CarRoute:
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
        url = f"{self.config.base_url}?version=1&format=json"
        status, response_body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, response_body = self._transport(
                    url, headers, body, self.config.timeout_seconds
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise TMapCarApiError(
                        f"TMAP 자동차 경로 네트워크 오류: {exc}"
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))

        if status >= 400:
            detail = response_body.decode("utf-8-sig", errors="replace")[:300]
            raise TMapCarApiError(
                f"TMAP 자동차 경로 HTTP 오류 {status}: {detail}"
            )
        try:
            response = json.loads(response_body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TMapCarApiError(
                "TMAP 자동차 경로 API가 JSON을 반환하지 않았습니다."
            ) from exc

        features = response.get("features") or []
        geometry = _line_string_geometry(features)
        for feature in features:
            properties: dict[str, Any] = feature.get("properties") or {}
            distance = properties.get("totalDistance")
            duration = properties.get("totalTime")
            if distance is not None and duration is not None:
                try:
                    return CarRoute(
                        distance_meters=float(distance),
                        duration_seconds=int(float(duration)),
                        total_fare_krw=_optional_int(
                            properties.get("totalFare")
                        ),
                        taxi_fare_krw=_optional_int(
                            properties.get("taxiFare")
                        ),
                        geometry=geometry,
                    )
                except (TypeError, ValueError):
                    break
        raise TMapCarApiError("TMAP 자동차 경로 결과가 없습니다.")
