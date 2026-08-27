"""SK open API 장소 혼잡도 클라이언트."""

from __future__ import annotations

import json
import os
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SK_CROWD_BASE_URL = "https://apis.openapi.sk.com/puzzle/place"
Transport = Callable[[str, dict[str, str], float], tuple[int, bytes]]


class SKCrowdApiError(RuntimeError):
    """장소 혼잡도 호출 또는 응답 오류."""


@dataclass(frozen=True)
class SKCrowdConfig:
    app_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = SK_CROWD_BASE_URL

    @classmethod
    def from_env(cls) -> "SKCrowdConfig":
        app_key = (
            os.getenv("SK_CROWD_APP_KEY", "").strip()
            or os.getenv("TMAP_APP_KEY", "").strip()
        )
        if not app_key:
            raise ValueError(
                "환경변수 SK_CROWD_APP_KEY 또는 TMAP_APP_KEY가 필요합니다. "
                "SK open API 앱에 장소 혼잡도 상품을 추가하세요."
            )
        return cls(
            app_key=app_key,
            timeout_seconds=float(os.getenv("SK_CROWD_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("SK_CROWD_MAX_RETRIES", "2")),
            base_url=os.getenv("SK_CROWD_BASE_URL", SK_CROWD_BASE_URL).strip()
            or SK_CROWD_BASE_URL,
        )


@dataclass(frozen=True)
class CrowdPlace:
    poi_id: str
    name: str


@dataclass(frozen=True)
class CrowdSnapshot:
    poi_id: str
    poi_name: str
    congestion: float
    congestion_level: int
    measured_at: str

    @property
    def normalized_level(self) -> float:
        """1~4단계를 점수용 0~1 혼잡도로 변환한다."""

        return (self.congestion_level - 1) / 3

    @property
    def label(self) -> str:
        return {1: "여유", 2: "보통", 3: "혼잡", 4: "매우 혼잡"}[
            self.congestion_level
        ]


def normalize_place_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def match_crowd_place(
    title: str,
    places: tuple[CrowdPlace, ...],
) -> CrowdPlace | None:
    """오매칭 방지를 위해 공백·기호 제거 후 정확히 같은 이름만 연결한다."""

    target = normalize_place_name(title)
    matches = [place for place in places if normalize_place_name(place.name) == target]
    return matches[0] if len(matches) == 1 else None


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


class SKCrowdClient:
    def __init__(
        self,
        config: SKCrowdConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "SKCrowdClient":
        return cls(SKCrowdConfig.from_env())

    def _get(self, path: str, query: dict[str, object] | None = None) -> dict:
        url = f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}"
        if query:
            url += "?" + urlencode(query)
        headers = {
            "Accept": "application/json",
            "appKey": self.config.app_key,
            "User-Agent": "PlanB-API/0.1",
        }
        status, body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, body = self._transport(
                    url, headers, self.config.timeout_seconds
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise SKCrowdApiError(
                        f"SK 장소 혼잡도 네트워크 오류: {exc}"
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))

        if status >= 400:
            detail = body.decode("utf-8-sig", errors="replace")[:300]
            raise SKCrowdApiError(
                f"SK 장소 혼잡도 HTTP 오류 {status}: {detail}"
            )
        try:
            response = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SKCrowdApiError(
                "SK 장소 혼잡도 API가 JSON을 반환하지 않았습니다."
            ) from exc
        api_status = response.get("status") or {}
        if str(api_status.get("code", "00")) != "00":
            raise SKCrowdApiError(
                "SK 장소 혼잡도 응답 오류: "
                f"{api_status.get('message') or api_status.get('code')}"
            )
        return response

    def supported_places(self) -> tuple[CrowdPlace, ...]:
        response = self._get("meta/pois", {"offset": 0, "limit": 1000})
        places: list[CrowdPlace] = []
        for item in response.get("contents") or []:
            poi_id = str(item.get("poiId") or "").strip()
            name = str(item.get("poiName") or "").strip()
            if poi_id and name:
                places.append(CrowdPlace(poi_id, name))
        if not places:
            raise SKCrowdApiError("SK 장소 혼잡도 지원 장소 목록이 비었습니다.")
        return tuple(places)

    def realtime(self, poi_id: str) -> CrowdSnapshot:
        response = self._get(f"congestion/rltm/pois/{poi_id}")
        contents = response.get("contents") or {}
        rows = contents.get("rltm") or []
        row = next((value for value in rows if value.get("type") == 1), None)
        if row is None:
            raise SKCrowdApiError("장소 전체(type=1) 실시간 혼잡도가 없습니다.")
        try:
            level = int(row["congestionLevel"])
            if level not in {1, 2, 3, 4}:
                raise ValueError
            return CrowdSnapshot(
                poi_id=str(contents.get("poiId") or poi_id),
                poi_name=str(contents.get("poiName") or ""),
                congestion=float(row["congestion"]),
                congestion_level=level,
                measured_at=str(row.get("datetime") or ""),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SKCrowdApiError("실시간 혼잡도 응답 형식이 올바르지 않습니다.") from exc
