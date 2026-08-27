"""외부 패키지 없는 Plan B 로컬 JSON API 서버."""

from __future__ import annotations

import argparse
import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .api_service import PlanBApiService
from .cli import load_env_file
from .crowd_client import SKCrowdApiError
from .kto_client import KTOApiError
from .route_client import TMapApiError
from .seoul_crowd_client import SeoulCrowdApiError
from .seoul_transit_client import SeoulTransitApiError
from .car_route_client import TMapCarApiError
from .place_search_client import TMapPlaceSearchError
from .trip_store import TripNotFoundError, TripVersionConflictError
from .weather_client import KMAApiError


WEB_INDEX_PATH = Path(__file__).resolve().parent.parent / "web" / "index.html"


class PlanBRequestHandler(BaseHTTPRequestHandler):
    service: PlanBApiService
    server_version = "PlanBApi/0.1"

    def _send_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, data: Any) -> None:
        body = json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
            default=str,
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        return {
            key: values[-1]
            for key, values in parse_qs(parsed.query).items()
            if values
        }

    def _dispatch_get(self) -> Any:
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            return self.service.health()
        if parsed.path == "/priorities":
            return self.service.priorities()
        if parsed.path == "/place-search":
            return self.service.place_search(self._query())
        if parsed.path == "/kto-category-match":
            return self.service.match_kto_category(self._query())
        if parsed.path == "/weather":
            return self.service.weather(self._query())
        if parsed.path == "/walking-route":
            return self.service.walking_route(self._query())
        if parsed.path == "/crowd":
            return self.service.crowd(self._query())
        if parsed.path == "/seoul-crowd":
            return self.service.seoul_crowd(self._query())
        if parsed.path == "/seoul-transit-route":
            return self.service.seoul_transit_route(self._query())
        if parsed.path == "/car-route":
            return self.service.car_route(self._query())
        if parsed.path.startswith("/places/"):
            content_id = parsed.path.removeprefix("/places/").strip("/")
            if not content_id:
                raise ValueError("content_id가 필요합니다.")
            content_type_id = self._query().get("content_type_id", "")
            if not content_type_id:
                raise ValueError("content_type_id 쿼리가 필요합니다.")
            return self.service.place(content_id, content_type_id)
        if parsed.path.startswith("/trip-plans/"):
            trip_id = parsed.path.removeprefix("/trip-plans/").strip("/")
            if not trip_id:
                raise ValueError("trip_id가 필요합니다.")
            return self.service.get_trip_plan(trip_id)
        raise FileNotFoundError("지원하지 않는 경로입니다.")

    def do_GET(self) -> None:
        if urlparse(self.path).path in ("/", "/index.html"):
            try:
                body = WEB_INDEX_PATH.read_bytes()
            except FileNotFoundError:
                self._send_html(
                    HTTPStatus.NOT_FOUND, b"web/index.html not found"
                )
                return
            self._send_html(HTTPStatus.OK, body)
            return
        self._handle(self._dispatch_get)

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 1_000_000:
            raise ValueError("JSON 요청 본문이 필요합니다.")
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("올바른 JSON 요청이 아닙니다.") from exc
        if not isinstance(body, dict):
            raise ValueError("JSON 객체를 입력해야 합니다.")
        return body

    def do_POST(self) -> None:
        def dispatch() -> Any:
            path = urlparse(self.path).path
            body = self._read_json_body()

            if path == "/recommendations":
                return self.service.recommendations(body)
            if path == "/trip-plans/validate":
                return self.service.validate_trip_plan(body)
            if path == "/trip-plans":
                return self.service.create_trip_plan(body)
            if path.startswith("/trip-plans/") and path.endswith("/replace-schedule"):
                trip_id = (
                    path.removeprefix("/trip-plans/")
                    .removesuffix("/replace-schedule")
                    .strip("/")
                )
                if not trip_id:
                    raise ValueError("trip_id가 필요합니다.")
                return self.service.replace_trip_schedule(trip_id, body)
            raise FileNotFoundError("지원하지 않는 경로입니다.")

        self._handle(dispatch)

    def do_PUT(self) -> None:
        def dispatch() -> Any:
            path = urlparse(self.path).path
            body = self._read_json_body()

            if path.startswith("/trip-plans/"):
                trip_id = path.removeprefix("/trip-plans/").strip("/")
                if not trip_id:
                    raise ValueError("trip_id가 필요합니다.")
                return self.service.replace_trip_plan(trip_id, body)
            raise FileNotFoundError("지원하지 않는 경로입니다.")

        self._handle(dispatch)

    def do_OPTIONS(self) -> None:
        self._send(HTTPStatus.NO_CONTENT, {})

    def _handle(self, operation: Any) -> None:
        try:
            self._send(HTTPStatus.OK, operation())
        except (FileNotFoundError, TripNotFoundError) as exc:
            self._send(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except TripVersionConflictError as exc:
            self._send(HTTPStatus.CONFLICT, {"error": str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self._send(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except (
            KTOApiError,
            KMAApiError,
            TMapApiError,
            SKCrowdApiError,
            SeoulCrowdApiError,
            SeoulTransitApiError,
            TMapCarApiError,
            TMapPlaceSearchError,
        ) as exc:
            self._send(HTTPStatus.BAD_GATEWAY, {"error": str(exc)})
        except Exception as exc:
            self._send(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": f"서버 내부 오류: {exc}"},
            )

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[API] {self.address_string()} - {format % args}")


def create_server(
    host: str = "127.0.0.1",
    port: int = 8000,
    cache_path: str = ".cache/plan_b_api.sqlite3",
) -> ThreadingHTTPServer:
    PlanBRequestHandler.service = PlanBApiService(cache_path)
    return ThreadingHTTPServer((host, port), PlanBRequestHandler)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    load_env_file()
    parser = argparse.ArgumentParser(description="Plan B JSON API 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--cache-db", default=".cache/plan_b_api.sqlite3")
    args = parser.parse_args()
    server = create_server(args.host, args.port, args.cache_db)
    print(f"Plan B API: http://{args.host}:{args.port}")
    print("종료: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
