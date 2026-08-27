"""기상청 단기예보 조회와 추천용 날씨 위험도 계산."""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen


KMA_BASE_URL = (
    "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"
)
KST = timezone(timedelta(hours=9))
FORECAST_BASE_HOURS = (2, 5, 8, 11, 14, 17, 20, 23)
Transport = Callable[[str, float], tuple[int, bytes]]


class KMAApiError(RuntimeError):
    """기상청 API 호출·응답 오류."""


@dataclass(frozen=True)
class KMAConfig:
    service_key: str
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = KMA_BASE_URL

    @classmethod
    def from_env(cls) -> "KMAConfig":
        service_key = os.getenv("KMA_SERVICE_KEY", "").strip()
        if not service_key:
            raise ValueError(
                "환경변수 KMA_SERVICE_KEY가 필요합니다. "
                "공공데이터포털에서 기상청 단기예보 API 활용신청 후 "
                ".env에 입력하세요."
            )
        return cls(
            service_key=service_key,
            timeout_seconds=float(os.getenv("KMA_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("KMA_MAX_RETRIES", "2")),
            base_url=os.getenv("KMA_BASE_URL", KMA_BASE_URL).strip()
            or KMA_BASE_URL,
        )

    @property
    def decoded_service_key(self) -> str:
        return unquote(self.service_key)


@dataclass(frozen=True)
class WeatherSnapshot:
    forecast_time: datetime
    grid_x: int
    grid_y: int
    temperature_c: float | None
    precipitation_probability: float | None
    precipitation_type: int | None
    precipitation_mm: float | None
    sky_code: int | None
    humidity_percent: float | None
    wind_speed_mps: float | None
    severity: float
    summary: str


def grid_from_latlon(latitude: float, longitude: float) -> tuple[int, int]:
    """WGS84 위경도를 기상청 5km 격자로 변환한다."""

    re_value = 6371.00877 / 5.0
    slat1 = math.radians(30.0)
    slat2 = math.radians(60.0)
    olon = math.radians(126.0)
    olat = math.radians(38.0)
    xo, yo = 43.0, 136.0

    sn = math.log(
        math.cos(slat1) / math.cos(slat2)
    ) / math.log(
        math.tan(math.pi * 0.25 + slat2 * 0.5)
        / math.tan(math.pi * 0.25 + slat1 * 0.5)
    )
    sf = (
        math.tan(math.pi * 0.25 + slat1 * 0.5) ** sn
        * math.cos(slat1)
        / sn
    )
    ro = (
        re_value
        * sf
        / math.tan(math.pi * 0.25 + olat * 0.5) ** sn
    )
    ra = (
        re_value
        * sf
        / math.tan(math.pi * 0.25 + math.radians(latitude) * 0.5) ** sn
    )
    theta = math.radians(longitude) - olon
    if theta > math.pi:
        theta -= 2.0 * math.pi
    if theta < -math.pi:
        theta += 2.0 * math.pi
    theta *= sn

    x = math.floor(ra * math.sin(theta) + xo + 0.5)
    y = math.floor(ro - ra * math.cos(theta) + yo + 0.5)
    return x, y


def latest_forecast_base(now: datetime | None = None) -> tuple[str, str]:
    """발표 후 10분이 지난 가장 최근 단기예보 기준시각을 반환한다."""

    current = now or datetime.now(KST)
    if current.tzinfo is not None:
        current = current.astimezone(KST).replace(tzinfo=None)
    cutoff = current - timedelta(minutes=10)
    for day_offset in (0, -1):
        day = cutoff.date() + timedelta(days=day_offset)
        for hour in reversed(FORECAST_BASE_HOURS):
            candidate = datetime.combine(day, datetime.min.time()).replace(
                hour=hour
            )
            if candidate <= cutoff:
                return candidate.strftime("%Y%m%d"), candidate.strftime("%H%M")
    raise AssertionError("단기예보 기준시각을 계산할 수 없습니다.")


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
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def parse_precipitation_mm(value: Any) -> float | None:
    """강수량 문구의 최소 수치를 mm로 변환한다."""

    text = str(value or "").strip()
    if not text or "강수없음" in text:
        return 0.0
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def calculate_weather_severity(
    *,
    precipitation_probability: float | None,
    precipitation_type: int | None,
    precipitation_mm: float | None,
    temperature_c: float | None,
    wind_speed_mps: float | None,
) -> float:
    """강수·강풍·폭염·한파 중 가장 큰 위험도를 0~1로 반환한다."""

    pop = max(0.0, min(1.0, (precipitation_probability or 0.0) / 100.0))
    amount = max(0.0, precipitation_mm or 0.0)
    if precipitation_type not in (None, 0):
        precipitation = 0.5 + 0.2 * pop + 0.3 * min(1.0, amount / 20.0)
    else:
        precipitation = 0.35 * pop + 0.3 * min(1.0, amount / 20.0)

    wind = max(
        0.0,
        min(1.0, ((wind_speed_mps or 0.0) - 7.0) / 13.0),
    )
    heat_cold = 0.0
    if temperature_c is not None:
        if temperature_c >= 33:
            heat_cold = min(1.0, 0.6 + (temperature_c - 33) / 10)
        elif temperature_c <= -5:
            heat_cold = min(1.0, 0.6 + (-5 - temperature_c) / 15)

    return round(max(precipitation, wind, heat_cold), 3)


def _weather_summary(
    precipitation_type: int | None,
    sky_code: int | None,
    severity: float,
) -> str:
    precipitation_labels = {
        1: "비",
        2: "비·눈",
        3: "눈",
        4: "소나기",
    }
    sky_labels = {1: "맑음", 3: "구름많음", 4: "흐림"}
    state = precipitation_labels.get(
        precipitation_type or 0,
        sky_labels.get(sky_code or 0, "날씨 정보"),
    )
    level = "위험" if severity >= 0.8 else "주의" if severity >= 0.5 else "양호"
    return f"{state}·{level}"


class KMAClient:
    def __init__(
        self,
        config: KMAConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "KMAClient":
        return cls(KMAConfig.from_env())

    def __enter__(self) -> "KMAClient":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def village_forecast(
        self,
        *,
        latitude: float,
        longitude: float,
        target_time: datetime,
        now: datetime | None = None,
    ) -> WeatherSnapshot:
        nx, ny = grid_from_latlon(latitude, longitude)
        base_date, base_time = latest_forecast_base(now)
        query = urlencode(
            {
                "serviceKey": self.config.decoded_service_key,
                "pageNo": 1,
                "numOfRows": 1000,
                "dataType": "JSON",
                "base_date": base_date,
                "base_time": base_time,
                "nx": nx,
                "ny": ny,
            }
        )
        url = (
            f"{self.config.base_url.rstrip('/')}/getVilageFcst?{query}"
        )

        status, body = 0, b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status, body = self._transport(
                    url, self.config.timeout_seconds
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise KMAApiError(f"기상청 API 네트워크 오류: {exc}") from exc
                time.sleep(0.5 * (2**attempt))
                continue
            if status not in {429, 500, 502, 503, 504}:
                break
            if attempt < self.config.max_retries:
                time.sleep(0.5 * (2**attempt))

        if status >= 400:
            detail = body.decode("utf-8-sig", errors="replace")[:300]
            raise KMAApiError(f"기상청 API HTTP 오류 {status}: {detail}")
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KMAApiError("기상청 API가 JSON을 반환하지 않았습니다.") from exc

        response = payload.get("response") or {}
        header = response.get("header") or {}
        if str(header.get("resultCode", "")) not in {"00", "0000"}:
            raise KMAApiError(
                f"기상청 API 오류 {header.get('resultCode')}: "
                f"{header.get('resultMsg', '알 수 없는 오류')}"
            )
        items = (
            ((response.get("body") or {}).get("items") or {}).get("item")
            or []
        )
        if not isinstance(items, list) or not items:
            raise KMAApiError("기상청 단기예보 결과가 없습니다.")
        return self._snapshot_from_items(items, target_time, nx, ny)

    @staticmethod
    def _snapshot_from_items(
        items: list[dict[str, Any]],
        target_time: datetime,
        nx: int,
        ny: int,
    ) -> WeatherSnapshot:
        target = target_time
        if target.tzinfo is not None:
            target = target.astimezone(KST).replace(tzinfo=None)

        grouped: dict[datetime, dict[str, Any]] = {}
        for item in items:
            date_text = str(item.get("fcstDate") or "")
            time_text = str(item.get("fcstTime") or "").zfill(4)
            try:
                forecast_at = datetime.strptime(
                    date_text + time_text, "%Y%m%d%H%M"
                )
            except ValueError:
                continue
            grouped.setdefault(forecast_at, {})[
                str(item.get("category") or "")
            ] = item.get("fcstValue")
        if not grouped:
            raise KMAApiError("기상청 예보 시각을 해석할 수 없습니다.")

        forecast_at = min(
            grouped,
            key=lambda value: (
                value < target,
                abs((value - target).total_seconds()),
            ),
        )
        values = grouped[forecast_at]
        temperature = _optional_float(values.get("TMP"))
        pop = _optional_float(values.get("POP"))
        pty = _optional_int(values.get("PTY"))
        pcp = parse_precipitation_mm(values.get("PCP"))
        sky = _optional_int(values.get("SKY"))
        humidity = _optional_float(values.get("REH"))
        wind = _optional_float(values.get("WSD"))
        severity = calculate_weather_severity(
            precipitation_probability=pop,
            precipitation_type=pty,
            precipitation_mm=pcp,
            temperature_c=temperature,
            wind_speed_mps=wind,
        )
        return WeatherSnapshot(
            forecast_time=forecast_at,
            grid_x=nx,
            grid_y=ny,
            temperature_c=temperature,
            precipitation_probability=pop,
            precipitation_type=pty,
            precipitation_mm=pcp,
            sky_code=sky,
            humidity_percent=humidity,
            wind_speed_mps=wind,
            severity=severity,
            summary=_weather_summary(pty, sky, severity),
        )
