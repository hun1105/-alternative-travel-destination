"""한국관광공사 국문 관광정보 OpenAPI 클라이언트."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode
from urllib.request import Request, urlopen


KTO_BASE_URL = "https://apis.data.go.kr/B551011/KorService2"
SUCCESS_CODES = {"0000", "00"}
Transport = Callable[[str, float], tuple[int, bytes]]


class KTOApiError(RuntimeError):
    """공사 API 호출 또는 응답 검증 실패."""

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        result_code: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.result_code = result_code
        self.status_code = status_code


@dataclass(frozen=True)
class KTOConfig:
    """공사 API 공통 설정."""

    service_key: str
    mobile_app: str = "PlanB"
    mobile_os: str = "ETC"
    timeout_seconds: float = 10.0
    max_retries: int = 2
    base_url: str = KTO_BASE_URL

    @classmethod
    def from_env(cls) -> "KTOConfig":
        service_key = os.getenv("KTO_SERVICE_KEY", "").strip()
        if not service_key:
            raise ValueError("환경변수 KTO_SERVICE_KEY가 필요합니다.")

        return cls(
            service_key=service_key,
            mobile_app=os.getenv("KTO_MOBILE_APP", "PlanB").strip() or "PlanB",
            mobile_os=os.getenv("KTO_MOBILE_OS", "ETC").strip() or "ETC",
            timeout_seconds=float(os.getenv("KTO_TIMEOUT_SECONDS", "10")),
            max_retries=int(os.getenv("KTO_MAX_RETRIES", "2")),
            base_url=os.getenv("KTO_BASE_URL", KTO_BASE_URL).strip() or KTO_BASE_URL,
        )

    @property
    def decoded_service_key(self) -> str:
        """Encoding 키가 입력돼도 URL에서 한 번만 인코딩되도록 정규화한다."""

        return unquote(self.service_key)


@dataclass(frozen=True)
class KTOResponse:
    """목록형·상세형 응답의 공통 결과."""

    operation: str
    items: list[dict[str, Any]]
    total_count: int
    page_no: int
    num_of_rows: int
    raw: dict[str, Any]


def _default_transport(url: str, timeout: float) -> tuple[int, bytes]:
    request = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "PlanB-API/0.1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


class KTOClient:
    """표준 라이브러리만 사용하는 동기식 공사 OpenAPI 클라이언트."""

    def __init__(
        self,
        config: KTOConfig,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.config = config
        self._transport = transport or _default_transport

    @classmethod
    def from_env(cls) -> "KTOClient":
        return cls(KTOConfig.from_env())

    def __enter__(self) -> "KTOClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        """연결 풀을 사용하지 않으므로 호환성을 위한 빈 메서드다."""

    def _common_params(self, *, page_no: int, num_of_rows: int) -> dict[str, Any]:
        return {
            "serviceKey": self.config.decoded_service_key,
            "MobileOS": self.config.mobile_os,
            "MobileApp": self.config.mobile_app,
            "_type": "json",
            "pageNo": page_no,
            "numOfRows": num_of_rows,
        }

    @staticmethod
    def _clean_params(params: Mapping[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in params.items() if value is not None}

    def _safe_error_detail(self, response_body: bytes) -> str:
        """HTTP 오류 본문에서 인증키를 제거하고 진단 문구만 반환한다."""

        text = response_body.decode("utf-8-sig", errors="replace").strip()
        if not text:
            return "응답 본문 없음"

        service_key = self.config.service_key
        decoded_key = self.config.decoded_service_key
        for secret in {service_key, decoded_key}:
            if secret:
                text = text.replace(secret, "***")

        # XML/HTML 태그와 과도한 공백을 제거해 터미널에서 읽기 쉽게 만든다.
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:500]

    def _request(
        self,
        operation: str,
        *,
        params: Mapping[str, Any] | None = None,
        page_no: int = 1,
        num_of_rows: int = 10,
    ) -> KTOResponse:
        query = self._common_params(page_no=page_no, num_of_rows=num_of_rows)
        query.update(self._clean_params(params or {}))
        url = (
            f"{self.config.base_url.rstrip('/')}/{operation}"
            f"?{urlencode(query, doseq=True)}"
        )

        status_code = 0
        response_body = b""
        for attempt in range(self.config.max_retries + 1):
            try:
                status_code, response_body = self._transport(
                    url,
                    self.config.timeout_seconds,
                )
            except (URLError, TimeoutError, OSError) as exc:
                if attempt >= self.config.max_retries:
                    raise KTOApiError(
                        f"공사 API 네트워크 오류: {exc}",
                        operation=operation,
                    ) from exc
                time.sleep(0.5 * (2**attempt))
                continue

            if status_code not in {429, 500, 502, 503, 504}:
                break
            if attempt >= self.config.max_retries:
                break
            time.sleep(0.5 * (2**attempt))

        if status_code >= 400:
            detail = self._safe_error_detail(response_body)
            raise KTOApiError(
                f"공사 API HTTP 오류 {status_code}: {detail}",
                operation=operation,
                status_code=status_code,
            )

        text = response_body.decode("utf-8-sig", errors="replace")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            snippet = text[:200].replace("\n", " ")
            raise KTOApiError(
                f"JSON 응답이 아닙니다: {snippet}",
                operation=operation,
                status_code=status_code,
            ) from exc

        api_response = payload.get("response")
        if not isinstance(api_response, dict):
            top_level_code = str(payload.get("resultCode", ""))
            top_level_message = payload.get("resultMsg")
            if top_level_code or top_level_message:
                raise KTOApiError(
                    f"공사 API 오류 {top_level_code or 'UNKNOWN'}: "
                    f"{top_level_message or '알 수 없는 오류'}",
                    operation=operation,
                    result_code=top_level_code or None,
                )
            detail = self._safe_error_detail(
                json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
            raise KTOApiError(
                f"공사 API 응답에 response 객체가 없습니다: {detail}",
                operation=operation,
            )

        header = api_response.get("header") or {}
        result_code = str(header.get("resultCode", ""))
        if result_code not in SUCCESS_CODES:
            result_message = header.get("resultMsg", "알 수 없는 오류")
            raise KTOApiError(
                f"공사 API 오류 {result_code}: {result_message}",
                operation=operation,
                result_code=result_code,
            )

        body = api_response.get("body") or {}
        item_data = (body.get("items") or {}).get("item", [])
        if isinstance(item_data, dict):
            items = [item_data]
        elif isinstance(item_data, list):
            items = item_data
        else:
            items = []

        return KTOResponse(
            operation=operation,
            items=items,
            total_count=int(body.get("totalCount") or 0),
            page_no=int(body.get("pageNo") or page_no),
            num_of_rows=int(body.get("numOfRows") or num_of_rows),
            raw=payload,
        )

    def area_codes(
        self,
        *,
        area_code: str | None = None,
        page_no: int = 1,
        num_of_rows: int = 100,
    ) -> KTOResponse:
        """시도 또는 특정 시도의 시군구 코드를 조회한다."""

        return self._request(
            "areaCode2",
            params={"areaCode": area_code},
            page_no=page_no,
            num_of_rows=num_of_rows,
        )

    def category_codes(
        self,
        *,
        content_type_id: str | None = None,
        cat1: str | None = None,
        cat2: str | None = None,
        cat3: str | None = None,
        page_no: int = 1,
        num_of_rows: int = 100,
    ) -> KTOResponse:
        """관광 타입별 대·중·소 분류 코드를 조회한다."""

        return self._request(
            "categoryCode2",
            params={
                "contentTypeId": content_type_id,
                "cat1": cat1,
                "cat2": cat2,
                "cat3": cat3,
            },
            page_no=page_no,
            num_of_rows=num_of_rows,
        )

    def area_based_list(
        self,
        *,
        area_code: str | None = None,
        sigungu_code: str | None = None,
        content_type_id: str | None = None,
        cat1: str | None = None,
        cat2: str | None = None,
        cat3: str | None = None,
        arrange: str = "A",
        page_no: int = 1,
        num_of_rows: int = 20,
    ) -> KTOResponse:
        """지역과 관광 타입을 기준으로 관광정보를 조회한다."""

        return self._request(
            "areaBasedList2",
            params={
                "areaCode": area_code,
                "sigunguCode": sigungu_code,
                "contentTypeId": content_type_id,
                "cat1": cat1,
                "cat2": cat2,
                "cat3": cat3,
                "arrange": arrange,
            },
            page_no=page_no,
            num_of_rows=num_of_rows,
        )

    def location_based_list(
        self,
        *,
        map_x: float,
        map_y: float,
        radius: int = 2000,
        content_type_id: str | None = None,
        arrange: str = "E",
        page_no: int = 1,
        num_of_rows: int = 20,
    ) -> KTOResponse:
        """WGS84 경도·위도와 반경으로 주변 관광정보를 조회한다."""

        if not 0 < radius <= 20_000:
            raise ValueError("radius는 1~20000미터 범위여야 합니다.")

        return self._request(
            "locationBasedList2",
            params={
                "mapX": map_x,
                "mapY": map_y,
                "radius": radius,
                "contentTypeId": content_type_id,
                "arrange": arrange,
            },
            page_no=page_no,
            num_of_rows=num_of_rows,
        )

    def keyword_search(
        self,
        keyword: str,
        *,
        area_code: str | None = None,
        sigungu_code: str | None = None,
        content_type_id: str | None = None,
        arrange: str = "A",
        page_no: int = 1,
        num_of_rows: int = 20,
    ) -> KTOResponse:
        """키워드로 관광정보를 조회한다."""

        if not keyword.strip():
            raise ValueError("keyword는 비어 있을 수 없습니다.")

        return self._request(
            "searchKeyword2",
            params={
                "keyword": keyword.strip(),
                "areaCode": area_code,
                "sigunguCode": sigungu_code,
                "contentTypeId": content_type_id,
                "arrange": arrange,
            },
            page_no=page_no,
            num_of_rows=num_of_rows,
        )

    def festival_search(
        self,
        *,
        event_start_date: str,
        event_end_date: str | None = None,
        area_code: str | None = None,
        sigungu_code: str | None = None,
        arrange: str = "A",
        page_no: int = 1,
        num_of_rows: int = 20,
    ) -> KTOResponse:
        """행사 시작·종료일을 기준으로 축제 정보를 조회한다."""

        if len(event_start_date) != 8 or not event_start_date.isdigit():
            raise ValueError("event_start_date는 YYYYMMDD 형식이어야 합니다.")
        if event_end_date and (len(event_end_date) != 8 or not event_end_date.isdigit()):
            raise ValueError("event_end_date는 YYYYMMDD 형식이어야 합니다.")

        return self._request(
            "searchFestival2",
            params={
                "eventStartDate": event_start_date,
                "eventEndDate": event_end_date,
                "areaCode": area_code,
                "sigunguCode": sigungu_code,
                "arrange": arrange,
            },
            page_no=page_no,
            num_of_rows=num_of_rows,
        )

    def detail_common(self, content_id: str) -> KTOResponse:
        """주소·좌표·개요·대표 이미지 등 공통 상세정보를 조회한다."""

        return self._request(
            "detailCommon2",
            params={"contentId": content_id},
            num_of_rows=10,
        )

    def detail_intro(self, content_id: str, content_type_id: str) -> KTOResponse:
        """운영시간·휴무·요금 등 관광 타입별 소개정보를 조회한다."""

        return self._request(
            "detailIntro2",
            params={"contentId": content_id, "contentTypeId": content_type_id},
            num_of_rows=10,
        )

    def detail_info(self, content_id: str, content_type_id: str) -> KTOResponse:
        """시설·코스·프로그램 등 반복 상세정보를 조회한다."""

        return self._request(
            "detailInfo2",
            params={"contentId": content_id, "contentTypeId": content_type_id},
            num_of_rows=100,
        )

    def detail_images(self, content_id: str) -> KTOResponse:
        """관광지 원본·부가 이미지를 조회한다."""

        return self._request(
            "detailImage2",
            params={
                "contentId": content_id,
                "imageYN": "Y",
            },
            num_of_rows=100,
        )

    def detail_bundle(
        self,
        content_id: str,
        content_type_id: str,
        *,
        include_info: bool = True,
        include_images: bool = True,
    ) -> dict[str, Any]:
        """추천 엔진에서 사용할 장소 상세 묶음을 생성한다."""

        result: dict[str, Any] = {
            "content_id": content_id,
            "content_type_id": content_type_id,
            "common": self.detail_common(content_id).items,
            "intro": self.detail_intro(content_id, content_type_id).items,
        }
        if include_info:
            result["info"] = self.detail_info(content_id, content_type_id).items
        if include_images:
            result["images"] = self.detail_images(content_id).items
        return result
