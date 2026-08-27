"""TMAP 장소 통합검색 클라이언트."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


TMAP_POI_URL = "https://apis.openapi.sk.com/tmap/pois"
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class TMapPlaceSearchError(RuntimeError):
    """TMAP 장소 검색 호출 또는 응답 오류."""


@dataclass(frozen=True)
class TMapPlaceSearchConfig:
    app_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = TMAP_POI_URL

    @classmethod
    def from_env(cls) -> "TMapPlaceSearchConfig":
        app_key = os.getenv("TMAP_APP_KEY", "").strip()
        if not app_key:
            raise ValueError("환경변수 TMAP_APP_KEY가 필요합니다.")
        return cls(
            app_key=app_key,
            timeout_seconds=float(os.getenv("TMAP_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("TMAP_MAX_RETRIES", "2")),
            base_url=os.getenv("TMAP_POI_URL", TMAP_POI_URL).strip()
            or TMAP_POI_URL,
        )


@dataclass(frozen=True)
class PlaceSearchResult:
    place_id: str
    name: str
    longitude: float
    latitude: float
    address: str = ""
    category: str = ""
    phone: str = ""
    desc: str = ""

    def selection_payload(self) -> dict[str, Any]:
        return {"provider": "tmap", **asdict(self)}


@dataclass(frozen=True)
class PlaceSearchResponse:
    query: str
    total_count: int
    items: tuple[PlaceSearchResult, ...]


def _default_transport(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _text(item: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = item.get(name)
        if value not in (None, ""):
            # TMAP 응답의 lowerBizName 등 일부 필드는 "박물관\/기념관"처럼
            # 슬래시 앞에 실제 백슬래시 문자가 섞여서 온다(JSON 이스케이프가
            # 아니라 원본 데이터 자체의 특이사항). 표시용으로 정리한다.
            return str(value).replace("\\/", "/").strip()
    return ""


def _coordinate(item: Mapping[str, Any], *names: str) -> float | None:
    value = _text(item, *names)
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _address(item: Mapping[str, Any]) -> str:
    road_parts = [
        _text(item, "upperAddrName"),
        _text(item, "middleAddrName"),
        _text(item, "roadName"),
        "-".join(filter(None, [
            _text(item, "firstBuildNo"),
            _text(item, "secondBuildNo"),
        ])),
    ]
    road = " ".join(part for part in road_parts if part)
    if road:
        return road
    return " ".join(
        part
        for part in (
            _text(item, "upperAddrName"),
            _text(item, "middleAddrName"),
            _text(item, "lowerAddrName"),
            _text(item, "detailAddrName"),
        )
        if part
    )


def parse_place_search_response(body: bytes, query: str) -> PlaceSearchResponse:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TMapPlaceSearchError(
            "TMAP 장소 검색 API가 JSON을 반환하지 않았습니다."
        ) from exc
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        raise TMapPlaceSearchError(
            f"TMAP 장소 검색 오류: {error.get('message') or error}"
        )
    search_info = payload.get("searchPoiInfo") or {}
    pois = search_info.get("pois") or {}
    raw_items = pois.get("poi") or []
    if isinstance(raw_items, dict):
        raw_items = [raw_items]
    items: list[PlaceSearchResult] = []
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        longitude = _coordinate(item, "frontLon", "noorLon", "lon")
        latitude = _coordinate(item, "frontLat", "noorLat", "lat")
        place_id = _text(item, "id", "pkey")
        name = _text(item, "name")
        if longitude is None or latitude is None or not place_id or not name:
            continue
        items.append(PlaceSearchResult(
            place_id=place_id,
            name=name,
            longitude=longitude,
            latitude=latitude,
            address=_address(item),
            category=_text(
                item, "lowerBizName", "middleBizName", "upperBizName", "mlClass"
            ),
            phone=_text(item, "telNo"),
            desc=_text(item, "desc"),
        ))
    total = search_info.get("totalCount", len(items))
    try:
        total_count = int(total)
    except (TypeError, ValueError):
        total_count = len(items)
    return PlaceSearchResponse(query=query, total_count=total_count, items=tuple(items))


class TMapPlaceSearchClient:
    def __init__(
        self,
        config: TMapPlaceSearchConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "TMapPlaceSearchClient":
        return cls(TMapPlaceSearchConfig.from_env())

    def search(
        self,
        query: str,
        *,
        count: int = 10,
        page: int = 1,
        center_x: float | None = None,
        center_y: float | None = None,
        radius_km: int = 20,
    ) -> PlaceSearchResponse:
        query = query.strip()
        if not query:
            raise ValueError("장소 검색어가 필요합니다.")
        if not 1 <= count <= 20 or page < 1:
            raise ValueError("count는 1~20, page는 1 이상이어야 합니다.")
        if (center_x is None) != (center_y is None):
            raise ValueError("center_x와 center_y를 함께 입력해야 합니다.")
        params: dict[str, Any] = {
            "version": "1",
            "format": "json",
            "searchKeyword": query,
            "searchtypCd": "A",
            "reqCoordType": "WGS84GEO",
            "resCoordType": "WGS84GEO",
            "count": count,
            "page": page,
        }
        if center_x is not None and center_y is not None:
            params.update({
                "centerLon": f"{center_x:.7f}",
                "centerLat": f"{center_y:.7f}",
                "radius": radius_km,
            })
        url = f"{self.config.base_url}?{urlencode(params)}"
        headers = {
            "Accept": "application/json",
            "appKey": self.config.app_key,
            "User-Agent": "PlanB-API/0.1",
        }
        status, response_body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, response_body = self._transport(
                    url, headers, self.config.timeout_seconds
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise TMapPlaceSearchError(
                        f"TMAP 장소 검색 네트워크 오류: {exc}"
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))
        if status >= 400:
            detail = response_body.decode("utf-8-sig", errors="replace")[:300]
            raise TMapPlaceSearchError(
                f"TMAP 장소 검색 HTTP 오류 {status}: {detail}"
            )
        return parse_place_search_response(response_body, query)
